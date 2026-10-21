"""检查助手生命周期，不注册真实快捷键。"""
import os
import unittest
from unittest.mock import Mock, patch
import keyboard_session


class KeyboardSessionTests(unittest.TestCase):
    def tearDown(self):
        keyboard_session._process = None

    @patch('fill_helper.AUTOMATION_READY')
    @patch('keyboard_session.subprocess.Popen')
    def test_reuses_running_helper_and_restarts_after_exit(self, spawn, ready):
        process = spawn.return_value
        process.poll.return_value = None
        keyboard_session.start()
        keyboard_session.start()
        self.assertEqual(spawn.call_count, 1)
        command = spawn.call_args.args[0]
        self.assertIn('--owner-pid', command)
        self.assertIn(str(os.getpid()), command)
        process.poll.return_value = 0
        keyboard_session.start()
        self.assertEqual(spawn.call_count, 2)

    @patch('fill_helper.read_keyboard_state')
    @patch('fill_helper.AUTOMATION_READY')
    @patch('keyboard_session.subprocess.run')
    def test_close_stops_process_tree_and_removes_owned_readiness(self, run, ready, read):
        process = Mock(pid=1234)
        process.poll.return_value = None
        keyboard_session._process = process
        read.return_value = {'owner_pid': os.getpid()}
        keyboard_session.stop()
        self.assertEqual(run.call_args.args[0], ['taskkill', '/PID', '1234', '/T', '/F'])
        ready.unlink.assert_called_once_with(missing_ok=True)
        self.assertIsNone(keyboard_session._process)
        keyboard_session.stop()
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
