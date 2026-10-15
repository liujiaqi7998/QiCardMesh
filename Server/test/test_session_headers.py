from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings

from qicardmesh.captcha import CAPTCHA_SESSION_KEY
from qicardmesh.passwords import hash_password_for_api


@override_settings(CACHES={
    'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'},
})
class SessionHeaderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = hash_password_for_api('session-test-password')
        get_user_model().objects.create_user(username='session-user', password=cls.password)

    def test_login_loads_captcha_session_without_cookie(self):
        response = self.client.get('/api/auth/captcha')
        session_id = response['X-sessionid']
        for path in ('/api/auth/login', '/api/auth/login/'):
            with self.subTest(path=path):
                response = Client().post(path, {
                    'username': 'session-user', 'password': self.password,
                }, content_type='application/json', headers={'x-sessionid': session_id, 'X-Device-ID': 'session-test-device'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.wsgi_request.session.session_key, session_id)
                self.assertIn(CAPTCHA_SESSION_KEY, response.wsgi_request.session)

    @override_settings(SESSION_COOKIE_NAME='custom_session')
    def test_cookie_has_priority_over_header(self):
        self.client.get('/api/auth/captcha')
        cookie_id = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        other_id = Client().get('/api/auth/captcha')['X-sessionid']
        response = self.client.get('/api/auth/login', headers={'X-Sessionid': other_id})
        self.assertEqual(response.wsgi_request.session.session_key, cookie_id)
        self.assertIn(CAPTCHA_SESSION_KEY, response.wsgi_request.session)

    def test_invalid_or_absent_header_does_not_load_session(self):
        for value in ('', 'not-a-session', 'a' * 32):
            with self.subTest(value=value):
                response = self.client.get('/api/auth/login', headers={'X-Sessionid': value})
                self.assertEqual(dict(response.wsgi_request.session), {})

    def test_session_id_does_not_replace_login_credentials(self):
        session_id = self.client.get('/api/auth/captcha')['X-sessionid']
        response = Client().post('/api/auth/login', {}, content_type='application/json',
                                 headers={'X-Sessionid': session_id})
        self.assertEqual(response.json()['code'], 40002)

    def test_header_does_not_enable_admin_session(self):
        session_id = self.client.get('/api/auth/captcha')['X-sessionid']
        response = Client().get('/admin/login/', headers={'X-Sessionid': session_id})
        self.assertNotIn(CAPTCHA_SESSION_KEY, response.wsgi_request.session)

    async def test_asgi_loads_header_session(self):
        first = await self.async_client.get('/api/auth/captcha')
        session_id = first['X-sessionid']
        self.async_client.cookies.clear()
        response = await self.async_client.get('/api/auth/login', headers={'X-Sessionid': session_id})
        session = response.asgi_request.session
        self.assertEqual(session.session_key, session_id)
        self.assertIsNotNone(await session.aget(CAPTCHA_SESSION_KEY))
