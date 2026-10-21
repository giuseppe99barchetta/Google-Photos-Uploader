import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlencode

from uploader_config import Config


FAKE_AUTH = urlencode({
    'androidId': 'test-device', 'client_sig': 'test-signature',
    'callerSig': 'test-signature', 'device_country': 'it',
    'Email': 'test@example.invalid', 'google_play_services_version': '1',
    'lang': 'it', 'oauth2_foreground': '1', 'sdk_version': '28',
    'service': 'oauth2:https://www.googleapis.com/auth/photos.native',
    'Token': 'fake-token-for-offline-tests-only',
})


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.photos = self.base / 'photos'
        self.photos.mkdir()
        self.env = {'AUTH_DATA': FAKE_AUTH, 'WATCHED_FOLDER': str(self.photos),
                    'STATE_DIR': str(self.base / 'state')}

    def test_defaults_preserve_originals_and_hide_credentials_in_repr(self):
        config = Config.from_env(self.env)
        self.assertFalse(config.delete_after_upload)
        self.assertNotIn('fake-token', repr(config))
        self.assertTrue(config.state_dir.is_dir())

    def test_missing_or_placeholder_authentication_fails_without_echoing_input(self):
        for auth in ('', 'INSERT_YOUR_AUTH_DATA_HERE', 'Token=private-value'):
            with self.subTest(auth=auth):
                with self.assertRaises(ValueError) as error:
                    Config.from_env({**self.env, 'AUTH_DATA': auth})
                self.assertNotIn('private-value', str(error.exception))

    def test_secret_file_is_supported(self):
        secret = self.base / 'auth.txt'
        secret.write_text(FAKE_AUTH + '\n', encoding='utf-8')
        config = Config.from_env({**self.env, 'AUTH_DATA': '', 'AUTH_DATA_FILE': str(secret)})
        self.assertEqual(config.auth_data, FAKE_AUTH)

    def test_conflicting_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_env({**self.env, 'AUTH_DATA_FILE': 'secret.txt'})

    def test_invalid_numbers_and_booleans_are_rejected(self):
        for option, value in [('STABLE_SECONDS', 'nan'), ('SCAN_INTERVAL', 'inf'),
                              ('REQUEST_TIMEOUT', '0'), ('MAX_ATTEMPTS', '1.5'),
                              ('RETRY_BASE', '-1'), ('DELETE_AFTER_UPLOAD', 'yes')]:
            with self.subTest(option=option):
                with self.assertRaises(ValueError):
                    Config.from_env({**self.env, option: value})

    def test_state_must_not_overlap_watched_folder(self):
        for state in (self.photos, self.photos / 'state', self.base):
            with self.subTest(state=state):
                with self.assertRaises(ValueError):
                    Config.from_env({**self.env, 'STATE_DIR': str(state)})

    def test_missing_nas_at_startup_can_be_recovered_by_supervisor(self):
        config = Config.from_env({**self.env, 'WATCHED_FOLDER': str(self.base / 'offline')})
        self.assertFalse(config.root.exists())

    def test_invalid_interval_relationships_are_rejected(self):
        for options in ({'STABILITY_TIMEOUT': '1'}, {'RETRY_MAX': '1'}):
            with self.assertRaises(ValueError):
                Config.from_env({**self.env, **options})
