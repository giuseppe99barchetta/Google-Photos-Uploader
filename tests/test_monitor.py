import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from script_gphoto import WatchSupervisor, healthcheck, write_health


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_missing_folder_recovers_when_it_returns(self):
        folder = self.root / 'nas'
        supervisor = WatchSupervisor(folder, Mock())
        self.addCleanup(supervisor.stop)
        self.assertFalse(supervisor.check())
        folder.mkdir()
        self.assertTrue(supervisor.check())
        self.assertTrue(supervisor.observer.is_alive())

    def test_dead_emitter_restarts_observer(self):
        old_observer = Mock()
        old_observer.emitters = [Mock(is_alive=Mock(return_value=False))]
        supervisor = WatchSupervisor(self.root, Mock())
        self.addCleanup(supervisor.stop)
        supervisor.observer = old_observer
        info = self.root.stat()
        supervisor.identity = (info.st_dev, info.st_ino)
        self.assertTrue(supervisor.check())
        old_observer.stop.assert_called_once()
        self.assertIsNot(supervisor.observer, old_observer)

    def test_permission_error_marks_monitor_unavailable(self):
        supervisor = WatchSupervisor(self.root, Mock())
        with patch('script_gphoto.os.scandir', side_effect=PermissionError('offline')):
            self.assertFalse(supervisor.check())

    def test_health_requires_recent_positive_heartbeat(self):
        self.assertFalse(healthcheck(self.root))
        write_health(self.root, True)
        self.assertTrue(healthcheck(self.root))
        write_health(self.root, False)
        self.assertFalse(healthcheck(self.root))
        (self.root / 'health.json').write_text(json.dumps({'ok': True, 'updated_at': time.time() - 100}))
        self.assertFalse(healthcheck(self.root))
        (self.root / 'health.json').write_text('invalid')
        self.assertFalse(healthcheck(self.root))
