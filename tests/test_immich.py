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

from immich_manager import validate_immich_url
from sync_service import SyncService
from uploader_config import Config

AUTH_A = (
    'androidId=dev1&client_sig=s1&callerSig=s2&device_country=us'
    '&Email=userA%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=tok-immich-a'
)
AUTH_B = (
    'androidId=dev1&client_sig=s1&callerSig=s2&device_country=us'
    '&Email=userB%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=tok-immich-b'
)


def _fake_response(status_code: int, payload: object) -> Mock:
    resp = Mock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


class ImmichIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data_root = self.base / 'data'
        self.immich_root = self.base / 'immich'
        self.outside = self.base / 'outside'
        self.state = self.base / 'state'
        self.secret_file = self.base / 'auth.txt'
        for d in (self.data_root, self.immich_root, self.outside, self.state):
            d.mkdir(parents=True)
        (self.immich_root / 'library' / 'userA').mkdir(parents=True)
        (self.immich_root / 'upload' / 'uuid-user-a').mkdir(parents=True)
        (self.immich_root / 'thumbs').mkdir(parents=True)
        self.secret_file.write_text(AUTH_A, encoding='utf-8')

        self.immich_users = [
            {
                'id': 'uuid-user-a',
                'name': 'User A',
                'email': 'a@home.local',
                'isAdmin': True,
                'status': 'active',
                'storageLabel': 'userA',
                'quotaUsageInBytes': 1024,
            },
        ]
        self.expected_api_key = 'immich-admin-super-secret-key'
        self.uploads_by_auth: dict[str, list[Path]] = {}

        self.env = {
            'AUTH_DATA_FILE': str(self.secret_file),
            'WATCHED_FOLDER': str(self.data_root),
            'APPROVED_MEDIA_ROOTS': f'{self.data_root},{self.immich_root}',
            'IMMICH_URL': 'http://immich-server:2283',
            'IMMICH_PUBLIC_URL': 'http://localhost:2283',
            'IMMICH_MEDIA_ROOT': str(self.immich_root),
            'STATE_DIR': str(self.state),
            'STABLE_SECONDS': '0.01',
            'STABILITY_TIMEOUT': '5',
            'DASHBOARD_PASSWORD': 'strong-test-password',
        }

    def _fake_http(self, method: str, url: str, headers=None, json=None, timeout=None):
        headers = headers or {}
        if url.endswith('/api/server/ping'):
            return _fake_response(200, {'res': 'pong'})
        if url.endswith('/api/server/version'):
            return _fake_response(200, {'major': 3, 'minor': 2, 'patch': 2})
        if headers.get('x-api-key') != self.expected_api_key:
            return _fake_response(401, {'message': 'Invalid API key'})
        if method == 'GET' and url.endswith('/api/users/me'):
            return _fake_response(200, self.immich_users[0])
        if method == 'GET' and url.endswith('/api/admin/users'):
            return _fake_response(200, list(self.immich_users))
        if method == 'POST' and url.endswith('/api/admin/users'):
            created = {
                'id': 'uuid-user-b',
                'name': json['name'],
                'email': json['email'],
                'isAdmin': bool(json.get('isAdmin', False)),
                'status': 'active',
                'storageLabel': json.get('storageLabel'),
                'quotaUsageInBytes': 0,
            }
            self.immich_users.append(created)
            return _fake_response(201, created)
        return _fake_response(404, {'message': 'Not found'})

    def _client_factory(self, *, auth_data: str, **kwargs):
        client = Mock()
        client.db_path = Path('/tmp/default.db')

        def fake_upload(target, **upload_kwargs):
            batch = [Path(p) for p in target]
            self.uploads_by_auth.setdefault(auth_data, []).extend(batch)
            return {p.as_posix(): f'mk-{p.name}' for p in batch}

        client.upload.side_effect = fake_upload
        return client

    def _make_service(self):
        cfg = Config.from_env(self.env)
        validator = Mock(return_value={'email': 'ok@example.com', 'expiry': '9999999999'})
        service = SyncService(
            cfg,
            client_factory=self._client_factory,
            auth_validator=validator,
            immich_http_request=self._fake_http,
        )
        service.processor.settings_provider = lambda: {**service.get_settings(), 'stable_seconds': 0}
        self.addCleanup(service.store.close)
        return service

    def test_url_validation_rejects_unsafe_urls(self):
        self.assertEqual(validate_immich_url('http://immich-server:2283/'), 'http://immich-server:2283')
        for bad in ('file:///etc/passwd', 'ftp://host:21', 'http://user:pass@immich:2283', 'not-a-url'):
            with self.assertRaises(ValueError):
                validate_immich_url(bad)

    def test_immich_user_discovery_creation_auto_bind_and_upload_routing(self):
        service = self._make_service()
        account_a = service.list_accounts()[0]
        account_b = service.add_account(name='Account B', auth_data=AUTH_B)['account']

        # Before API key is configured, disk discovery still detects userA and skips thumbs.
        before = service.list_immich_users()
        self.assertFalse(before['api_connected'])
        labels = {d['label'] for d in before['disk_folders']}
        self.assertIn('userA', labels)
        self.assertNotIn('thumbs', labels)

        # Save Immich Admin API key -> stored atomically with 0600 outside SQLite.
        st = service.configure_immich(api_key=self.expected_api_key)
        self.assertTrue(st['reachable'])
        self.assertEqual(st['version'], 'v3.2.2')
        self.assertTrue(st['api_key_valid'])
        self.assertTrue(st['admin_access'])
        key_file = self.state / 'immich_api.secret'
        self.assertTrue(key_file.is_file())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(key_file.stat().st_mode), 0o600)
        self.assertNotIn(self.expected_api_key.encode(), (self.state / 'uploads.db').read_bytes())

        # Discover User A -> prefers library/userA over upload/uuid-user-a.
        listed = service.list_immich_users()
        self.assertTrue(listed['api_connected'])
        user_a = listed['users'][0]
        self.assertEqual(Path(user_a['recommended_path']).resolve(), (self.immich_root / 'library' / 'userA').resolve())
        self.assertTrue(user_a['folder_exists'])

        # Bind User A -> Account A.
        bound_a = service.bind_immich_folder(
            path=user_a['recommended_path'],
            account_id=account_a['id'],
            folder_name='Immich - User A',
        )
        self.assertEqual(bound_a['folder']['account_id'], account_a['id'])

        # Create User B via Immich API with auto_bind to Account B BEFORE User B's folder exists on disk!
        created_b = service.create_immich_user(
            name='User B',
            email='b@home.local',
            password='userb-pass123',
            storage_label='userB',
            account_id=account_b['id'],
            auto_bind=True,
        )
        self.assertIsNotNone(created_b['folder'])
        user_b_folder = Path(created_b['folder']['path'])
        self.assertFalse(user_b_folder.exists())

        # Simulate Immich creating User B's folder on first mobile photo upload + User A dropping a photo.
        photo_a = self.immich_root / 'library' / 'userA' / 'picA.jpg'
        photo_a.write_bytes(b'pic-a')
        user_b_folder.mkdir(parents=True)
        photo_b = user_b_folder / 'picB.heic'
        photo_b.write_bytes(b'pic-b')

        service.scan_folder_now(bound_a['folder']['id'])
        service.scan_folder_now(created_b['folder']['id'])
        while service.processor.process_once():
            pass

        self.assertEqual(self.uploads_by_auth.get(AUTH_A), [photo_a.resolve()])
        self.assertEqual(self.uploads_by_auth.get(AUTH_B), [photo_b.resolve()])

    @unittest.skipUnless(HTTPX_AVAILABLE, 'httpx required for TestClient')
    def test_immich_api_security_redaction_and_traversal_protection(self):
        service = self._make_service()
        client = TestClient(create_app(service, manage_lifecycle=False))
        self.addCleanup(client.close)

        # Unauthenticated requests rejected.
        self.assertEqual(client.get('/api/immich/status').status_code, 401)
        self.assertEqual(client.get('/api/immich/users').status_code, 401)

        login = client.post('/api/session/login', json={'username': 'admin', 'password': 'strong-test-password'})
        headers = {'X-CSRF-Token': login.json()['csrf_token']}

        # Configure API key and ensure key is NEVER echoed in any response or log.
        cfg_resp = client.put('/api/immich/config', json={'api_key': self.expected_api_key}, headers=headers)
        self.assertEqual(cfg_resp.status_code, 200)
        self.assertNotIn(self.expected_api_key, cfg_resp.text)
        self.assertNotIn(self.expected_api_key, client.get('/api/immich/status').text)
        self.assertNotIn(self.expected_api_key, client.get('/api/immich/users').text)
        self.assertNotIn(self.expected_api_key, client.get('/api/logs').text)

        # Path traversal / outside approved roots rejected on bind & config.
        account_id = service.list_accounts()[0]['id']
        bad_bind = client.post(
            '/api/immich/bind',
            json={'path': str(self.outside), 'account_id': account_id, 'folder_name': 'Bad'},
            headers=headers,
        )
        self.assertEqual(bad_bind.status_code, 403)
        bad_root = client.put('/api/immich/config', json={'media_root': str(self.outside)}, headers=headers)
        self.assertEqual(bad_root.status_code, 403)

    @unittest.skipUnless(HTTPX_AVAILABLE, 'httpx required for TestClient')
    def test_invalid_immich_key_does_not_return_401_or_logout_dashboard_session(self):
        service = self._make_service()
        client = TestClient(create_app(service, manage_lifecycle=False))
        self.addCleanup(client.close)

        login = client.post('/api/session/login', json={'username': 'admin', 'password': 'strong-test-password'})
        headers = {'X-CSRF-Token': login.json()['csrf_token']}

        # Pasting Google AUTH_DATA or whitespace into Immich API key is rejected cleanly with 400.
        google_paste = client.put('/api/immich/config', json={'api_key': AUTH_A}, headers=headers)
        self.assertEqual(google_paste.status_code, 400)
        self.assertIn('Google Android AUTH_DATA', google_paste.json()['detail'])

        # Pasting an invalid Immich key is rejected with 400 before saving.
        bad_key = client.put('/api/immich/config', json={'api_key': 'wrong-key-value'}, headers=headers)
        self.assertEqual(bad_key.status_code, 400)

        # Even if a previously stored key later expires (401 from Immich), /api/immich/users returns 200
        # with api_connected=False rather than 401, so the dashboard session stays logged in!
        (self.state / 'immich_api.secret').write_text('expired-immich-key\n', encoding='utf-8')
        service.immich._api_key = 'expired-immich-key'
        users_resp = client.get('/api/immich/users')
        self.assertEqual(users_resp.status_code, 200)
        self.assertFalse(users_resp.json()['api_connected'])
        self.assertIn('401', users_resp.json()['message'])
        # Dashboard session is still authenticated!
        self.assertEqual(client.get('/api/overview').status_code, 200)


if __name__ == '__main__':
    unittest.main()
