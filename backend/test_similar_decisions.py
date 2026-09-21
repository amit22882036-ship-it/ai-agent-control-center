import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from app import agent_manager as manager
from app.main import decide_similar_codex_agent, disable_similar_codex_agent
import test_waiting


class SavedInput(io.StringIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class SimilarDecisionTests(unittest.TestCase):
    fake_agent = test_waiting.WaitingTests.fake_agent
    tearDown = test_waiting.WaitingTests.tearDown
    make_launcher = test_waiting.WaitingTests.make_launcher
    wait_until_waiting = test_waiting.WaitingTests.wait_until_waiting

    @unittest.skipUnless(os.name == 'nt', 'Windows CLI shim')
    def test_windows_auto_resume_stdin_command_cwd_and_watcher(self):
        session = str(uuid4())
        with tempfile.TemporaryDirectory(prefix='codex similar ') as directory:
            self.make_launcher(directory,
                'import json, os, sys, time\n'
                'sys.stdin.reconfigure(encoding="utf-8")\n'
                'prompt = sys.stdin.read()\n'
                f'print("session id: {session}", flush=True)\n'
                'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
                'print("codex", flush=True)\n'
                'if "Approved examples (JSON):" in prompt:\n'
                '    print("CONTROL_CENTER_SIMILAR_HANDLED", flush=True)\n'
                '    time.sleep(60)\n'
                'elif "The user has delegated" in prompt:\n'
                '    print("CONTROL_CENTER_WAITING: Which other variable name?", flush=True)\n'
                'else:\n'
                '    print("CONTROL_CENTER_WAITING: Which local variable name?", flush=True)\n')
            with patch.dict(os.environ, {'APPDATA': directory}):
                for sandbox in ('read-only', 'workspace-write'):
                    agent_id = manager.start_agent('Original task', 'codex', sandbox)
                    self.wait_until_waiting(agent_id)
                    with patch.object(manager.notifications, 'transition') as notify:
                        manager.decide_similar_agent(agent_id)
                        deadline = time.monotonic() + 8
                        # No GET: the process watcher must trigger the auto resume.
                        while not manager.agent_similar_decisions[agent_id].handled:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                        notify.assert_not_called()
                    detail = manager.get_agent(agent_id)
                    self.assertEqual(detail['status'], 'running')
                    self.assertEqual(detail['session_id'], session)
                    payloads = [json.loads(line) for line in detail['output'] if line.startswith('{')]
                    self.assertEqual(len(payloads), 3)
                    for payload in payloads[1:]:
                        self.assertEqual(payload['args'], ['exec', '--sandbox', sandbox, '--color', 'never',
                                                          '--skip-git-repo-check', 'resume', session, '-'])
                        self.assertEqual(Path(payload['cwd']), manager._project_root)
                    self.assertIn('Which local variable name?', payloads[-1]['prompt'])
                    self.assertIn('Which other variable name?', payloads[-1]['prompt'])
                    self.assertTrue(manager.agents[agent_id].stdin.closed)
                    self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')

    def waiting_agent(self):
        agent_id, process = self.fake_agent()
        process.poll.return_value = 0
        manager.agent_statuses[agent_id] = 'waiting'
        manager.agent_waiting_questions[agent_id] = 'Which local variable name?'
        manager.agent_outputs[agent_id].append('Previous output')
        return agent_id, process

    def replacement(self):
        process = Mock(pid=12345, stdin=SavedInput(), stdout=io.StringIO())
        process.stdout.close()
        process.poll.return_value = None
        return process

    def resume(self, action, agent_id):
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_start_reader'):
            result = action(agent_id)
        return result, process

    def finish(self, agent_id, question=None, handled=False):
        process = manager.agents[agent_id]
        text = (manager._similar_handled_marker + '\n') if handled else ''
        if question:
            text += 'CONTROL_CENTER_WAITING: ' + question + '\n'
        process.stdout = io.StringIO('codex\n' + text)
        manager._read_output(agent_id, process)
        process.poll.return_value = 0
        return process

    def test_defaults_isolation_and_one_time_actions(self):
        for action in (manager.decide_agent, lambda key: manager.reply_agent(key, 'Use x')):
            agent_id, _ = self.waiting_agent()
            self.resume(action, agent_id)
            policy = manager.agent_similar_decisions[agent_id]
            self.assertFalse(policy.enabled)
            self.assertEqual(policy.examples, [])
        first, _ = self.waiting_agent()
        second, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, first)
        self.assertTrue(manager.get_agent(first)['similar_decisions_enabled'])
        self.assertFalse(manager.get_agent(second)['similar_decisions_enabled'])

    def test_rejections(self):
        for action in (decide_similar_codex_agent, disable_similar_codex_agent):
            with self.assertRaises(HTTPException) as error:
                action('missing')
            self.assertEqual(error.exception.status_code, 404)
        for field, value in (('type', 'mock'), ('status', 'running'), ('status', 'finished'),
                             ('status', 'stopped'), ('session', None), ('question', None)):
            with self.subTest(field=field, value=value):
                agent_id, process = self.waiting_agent()
                registry = {'type': manager.agent_types, 'status': manager.agent_statuses,
                            'session': manager.agent_sessions, 'question': manager.agent_waiting_questions}[field]
                registry[agent_id] = value
                if value == 'running':
                    process.poll.return_value = None
                with patch.object(manager, '_codex_command', return_value='fixed command'), \
                     patch.object(manager, '_spawn_process') as spawn:
                    with self.assertRaises(HTTPException) as error:
                        decide_similar_codex_agent(agent_id)
                    self.assertEqual(error.exception.status_code, 409)
                    spawn.assert_not_called()

    def test_enable_preserves_identity_metadata_history_and_cancels_notification(self):
        for sandbox in ('read-only', 'workspace-write'):
            agent_id, old = self.waiting_agent()
            manager.agent_sandboxes[agent_id] = sandbox
            session = manager.agent_sessions[agent_id]
            with patch.object(manager.notifications, 'cancel') as cancel:
                result, replacement = self.resume(decide_similar_codex_agent, agent_id)
                cancel.assert_called_once_with(agent_id)
            self.assertEqual(result, {'agent_id': agent_id, 'status': 'running'})
            self.assertIs(manager.agents[agent_id], replacement)
            self.assertIsNot(replacement, old)
            detail = manager.get_agent(agent_id)
            self.assertEqual(detail['task'], 'Original task')
            self.assertEqual(detail['session_id'], session)
            self.assertEqual(detail['sandbox'], sandbox)
            self.assertIsNone(detail['waiting_question'])
            self.assertEqual(detail['output'], ['Previous output', '--- Similar Decisions Enabled ---',
                                               'Decide similar questions automatically'])
            self.assertEqual(manager.agent_similar_decisions[agent_id].examples, ['Which local variable name?'])
            self.assertIn(manager._delegated_decision, replacement.stdin.saved)

    def test_auto_decline_never_rechecks_and_notifies_only_final_wait(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Which other name?')
        with patch.object(manager.notifications, 'transition') as notify:
            _, process = self.resume(manager.get_agent, agent_id)
            self.assertEqual(manager.agent_statuses[agent_id], 'running')
            notify.assert_not_called()
            self.assertIn('Which local variable name?', process.stdin.saved)
            self.assertIn('Which other name?', process.stdin.saved)
            self.assertIn('decision types and scope', process.stdin.saved)
            self.assertEqual(manager.agent_outputs[agent_id][-2:],
                             ['--- Automatic Similar Decision ---', 'Which other name?'])
            # Even an incorrectly paraphrased decline cannot cause a loop.
            self.finish(agent_id, 'Please choose another name?')
            with patch.object(manager, '_spawn_process') as spawn:
                for _ in range(3):
                    self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
                    manager.get_agents()
                spawn.assert_not_called()
            notify.assert_called_once_with(agent_id, 'waiting', 'Original task')
        # Ordinary user reply does not erase the declined-question protection.
        self.resume(lambda key: manager.reply_agent(key, 'x'), agent_id)
        self.finish(agent_id, 'Which other name?')
        with patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
            spawn.assert_not_called()

    def test_explicit_approval_rearms_only_previously_declined_question(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        policy = manager.agent_similar_decisions[agent_id]
        question = 'Question B?'
        policy.attempted.add('Unrelated declined question')
        self.finish(agent_id, question)
        self.resume(manager.get_agent, agent_id)
        self.finish(agent_id, question)
        with patch.object(manager, '_spawn_process') as spawn:
            for _ in range(3):
                self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
                manager.get_agents()
            spawn.assert_not_called()
        self.assertIn(question, policy.attempted)

        # A failed explicit approval must leave both registries untouched.
        examples = policy.examples.copy()
        attempted = policy.attempted.copy()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('launch failed')):
            with self.assertRaises(OSError):
                manager.decide_similar_agent(agent_id)
        self.assertEqual(policy.examples, examples)
        self.assertEqual(policy.attempted, attempted)

        # Use surrounding whitespace to check the same normalization as the guard.
        manager.agent_waiting_questions[agent_id] = f'  {question}  '
        self.resume(manager.decide_similar_agent, agent_id)
        self.assertIn(f'  {question}  ', policy.examples)
        self.assertEqual(policy.attempted, {'Unrelated declined question'})
        self.finish(agent_id, question)
        replacement = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=replacement) as spawn, \
             patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_start_reader'):
            for _ in range(3):
                self.assertEqual(manager.get_agent(agent_id)['status'], 'running')
                manager.get_agents()
            spawn.assert_called_once()
            self.finish(agent_id, question)
            for _ in range(3):
                self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
                manager.get_agents()
            spawn.assert_called_once()
        self.assertIn(question, policy.attempted)
        self.assertEqual(manager.agent_outputs[agent_id].count('--- Automatic Similar Decision ---'), 2)

    def test_handled_then_different_question_can_get_one_attempt(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Second question')
        self.resume(manager.get_agent, agent_id)
        self.finish(agent_id, 'Third question', handled=True)
        _, process = self.resume(manager.get_agent, agent_id)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')
        self.assertIn('Third question', process.stdin.saved)
        self.finish(agent_id, handled=True)
        with patch.object(manager.notifications, 'transition') as notify:
            self.assertEqual(manager.get_agent(agent_id)['status'], 'finished')
            notify.assert_called_once_with(agent_id, 'finished', 'Original task')

    def test_failed_auto_launch_restores_wait_once_and_manual_reply(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Later question')
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('launch failed')) as spawn, \
             patch.object(manager.notifications, 'transition') as notify, \
             self.assertLogs(manager.logger, level='ERROR'):
            for _ in range(2):
                detail = manager.get_agent(agent_id)
                self.assertEqual(detail['status'], 'waiting')
                self.assertEqual(detail['waiting_question'], 'Later question')
            spawn.assert_called_once()
            notify.assert_called_once()
        self.resume(lambda key: manager.reply_agent(key, 'Answer'), agent_id)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')

    def test_failed_manual_enable_does_not_change_policy_or_history(self):
        agent_id, _ = self.waiting_agent()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('launch failed')):
            with self.assertRaises(HTTPException) as error:
                decide_similar_codex_agent(agent_id)
            self.assertEqual(error.exception.status_code, 503)
        detail = manager.get_agent(agent_id)
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Which local variable name?')
        self.assertFalse(detail['similar_decisions_enabled'])
        self.assertEqual(detail['output'], ['Previous output'])

    def test_policy_survives_reply_and_decide_and_adds_only_explicit_examples(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        policy = manager.agent_similar_decisions[agent_id]
        for action in (manager.decide_agent, lambda key: manager.reply_agent(key, 'Answer')):
            self.finish(agent_id, 'Different question')
            manager.agent_statuses[agent_id] = 'waiting'
            self.resume(action, agent_id)
            self.assertIs(manager.agent_similar_decisions[agent_id], policy)
            self.assertTrue(policy.enabled)
            self.assertEqual(policy.examples, ['Which local variable name?'])
        self.finish(agent_id, 'Different question')
        manager.agent_statuses[agent_id] = 'waiting'
        self.resume(manager.decide_similar_agent, agent_id)
        self.assertEqual(policy.examples, ['Which local variable name?', 'Different question'])

    def test_disable_does_not_interrupt_and_later_wait_is_normal(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Later question')
        _, process = self.resume(manager.get_agent, agent_id)
        with patch.object(manager, '_stop_windows_tree') as stop:
            self.assertFalse(disable_similar_codex_agent(agent_id)['similar_decisions_enabled'])
            stop.assert_not_called()
        self.assertIs(manager.agents[agent_id], process)
        self.assertIsNone(process.poll())
        self.assertEqual(manager.agent_similar_decisions[agent_id].examples, [])
        self.finish(agent_id, 'Another question')
        with patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
            spawn.assert_not_called()

    def test_stop_redirect_and_policy_survive_replacement(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Later question')
        _, automatic = self.resume(manager.get_agent, agent_id)
        with patch.object(manager, '_stop_windows_tree') as stop:
            _, redirected = self.resume(lambda key: manager.redirect_agent(key, 'Correction'), agent_id)
            stop.assert_called_once_with(automatic)
            self.assertTrue(manager.agent_similar_decisions[agent_id].enabled)
            stop.reset_mock()
            self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
            stop.assert_called_once_with(redirected)

    def test_stop_exited_process_does_not_launch_auto_resume(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Later question')
        with patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
            spawn.assert_not_called()
