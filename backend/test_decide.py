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
from app.main import decide_codex_agent
import test_waiting


class DecideTests(unittest.TestCase):
    fake_agent = test_waiting.WaitingTests.fake_agent
    make_launcher = test_waiting.WaitingTests.make_launcher
    tearDown = test_waiting.WaitingTests.tearDown
    wait_until_waiting = test_waiting.WaitingTests.wait_until_waiting

    def test_decide_rejections(self):
        with self.assertRaises(HTTPException) as error:
            decide_codex_agent(str(uuid4()))
        self.assertEqual(error.exception.status_code, 404)
        for kind, status, has_session in (
            ('mock', 'waiting', True), ('codex', 'running', True),
            ('codex', 'finished', True), ('codex', 'stopped', True),
            ('codex', 'waiting', False),
        ):
            with self.subTest(kind=kind, status=status, session=has_session):
                agent_id, process = self.fake_agent(kind, has_session)
                manager.agent_statuses[agent_id] = status
                process.poll.return_value = None if status == 'running' else 0
                with patch.object(manager, '_spawn_process') as spawn:
                    with self.assertRaises(HTTPException) as error:
                        decide_codex_agent(agent_id)
                    self.assertEqual(error.exception.status_code, 409)
                    spawn.assert_not_called()

    def test_failed_decide_preserves_waiting_question_and_history(self):
        agent_id, process = self.fake_agent()
        process.poll.return_value = 0
        manager.agent_waiting_questions[agent_id] = 'Which design?'
        manager.agent_outputs[agent_id].append('CONTROL_CENTER_WAITING: Which design?')
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('Cannot launch')):
            with self.assertRaises(HTTPException) as error:
                decide_codex_agent(agent_id)
            self.assertEqual(error.exception.status_code, 503)
        detail = manager.get_agent(agent_id)
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Which design?')
        self.assertEqual(detail['output'], ['CONTROL_CENTER_WAITING: Which design?'])

    def wait_for_lines(self, agent_id, count):
        deadline = time.monotonic() + 5
        while len(manager.agent_outputs[agent_id]) < count:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)

    @unittest.skipUnless(os.name == 'nt', 'Windows delegation shim')
    def test_delegate_preserves_agent_then_redirect_wait_and_stop(self):
        session_id = str(uuid4())
        with tempfile.TemporaryDirectory(prefix='codex decide ') as directory:
            self.make_launcher(directory,
                'import json, os, sys, time\n'
                'sys.stdin.reconfigure(encoding="utf-8")\n'
                'prompt = sys.stdin.read()\n'
                f'print("session id: {session_id}", flush=True)\n'
                'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
                'if "The user has delegated this current decision to you." in prompt:\n'
                '    time.sleep(60)\n'
                'elif "Ask a different question" in prompt:\n'
                '    print("CONTROL_CENTER_WAITING: Which file next?", flush=True)\n'
                'else:\n'
                '    print("CONTROL_CENTER_WAITING: Which design?", flush=True)\n')
            with patch.dict(os.environ, {'APPDATA': directory}):
                for sandbox in ('read-only', 'workspace-write'):
                    agent_id = manager.start_agent('Original user task', 'codex', sandbox)
                    self.wait_until_waiting(agent_id)
                    history = manager.agent_outputs[agent_id].copy()
                    original_process = manager.agents[agent_id]
                    with patch.object(manager.notifications, 'cancel', wraps=manager.notifications.cancel) as cancel:
                        result = decide_codex_agent(agent_id)
                        cancel.assert_called_once_with(agent_id)
                    self.assertEqual(result, {'agent_id': agent_id, 'status': 'running'})
                    self.assertIsNone(manager.agent_waiting_questions[agent_id])
                    replacement = manager.agents[agent_id]
                    self.assertIsNot(replacement, original_process)
                    self.assertTrue(replacement.stdin.closed)
                    self.wait_for_lines(agent_id, len(history) + 4)
                    detail = manager.get_agent(agent_id)
                    self.assertEqual(detail['agent_id'], agent_id)
                    self.assertEqual(detail['session_id'], session_id)
                    self.assertEqual(detail['sandbox'], sandbox)
                    self.assertEqual(detail['task'], 'Original user task')
                    self.assertEqual(detail['output'][:len(history) + 2],
                                     history + ['--- Delegated Decision ---', 'Decide for me this time'])
                    payload = json.loads(detail['output'][len(history) + 3])
                    self.assertEqual(payload['args'], ['exec', '--sandbox', sandbox, '--color', 'never',
                                                     '--skip-git-repo-check', 'resume', session_id, '-'])
                    self.assertEqual(Path(payload['cwd']), manager._project_root)
                    self.assertIn(manager._delegated_decision, payload['prompt'])
                    self.assertIn('CONTROL_CENTER_WAITING: <your question>', payload['prompt'])
                    self.assertIn('only to the current decision', payload['prompt'])
                    self.assertNotIn(manager._delegated_decision, ' '.join(payload['args']))
                    self.assertEqual(sum(item['agent_id'] == agent_id for item in manager.get_agents()), 1)
                    manager.redirect_agent(agent_id, 'Ask a different question')
                    self.assertIsNotNone(replacement.poll())
                    self.wait_until_waiting(agent_id)
                    self.assertEqual(manager.get_agent(agent_id)['waiting_question'], 'Which file next?')
                    decide_codex_agent(agent_id)
                    current = manager.agents[agent_id]
                    self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
                    self.assertIsNotNone(current.poll())


if __name__ == '__main__':
    unittest.main()
