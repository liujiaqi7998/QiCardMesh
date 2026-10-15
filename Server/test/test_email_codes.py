import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from redis.exceptions import ConnectionError as RedisConnectionError

from qicardmesh.email_codes import verify_email_code
from qicardmesh.passwords import hash_password_for_api
from qicardmesh.captcha import CAPTCHA_SESSION_KEY


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}},
    MAILERS={'default': {'BACKEND': 'django.core.mail.backends.locmem.EmailBackend'}},
    DEFAULT_FROM_EMAIL='Quarbor Logic <noreply@example.com>',
)
class EmailCodeTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client.defaults["HTTP_X_DEVICE_ID"] = "test-email-device"
        self.user = get_user_model().objects.create_user(
            username='email-user', email='alice@example.com',
            password=hash_password_for_api('existing-password')
        )

    def post(self, path, data):
        return self.client.post(path, data=data, content_type='application/json')

    def captcha(self):
        response = self.client.get('/api/auth/captcha')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/png')
        challenge = self.client.session[CAPTCHA_SESSION_KEY]
        return challenge['code']

    def send_code(self, email='Alice@EXAMPLE.com'):
        response = self.post('/api/auth/email-send', {'email': email, 'captcha': self.captcha()})
        self.assertEqual(response.status_code, 200, response.content)
        return re.search(r'\b[0-9]{6}\b', mail.outbox[-1].body).group()

    def test_captcha_session_header_matches_cookie_for_new_and_existing_sessions(self):
        first = self.client.get('/api/auth/captcha')
        session_id = first['X-sessionid']
        self.assertTrue(session_id)
        self.assertEqual(session_id, first.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertEqual(session_id, self.client.session.session_key)
        self.assertIn(CAPTCHA_SESSION_KEY, self.client.session)

        refreshed = self.client.get('/api/auth/captcha/')
        self.assertEqual(refreshed['X-sessionid'], session_id)
        self.assertEqual(refreshed['X-sessionid'], refreshed.cookies[settings.SESSION_COOKIE_NAME].value)

    def test_captcha_replaces_invalid_session_cookie_and_returns_new_id(self):
        self.client.cookies[settings.SESSION_COOKIE_NAME] = 'a' * 32
        response = self.client.get('/api/auth/captcha')
        self.assertNotEqual(response['X-sessionid'], 'a' * 32)
        self.assertEqual(response['X-sessionid'], response.cookies[settings.SESSION_COOKIE_NAME].value)
        self.assertIn(CAPTCHA_SESSION_KEY, self.client.session)

    def login(self, code, email='alice@example.com'):
        return self.post('/api/auth/login', {'email': email, 'email_code': code})

    def password_login(self):
        return self.post('/api/auth/login', {
            'username': 'email-user',
            'password': hash_password_for_api('existing-password'),
        })

    def test_send_html_mail_and_login_once(self):
        with patch('qicardmesh.email_codes.secrets.randbelow', return_value=123):
            code = self.send_code()
        self.assertEqual(code, '000123')
        message = mail.outbox[-1]
        self.assertEqual(message.subject, 'Quarbor Logic 奇桦科技-验证码')
        self.assertEqual(message.to, ['alice@example.com'])
        self.assertEqual(message.alternatives[0].mimetype, 'text/html')
        self.assertIn(code, message.alternatives[0].content)
        response = self.login(code, 'ALICE@example.com')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        self.assertTrue(data['access_token'].startswith('access_'))
        self.assertTrue(data['refresh_token'].startswith('refresh_'))
        self.assertNotIn('user', data)
        me = self.client.get('/api/auth/me', HTTP_AUTHORIZATION='Bearer ' + data['access_token'])
        self.assertEqual(me.json()['data']['id'], self.user.pk)
        self.assertEqual(self.login(code).json()['code'], 40105)

    def test_same_email_send_cooldown(self):
        code = self.send_code()
        response = self.post('/api/auth/email-send/', {
            'email': 'alice@EXAMPLE.com', 'captcha': self.captcha(),
        })
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 42901)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(verify_email_code('alice@example.com', code))

    def test_expired_code_is_denied(self):
        code = self.send_code()
        with patch('qicardmesh.email_codes.time.time', return_value=time.time() + 301):
            self.assertEqual(self.login(code).json()['code'], 40105)

    def test_wrong_attempt_limit(self):
        code = self.send_code()
        wrong = '999999' if code != '999999' else '000000'
        for _ in range(settings.EMAIL_CODE_MAX_ATTEMPTS):
            self.assertEqual(self.login(wrong).json()['code'], 40105)
        self.assertEqual(self.login(code).json()['code'], 42902)

    def test_email_code_login_is_locked_after_five_failures(self):
        code = self.send_code()
        wrong = '999999' if code != '999999' else '000000'
        for _ in range(5):
            self.assertEqual(self.login(wrong).json()['code'], 40105)
        self.assertEqual(self.login(code).json()['code'], 42902)

    def test_successful_email_code_login_clears_failure_counter(self):
        code = self.send_code()
        wrong = '999999' if code != '999999' else '000000'
        for _ in range(4):
            self.assertEqual(self.login(wrong).json()['code'], 40105)
        self.assertEqual(self.login(code).status_code, 200)

    def test_wrong_email_cannot_use_code(self):
        code = self.send_code()
        self.assertFalse(verify_email_code('other@example.com', code))
        self.assertTrue(verify_email_code('alice@example.com', code))

    def test_concurrent_verification_only_succeeds_once(self):
        code = self.send_code()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: verify_email_code('alice@example.com', code), range(4)))
        self.assertEqual(sum(results), 1)

    def test_smtp_failure_does_not_store_new_code(self):
        with patch('qicardmesh.email_codes.EmailMultiAlternatives.send', side_effect=smtplib.SMTPException('failed')):
            response = self.post('/api/auth/email-send', {
                'email': 'alice@example.com', 'captcha': self.captcha(),
            })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 50301)
        self.assertFalse(verify_email_code('alice@example.com', '000000'))

    def test_redis_failure_returns_service_error(self):
        with patch('qicardmesh.email_codes.cache.add', side_effect=RedisConnectionError):
            response = self.post('/api/auth/email-send', {
                'email': 'alice@example.com', 'captcha': self.captcha(),
            })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 50302)

    def test_inactive_duplicate_and_unknown_users_cannot_login(self):
        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.login(self.send_code()).json()['code'], 40103)
        cache.clear()
        self.user.is_active = True
        self.user.save()
        get_user_model().objects.create_user(username='duplicate', email='ALICE@example.com')
        self.assertEqual(self.login(self.send_code()).json()['code'], 40103)
        self.assertEqual(self.login(self.send_code('unknown@example.com'), 'unknown@example.com').json()['code'], 40103)

    def test_resend_replaces_old_code(self):
        with patch('qicardmesh.email_codes.secrets.randbelow', return_value=111111):
            old = self.send_code()
        with patch('qicardmesh.email_codes.time.time', return_value=time.time() + 61):
            with patch('qicardmesh.email_codes.secrets.randbelow', return_value=222222):
                new = self.send_code()
            self.assertFalse(verify_email_code('alice@example.com', old))
            self.assertTrue(verify_email_code('alice@example.com', new))

    def test_invalid_requests(self):
        for email in (None, '', 'bad', 'a@example.com\r\nBcc: x@example.com', ['a@example.com']):
            with self.subTest(email=email):
                self.assertEqual(self.post('/api/auth/email-send', {
                    'email': email, 'captcha': self.captcha(),
                }).json()['code'], 40002)
        self.assertEqual(self.post('/api/auth/email-send', []).json()['code'], 40001)
        self.assertEqual(self.client.get('/api/auth/email-send').status_code, 405)

    def test_email_send_requires_case_insensitive_session_captcha(self):
        response = self.post('/api/auth/email-send', {
            'email': 'alice@example.com', 'captcha': self.captcha().swapcase(),
        })
        self.assertEqual(response.status_code, 200)

        response = self.post('/api/auth/email-send', {
            'email': 'alice@example.com', 'captcha': 'AAAAA',
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 40005)
        for code in (None, 123456, '123', '１２３４５６'):
            with self.subTest(code=code):
                self.assertEqual(self.login(code).json()['code'], 40002)
        mixed = {'email': 'alice@example.com', 'email_code': '123456', 'username': 'email-user', 'password': 'existing-password'}
        self.assertEqual(self.post('/api/auth/login', mixed).json()['code'], 40002)

    def test_change_password_stores_django_hash_of_client_digest(self):
        login = self.password_login().json()['data']
        code = self.send_code()
        client_password = hash_password_for_api('new-secure-password-123')
        response = self.client.post(
            '/api/auth/change-password',
            data={'email_code': code, 'password': client_password},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + login['access_token'],
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.password.startswith('pbkdf2_'))
        self.assertTrue(self.user.check_password(client_password))
        self.assertFalse(self.user.check_password('new-secure-password-123'))
        self.assertEqual(self.login(code).json()['code'], 40105)
        new_login = self.post('/api/auth/login', {
            'username': 'email-user', 'password': client_password
        })
        self.assertEqual(new_login.status_code, 200)

    def test_change_password_rejects_plaintext_before_consuming_code(self):
        login = self.password_login().json()['data']
        code = self.send_code()
        response = self.client.post(
            '/api/auth/change-password',
            data={'email_code': code, 'password': 'new-secure-password-123'},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + login['access_token'],
        )
        self.assertEqual(response.json()['code'], 40003)
        self.assertTrue(verify_email_code('alice@example.com', code))

    def test_change_email_verifies_new_email_and_rejects_duplicates(self):
        login = self.password_login().json()['data']
        new_email = 'new-email@example.com'
        code = self.send_code(new_email)
        response = self.client.post(
            '/api/auth/change-email',
            data={'email_code': code, 'email': new_email},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + login['access_token'],
        )
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, new_email)
        self.assertFalse(verify_email_code(new_email, code))

        get_user_model().objects.create_user(username='other', email='occupied@example.com')
        occupied_code = self.send_code('occupied@example.com')
        response = self.client.post(
            '/api/auth/change-email',
            data={'email_code': occupied_code, 'email': 'occupied@example.com'},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + login['access_token'],
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['code'], 40901)
        self.assertTrue(verify_email_code('occupied@example.com', occupied_code))

    def test_change_email_invalid_code_does_not_change_email(self):
        login = self.password_login().json()['data']
        response = self.client.post(
            '/api/auth/change-email',
            data={'email_code': '123456', 'email': 'new-email@example.com'},
            content_type='application/json',
            HTTP_AUTHORIZATION='Bearer ' + login['access_token'],
        )
        self.assertEqual(response.status_code, 401)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'alice@example.com')

