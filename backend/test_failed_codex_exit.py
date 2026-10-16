"""A failed Codex run must never count as Task completion."""
import io
import os
import tempfile
import unittest
from unittest.mock import patch

from app import agent_manager as manager
import test_agent_manager
import test_dependencies

FAILURE_CODES = (1, 2, -1, 0xC000013A)  # generic, usage, signal-like, Windows Ctrl+C


class FailedCodexExitTests(unittest.TestCase):
    setUp = test_dependencies.DependencyTests.setUp
    tearDown = test_dependencies.DependencyTests.tearDown
    create = test_dependencies.DependencyTests.create
    replacement = test_dependencies.DependencyTests.replacement
    recover = test_dependencies.DependencyTests.recover
    resume = test_dependencies.DependencyTests.resume
    task_for = test_dependencies.DependencyTests.task_for
    task = test_dependencies.DependencyTests.task
    state = test_dependencies.DependencyTests.state
    edge = test_dependencies.DependencyTests.edge
    reasons = test_dependencies.DependencyTests.reasons

    def exit(self, key, text, code):
        """Deliver output, then let the worker exit with `code` and finalize it as its watcher would."""
        process = manager.agents[key]
        process.stdout = io.StringIO(text)
        manager._read_output(key, process)
        process.poll.return_value = code
        manager._finalize_process(key, process, allow_auto=False)

    def dependent_pair(self, kind='codex'):
        key, _ = self.create(kind=kind)
        source, dependent = self.task_for(key), self.task()
        self.edge(dependent, source)
        self.assertEqual(self.state(dependent)['status'], 'blocked')
        return key, source, dependent

    def test_nonzero_exit_without_question_does_not_complete_and_keeps_dependents_blocked(self):
        for code in FAILURE_CODES:
            with self.subTest(code=code):
                key, source, dependent = self.dependent_pair()
                self.notify.reset_mock()
                with patch.object(manager, '_spawn_process') as spawn:
                    self.exit(key, 'codex\nPartial work before the crash\n', code)
                    spawn.assert_not_called()
                self.assertEqual(manager.agent_statuses[key], 'stopped')
                self.assertNotEqual(self.state(source)['status'], 'completed')
                self.assertEqual(self.state(source)['status'], 'pending')
                assignment = manager._store.list_task_assignments(source['task_id'])[-1]
                self.assertEqual(assignment['ended_reason'], 'stopped')
                self.assertEqual(self.state(dependent)['status'], 'blocked')
                self.assertEqual(self.reasons(dependent), {'dependency_incomplete'})
                marker = f'--- Codex exited with code {code}; the Task was not completed ---'
                self.assertEqual(manager.agent_outputs[key][-1], marker)
                self.assertEqual(manager._store.full_output(key)[-1], marker)
                # No "Agent finished" notification for a failure ('stopped' is not announced).
                self.assertNotIn('finished', [call.args[1] for call in self.notify.call_args_list])

    def test_failure_survives_restart_recovery(self):
        key, source, dependent = self.dependent_pair()
        self.exit(key, 'codex\nboom\n', 1)
        self.recover()
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.state(source)['status'], 'pending')
        self.assertEqual(self.state(dependent)['status'], 'blocked')

    def test_nonzero_exit_with_valid_question_still_waits_and_reply_resumes(self):
        key, source, dependent = self.dependent_pair()
        self.exit(key, f'session id: {key}\ncodex\nCONTROL_CENTER_WAITING: Which option?\n', 1)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')
        self.assertEqual(self.state(source)['status'], 'waiting')
        self.assertEqual(self.state(dependent)['status'], 'blocked')
        self.assertNotIn('the Task was not completed', '\n'.join(manager.agent_outputs[key]))
        self.resume(key, lambda k: manager.reply_agent(k, 'Option A'))
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(self.state(source)['status'], 'in_progress')

    def test_successful_exit_still_completes_and_unblocks(self):
        key, source, dependent = self.dependent_pair()
        self.exit(key, 'codex\nAll done\n', 0)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.state(source)['status'], 'completed')
        self.assertEqual(self.state(dependent)['status'], 'pending')
        self.assertEqual(self.reasons(dependent), set())
        self.assertNotIn('the Task was not completed', '\n'.join(manager.agent_outputs[key]))
        self.notify.assert_called_with(key, 'finished', 'Task')

    def test_mock_provider_behaviour_is_unchanged(self):
        for code in (0, 1):
            with self.subTest(code=code):
                key, source, dependent = self.dependent_pair(kind='mock')
                self.exit(key, 'Agent finished\n', code)
                self.assertEqual(manager.agent_statuses[key], 'finished')
                self.assertEqual(self.state(source)['status'], 'completed')
                self.assertEqual(self.state(dependent)['status'], 'pending')


class FailedCodexLauncherTests(unittest.TestCase):
    """End to end through the real Windows launcher path with a fake codex.cmd (no AI)."""
    from workspace_test_support import process_test_setup as setUp
    tearDown = test_agent_manager.AgentTests.tearDown
    make_launcher = test_agent_manager.AgentTests.make_launcher

    @unittest.skipUnless(os.name == 'nt', 'Windows launcher integration')
    def test_real_nonzero_exit_code_reaches_classification(self):
        for code, expected in ((3, 'stopped'), (0, 'finished')):
            with self.subTest(code=code), tempfile.TemporaryDirectory(prefix='codex shim ') as directory:
                self.make_launcher(directory, 'import sys\nsys.stdin.read()\n'
                                   f'print("codex", flush=True)\nprint("work", flush=True)\nsys.exit({code})\n')
                with patch.dict(os.environ, {'APPDATA': directory}):
                    agent_id = manager.start_agent('Shim task', 'codex', 'read-only')
                    process = manager.agents[agent_id]
                    self.assertEqual(process.wait(timeout=10), code)
                    process._control_center_watcher.join(10)
                    self.assertEqual(manager.get_agent(agent_id)['status'], expected)
                    failed = f'--- Codex exited with code {code}; the Task was not completed ---'
                    self.assertEqual(failed in manager.agent_outputs[agent_id], code != 0)


if __name__ == '__main__':
    unittest.main()
