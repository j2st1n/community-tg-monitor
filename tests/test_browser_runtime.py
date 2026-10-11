import os
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from app import browser_runtime as runtime


class DesktopRuntimeTest(unittest.TestCase):
    def test_child_environment_excludes_account_and_bot_credentials(self):
        with patch.dict(os.environ, {'PATH': '/bin', 'SBSB_COOKIE': 'private-session',
                                     'TG_BOT_TOKEN': 'private-token', 'API_KEY': 'private-key'}, clear=True):
            result = runtime._browser_env(':123')
        self.assertEqual({'PATH': '/bin', 'DISPLAY': ':123'}, result)

    def test_shutdown_targets_only_the_owned_group_and_waits(self):
        process = Mock(pid=4321)
        process.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 3), 0]
        with patch.object(runtime.os, 'killpg') as kill:
            runtime._stop_process_group(process)
        self.assertEqual([(4321, signal.SIGTERM), (4321, signal.SIGKILL)],
                         [call.args for call in kill.call_args_list])
        self.assertEqual(2, process.wait.call_count)

    def test_desktop_launcher_rejects_headless_request_without_starting_processes(self):
        with patch.object(runtime.subprocess, 'Popen') as spawn:
            with self.assertRaises(ValueError):
                runtime.DesktopChromium(Mock(), Mock()).launch(headless=True)
        spawn.assert_not_called()


if __name__ == '__main__':
    unittest.main()
