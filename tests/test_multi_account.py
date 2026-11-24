import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from sync_service import SyncService
from uploader_config import Config

AUTH_ACCOUNT_A = (
    'androidId=same-device-android-id&client_sig=sig1&callerSig=sig2&device_country=us'
    '&Email=userA%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=secret-token-account-a'
)
AUTH_ACCOUNT_B = (
    'androidId=same-device-android-id&client_sig=sig1&callerSig=sig2&device_country=us'
    '&Email=userB%40example.com&google_play_services_version=1&lang=en'
    '&oauth2_foreground=1&sdk_version=28&service=oauth2&Token=secret-token-account-b'
)


class MultiAccountImmichTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.data_root = self.base / 'data'
        self.immich_root = self.base / 'immich'
        self.user_a_dir = self.immich_root / 'userA'
        self.user_b_dir = self.immich_root / 'userB'
        self.state = self.base / 'state'
        self.secret_file = self.base / 'gphotos_auth.txt'
        for d in (self.data_root, self.immich_root, self.user_a_dir, self.user_b_dir, self.state):
            d.mkdir(parents=True)
        self.secret_file.write_text(AUTH_ACCOUNT_A, encoding='utf-8')

        self.env = {
            'AUTH_DATA_FILE': str(self.secret_file),
            'WATCHED_FOLDER': str(self.data_root),
            'APPROVED_MEDIA_ROOTS': f'{self.data_root},{self.immich_root}',
            'STATE_DIR': str(self.state),
            'STABLE_SECONDS': '0.01',
            'STABILITY_TIMEOUT': '5',
            'UPLOAD_THREADS': '2',
            'UPLOAD_BATCH_SIZE': '2',
            'RETRY_BASE': '1',
            'RETRY_MAX': '10',
            'DASHBOARD_PASSWORD': 'strong-test-password',
        }
        self.uploads_by_auth: dict[str, list[Path]] = {}
        self.clients_by_auth: dict[str, Mock] = {}

    def _client_factory(self, *, auth_data: str, **kwargs):
        client = Mock()
        client.db_path = Path('/tmp/default.db')

        def fake_upload(target, **upload_kwargs):
            batch = [Path(p) for p in target]
            self.uploads_by_auth.setdefault(auth_data, []).extend(batch)
            return {p.as_posix(): f'mk-{p.name}' for p in batch}

        client.upload.side_effect = fake_upload
        self.clients_by_auth[auth_data] = client
        return client

    def _make_service(self):
        cfg = Config.from_env(self.env)
        validator = Mock(return_value={'email': 'ok@example.com', 'expiry': '9999999999'})
        service = SyncService(cfg, client_factory=self._client_factory, auth_validator=validator)
        service.processor.settings_provider = lambda: {**service.get_settings(), 'stable_seconds': 0}
        self.addCleanup(service.store.close)
        return service

    def test_two_immich_user_folders_route_strictly_to_respective_google_accounts(self):
        service = self._make_service()
        accounts = service.list_accounts()
        self.assertEqual(len(accounts), 1)
        account_a = accounts[0]
        service.rename_account(account_a['id'], 'Account A')

        # Connect Account B (same androidId, separate Token) bound to Immich User B with Immich preset.
        res_b = service.add_account(
            name='Account B',
            auth_data=AUTH_ACCOUNT_B,
            bind_folder_path=str(self.user_b_dir),
            folder_name='Immich User B',
            immich_preset=True,
        )
        self.assertTrue(res_b['saved'])
        account_b = res_b['account']
        folder_b = res_b['folder']
        self.assertEqual(folder_b['account_id'], account_b['id'])

        # Bind Immich User A to Account A with Immich preset.
        folder_a = service.add_folder(
            name='Immich User A',
            path=str(self.user_a_dir),
            account_id=account_a['id'],
            immich_preset=True,
        )

        # Create real photos + Immich generated thumbs/encoded-video that must be excluded.
        a_photo = self.user_a_dir / '2026' / 'a_holiday.jpg'
        a_photo.parent.mkdir()
        a_photo.write_bytes(b'user-a-photo')
        a_thumb = self.user_a_dir / 'thumbs' / 'a_thumb.jpg'
        a_thumb.parent.mkdir()
        a_thumb.write_bytes(b'user-a-thumb-should-skip')

        b_photo = self.user_b_dir / '2026' / 'b_family.heic'
        b_photo.parent.mkdir()
        b_photo.write_bytes(b'user-b-photo')
        b_encoded = self.user_b_dir / 'encoded-video' / 'b_transcoded.mp4'
        b_encoded.parent.mkdir()
        b_encoded.write_bytes(b'user-b-encoded-should-skip')

        service.scan_folder_now(folder_a['id'])
        service.scan_folder_now(folder_b['id'])

        while service.processor.process_once():
            pass

        self.assertEqual(self.uploads_by_auth.get(AUTH_ACCOUNT_A), [a_photo.resolve()])
        self.assertEqual(self.uploads_by_auth.get(AUTH_ACCOUNT_B), [b_photo.resolve()])
        self.assertIsNone(service.store.get(a_thumb.resolve()))
        self.assertIsNone(service.store.get(b_encoded.resolve()))
        self.assertEqual(service.store.get(a_photo.resolve())['status'], 'done')
        self.assertEqual(service.store.get(b_photo.resolve())['status'], 'done')

        # Verify Account B secret stored atomically outside SQLite with 0600 perms and isolated cache DB.
        b_secret_path = self.state / 'accounts' / f'account_{account_b["id"]}.secret'
        self.assertTrue(b_secret_path.is_file())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(b_secret_path.stat().st_mode), 0o600)
        db_bytes = (self.state / 'uploads.db').read_bytes()
        self.assertNotIn(b'secret-token-account-a', db_bytes)
        self.assertNotIn(b'secret-token-account-b', db_bytes)
        self.assertNotEqual(
            self.clients_by_auth[AUTH_ACCOUNT_A].db_path,
            self.clients_by_auth[AUTH_ACCOUNT_B].db_path,
        )

    def test_round_robin_prevents_large_account_backlog_from_starving_second_account(self):
        service = self._make_service()
        account_a = service.list_accounts()[0]
        account_b = service.add_account(name='Account B', auth_data=AUTH_ACCOUNT_B)['account']

        folder_a = service.add_folder(name='Folder A', path=str(self.user_a_dir), account_id=account_a['id'])
        folder_b = service.add_folder(name='Folder B', path=str(self.user_b_dir), account_id=account_b['id'])

        for i in range(4):
            (self.user_a_dir / f'a_{i}.jpg').write_bytes(f'a-{i}'.encode())
        for i in range(2):
            (self.user_b_dir / f'b_{i}.jpg').write_bytes(f'b-{i}'.encode())

        service.scan_folder_now(folder_a['id'])
        service.scan_folder_now(folder_b['id'])

        order: list[str] = []
        for auth_str, label in ((AUTH_ACCOUNT_A, 'A'), (AUTH_ACCOUNT_B, 'B')):
            def make_upload(lbl):
                def _upload(target, **kwargs):
                    order.append(lbl)
                    return {Path(p).as_posix(): f'mk-{Path(p).name}' for p in target}
                return _upload
            self.clients_by_auth[auth_str].upload.side_effect = make_upload(label)

        while service.processor.process_once():
            pass

        # Batch size=2: Batch 1 -> A (2), Batch 2 -> B (2, not starved!), Batch 3 -> A (2).
        self.assertEqual(order, ['A', 'B', 'A'])

    def test_expired_account_a_does_not_block_account_b(self):
        service = self._make_service()
        account_a = service.list_accounts()[0]
        account_b = service.add_account(name='Account B', auth_data=AUTH_ACCOUNT_B)['account']

        folder_a = service.add_folder(name='Folder A', path=str(self.user_a_dir), account_id=account_a['id'])
        folder_b = service.add_folder(name='Folder B', path=str(self.user_b_dir), account_id=account_b['id'])

        a_file = self.user_a_dir / 'a1.jpg'
        b_file = self.user_b_dir / 'b1.jpg'
        a_file.write_bytes(b'aaa')
        b_file.write_bytes(b'bbb')
        service.scan_folder_now(folder_a['id'])
        service.scan_folder_now(folder_b['id'])

        # Simulate Account A returning HTTP 401 unauthorized.
        err = RuntimeError('Unauthorized token expired')
        err.response = Mock(status_code=401)
        self.clients_by_auth[AUTH_ACCOUNT_A].upload.side_effect = err

        self.assertTrue(service.processor.process_once())  # Batch 1: A fails -> marked expired
        self.assertEqual(service.auth.get_account(account_a['id'])['status'], 'expired')

        self.assertTrue(service.processor.process_once())  # Batch 2: B succeeds cleanly!
        self.assertEqual(service.store.get(b_file.resolve())['status'], 'done')
        self.assertEqual(service.store.get(a_file.resolve())['status'], 'pending')

    def test_dedicated_subfolder_auto_creation_and_restart_persistence(self):
        service = self._make_service()
        res_b = service.add_account(
            name='User B Personal',
            auth_data=AUTH_ACCOUNT_B,
            create_dedicated_folder=True,
            parent_root=str(self.data_root),
        )
        self.assertTrue(res_b['saved'])
        dedicated_path = Path(res_b['folder']['path'])
        self.assertEqual(dedicated_path, (self.data_root / 'user-b-personal').resolve())
        self.assertTrue(dedicated_path.is_dir())

        photo = dedicated_path / 'sample.jpg'
        photo.write_bytes(b'dedicated-photo')
        service.scan_folder_now(res_b['folder']['id'])
        self.assertTrue(service.processor.process_once())
        self.assertEqual(service.store.get(photo.resolve())['status'], 'done')

        # Simulate full container restart: both accounts and done jobs persist without re-uploading.
        restarted = self._make_service()
        reloaded_accounts = restarted.list_accounts()
        self.assertEqual(len(reloaded_accounts), 2)
        self.assertTrue(all(a['configured'] and a['status'] == 'valid' for a in reloaded_accounts))
        self.assertFalse(restarted.processor.process_once())

    def test_removing_account_pauses_and_unbinds_folders_without_cross_account_leakage(self):
        service = self._make_service()
        account_b = service.add_account(
            name='Account B',
            auth_data=AUTH_ACCOUNT_B,
            bind_folder_path=str(self.user_b_dir),
        )['account']
        folder_b_id = service.store.get_folder_by_path(self.user_b_dir.resolve())['id']

        # Refuses non-forced removal while a folder is still bound.
        with self.assertRaises(ValueError):
            service.remove_account(account_b['id'], force=False)

        # Forced disconnect pauses & unbinds folder B and deletes secret file.
        res = service.remove_account(account_b['id'], force=True)
        self.assertEqual(res['unbound_folders'], 1)
        self.assertFalse((self.state / 'accounts' / f'account_{account_b["id"]}.secret').exists())
        unbound_folder = service.store.get_folder(folder_b_id)
        self.assertIsNone(unbound_folder['account_id'])
        self.assertTrue(unbound_folder['paused'])

        # Dropping a new file in unbound folder B MUST NEVER upload to Account A!
        new_file = self.user_b_dir / 'orphan.jpg'
        new_file.write_bytes(b'orphan-bytes')
        service.scan_folder_now(folder_b_id)
        self.assertFalse(service.processor.process_once())
        self.assertEqual(self.uploads_by_auth.get(AUTH_ACCOUNT_A, []), [])
        self.assertTrue(new_file.exists())


if __name__ == '__main__':
    unittest.main()
