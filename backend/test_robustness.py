import io
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from app import agent_manager as manager
from app.main import get_mock_agent
import test_persistence


class RobustnessTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output

    def test_session_metadata_cannot_be_replaced_by_user_or_model_text(self):
        key, process = self.create()
        session, fake = str(uuid4()), str(uuid4())
        transcript = f'session id: {session}\nuser\nsession id: {fake}\ncodex\nsession id: {fake}\n'
        process.stdout = io.StringIO(transcript)
        manager._read_output(key, process)
        self.assertEqual(manager.agent_sessions[key], session)
        self.assertEqual(manager._store.full_output(key), transcript.splitlines())
        other, process = self.create()
        process.stdout = io.StringIO(f'user\nsession id: {fake}\ncodex\nDone\n')
        manager._read_output(other, process)
        self.assertIsNone(manager.agent_sessions[other])

    def test_failed_automatic_cleanup_does_not_mark_live_replacement_waiting(self):
        for mode in ('similar', 'always'):
            with self.subTest(mode=mode):
                key, old = self.create()
                self.output(key, f'session id: {key}\ncodex\nCONTROL_CENTER_WAITING: Choose?\n')
                old.poll.return_value = 0
                if mode == 'similar':
                    manager.agent_similar_decisions[key].enabled = True
                    manager.agent_similar_decisions[key].examples = ['Choose?']
                else:
                    manager.agent_always_decisions[key].enabled = True
                replacement = self.replacement()
                with patch.object(manager, '_codex_command', return_value='fixed'), \
                     patch.object(manager, '_spawn_process', return_value=replacement) as spawn, \
                     patch.object(manager, 'Thread'), \
                     patch.object(replacement.stdin, 'write', side_effect=BrokenPipeError('closed')), \
                     patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('cannot confirm stop')), \
                     self.assertLogs(manager.logger, level='ERROR'):
                    manager._finalize_process(key, old)
                    self.assertIs(manager.agents[key], replacement)
                    self.assertEqual(manager.get_agent(key)['status'], 'running')
                    self.assertIsNone(manager.agent_waiting_questions[key])
                    manager._finalize_process(key, old)
                    spawn.assert_called_once()
                self.assertEqual(manager._store.load_agents()[-1]['status'], 'running')
                self.assertFalse(manager.agent_outputs[key].hold)

    def test_shutdown_reports_failed_persistence_instead_of_success(self):
        key, _ = self.create(kind='mock')
        with patch.object(manager._store, 'save_agent', side_effect=sqlite3.OperationalError('disk offline')), \
             self.assertLogs(manager.logger, level='ERROR'):
            failures = manager.shutdown_agents()
        self.assertEqual([item['agent_id'] for item in failures], [key])
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertTrue(manager.agent_outputs[key].dirty)

    def test_legacy_details_storage_failure_is_a_sanitized_503(self):
        key, _ = self.create()
        with patch.object(manager._store, 'full_output', side_effect=sqlite3.OperationalError('private database path')):
            with self.assertRaises(HTTPException) as error:
                get_mock_agent(key)
            self.assertEqual(error.exception.status_code, 503)
            self.assertNotIn('private', error.exception.detail)
            self.assertNotIn('output', get_mock_agent(key, include_output=False))
