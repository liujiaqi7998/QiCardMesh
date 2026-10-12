import json

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings


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
            password="correct horse battery staple",
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
            {"username": "alice@EXAMPLE.com", "password": "correct horse battery staple"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], "0")
        self.assertEqual(body["data"]["token_type"], "Bearer")
        self.assertTrue(body["data"]["access_token"])
        self.assertTrue(body["data"]["refresh_token"])

    def test_refresh_rotates_pair_and_old_access_is_rejected(self):
        login = self.post_json(
            "/api/auth/login",
            {"username": "alice", "password": "correct horse battery staple"},
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
            {"username": "alice", "password": "correct horse battery staple"},
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
