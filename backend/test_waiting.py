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
from pydantic import ValidationError

from app import agent_manager as manager
from app.main import ReplyAgentRequest, reply_codex_agent
import test_redirect


class WaitingTests(unittest.TestCase):
    fake_agent = test_redirect.RedirectTests.fake_agent
    make_launcher = test_redirect.RedirectTests.make_launcher
    tearDown = test_redirect.RedirectTests.tearDown

    def test_marker_detection_and_exit(self):
        agent_id, process = self.fake_agent()
        line = '\x1b[35mCONTROL_CENTER_WAITING:\x1b[0m Which module should I inspect?'
        process.stdout = io.StringIO(line + '\n')
        manager._read_output(agent_id, process)
        self.assertEqual(manager.get_agent(agent_id)["status"], "running")
        self.assertEqual(manager.get_agent(agent_id)["waiting_question"], "Which module should I inspect?")
        self.assertEqual(manager.get_agent(agent_id)["output"], [line])
        process.poll.return_value = 0
        self.assertEqual(manager.get_agent(agent_id)["status"], "waiting")
        self.assertEqual(manager.get_agents()[0]["status"], "waiting")

    def test_exit_waits_for_buffered_marker(self):
        agent_id, process = self.fake_agent()
        process.stdout = io.StringIO('CONTROL_CENTER_WAITING: Pick a module.\n')
        process.poll.return_value = 0
        self.assertEqual(manager.get_agent(agent_id)["status"], "running")
        manager._read_output(agent_id, process)
        self.assertEqual(manager.get_agent(agent_id)["status"], "waiting")

    def test_only_exact_marker_on_current_codex_process_waits(self):
        for output in ('Any questions?', 'control_center_waiting: Choose?',
                       'Example CONTROL_CENTER_WAITING: Choose?', 'CONTROL_CENTER_WAITING:   ',
                       'Normal completion'):
            agent_id, process = self.fake_agent()
            process.stdout = io.StringIO(output + '\n')
            manager._read_output(agent_id, process)
            process.poll.return_value = 0
            self.assertEqual(manager.get_agent(agent_id)["status"], "finished")
            self.assertIsNone(manager.get_agent(agent_id)["waiting_question"])
        mock_id, mock_process = self.fake_agent('mock')
        mock_process.stdout = io.StringIO('CONTROL_CENTER_WAITING: Question?\n')
        manager._read_output(mock_id, mock_process)
        mock_process.poll.return_value = 0
        self.assertEqual(manager.get_agent(mock_id)["status"], 'finished')
        agent_id, old = self.fake_agent()
        old.stdout = io.StringIO('CONTROL_CENTER_WAITING: Stale question?\n')
        with patch.dict(manager.agents, {agent_id: object()}):
            manager._read_output(agent_id, old)
        self.assertIsNone(manager.get_agent(agent_id)["waiting_question"])

    def test_reply_rejections(self):
        for answer in ('', ' ', '\n\t'):
            with self.assertRaises(ValidationError):
                ReplyAgentRequest(answer=answer)
            with self.assertRaises(ValueError):
                manager.reply_agent('missing', answer)
        with self.assertRaises(HTTPException) as error:
            reply_codex_agent(str(uuid4()), ReplyAgentRequest(answer='Answer'))
        self.assertEqual(error.exception.status_code, 404)
        for kind, status, has_session in (
            ('mock', 'waiting', True), ('codex', 'running', True),
            ('codex', 'finished', True), ('codex', 'stopped', True),
            ('codex', 'waiting', False),
        ):
            agent_id, process = self.fake_agent(kind, has_session)
            manager.agent_statuses[agent_id] = status
            process.poll.return_value = None if status == 'running' else 0
            with patch.object(manager, '_spawn_process') as spawn:
                with self.assertRaises(HTTPException) as error:
                    reply_codex_agent(agent_id, ReplyAgentRequest(answer='Answer'))
                self.assertEqual(error.exception.status_code, 409)
                spawn.assert_not_called()

    def test_stop_waiting_and_redirect_rejected(self):
        agent_id, process = self.fake_agent()
        manager.agent_waiting_questions[agent_id] = 'Choose a module?'
        process.poll.return_value = 0
        self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
        with self.assertRaises(ValueError):
            manager.redirect_agent(agent_id, 'Answer')
        with patch.object(manager, '_stop_windows_tree') as stop:
            self.assertEqual(manager.stop_agent(agent_id), {'agent_id': agent_id, 'status': 'stopped'})
            stop.assert_not_called()
        self.assertEqual(manager.get_agent(agent_id)['status'], 'stopped')
        self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
        with self.assertRaises(ValueError):
            manager.reply_agent(agent_id, 'Answer')

    def test_stop_drains_final_output_before_deciding_status(self):
        for output, expected in (
            ('Completed normally.\n', 'finished'),
            ('CONTROL_CENTER_WAITING: Choose a module?\n', 'stopped'),
        ):
            agent_id, process = self.fake_agent()
            process.stdout = io.StringIO(output)
            process.poll.return_value = 0
            reader = manager.agent_readers[agent_id]
            reader.join.side_effect = lambda timeout: manager._read_output(agent_id, process)
            result = manager.stop_agent(agent_id)
            self.assertEqual(result['status'], expected)
            self.assertEqual(manager.get_agent(agent_id)['status'], expected)
            self.assertEqual(manager.get_agent(agent_id)['output'], [output.rstrip('\n')])

    def test_stop_does_not_guess_status_when_output_is_still_pending(self):
        agent_id, process = self.fake_agent()
        process.stdout = io.StringIO('Completed normally.\n')
        process.poll.return_value = 0
        manager.agent_readers[agent_id].is_alive.return_value = True
        try:
            with self.assertRaises(RuntimeError):
                manager.stop_agent(agent_id)
            self.assertEqual(manager.agent_statuses[agent_id], 'running')
        finally:
            manager._read_output(agent_id, process)
        self.assertEqual(manager.get_agent(agent_id)['status'], 'finished')

    def test_failed_reply_spawn_preserves_waiting(self):
        agent_id, process = self.fake_agent()
        manager.agent_waiting_questions[agent_id] = 'Choose?'
        manager.agent_outputs[agent_id].append('CONTROL_CENTER_WAITING: Choose?')
        process.poll.return_value = 0
        with patch.object(manager, '_codex_command', return_value='fixed'), \
             patch.object(manager, '_spawn_process', side_effect=OSError('Cannot launch')):
            with self.assertRaises(HTTPException) as error:
                reply_codex_agent(agent_id, ReplyAgentRequest(answer='Answer'))
            self.assertEqual(error.exception.status_code, 503)
        detail = manager.get_agent(agent_id)
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Choose?')
        self.assertEqual(detail['output'], ['CONTROL_CENTER_WAITING: Choose?'])

    def wait_until_waiting(self, agent_id):
        deadline = time.monotonic() + 5
        while manager.get_agent(agent_id)['status'] != 'waiting':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    @unittest.skipUnless(os.name == 'nt', 'Windows reply shim')
    def test_repeated_reply_preserves_identity_session_and_output(self):
        session_id = str(uuid4())
        with tempfile.TemporaryDirectory(prefix='codex waiting ') as directory:
            self.make_launcher(directory,
                'import json, os, sys, time\n'
                'sys.stdin.reconfigure(encoding="utf-8")\n'
                'prompt = sys.stdin.read()\n'
                f'print("session id: {session_id}", flush=True)\n'
                'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
                'time.sleep(0.2)\n'
                'print("CONTROL_CENTER_WAITING: Which module next?", flush=True)\n')
            with patch.dict(os.environ, {'APPDATA': directory}):
                for sandbox in ('read-only', 'workspace-write'):
                    agent_id = manager.start_agent('Original user task', 'codex', sandbox)
                    self.wait_until_waiting(agent_id)
                    initial = json.loads(manager.agent_outputs[agent_id][1])
                    self.assertIn('User request:\nOriginal user task', initial['prompt'])
                    self.assertIn('CONTROL_CENTER_WAITING: <your question>', initial['prompt'])
                    self.assertIn('Do not use this marker if you can reasonably continue without user input.', initial['prompt'])
                    for answer in ('Inspect auth "please" & echo literal\nOnly auth.', 'Now inspect tests.'):
                        history = manager.agent_outputs[agent_id].copy()
                        old = manager.agents[agent_id]
                        result = reply_codex_agent(agent_id, ReplyAgentRequest(answer=answer))
                        self.assertEqual(result, {'agent_id': agent_id, 'status': 'running'})
                        self.assertIsNone(manager.agent_waiting_questions[agent_id])
                        self.assertEqual(manager.agent_statuses[agent_id], 'running')
                        self.assertIsNot(manager.agents[agent_id], old)
                        self.assertTrue(manager.agents[agent_id].stdin.closed)
                        self.wait_until_waiting(agent_id)
                        detail = manager.get_agent(agent_id)
                        self.assertEqual(detail['agent_id'], agent_id)
                        self.assertEqual(detail['session_id'], session_id)
                        self.assertEqual(detail['task'], 'Original user task')
                        self.assertEqual(detail['sandbox'], sandbox)
                        self.assertEqual(detail['waiting_question'], 'Which module next?')
                        self.assertEqual(detail['output'][:len(history) + 2], history + ['--- User Reply ---', answer])
                        payload = json.loads(detail['output'][len(history) + 3])
                        self.assertEqual(payload['args'], ['exec', '--sandbox', sandbox, '--color', 'never',
                                                         '--skip-git-repo-check', 'resume', session_id, '-'])
                        self.assertNotIn(answer, payload['args'])
                        self.assertIn('User request:\n' + answer, payload['prompt'])
                        self.assertIn('CONTROL_CENTER_WAITING: <your question>', payload['prompt'])
                        self.assertEqual(Path(payload['cwd']), manager._project_root)
                        self.assertEqual(sum(item['agent_id'] == agent_id for item in manager.get_agents()), 1)
                    self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')


if __name__ == '__main__':
    unittest.main()
