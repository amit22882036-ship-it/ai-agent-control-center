from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from pydantic import ValidationError
from fastapi import HTTPException
from app import agent_manager as manager
from app.agent_names import default_display_name
from app.persistence import AgentStore
from app.main import RenameAgentRequest, rename_agent_route
import test_persistence


class AgentNameTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    recover = test_persistence.PersistenceTests.recover
    output = test_persistence.PersistenceTests.output

    def test_names_are_deterministic_bounded_and_unicode_safe(self):
        self.assertEqual(default_display_name('  Review   API\nbehavior '), 'Review API behavior')
        self.assertEqual(default_display_name('   '), 'Untitled agent')
        self.assertLessEqual(len(default_display_name('a' * 100)), 56)
        self.assertEqual(default_display_name('שלום'), 'שלום')

    def test_explicit_v1_migration_preserves_metadata_and_output(self):
        parent, _ = self.create(task='Review authentication', sandbox='workspace-write')
        child, _ = self.create(parent=parent)
        self.output(parent, f'session id: {parent}\nCONTROL_CENTER_WAITING: Which database?\n', finish=True)
        manager.agent_similar_decisions[parent].enabled = True
        manager.agent_similar_decisions[parent].examples = ['Which database?']
        manager.agent_always_decisions[parent].configured = True
        manager._save_agent(parent)
        before = manager._store.load_agents()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE agents DROP COLUMN display_name')
            db.execute('ALTER TABLE agents DROP COLUMN display_color')
            db.execute('DROP TABLE agent_name_history')
            db.execute('DROP TABLE task_assignments')
            db.execute('DROP TABLE tasks')
            db.execute('PRAGMA user_version=1')
        migrated = AgentStore(self.path).load_agents()
        self.assertEqual(migrated, before)
        self.assertEqual(AgentStore(self.path).load_agents(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
        self.assertEqual(migrated[1]['parent_id'], parent)
        self.assertEqual(migrated[1]['agent_id'], child)

    def test_rename_persists_without_changing_identity_task_or_history(self):
        key, _ = self.create(task='Original assignment')
        self.output(key, 'existing output\n', finish=True)
        before = manager.get_agent(key)
        response = rename_agent_route(key, RenameAgentRequest(display_name='  Backend Engineer  '))
        self.assertEqual(response, {'agent_id': key, 'display_name': 'Backend Engineer'})
        self.recover()
        after = manager.get_agent(key)
        self.assertEqual(after, {**before, 'display_name': 'Backend Engineer'})
        self.assertEqual(manager.get_agents()[0]['display_name'], 'Backend Engineer')

    def test_rename_validation_and_missing_agent(self):
        for value in ('', '   ', 'a' * 81):
            with self.assertRaises(ValidationError):
                RenameAgentRequest(display_name=value)
        with self.assertRaises(HTTPException) as error:
            rename_agent_route('missing', RenameAgentRequest(display_name='Name'))
        self.assertEqual(error.exception.status_code, 404)

    def test_failed_rename_keeps_old_name_and_does_not_publish(self):
        key, _ = self.create()
        old = manager.agent_names[key]
        with patch.object(manager._store, 'save_agent', side_effect=sqlite3.OperationalError('offline')), patch.object(manager, '_emit_agent_change') as emit, self.assertLogs(manager.logger):
            with self.assertRaises(HTTPException) as error:
                rename_agent_route(key, RenameAgentRequest(display_name='New name'))
            self.assertEqual(error.exception.status_code, 503)
            emit.assert_not_called()
        self.assertEqual(manager.agent_names[key], old)
