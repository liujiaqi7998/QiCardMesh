import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from pydantic import BaseModel, ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError

from qicardmesh.api import api_endpoint
from qicardmesh.passwords import hash_password_for_api
from qicardmesh.schemas import (
    ENDPOINTS, AccessSession, ChangeEmailResponse, ChangePasswordResponse,
    EmailCodeLoginRequest, LoginRequest, PasswordLoginRequest, TokenPair, build_schema,
)
from qicardmesh.tokens import _access_key, _refresh_key, get_access_session, get_refresh_session, issue_tokens
from qicardmesh.urls import urlpatterns


class ApiContractTests(SimpleTestCase):
    def test_every_api_route_uses_its_registered_contract(self):
        routes = [route for route in urlpatterns if route.callback.__module__ == 'qicardmesh.views']
        self.assertEqual({route.name for route in routes if route.name}, set(ENDPOINTS))
        for route in routes:
            self.assertIn(route.callback.api_contract, ENDPOINTS.values())
            if route.name:
                self.assertIs(route.callback.api_contract, ENDPOINTS[route.name])
        for endpoint in ENDPOINTS.values():
            self.assertTrue(issubclass(endpoint.response, BaseModel))
            if endpoint.request:
                self.assertTrue(issubclass(endpoint.request, BaseModel))

    def test_openapi_references_resolve_and_use_runtime_models(self):
        response = self.client.get('/api/openapi.json')
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema, build_schema())
        self.assertEqual(schema['openapi'], '3.1.0')
        self.assertEqual(set(schema['paths']), {reverse(name) for name in ENDPOINTS} | {reverse('auth-captcha')})

        def check_refs(value):
            if isinstance(value, dict):
                if '$ref' in value:
                    target = schema
                    for part in value['$ref'].removeprefix('#/').split('/'):
                        target = target[part]
                for child in value.values():
                    check_refs(child)
            elif isinstance(value, list):
                for child in value:
                    check_refs(child)

        check_refs(schema)
        components = schema['components']['schemas']
        for name, endpoint in ENDPOINTS.items():
            operation = schema['paths'][reverse(name)][endpoint.method]
            response_ref = operation['responses']['200']['content']['application/json']['schema']['$ref']
            generated = endpoint.response.model_json_schema(
                mode='serialization', ref_template='#/components/schemas/{model}',
            )
            self.assertEqual(components[response_ref.rsplit('/', 1)[1]]['properties'], generated['properties'])
        email_ref = components['ChangeEmailResponse']['properties']['data']['$ref']
        self.assertEqual(set(components[email_ref.rsplit('/', 1)[1]]['properties']), {'email'})
        self.assertEqual(components['ChangePasswordResponse']['properties']['data']['type'], 'null')
        self.assertEqual(self.client.get('/api/docs/').status_code, 200)

    @override_settings(CAPTCHA_TTL=120, SESSION_COOKIE_NAME='custom_session')
    def test_captcha_documents_binary_response_and_session_flow(self):
        operation = self.client.get('/api/openapi.json').json()['paths'][reverse('auth-captcha')]['get']
        self.assertEqual(operation['security'], [])
        self.assertNotIn('requestBody', operation)
        self.assertEqual(operation['responses']['200']['content'], {
            'image/png': {'schema': {'type': 'string', 'format': 'binary'}},
        })
        self.assertIn('custom_session', operation['description'])
        self.assertIn('120 秒', operation['description'])
        self.assertIn('/api/auth/email-send', operation['description'])
        self.assertIn('Set-Cookie', operation['responses']['200']['headers'])
        self.assertIn('405', operation['responses'])

    def test_login_union_normalizes_credentials_and_rejects_mixed_modes(self):
        password = hash_password_for_api('test-password')
        payload = LoginRequest.model_validate({'username': ' alice ', 'password': password})
        self.assertIsInstance(payload.root, PasswordLoginRequest)
        self.assertEqual(payload.root.username, 'alice')
        payload = LoginRequest.model_validate({'email': ' Alice@EXAMPLE.com ', 'email_code': '012345'})
        self.assertIsInstance(payload.root, EmailCodeLoginRequest)
        self.assertEqual(payload.root.email, 'alice@example.com')
        for extra in ({'email': None}, {'email_code': None}, {'email': 'alice@example.com', 'email_code': '123456'}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                LoginRequest.model_validate({'username': 'alice', 'password': password, **extra})

    def test_output_validation_rejects_invalid_response_data(self):
        @api_endpoint('auth-me')
        def broken_view(request):
            return {'data': {'id': '1', 'username': 'alice', 'email': 'alice@example.com'}}

        with self.assertRaises(ValidationError):
            broken_view(RequestFactory().get('/api/auth/me'))
        with self.assertRaises(ValidationError):
            ChangeEmailResponse(data={'id': 1})
        with self.assertRaises(ValidationError):
            ChangePasswordResponse(data={})


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class RequestValidationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(
            username='schema-user', email='schema@example.com',
            password=hash_password_for_api('schema-password'),
        )

    def setUp(self):
        cache.clear()
        self.pair = issue_tokens(self.user.pk)
        self.auth = {'HTTP_AUTHORIZATION': 'Bearer ' + self.pair.access_token}

    def post(self, route, body):
        return self.client.post(reverse(route), data=json.dumps(body),
                                content_type='application/json', **self.auth)

    def test_all_body_endpoints_reject_non_object_json(self):
        for route, endpoint in ENDPOINTS.items():
            if endpoint.request is None:
                continue
            for body in ('[]', 'null', '123', '"text"', '{', b'\xff'):
                with self.subTest(route=route, body=body):
                    response = self.client.post(reverse(route), body,
                                                content_type='application/json', **self.auth)
                    if route == 'mqtt-auth':
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json(), {
                            'result': 'deny', 'is_superuser': False, 'expire_at': 0, 'acl': [],
                        })
                    else:
                        self.assertEqual(response.status_code, 400)
                        self.assertEqual(response.json()['code'], 40001)

    def test_all_body_endpoints_validate_missing_fields(self):
        for route, expected in (
            ('auth-login', 40002), ('auth-email-send', 40002), ('auth-refresh', 40002),
            ('auth-change-password', 40003), ('auth-change-email', 40004),
        ):
            with self.subTest(route=route):
                self.assertEqual(self.post(route, {}).json()['code'], expected)
        self.assertEqual(self.post('mqtt-auth', {}).json()['result'], 'deny')

    def test_no_string_coercion_or_whitespace_tokens(self):
        for value in (123, True, [], {}, None, '', '   '):
            with self.subTest(value=value):
                self.assertEqual(self.post('auth-refresh', {'refresh_token': value}).json()['code'], 40002)
                self.assertEqual(self.post('mqtt-auth', {'access_token': value}).json()['result'], 'deny')
                self.assertEqual(self.post('auth-login', {
                    'username': value, 'password': hash_password_for_api('schema-password'),
                }).json()['code'], 40002)
        for code in (123456, '１２３４５６', '12345', '123456\n'):
            self.assertEqual(self.post('auth-login', {
                'email': 'schema@example.com', 'email_code': code,
            }).json()['code'], 40002)

    def test_field_error_codes_and_validation_precede_side_effects(self):
        with patch('qicardmesh.views.verify_email_code') as verify:
            for password in (None, 123, 'plaintext', 'sha256$' + 'A' * 64, 'sha256$' + 'a' * 64 + '\n'):
                self.assertEqual(self.post('auth-change-password', {
                    'password': password, 'email_code': '123456',
                }).json()['code'], 40003)
            self.assertEqual(self.post('auth-change-password', {
                'password': hash_password_for_api('new-password'), 'email_code': 123456,
            }).json()['code'], 40002)
            self.assertEqual(self.post('auth-change-email', {
                'email': 'invalid', 'email_code': 123456,
            }).json()['code'], 40004)
            self.assertEqual(self.post('auth-change-email', {
                'email': 'new@example.com', 'email_code': 123456,
            }).json()['code'], 40002)
            verify.assert_not_called()

    def test_authentication_still_precedes_body_validation(self):
        for route in ('auth-change-password', 'auth-change-email'):
            response = self.client.post(reverse(route), '[]', content_type='application/json')
            self.assertEqual(response.json()['code'], 40102)

    def test_method_errors_preserve_mqtt_contract(self):
        for route, endpoint in ENDPOINTS.items():
            method = self.client.get if endpoint.method == 'post' else self.client.post
            response = method(reverse(route), **self.auth)
            if route == 'mqtt-auth':
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['result'], 'deny')
            else:
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response.json()['code'], 40501)

    def test_cache_records_are_typed_and_invalid_records_fail_closed(self):
        self.assertIsInstance(self.pair, TokenPair)
        session = get_access_session(self.pair.access_token)
        self.assertIsInstance(session, AccessSession)
        old_record = session.model_dump(exclude={'expires_at'})
        cache.set(_access_key(self.pair.access_token), old_record)
        self.assertIsNone(get_access_session(self.pair.access_token).expires_at)
        cache.set(_access_key(self.pair.access_token), {'user_id': str(self.user.pk)})
        self.assertIsNone(get_access_session(self.pair.access_token))
        cache.set(_refresh_key(self.pair.refresh_token), {'user_id': str(self.user.pk)})
        self.assertIsNone(get_refresh_session(self.pair.refresh_token))

    def test_mqtt_cache_failure_returns_deny(self):
        with patch('qicardmesh.views.get_access_session', side_effect=RedisConnectionError):
            response = self.post('mqtt-auth', {'access_token': self.pair.access_token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], 'deny')

