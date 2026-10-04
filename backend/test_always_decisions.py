import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from app import agent_manager as manager
from app.main import decide_always_codex_agent, disable_always_codex_agent
import test_similar_decisions


class AlwaysDecisionTests(unittest.TestCase):
    from workspace_test_support import process_test_setup as setUp
    fake_agent = test_similar_decisions.SimilarDecisionTests.fake_agent
    waiting_agent = test_similar_decisions.SimilarDecisionTests.waiting_agent
    replacement = test_similar_decisions.SimilarDecisionTests.replacement
    resume = test_similar_decisions.SimilarDecisionTests.resume
    tearDown = test_similar_decisions.SimilarDecisionTests.tearDown
    make_launcher = test_similar_decisions.SimilarDecisionTests.make_launcher
    wait_until_waiting = test_similar_decisions.SimilarDecisionTests.wait_until_waiting

    def finish(self, agent_id, question=None, handled=False):
        process = manager.agents[agent_id]
        text = '\x1b[32mCONTROL_CENTER_ALWAYS_HANDLED\x1b[0m\n' if handled else ''
        if question:
            text += 'CONTROL_CENTER_WAITING: ' + question + '\n'
        process.stdout = io.StringIO('codex\n' + text)
        manager._read_output(agent_id, process)
        process.poll.return_value = 0

    def test_defaults_other_actions_and_isolation(self):
        for kind in ('mock', 'codex'):
            agent_id, _ = self.fake_agent(kind)
            self.assertFalse(manager.get_agent(agent_id)['always_decide_enabled'])
        for action in (manager.decide_agent, manager.decide_similar_agent,
                       lambda key: manager.reply_agent(key, 'Answer')):
            agent_id, _ = self.waiting_agent()
            self.resume(action, agent_id)
            self.assertFalse(manager.get_agent(agent_id)['always_decide_enabled'])
        first, _ = self.waiting_agent()
        second, _ = self.waiting_agent()
        self.resume(decide_always_codex_agent, first)
        self.assertTrue(manager.get_agent(first)['always_decide_enabled'])
        self.assertFalse(manager.get_agent(second)['always_decide_enabled'])

    def test_enable_rejections(self):
        for action in (decide_always_codex_agent, disable_always_codex_agent):
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
                        decide_always_codex_agent(agent_id)
                    self.assertEqual(error.exception.status_code, 409)
                    spawn.assert_not_called()

    def test_manual_enable_metadata_history_stdin_and_notification(self):
        for sandbox in ('read-only', 'workspace-write'):
            agent_id, old = self.waiting_agent()
            manager.agent_sandboxes[agent_id] = sandbox
            session = manager.agent_sessions[agent_id]
            with patch.object(manager.notifications, 'cancel') as cancel:
                result, process = self.resume(decide_always_codex_agent, agent_id)
                cancel.assert_called_once_with(agent_id)
            self.assertEqual(result, {'agent_id': agent_id, 'status': 'running'})
            detail = manager.get_agent(agent_id)
            self.assertIs(manager.agents[agent_id], process)
            self.assertIsNot(process, old)
            self.assertEqual(detail['session_id'], session)
            self.assertEqual(detail['sandbox'], sandbox)
            self.assertEqual(detail['task'], 'Original task')
            self.assertIsNone(detail['waiting_question'])
            self.assertEqual(detail['output'], ['Previous output', '--- Always Decide Enabled ---',
                                               'Always decide for this agent'])
            self.assertIn('Which local variable name?', process.stdin.saved)
            self.assertIn('Do not fabricate', process.stdin.saved)
            self.assertIn('approval/security rules', process.stdin.saved)
            self.assertTrue(process.stdin.closed)

    def test_failed_manual_spawn_preserves_waiting_and_history(self):
        agent_id, old = self.waiting_agent()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('launch failed')):
            with self.assertRaises(HTTPException) as error:
                decide_always_codex_agent(agent_id)
            self.assertEqual(error.exception.status_code, 503)
        detail = manager.get_agent(agent_id)
        self.assertFalse(detail['always_decide_enabled'])
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Which local variable name?')
        self.assertEqual(detail['output'], ['Previous output'])
        self.assertIs(manager.agents[agent_id], old)

    def test_failed_manual_stdin_restores_waiting_and_history(self):
        agent_id, old = self.waiting_agent()
        process = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_start_reader'), \
             patch.object(process.stdin, 'write', side_effect=BrokenPipeError('closed')), \
             patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            with self.assertRaises(HTTPException) as error:
                decide_always_codex_agent(agent_id)
            self.assertEqual(error.exception.status_code, 503)
        detail = manager.get_agent(agent_id)
        self.assertFalse(detail['always_decide_enabled'])
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Which local variable name?')
        self.assertEqual(detail['output'], ['Previous output'])
        self.assertIs(manager.agents[agent_id], old)

    def test_unresolved_wait_notifies_once_and_cannot_loop(self):
        agent_id, _ = self.waiting_agent()
        manager.agent_similar_decisions[agent_id].enabled = True
        manager.agent_similar_decisions[agent_id].examples.append('Which approach?')
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'What is your token?')
        with patch.object(manager.notifications, 'transition') as notify:
            _, process = self.resume(manager.get_agent, agent_id)
            self.assertEqual(manager.agent_statuses[agent_id], 'running')
            self.assertIn('What is your token?', process.stdin.saved)
            self.assertIn('unavailable information', process.stdin.saved)
            notify.assert_not_called()
            self.finish(agent_id, 'Please provide the missing token')
            with patch.object(manager, '_spawn_process') as spawn:
                for _ in range(3):
                    self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
                    manager.get_agents()
                spawn.assert_not_called()
            notify.assert_called_once_with(agent_id, 'waiting', 'Original task')
        # A manual reply ends the blocked episode, even if wording later repeats.
        self.resume(lambda key: manager.reply_agent(key, 'Use local configuration'), agent_id)
        self.finish(agent_id, 'What is your token?')
        self.resume(manager.get_agent, agent_id)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')

    def test_handled_new_episode_and_finished_notifications(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'Which approach?')
        with patch.object(manager.notifications, 'transition') as notify:
            self.resume(manager.get_agent, agent_id)
            self.finish(agent_id, 'Which file next?', handled=True)
            self.assertTrue(manager.agent_always_decisions[agent_id].handled)
            _, process = self.resume(manager.get_agent, agent_id)
            self.assertIn('Which file next?', process.stdin.saved)
            notify.assert_not_called()
            self.finish(agent_id, handled=True)
            self.assertEqual(manager.get_agent(agent_id)['status'], 'finished')
            notify.assert_called_once_with(agent_id, 'finished', 'Original task')

    def test_failed_automatic_launch_falls_back_once(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'Which approach?')
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('launch failed')) as spawn, \
             patch.object(manager.notifications, 'transition') as notify, \
             self.assertLogs(manager.logger, level='ERROR'):
            for _ in range(3):
                detail = manager.get_agent(agent_id)
                self.assertEqual(detail['status'], 'waiting')
                self.assertEqual(detail['waiting_question'], 'Which approach?')
                manager.get_agents()
            spawn.assert_called_once()
            notify.assert_called_once_with(agent_id, 'waiting', 'Original task')

    def test_priority_and_disable_restore_saved_similar_policy(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        similar = manager.agent_similar_decisions[agent_id]
        similar.attempted.add('Previously declined')
        self.finish(agent_id, 'New question')
        manager.agent_statuses[agent_id] = 'waiting'
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'Another question')
        with patch.object(manager, '_similar_prompt') as prompt:
            _, automatic = self.resume(manager.get_agent, agent_id)
            prompt.assert_not_called()
        with patch.object(manager, '_stop_windows_tree') as stop:
            result = disable_always_codex_agent(agent_id)
            self.assertFalse(result['always_decide_enabled'])
            stop.assert_not_called()
        self.assertIs(manager.agents[agent_id], automatic)
        self.assertIsNone(automatic.poll())
        self.assertIs(manager.agent_similar_decisions[agent_id], similar)
        self.assertTrue(similar.enabled)
        self.assertEqual(similar.examples, ['Which local variable name?'])
        self.assertEqual(similar.attempted, {'Previously declined'})
        self.finish(agent_id, 'Another question')
        _, process = self.resume(manager.get_agent, agent_id)
        self.assertIn('Approved examples (JSON):', process.stdin.saved)
        self.assertIn('Always Decide is disabled', process.stdin.saved)
        self.assertEqual(manager.agent_outputs[agent_id][-2], '--- Automatic Similar Decision ---')

    def test_failed_automatic_stdin_preserves_question_without_retry(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'Missing file?')
        history = manager.agent_outputs[agent_id].copy()
        process = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', return_value=process) as spawn, \
             patch.object(manager, '_start_reader'), \
             patch.object(process.stdin, 'write', side_effect=BrokenPipeError('closed')), \
             patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)), \
             patch.object(manager.notifications, 'transition') as notify, \
             self.assertLogs(manager.logger, level='ERROR'):
            for _ in range(3):
                detail = manager.get_agent(agent_id)
                self.assertEqual(detail['status'], 'waiting')
                self.assertEqual(detail['waiting_question'], 'Missing file?')
                self.assertEqual(detail['output'], history)
            spawn.assert_called_once()
            notify.assert_called_once_with(agent_id, 'waiting', 'Original task')

    def test_handled_marker_requires_current_automatic_process_and_exact_line(self):
        agent_id, process = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, handled=True)
        self.assertFalse(manager.agent_always_decisions[agent_id].handled)
        manager.agent_waiting_questions[agent_id] = 'Which approach?'
        self.resume(manager.get_agent, agent_id)
        process.stdout = io.StringIO('CONTROL_CENTER_ALWAYS_HANDLED\n')
        manager._read_output(agent_id, process)
        self.assertFalse(manager.agent_always_decisions[agent_id].handled)
        current = manager.agents[agent_id]
        current.stdout = io.StringIO('quoted CONTROL_CENTER_ALWAYS_HANDLED\n')
        manager._read_output(agent_id, current)
        self.assertFalse(manager.agent_always_decisions[agent_id].handled)

    def test_disable_after_similar_decline_does_not_block_new_similar_episode(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_similar_agent, agent_id)
        self.finish(agent_id, 'Another approach?')
        self.resume(manager.get_agent, agent_id)
        self.finish(agent_id, 'Another approach?')
        self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
        self.resume(manager.decide_always_agent, agent_id)
        manager.disable_always_agent(agent_id)
        self.finish(agent_id, 'New choice?')
        _, process = self.resume(manager.get_agent, agent_id)
        self.assertIn('Approved examples (JSON):', process.stdin.saved)
        self.assertIn('New choice?', process.stdin.saved)

    def test_disable_without_similar_returns_to_ordinary_wait(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        manager.disable_always_agent(agent_id)
        self.finish(agent_id, 'Which approach?')
        with patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
            spawn.assert_not_called()

    def test_policy_survives_actions_and_stop_targets_current_process(self):
        agent_id, _ = self.waiting_agent()
        self.resume(manager.decide_always_agent, agent_id)
        self.finish(agent_id, 'Which approach?')
        _, automatic = self.resume(manager.get_agent, agent_id)
        with patch.object(manager, '_stop_windows_tree') as stop:
            self.resume(lambda key: manager.redirect_agent(key, 'Correction'), agent_id)
            stop.assert_called_once_with(automatic)
        for action in (manager.decide_agent, manager.decide_similar_agent,
                       lambda key: manager.reply_agent(key, 'Answer')):
            self.finish(agent_id, 'Missing information')
            manager.agent_statuses[agent_id] = 'waiting'
            self.resume(action, agent_id)
            self.assertTrue(manager.agent_always_decisions[agent_id].enabled)
        with patch.object(manager, '_stop_windows_tree') as stop:
            self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
            stop.assert_called_once_with(manager.agents[agent_id])

    @unittest.skipUnless(os.name == 'nt', 'Windows CLI shim')
    def test_windows_resume_stdin_cwd_session_sandbox_and_watcher(self):
        session = str(uuid4())
        with tempfile.TemporaryDirectory(prefix='codex always ') as directory:
            self.make_launcher(directory,
                'import json, os, sys, time\n'
                'sys.stdin.reconfigure(encoding="utf-8")\n'
                'prompt = sys.stdin.read()\n'
                f'print("session id: {session}", flush=True)\n'
                'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
                'print("codex", flush=True)\n'
                'if "CONTROL_CENTER_ALWAYS_HANDLED" in prompt:\n'
                '    print("CONTROL_CENTER_ALWAYS_HANDLED", flush=True)\n'
                '    time.sleep(60)\n'
                'elif "The user has enabled Always Decide" in prompt:\n'
                '    print("CONTROL_CENTER_WAITING: Which other approach?", flush=True)\n'
                'else:\n'
                '    print("CONTROL_CENTER_WAITING: Which approach?", flush=True)\n')
            with patch.dict(os.environ, {'APPDATA': directory}):
                for sandbox in ('read-only', 'workspace-write'):
                    agent_id = manager.start_agent('Original task', 'codex', sandbox)
                    self.wait_until_waiting(agent_id)
                    with patch.object(manager.notifications, 'transition') as notify:
                        manager.decide_always_agent(agent_id)
                        deadline = time.monotonic() + 8
                        while not manager.agent_always_decisions[agent_id].handled:
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                        notify.assert_not_called()
                    detail = manager.get_agent(agent_id)
                    self.assertEqual(detail['status'], 'running')
                    payloads = [json.loads(line) for line in detail['output'] if line.startswith('{')]
                    self.assertEqual(len(payloads), 3)
                    for payload in payloads[1:]:
                        self.assertEqual(payload['args'], ['exec', '--sandbox', sandbox, '--color', 'never',
                                                          '--skip-git-repo-check', 'resume', session, '-'])
                        self.assertEqual(Path(payload['cwd']), self.workspace_path)
                    self.assertIn('Which approach?', payloads[1]['prompt'])
                    self.assertIn('Which other approach?', payloads[2]['prompt'])
                    self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
