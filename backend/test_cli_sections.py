import io
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager
import test_redirect


class CliSectionTests(unittest.TestCase):
    from workspace_test_support import process_test_setup as setUp
    fake_agent = test_redirect.RedirectTests.fake_agent
    tearDown = test_redirect.RedirectTests.tearDown

    def read(self, transcript):
        key, process = self.fake_agent(session=False)
        process.stdout = io.StringIO(transcript)
        manager._read_output(key, process)
        process.poll.return_value = 0
        detail = manager.get_agent(key)
        self.assertEqual(detail['output'], transcript.splitlines())
        return detail

    def test_echoed_examples_and_realistic_questions_finish_normally(self):
        for question in ('<your question>', 'Which database should I use?'):
            with self.subTest(question=question):
                detail = self.read('user\nInternal control-center instruction:\n'
                                   f'CONTROL_CENTER_WAITING: {question}\n'
                                   '\ncodex\nThe task is complete.\ntokens used\n100\n')
                self.assertEqual(detail['status'], 'finished')
                self.assertIsNone(detail['waiting_question'])

    def test_only_genuine_response_question_wins_over_echoed_question(self):
        session = str(uuid4())
        detail = self.read(f'session id: {session}\nuser\n'
                           'CONTROL_CENTER_WAITING: Which database should I use?\n'
                           '\x1b[32mcodex\x1b[0m\n'
                           '\x1b[35mCONTROL_CENTER_WAITING:\x1b[0m Which file should I inspect?\n')
        self.assertEqual(detail['status'], 'waiting')
        self.assertEqual(detail['waiting_question'], 'Which file should I inspect?')
        self.assertEqual(detail['session_id'], session)

    def test_non_response_sections_cannot_set_waiting(self):
        for section in ('user', 'thinking', 'exec', 'tool', 'system', 'developer',
                        'file update', 'tokens used', 'mcp startup: ready', 'warning: message', 'error: message'):
            with self.subTest(section=section):
                detail = self.read('codex\nWorking.\n' + section + '\nCONTROL_CENTER_WAITING: Echo\n')
                self.assertIsNone(detail['waiting_question'])
                self.assertEqual(detail['status'], 'finished')
        self.assertIsNone(self.read('CONTROL_CENTER_WAITING: No response header\n')['waiting_question'])

    def test_handled_markers_require_a_response_section(self):
        for section, expected in (('user', False), ('exec', False), ('codex', True)):
            for mode in ('similar', 'always'):
                with self.subTest(section=section, mode=mode):
                    key, process = self.fake_agent()
                    if mode == 'similar':
                        state = manager.agent_similar_decisions[key]
                        state.automatic_question = 'Question'
                        marker = manager._similar_handled_marker
                    else:
                        state = manager.agent_always_decisions[key]
                        state.automatic_attempt = True
                        marker = manager._always_handled_marker
                    transcript = f'{section}\nInternal control-center instruction:\n{marker}\n'
                    process.stdout = io.StringIO(transcript)
                    manager._read_output(key, process)
                    self.assertEqual(state.handled, expected)
                    self.assertEqual(manager.agent_outputs[key], transcript.splitlines())

    def test_stale_response_cannot_set_any_control_state(self):
        key, old = self.fake_agent(session=False)
        manager.agent_similar_decisions[key].automatic_question = 'Question'
        manager.agent_always_decisions[key].automatic_attempt = True
        old.stdout = io.StringIO(f'session id: {uuid4()}\ncodex\nCONTROL_CENTER_WAITING: Stale\n'
                                'CONTROL_CENTER_SIMILAR_HANDLED\nCONTROL_CENTER_ALWAYS_HANDLED\n')
        with patch.dict(manager.agents, {key: object()}):
            manager._read_output(key, old)
        self.assertIsNone(manager.agent_sessions[key])
        self.assertIsNone(manager.agent_waiting_questions[key])
        self.assertFalse(manager.agent_similar_decisions[key].handled)
        self.assertFalse(manager.agent_always_decisions[key].handled)

    def test_new_reader_does_not_inherit_response_section(self):
        key, process = self.fake_agent()
        process.stdout = io.StringIO('codex\nCompleted.\n')
        manager._read_output(key, process)
        process.stdout = io.StringIO('CONTROL_CENTER_WAITING: Unframed next process\n')
        manager._read_output(key, process)
        self.assertIsNone(manager.agent_waiting_questions[key])

    def test_empty_response_marker_does_not_create_wait(self):
        detail = self.read('codex\nCONTROL_CENTER_WAITING:   \n')
        self.assertEqual(detail['status'], 'finished')
        self.assertIsNone(detail['waiting_question'])
