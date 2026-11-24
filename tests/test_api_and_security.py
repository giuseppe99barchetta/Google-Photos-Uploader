import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

try:
    from fastapi.testclient import TestClient

    from api_server import create_app

    HTTPX_AVAILABLE = True
except (ImportError, RuntimeError):
    TestClient = None  # type: ignore[assignment]
    create_app = None  # type: ignore[assignment]
    HTTPX_AVAILABLE = False

from sync_service import SyncService
from uploader_config import Config

FAKE_AUTH = (
    'androidId=1234567890abcdef&client_sig=sig1&callerSig=sig2&device_country=us'
    '&Email=alice%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=super-secret-token-value-1'
)
REPLACEMENT_AUTH = (
    'androidId=fedcba0987654321&client_sig=sig1&callerSig=sig2&device_country=us'
    '&Email=bob%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=super-secret-token-value-2'
)


@unittest.skipUnless(HTTPX_AVAILABLE, 'httpx is required for FastAPI TestClient')
class ApiAndSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.media_a = self.base / 'media_a'
        self.media_b = self.base / 'media_b'
        self.outside = self.base / 'outside'
        self.state = self.base / 'state'
        self.secret_file = self.base / 'readonly_auth.txt'
        for d in (self.media_a, self.media_b, self.outside, self.state):
            d.mkdir()
        self.secret_file.write_text(FAKE_AUTH, encoding='utf-8')

        self.env = {
            'AUTH_DATA_FILE': str(self.secret_file),
            'WATCHED_FOLDER': str(self.media_a),
            'APPROVED_MEDIA_ROOTS': f'{self.media_a},{self.media_b}',
            'STATE_DIR': str(self.state),
            'STABLE_SECONDS': '0.01',
            'STABILITY_TIMEOUT': '5',
            'RETRY_BASE': '1',
            'RETRY_MAX': '10',
            'DASHBOARD_USERNAME': 'admin',
            'DASHBOARD_PASSWORD': 'strong-test-password',
        }
        self.mock_client = Mock()
        self.mock_client.upload.side_effect = lambda target, **kwargs: {
            Path(p).as_posix(): f'mk-{Path(p).name}' for p in target
        }
        self.auth_validator = Mock(return_value={'email': 'alice@example.com', 'expiry': '9999999999'})
        self.service = self._make_service()
        self.app = create_app(self.service, manage_lifecycle=False)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def _make_service(self, env_override=None):
        cfg = Config.from_env({**self.env, **(env_override or {})})
        service = SyncService(
            cfg,
            client_factory=Mock(return_value=self.mock_client),
            auth_validator=self.auth_validator,
        )
        self.addCleanup(service.store.close)
        return service

    def _login(self, password='strong-test-password'):
        res = self.client.post('/api/session/login', json={'username': 'admin', 'password': password})
        self.assertEqual(res.status_code, 200, res.text)
        csrf = res.json()['csrf_token']
        return {'X-CSRF-Token': csrf}

    def test_unauthorized_requests_are_rejected_and_ping_is_minimal(self):
        ping = self.client.get('/api/health/ping')
        self.assertEqual(ping.status_code, 200)
        self.assertEqual(ping.json(), {'ok': True})

        for endpoint in (
            '/api/overview',
            '/api/folders',
            '/api/folders/browse',
            '/api/queue',
            '/api/history',
            '/api/logs',
            '/api/auth/status',
            '/api/settings',
            '/api/notifications',
            '/api/health',
        ):
            res = self.client.get(endpoint)
            self.assertEqual(res.status_code, 401, endpoint)

    def test_first_time_setup_login_csrf_and_logout_flow(self):
        setup_state = self.base / 'setup_state'
        setup_state.mkdir()
        service = self._make_service({'STATE_DIR': str(setup_state), 'DASHBOARD_PASSWORD': ''})
        client = TestClient(create_app(service, manage_lifecycle=False))
        self.addCleanup(client.close)

        st = client.get('/api/session/status').json()
        self.assertFalse(st['authenticated'])
        self.assertTrue(st['setup_required'])

        # Setup requires >= 8 char password.
        bad = client.post('/api/session/setup', json={'username': 'admin', 'password': 'short'})
        self.assertEqual(bad.status_code, 422)

        ok = client.post('/api/session/setup', json={'username': 'admin', 'password': 'initial-password-123'})
        self.assertEqual(ok.status_code, 200)
        csrf = ok.json()['csrf_token']

        # Second setup attempt is forbidden.
        again = client.post('/api/session/setup', json={'username': 'admin', 'password': 'another-password-123'})
        self.assertEqual(again.status_code, 403)

        # Mutating call without CSRF fails with 403.
        no_csrf = client.post('/api/queue/pause')
        self.assertEqual(no_csrf.status_code, 403)

        # Mutating call with valid CSRF succeeds.
        with_csrf = client.post('/api/queue/pause', headers={'X-CSRF-Token': csrf})
        self.assertEqual(with_csrf.status_code, 200)
        self.assertTrue(with_csrf.json()['queue_paused'])

        # Logout revokes session.
        out = client.post('/api/session/logout', headers={'X-CSRF-Token': csrf})
        self.assertEqual(out.status_code, 200)
        self.assertEqual(client.get('/api/overview').status_code, 401)

    def test_folder_registration_browse_traversal_rejection_and_scan(self):
        headers = self._login()
        sub = self.media_b / 'family'
        sub.mkdir()
        photo = sub / 'vacation.jpg'
        photo.write_bytes(b'vacation image bytes')

        # Browse approved roots.
        roots = self.client.get('/api/folders/browse').json()
        self.assertEqual(len(roots['approved_roots']), 2)
        children = self.client.get('/api/folders/browse', params={'path': str(self.media_b)}).json()
        self.assertEqual([d['name'] for d in children['directories']], ['family'])

        # Reject path traversal and outside roots.
        self.assertEqual(
            self.client.get('/api/folders/browse', params={'path': str(self.outside)}).status_code, 403
        )
        self.assertEqual(
            self.client.post(
                '/api/folders',
                json={'name': 'Evil', 'path': f'{self.media_a}/../outside'},
                headers=headers,
            ).status_code,
            403,
        )

        # Register approved subdirectory and verify scan queued the photo.
        created = self.client.post(
            '/api/folders',
            json={'name': 'Family', 'path': str(sub), 'recursive': True},
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        folder_id = created.json()['folder']['id']

        scan = self.client.post(f'/api/folders/{folder_id}/scan', headers=headers)
        self.assertEqual(scan.status_code, 200)
        self.assertEqual(scan.json()['discovered_count'], 1)

        # Pause, resume, and remove folder without deleting local photo.
        self.assertEqual(self.client.post(f'/api/folders/{folder_id}/pause', headers=headers).status_code, 200)
        self.assertEqual(self.client.post(f'/api/folders/{folder_id}/resume', headers=headers).status_code, 200)
        self.assertEqual(self.client.delete(f'/api/folders/{folder_id}', headers=headers).status_code, 200)
        self.assertTrue(photo.exists())
        self.assertIsNotNone(self.service.store.get(photo))

    def test_queue_controls_history_export_and_health(self):
        headers = self._login()
        p1 = self.media_a / 'one.jpg'
        p2 = self.media_a / 'two.heic'
        p1.write_bytes(b'photo-one')
        p2.write_bytes(b'photo-two')
        self.service.processor.submit(p1)
        self.service.processor.submit(p2)

        # Cancel p2 before upload; original file stays untouched.
        cancel = self.client.post('/api/queue/cancel', json={'path': str(p2)}, headers=headers)
        self.assertEqual(cancel.status_code, 200)
        self.assertTrue(p2.exists())
        self.assertEqual(self.service.store.get(p2)['status'], 'cancelled')

        # Upload p1 via single worker step.
        self.service.processor.stable_seconds = 0
        self.service.update_settings({'stable_seconds': 1})
        self.service.processor.settings_provider = lambda: {**self.service.get_settings(), 'stable_seconds': 0}
        self.assertTrue(self.service.processor.process_once())
        self.assertEqual(self.service.store.get(p1)['status'], 'done')

        # Retry cancelled p2 and upload it.
        retry = self.client.post('/api/queue/retry', json={'path': str(p2)}, headers=headers)
        self.assertEqual(retry.status_code, 200)
        self.assertTrue(self.service.processor.process_once())
        self.assertEqual(self.service.store.get(p2)['status'], 'done')

        history = self.client.get('/api/history').json()
        self.assertEqual(history['total'], 2)
        csv_export = self.client.get('/api/history/export', params={'format': 'csv'})
        self.assertEqual(csv_export.status_code, 200)
        self.assertIn('one.jpg', csv_export.text)
        self.assertIn('two.heic', csv_export.text)

        health = self.client.get('/api/health').json()
        self.assertEqual(health['database']['ok'], True)
        self.assertEqual(health['upload_slots']['max_threads'], 2)
        self.assertEqual(health['upload_slots']['batch_size'], 4)

    def test_auth_replacement_preserves_working_credentials_on_failure_and_never_leaks_secrets(self):
        headers = self._login()
        initial_client = self.service.auth.get_client()
        self.assertIsNotNone(initial_client)

        # Status must mask email and NEVER include raw token.
        st = self.client.get('/api/auth/status').json()
        self.assertEqual(st['status'], 'valid')
        self.assertIn('***', st['account_hint'])
        self.assertNotIn('super-secret-token-value-1', str(st))

        # Invalid replacement fails and preserves working credentials + readonly secret file.
        self.auth_validator.side_effect = ValueError('BadAuthentication')
        bad_save = self.client.post('/api/auth/save', json={'auth_data': REPLACEMENT_AUTH}, headers=headers)
        self.assertEqual(bad_save.status_code, 400)
        self.assertIs(self.service.auth.get_client(), initial_client)
        self.assertEqual(self.secret_file.read_text(encoding='utf-8'), FAKE_AUTH)
        self.assertFalse((self.state / 'auth_override.secret').exists())

        # Valid replacement writes atomic 0o600 override file and swaps client without touching secret_file.
        self.auth_validator.side_effect = None
        self.auth_validator.return_value = {'email': 'bob@example.com', 'expiry': '9999999999'}
        good_save = self.client.post('/api/auth/save', json={'auth_data': REPLACEMENT_AUTH}, headers=headers)
        self.assertEqual(good_save.status_code, 200, good_save.text)
        override_file = self.state / 'auth_override.secret'
        self.assertTrue(override_file.is_file())
        if os.name != 'nt':
            mode = stat.S_IMODE(override_file.stat().st_mode)
            self.assertEqual(mode, 0o600)
        # Original mounted read-only secret was NEVER written to!
        self.assertEqual(self.secret_file.read_text(encoding='utf-8'), FAKE_AUTH)
        self.assertEqual(good_save.json()['auth_status']['source'], 'managed_override')

        # Verify ZERO secret leakage across all API responses & exports.
        for url in (
            '/api/auth/status',
            '/api/overview',
            '/api/health',
            '/api/logs',
            '/api/logs/export?format=json',
            '/api/history',
            '/api/history/export?format=json',
        ):
            body = self.client.get(url).text
            self.assertNotIn('super-secret-token-value-1', body, url)
            self.assertNotIn('super-secret-token-value-2', body, url)
            self.assertNotIn('strong-test-password', body, url)

    def test_settings_validation_and_persistence_across_restart(self):
        headers = self._login()
        # Out-of-bounds rejected.
        bad = self.client.put('/api/settings', json={'upload_threads': 99}, headers=headers)
        self.assertEqual(bad.status_code, 422)
        bad_rel = self.client.put(
            '/api/settings',
            json={'retry_base': 60, 'retry_max': 10},
            headers=headers,
        )
        self.assertEqual(bad_rel.status_code, 400)

        # Valid settings saved and persist across SyncService restart.
        ok = self.client.put(
            '/api/settings',
            json={'upload_threads': 3, 'upload_batch_size': 8, 'upload_quality': 'saver', 'scan_interval': 120},
            headers=headers,
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()['settings']['upload_threads'], 3)
        self.assertEqual(ok.json()['settings']['upload_batch_size'], 8)
        self.assertEqual(ok.json()['settings']['upload_quality'], 'saver')

        restarted = self._make_service()
        persisted = restarted.get_settings()
        self.assertEqual(persisted['upload_threads'], 3)
        self.assertEqual(persisted['upload_batch_size'], 8)
        self.assertEqual(persisted['upload_quality'], 'saver')
        self.assertEqual(persisted['scan_interval'], 120.0)

    def test_rate_limiting_protects_login_endpoint(self):
        for _ in range(10):
            res = self.client.post('/api/session/login', json={'username': 'admin', 'password': 'wrong-password'})
            self.assertEqual(res.status_code, 401)
        blocked = self.client.post('/api/session/login', json={'username': 'admin', 'password': 'wrong-password'})
        self.assertEqual(blocked.status_code, 429)
        self.assertIn('retry-after', {k.lower() for k in blocked.headers})

    def test_notifications_api_and_live_progress_tracker(self):
        headers = self._login()
        n, created = self.service.store.upsert_notification(
            'test:issue', 'warning', 'system', 'Disk Notice', 'Low space check'
        )
        self.assertTrue(created)
        listing = self.client.get('/api/notifications').json()
        self.assertEqual(listing['unread_count'], 1)
        self.assertEqual(len(listing['items']), 1)

        self.assertEqual(
            self.client.post(f"/api/notifications/{n['id']}/read", headers=headers).status_code, 200
        )
        self.assertEqual(self.client.get('/api/notifications').json()['unread_count'], 0)

        self.assertEqual(
            self.client.post(f"/api/notifications/{n['id']}/dismiss", headers=headers).status_code, 200
        )
        self.assertEqual(self.client.get('/api/notifications').json()['items'], [])

        # Live progress tracker never fabricates speed/ETA when elapsed < 0.25s.
        self.service.events.handle_progress({
            'phase': 'uploading', 'path': '/data/live.jpg', 'filename': 'live.jpg',
            'bytes_completed': 100, 'bytes_total': 1000,
        })
        snap = self.service.events.live_snapshot()
        self.assertEqual(snap['active_count'], 1)
        self.assertEqual(snap['active_uploads'][0]['percent'], 10.0)
        self.assertIsNone(snap['active_uploads'][0]['speed_bps'])
        self.assertIsNone(snap['active_uploads'][0]['eta_seconds'])

        self.service.events.handle_progress({
            'phase': 'complete', 'path': '/data/live.jpg', 'filename': 'live.jpg',
            'bytes_completed': 1000, 'bytes_total': 1000,
        })
        self.assertEqual(self.service.events.live_snapshot()['active_count'], 0)

    def test_multi_account_api_lifecycle_and_secret_redaction(self):
        headers = self._login()
        listed = self.client.get('/api/accounts')
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()['accounts']), 1)

        # Add a second Google account with dedicated subfolder creation under media_b.
        created = self.client.post(
            '/api/accounts',
            json={
                'name': 'User B Account',
                'auth_data': REPLACEMENT_AUTH,
                'create_dedicated_folder': True,
                'parent_root': str(self.media_b),
                'immich_preset': True,
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        account_b = created.json()['account']
        self.assertEqual(account_b['name'], 'User B Account')
        self.assertTrue(Path(created.json()['folder']['path']).is_dir())

        # Rename account B.
        renamed = self.client.patch(
            f"/api/accounts/{account_b['id']}",
            json={'name': 'Immich User B'},
            headers=headers,
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()['account']['name'], 'Immich User B')

        # Deleting without force returns 409 because dedicated folder is still bound.
        conflict = self.client.delete(f"/api/accounts/{account_b['id']}", headers=headers)
        self.assertEqual(conflict.status_code, 409)

        # Verify ZERO token leakage across accounts and health APIs.
        for url in ('/api/accounts', '/api/folders', '/api/health', '/api/overview'):
            body = self.client.get(url).text
            self.assertNotIn('super-secret-token-value-1', body, url)
            self.assertNotIn('super-secret-token-value-2', body, url)

        # Forced disconnect succeeds cleanly.
        deleted = self.client.delete(
            f"/api/accounts/{account_b['id']}", params={'force': 'true'}, headers=headers
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json()['unbound_folders'], 1)


if __name__ == '__main__':
    unittest.main()
