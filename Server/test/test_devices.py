import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import OperationalError, close_old_connections, connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError

from qicardmesh.models import LoginDevice
from qicardmesh.passwords import hash_password_for_api
from qicardmesh.tokens import (
    _access_key, _refresh_key, delete_device, get_access_session, get_refresh_session,
    issue_tokens, refresh_tokens, revoke_session,
)


CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}


@override_settings(CACHES=CACHES)
class LoginDeviceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = hash_password_for_api('device-test-password')
        cls.user = get_user_model().objects.create_user('device-user', email='device@example.com', password=cls.password)
        cls.other = get_user_model().objects.create_user('other-user')

    def setUp(self):
        cache.clear()

    def pair(self, device='phone', user=None):
        return issue_tokens((user or self.user).pk, device_id=device, platform='android', client_ip='192.0.2.1')

    def auth(self, pair):
        return {'HTTP_AUTHORIZATION': 'Bearer ' + pair.access_token}

    def login(self, device='phone', **headers):
        return self.client.post('/api/auth/login', {
            'username': self.user.username, 'password': self.password,
        }, content_type='application/json', HTTP_X_DEVICE_ID=device, **headers)

    def test_device_id_required_for_both_login_modes_before_consuming_email_code(self):
        with patch('qicardmesh.views.verify_email_code') as verify:
            for payload in (
                {'username': self.user.username, 'password': self.password},
                {'email': self.user.email, 'email_code': '123456'},
            ):
                for value in (None, '', '   ', 'x' * 129, 'invalid/id'):
                    with self.subTest(payload=list(payload), device=value):
                        headers = {} if value is None else {'HTTP_X_DEVICE_ID': value}
                        response = self.client.post('/api/auth/login', payload, content_type='application/json', **headers)
                        self.assertEqual(response.json()['code'], 40006)
            verify.assert_not_called()
        self.assertFalse(LoginDevice.objects.exists())

    def test_password_login_records_context_and_does_not_expose_digests(self):
        response = self.login(HTTP_X_PLATFORM='windows', REMOTE_ADDR='192.0.2.3')
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        row = LoginDevice.objects.get(user=self.user)
        self.assertEqual((row.device_id, row.platform, row.client_ip), ('phone', 'windows', '192.0.2.3'))
        self.assertNotEqual(row.access_digest, data['access_token'])
        self.assertNotEqual(row.refresh_digest, data['refresh_token'])
        self.assertEqual(set(data), {'token_type', 'access_token', 'refresh_token', 'expires_in'})

    def test_email_login_records_device_and_failed_login_does_not(self):
        with patch('qicardmesh.views.verify_email_code', return_value=False):
            failed = self.client.post('/api/auth/login', {'email': self.user.email, 'email_code': '123456'},
                                      content_type='application/json', HTTP_X_DEVICE_ID='email-phone')
        self.assertEqual(failed.json()['code'], 40105)
        self.assertFalse(LoginDevice.objects.exists())
        with patch('qicardmesh.views.verify_email_code', return_value=True):
            response = self.client.post('/api/auth/login', {'email': self.user.email, 'email_code': '123456'},
                                        content_type='application/json', HTTP_X_DEVICE_ID='email-phone')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginDevice.objects.get().device_id, 'email-phone')

    def test_same_device_login_replaces_pair_and_keeps_other_devices(self):
        old = self.pair()
        old_session = get_access_session(old.access_token)
        record_id = LoginDevice.objects.get(device_id='phone').pk
        other = self.pair('desktop')
        new = self.pair()
        self.assertEqual(LoginDevice.objects.count(), 2)
        self.assertEqual(LoginDevice.objects.get(device_id='phone').pk, record_id)
        self.assertIsNone(get_access_session(old.access_token))
        self.assertIsNone(refresh_tokens(old.refresh_token))
        revoke_session(old.access_token, old_session)  # late logout must not delete new login
        self.assertIsNotNone(get_access_session(new.access_token))
        self.assertIsNotNone(get_access_session(other.access_token))

    def test_devices_are_owned_paginated_and_current_is_token_based(self):
        current = self.pair()
        self.pair('desktop')
        self.pair('foreign', user=self.other)
        response = self.client.get('/api/auth/devices', HTTP_X_DEVICE_ID='desktop', **self.auth(current))
        data = response.json()['data']
        self.assertEqual(data['total'], 2)
        self.assertEqual({r['device_id'] for r in data['items']}, {'phone', 'desktop'})
        self.assertEqual([r['device_id'] for r in data['items'] if r['is_current']], ['phone'])
        self.assertNotIn('digest', str(data))
        paged = self.client.get('/api/auth/devices?page=2&page_size=1', **self.auth(current)).json()['data']
        self.assertEqual((paged['total'], len(paged['items']), paged['page']), (2, 1, 2))
        for query in ('page=0', 'page_size=101', 'page=no'):
            self.assertEqual(self.client.get('/api/auth/devices?' + query, **self.auth(current)).json()['code'], 40002)
        self.assertEqual(self.client.get('/api/auth/devices').status_code, 401)

    def test_delete_revokes_access_refresh_and_mqtt_even_when_cache_cleanup_fails(self):
        current, target = self.pair(), self.pair('desktop')
        row = LoginDevice.objects.get(device_id='desktop')
        with patch('qicardmesh.tokens.cache.delete_many', side_effect=RedisConnectionError):
            response = self.client.delete(f'/api/auth/devices/{row.id}', **self.auth(current))
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(cache.get(_access_key(target.access_token)))
        self.assertEqual(self.client.get('/api/auth/me', **self.auth(target)).status_code, 401)
        self.assertIsNone(refresh_tokens(target.refresh_token))
        mqtt = self.client.post('/api/auth/mqtt-auth', {'access_token': target.access_token}, content_type='application/json')
        self.assertEqual(mqtt.json()['result'], 'deny')
        self.assertIsNotNone(get_access_session(current.access_token))
        self.assertEqual(self.client.delete(f'/api/auth/devices/{row.id}', **self.auth(current)).json()['code'], 40401)

    def test_cannot_delete_another_users_device(self):
        current = self.pair()
        other = self.pair(user=self.other)
        row = LoginDevice.objects.get(user=self.other)
        response = self.client.delete(f'/api/auth/devices/{row.id}', **self.auth(current))
        self.assertEqual(response.status_code, 404)
        self.assertIsNotNone(get_access_session(other.access_token))

    def test_delete_current_device_and_logout_remove_records(self):
        current = self.pair()
        row = LoginDevice.objects.get()
        self.assertEqual(self.client.delete(f'/api/auth/devices/{row.id}', **self.auth(current)).status_code, 200)
        self.assertIsNone(get_access_session(current.access_token))
        current = self.pair()
        self.assertEqual(self.client.post('/api/auth/logout', **self.auth(current)).status_code, 200)
        self.assertFalse(LoginDevice.objects.exists())
        self.assertIsNone(refresh_tokens(current.refresh_token))

    def test_refresh_updates_same_record_without_changing_login_metadata(self):
        old = self.pair()
        row = LoginDevice.objects.get()
        new = refresh_tokens(old.refresh_token)
        row.refresh_from_db()
        self.assertEqual(row.session_id, new.session_id)
        self.assertEqual((row.device_id, row.platform, row.client_ip), ('phone', 'android', '192.0.2.1'))
        self.assertEqual(LoginDevice.objects.count(), 1)
        self.assertIsNone(get_access_session(old.access_token))
        self.assertIsNone(refresh_tokens(old.refresh_token))

    def test_cache_write_failure_rolls_back_login_and_refresh(self):
        current = self.pair()
        for action in (lambda: self.pair(), lambda: self.pair('new'), lambda: refresh_tokens(current.refresh_token)):
            with self.subTest(action=action), patch('qicardmesh.tokens.cache.set', side_effect=RedisConnectionError):
                with self.assertRaises(RedisConnectionError):
                    action()
            self.assertEqual(LoginDevice.objects.count(), 1)
            self.assertIsNotNone(get_access_session(current.access_token))
            self.assertIsNotNone(get_refresh_session(current.refresh_token))

    def test_delete_between_refresh_validation_and_rotation_cannot_resurrect(self):
        old = self.pair()
        stale = get_refresh_session(old.refresh_token)
        row = LoginDevice.objects.get()

        def delete_then_return_stale(_):
            delete_device(self.user.pk, row.pk)
            return stale

        with patch('qicardmesh.tokens.get_refresh_session', side_effect=delete_then_return_stale):
            self.assertIsNone(refresh_tokens(old.refresh_token))
        self.assertFalse(LoginDevice.objects.exists())

    def test_expiry_and_last_activity(self):
        current = self.pair()
        row = LoginDevice.objects.get()
        original_login = row.login_at
        LoginDevice.objects.filter(pk=row.pk).update(last_seen_at=timezone.now() - timedelta(minutes=2))
        seen_at = original_login + timedelta(minutes=3)
        with patch('qicardmesh.tokens.timezone.now', return_value=seen_at):
            self.assertIsNotNone(get_access_session(current.access_token))
        row.refresh_from_db()
        self.assertEqual(row.last_seen_at, seen_at)
        self.assertEqual(row.login_at, original_login)
        with patch('qicardmesh.tokens.timezone.now', return_value=seen_at + timedelta(seconds=30)):
            with self.assertNumQueries(1):
                self.assertIsNotNone(get_access_session(current.access_token))
        LoginDevice.objects.filter(pk=row.pk).update(access_expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(get_access_session(current.access_token))
        self.assertIsNotNone(get_refresh_session(current.refresh_token))
        LoginDevice.objects.filter(pk=row.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(refresh_tokens(current.refresh_token))

    def test_legacy_cached_tokens_without_device_record_are_rejected(self):
        old = self.pair()
        LoginDevice.objects.all().delete()
        self.assertIsNotNone(cache.get(_access_key(old.access_token)))
        self.assertIsNone(get_access_session(old.access_token))
        self.assertIsNone(refresh_tokens(old.refresh_token))

    def test_expired_records_are_hidden_and_cleanup_preserves_valid_devices(self):
        current = self.pair()
        expired = self.pair('expired')
        LoginDevice.objects.filter(device_id='expired').update(expires_at=timezone.now() - timedelta(seconds=1))
        data = self.client.get('/api/auth/devices', **self.auth(current)).json()['data']
        self.assertEqual(data['total'], 1)
        call_command('prune_login_devices', stdout=StringIO())
        self.assertEqual(LoginDevice.objects.count(), 1)
        self.assertIsNone(get_refresh_session(expired.refresh_token))
        self.assertIsNotNone(get_access_session(current.access_token))

    def test_disabled_user_cannot_refresh_and_deleted_user_devices_cascade(self):
        current = self.pair()
        self.user.is_active = False
        self.user.save(update_fields=['is_active'])
        self.assertEqual(self.client.get('/api/auth/me', **self.auth(current)).status_code, 401)
        self.assertIsNone(refresh_tokens(current.refresh_token))
        self.user.delete()
        self.assertFalse(LoginDevice.objects.exists())
        self.assertIsNone(get_access_session(current.access_token))

    def test_django_admin_login_is_independent(self):
        admin = get_user_model().objects.create_superuser('device-admin', password='admin-password')
        client = Client()
        response = client.post('/admin/login/', {'username': admin.username, 'password': 'admin-password', 'next': '/admin/'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LoginDevice.objects.exists())
        pair = self.pair(user=admin)
        LoginDevice.objects.filter(user=admin).delete()
        self.assertIsNone(get_access_session(pair.access_token))
        self.assertEqual(client.get('/admin/').status_code, 200)


@override_settings(CACHES=CACHES)
class DeviceRotationConcurrencyTests(TransactionTestCase):
    def test_concurrent_refresh_has_exactly_one_winner(self):
        cache.clear()
        user = get_user_model().objects.create_user('concurrent-user')
        old = issue_tokens(user.pk, device_id='phone')
        stale = get_refresh_session(old.refresh_token)
        ready = Barrier(2)

        def rotate():
            close_old_connections()
            try:
                ready.wait(timeout=10)
                for attempt in range(20):
                    try:
                        return refresh_tokens(old.refresh_token)
                    except OperationalError as exc:
                        # SQLite uses database/table write locks; retry the same
                        # stale generation to exercise the conditional update.
                        if connection.vendor != 'sqlite' or 'locked' not in str(exc) or attempt == 19:
                            raise
                        time.sleep(0.01)
            finally:
                close_old_connections()

        with patch('qicardmesh.tokens.get_refresh_session', return_value=stale):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(rotate) for _ in range(2)]
                results = [future.result(timeout=15) for future in futures]
        winners = [result for result in results if result is not None]
        self.assertEqual(len(winners), 1)
        self.assertIsNotNone(get_access_session(winners[0].access_token))
        self.assertIsNone(get_access_session(old.access_token))
        self.assertEqual(LoginDevice.objects.get().session_id, winners[0].session_id)
