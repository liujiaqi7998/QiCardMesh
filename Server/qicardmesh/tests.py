import json
import uuid

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.http import HttpResponse
from django.test import AsyncClient, Client, RequestFactory, TestCase, override_settings

from config.context import get_request_context

from .middleware import RequestIdMiddleware
from .passwords import hash_password_for_api


@override_settings(
    CACHES={
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    },
    ACCESS_TOKEN_TTL=900,
    REFRESH_TOKEN_TTL=3600,
)
class AuthenticationApiTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="alice",
            email="Alice@example.com",
            password=hash_password_for_api("correct horse battery staple"),
        )

    def post_json(self, path, payload, **extra):
        return self.client.post(
            path,
            data=json.dumps(payload),
            content_type="application/json",
            **extra,
        )

    def test_login_accepts_email_and_returns_token_pair(self):
        response = self.post_json(
            "/api/auth/login",
            {"username": "alice@EXAMPLE.com", "password": hash_password_for_api("correct horse battery staple")},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["token_type"], "Bearer")
        self.assertTrue(body["data"]["access_token"].startswith("access_"))
        self.assertTrue(body["data"]["refresh_token"].startswith("refresh_"))
        self.assertNotIn("user", body["data"])

    def test_plain_password_login_is_rejected(self):
        response = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": "correct horse battery staple"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], 40003)

    def test_password_login_is_locked_after_five_failures(self):
        wrong_password = hash_password_for_api("wrong password")
        for _ in range(5):
            response = self.post_json(
                "/api/auth/login",
                {"username": "alice", "password": wrong_password},
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["code"], 40101)

        response = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["code"], 42902)

    def test_successful_password_login_clears_failure_counter(self):
        wrong_password = hash_password_for_api("wrong password")
        for _ in range(4):
            self.post_json("/api/auth/login", {"username": "alice", "password": wrong_password})
        response = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        )
        self.assertEqual(response.status_code, 200)

        response = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": wrong_password},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["code"], 40101)

    def test_refresh_rotates_pair_and_old_access_is_rejected(self):
        login = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        ).json()["data"]
        refreshed = self.post_json(
            "/api/auth/refresh", {"refresh_token": login["refresh_token"]}
        )
        self.assertEqual(refreshed.status_code, 200)
        refreshed_data = refreshed.json()["data"]
        self.assertNotEqual(login["access_token"], refreshed_data["access_token"])
        old_api = self.client.get(
            "/api", HTTP_AUTHORIZATION=f"Bearer {login['access_token']}"
        )
        self.assertEqual(old_api.status_code, 401)

    def test_logout_revokes_access_and_refresh_tokens(self):
        login = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        ).json()["data"]
        response = self.client.post(
            "/api/auth/logout", HTTP_AUTHORIZATION=f"Bearer {login['access_token']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.get("/api", HTTP_AUTHORIZATION=f"Bearer {login['access_token']}").status_code,
            401,
        )
        self.assertEqual(
            self.post_json("/api/auth/refresh", {"refresh_token": login["refresh_token"]}).status_code,
            401,
        )

    def test_mqtt_auth_returns_user_acl_and_expiration(self):
        login = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        ).json()["data"]
        response = self.post_json(
            "/api/auth/mqtt-auth", {"access_token": login["access_token"]}
        )
        body = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["result"], "allow")
        self.assertFalse(body["is_superuser"])
        self.assertGreater(body["expire_at"], 0)
        self.assertEqual(body["acl"][0]["topic"], f"QiCardMesh/device/{self.user.id}/#")

    def test_mqtt_auth_denies_invalid_token(self):
        response = self.post_json("/api/auth/mqtt-auth", {"access_token": "invalid"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], "deny")

    def test_me_returns_authenticated_user(self):
        login = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": hash_password_for_api("correct horse battery staple")},
        ).json()["data"]
        response = self.client.get(
            "/api/auth/me", HTTP_AUTHORIZATION=f"Bearer {login['access_token']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], {
            "id": self.user.id,
            "username": "alice",
            "email": "Alice@example.com",
        })

    def test_error_responses_use_central_five_digit_business_codes(self):
        invalid_json = self.client.post(
            "/api/auth/login", data="[]", content_type="application/json"
        )
        self.assertEqual(invalid_json.status_code, 400)
        self.assertEqual(invalid_json.json()["code"], 40001)
        self.assertEqual(invalid_json.json()["message"], "请求体必须是 JSON 对象")

        invalid_token = self.client.get("/api/auth/me")
        self.assertEqual(invalid_token.status_code, 401)
        self.assertEqual(invalid_token.json()["code"], 40102)

        method_error = self.client.get("/api/auth/login")
        self.assertEqual(method_error.status_code, 405)
        self.assertEqual(method_error.json()["code"], 40501)


class RequestIdMiddlewareTests(TestCase):
    def test_every_response_has_a_unique_request_id(self):
        first = self.client.get("/api/openapi.json")
        second = self.client.get("/api/openapi.json")
        first_id = first["X-Request-ID"]
        second_id = second["X-Request-ID"]
        uuid.UUID(first_id)
        uuid.UUID(second_id)
        self.assertNotEqual(first_id, second_id)

    def test_handler_can_read_the_request_id_returned_in_response(self):
        captured = {}

        def handler(request):
            captured["request_id"] = request.request_id
            context = get_request_context()
            captured["context_id"] = context.request_id
            captured["client_ip"] = context.client_ip
            captured["device_id"] = context.device_id
            captured["platform"] = context.platform
            return HttpResponse("ok")

        request = RequestFactory().get(
            "/test-request-id",
            HTTP_CF_CONNECTING_IP="203.0.113.10",
            HTTP_X_DEVICE_ID=" device-123 ",
            HTTP_X_PLATFORM=" android ",
        )
        response = RequestIdMiddleware(handler)(request)
        self.assertEqual(response["X-Request-ID"], captured["request_id"])
        self.assertEqual(response["X-Request-ID"], captured["context_id"])
        self.assertEqual(captured["client_ip"], "203.0.113.10")
        self.assertEqual(captured["device_id"], "device-123")
        self.assertEqual(captured["platform"], "android")
        self.assertIsNone(get_request_context().request_id)


class CsrfDisabledTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser(
            username="admin", email="admin@example.com", password="test-admin-password"
        )

    def test_admin_login_and_logout_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/admin/login/",
            {"username": "admin", "password": "test-admin-password", "next": "/admin/"},
            secure=True,
            HTTP_ORIGIN="https://different.example",
        )
        self.assertRedirects(response, "/admin/", fetch_redirect_response=False)
        self.assertEqual(client.session["_auth_user_id"], str(self.admin.pk))
        response = client.post("/admin/logout/", secure=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)

    async def test_asgi_admin_login_without_csrf_token(self):
        client = AsyncClient(enforce_csrf_checks=True)
        response = await client.post(
            "/admin/login/",
            {"username": "admin", "password": "test-admin-password", "next": "/admin/"},
            secure=True,
            headers={"Origin": "https://different.example"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/admin/")

    def test_admin_still_requires_correct_password(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            "/admin/login/", {"username": "admin", "password": "wrong-password"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", client.session)
