import base64
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from app import agent_manager as manager
from app import system_notifications as native
from app.main import NotificationPreference, notification_heartbeat, set_notification_preference
import test_redirect


class FakeTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.deliver = Mock()
        self.service = native.SystemNotifications(
            deliver=self.deliver, clock=lambda: self.now, timer=FakeTimer)
        self.service.set_enabled(True)

    def schedule(self, status='waiting'):
        self.service.transition('stable-uuid', status, 'Review authentication')
        return self.service._pending['stable-uuid']

    def test_waiting_finished_only_once_per_transition(self):
        for status, title in [('waiting', 'Agent needs your input'), ('finished', 'Agent finished')]:
            timer = self.schedule(status)
            self.assertEqual(timer.delay, native.DELIVERY_GRACE_SECONDS)
            self.now += native.DELIVERY_GRACE_SECONDS
            timer.callback()
            timer.callback()
            self.deliver.assert_called_with(title, 'Review authentication')
        self.assertEqual(self.deliver.call_count, 2)
        for status in ('running', 'stopped'):
            self.service.transition('stable-uuid', status, 'Task')
        self.assertEqual(self.service._pending, {})

    def test_active_dashboard_suppresses_native(self):
        self.service.heartbeat()
        timer = self.schedule()
        self.now += native.DELIVERY_GRACE_SECONDS - 1
        self.service.heartbeat()
        self.now += 1
        timer.callback()
        self.deliver.assert_not_called()

    def test_expired_and_recently_closed_dashboard_deliver(self):
        for age in (0, native.DASHBOARD_LEASE_SECONDS + 1):
            self.service.heartbeat()
            self.now += age
            timer = self.schedule()
            self.now += native.DELIVERY_GRACE_SECONDS
            timer.callback()
        self.assertEqual(self.deliver.call_count, 2)

    def test_disable_cancels_pending_and_reenable_does_not_replay(self):
        timer = self.schedule()
        self.service.set_enabled(False)
        self.assertTrue(timer.cancelled)
        self.service.heartbeat()
        self.assertIsNone(self.service.last_seen)
        self.service.transition('other', 'finished', 'Task')
        self.service.set_enabled(True)
        timer.callback()
        self.deliver.assert_not_called()
        self.assertEqual(self.service._pending, {})

    def test_resume_or_stop_cancels_old_notification(self):
        timer = self.schedule()
        self.service.cancel('stable-uuid')
        timer.callback()
        self.deliver.assert_not_called()

    def test_failure_is_contained(self):
        self.deliver.side_effect = OSError('Windows denied delivery')
        timer = self.schedule()
        with self.assertLogs(native.logger, level='ERROR'):
            timer.callback()
        self.assertEqual(self.service._pending, {})

    def test_preferences_and_heartbeat_endpoints(self):
        with patch('app.main.notifications', self.service):
            self.assertEqual(set_notification_preference(NotificationPreference(enabled=False)), {'enabled': False})
            set_notification_preference(NotificationPreference(enabled=True))
            notification_heartbeat()
            self.assertEqual(self.service.last_seen, self.now)

    @unittest.skipUnless(os.name == 'nt', 'Windows executable')
    def test_notification_text_is_stdin_data_only(self):
        title = 'Title " ; $(Get-Process) & %PATH%'
        body = '<script> & " newline\nUnicode שלום'
        with patch.object(native.subprocess, 'run') as run:
            native.deliver_windows_notification(title, body)
            first = run.call_args
            native.deliver_windows_notification('Different title', 'Different body')
            self.assertEqual(first.args[0], run.call_args.args[0])
        self.assertEqual(json.loads(first.kwargs['input']), {'title': title, 'body': body})
        self.assertFalse(first.kwargs['shell'])
        self.assertIn('-WindowStyle', first.args[0])
        self.assertEqual(base64.b64decode(first.args[0][-1]).decode('utf-16-le'), native._SCRIPT)
        self.assertNotIn(body, ' '.join(first.args[0]))


class CompletionTests(unittest.TestCase):
    fake_agent = test_redirect.RedirectTests.fake_agent
    tearDown = test_redirect.RedirectTests.tearDown

    def test_completion_without_get_polling(self):
        for output, expected in [('Done', 'finished'), ('CONTROL_CENTER_WAITING: Which file?', 'waiting')]:
            def spawn(command, agent_type):
                return subprocess.Popen(
                    [sys.executable, '-c', 'import sys; sys.stdin.read(); print("codex", flush=True); print(sys.argv[1], flush=True)', output],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            with patch.object(manager, '_spawn_process', side_effect=spawn), \
                 patch.object(manager, '_codex_command', return_value='fixed command'), \
                 patch.object(manager.notifications, 'transition') as notify:
                agent_id = manager.start_agent('Review task', 'codex')
                deadline = time.monotonic() + 5
                # Deliberately inspect the registry, not any GET/status helper.
                while manager.agent_statuses[agent_id] == 'running':
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                self.assertEqual(manager.agent_statuses[agent_id], expected)
                self.assertEqual(manager.agent_outputs[agent_id], ['codex', output])
                notify.assert_called_once_with(agent_id, expected, 'Review task')
                manager.get_agents()
                manager.get_agent(agent_id)
                self.assertEqual(notify.call_count, 1)

    def test_buffered_output_must_be_parsed_first(self):
        agent_id, process = self.fake_agent()
        process.poll.return_value = 0
        process.stdout = io.StringIO('codex\nCONTROL_CENTER_WAITING: Which module?\n')
        with patch.object(manager.notifications, 'transition') as notify:
            manager._finalize_process(agent_id, process)
            self.assertEqual(manager.agent_statuses[agent_id], 'running')
            notify.assert_not_called()
            manager._read_output(agent_id, process)
            manager._finalize_process(agent_id, process)
            self.assertEqual(manager.agent_statuses[agent_id], 'waiting')
            notify.assert_called_once()

    def test_stopped_and_stale_processes_cannot_finalize(self):
        agent_id, process = self.fake_agent()
        process.poll.return_value = 0
        with patch.object(manager.notifications, 'transition') as notify:
            manager.agent_statuses[agent_id] = 'stopped'
            manager._finalize_process(agent_id, process)
            self.assertEqual(manager.agent_statuses[agent_id], 'stopped')
            manager.agent_statuses[agent_id] = 'running'
            replacement = Mock()
            with patch.dict(manager.agents, {agent_id: replacement}):
                manager._finalize_process(agent_id, process)
                self.assertEqual(manager.agent_statuses[agent_id], 'running')
            notify.assert_not_called()

    def test_notification_failure_does_not_change_completion(self):
        agent_id, process = self.fake_agent()
        process.poll.return_value = 0
        with patch.object(manager.notifications, 'transition', side_effect=RuntimeError('Failed')), \
             self.assertLogs(manager.logger, level='ERROR'):
            manager._finalize_process(agent_id, process)
        self.assertEqual(manager.agent_statuses[agent_id], 'finished')


if __name__ == '__main__':
    unittest.main()
