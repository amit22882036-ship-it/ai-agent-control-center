from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch
from typing import get_args
from pydantic import ValidationError
from fastapi import HTTPException
from app import agent_manager as manager
from app.agent_names import DisplayColor
from app.persistence import AgentStore
from app.main import AgentColorRequest, agent_color_route, agent_name_history_route
import test_persistence


class AgentIdentityTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    recover = test_persistence.PersistenceTests.recover
    output = test_persistence.PersistenceTests.output
    resume = test_persistence.PersistenceTests.resume

    def history(self, key):
        return agent_name_history_route(key)['history']

    def test_initial_names_renames_unicode_order_and_recovery(self):
        key, _ = self.create(task='Initial name')
        self.output(key, f'session id: {key}\nexisting output\n', finish=True)
        before = manager.get_agent(key)
        self.assertEqual([x['name'] for x in self.history(key)], ['Initial name'])
        for name in ['שלום 🐍', 'Final name', ' Final   name ']:
            manager.rename_agent(key, name)
        history = self.history(key)
        self.assertEqual([x['name'] for x in history], ['Final name', 'שלום 🐍', 'Initial name'])
        self.assertTrue(all(x['changed_at'].endswith('Z') for x in history))
        self.recover()
        self.assertEqual(self.history(key), history)
        self.assertEqual(manager.get_agent(key), {**before, 'display_name': 'Final name'})

    def test_palette_defaults_persistence_and_independent_child(self):
        key, _ = self.create(sandbox='workspace-write')
        self.output(key, f'session id: {key}\n', finish=True)
        before = manager.get_agent(key)
        history = self.history(key)
        self.assertEqual(before['display_color'], 'neutral')
        for color in get_args(DisplayColor):
            agent_color_route(key, AgentColorRequest(display_color=color))
            self.assertEqual(manager._store.load_agents()[0]['display_color'], color)
        child, _ = self.create(parent=key, kind='mock')
        self.assertEqual(manager.get_agent(child)['display_color'], 'neutral')
        self.assertEqual(manager.get_agents()[0]['display_color'], 'pink')
        self.recover()
        self.assertEqual(manager.get_agent(key), {**before, 'display_color': 'pink', 'child_ids': [child]})
        self.assertEqual(self.history(key), history)

    def test_invalid_palette_and_missing_agents(self):
        key, _ = self.create()
        for color in ['#fff', 'rgb(0,0,0)', 'url(x)', 'BLUE', '']:
            with self.assertRaises(ValidationError):
                AgentColorRequest(display_color=color)
            with self.assertRaises(ValueError):
                manager.set_agent_color(key, color)
        for action in [lambda: agent_color_route('missing', AgentColorRequest(display_color='blue')),
                       lambda: agent_name_history_route('missing')]:
            with self.assertRaises(HTTPException) as error:
                action()
            self.assertEqual(error.exception.status_code, 404)
        self.assertEqual(manager._store.load_agents()[0]['display_color'], 'neutral')

    def test_history_failure_rolls_back_name_and_prevents_publication(self):
        key, _ = self.create()
        before = self.history(key)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TRIGGER reject_name BEFORE INSERT ON agent_name_history
                          BEGIN SELECT RAISE(ABORT, 'history unavailable'); END""")
        with patch.object(manager, '_emit_agent_change') as emit, self.assertLogs(manager.logger):
            with self.assertRaises(RuntimeError):
                manager.rename_agent(key, 'New name')
            emit.assert_not_called()
        self.assertEqual(manager.agent_names[key], 'Task')
        self.assertEqual(manager._store.load_agents()[0]['display_name'], 'Task')
        self.assertEqual(self.history(key), before)

    def test_color_failure_rolls_back_and_success_publishes_after_commit(self):
        key, _ = self.create()
        with patch.object(manager._store, 'save_agent', side_effect=sqlite3.OperationalError('offline')), patch.object(manager, '_emit_agent_change') as emit, self.assertLogs(manager.logger):
            with self.assertRaises(RuntimeError):
                manager.set_agent_color(key, 'blue')
            emit.assert_not_called()
        self.assertEqual(manager.agent_colors[key], 'neutral')
        def assert_committed(agent_id):
            self.assertEqual(agent_id, key)
            self.assertEqual(manager._store.load_agents()[0]['display_color'], 'blue')
            self.assertEqual(self.history(key)[0]['name'], manager.agent_names[key])
        with patch.object(manager, '_emit_agent_change', side_effect=assert_committed):
            manager.set_agent_color(key, 'blue')
            manager.rename_agent(key, 'Committed')

    def test_v2_migration_backfills_current_name_and_is_idempotent(self):
        key, _ = self.create(task='Original', sandbox='workspace-write')
        child, _ = self.create(parent=key)
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?\n', finish=True)
        manager.rename_agent(key, 'שם נוכחי')
        before = manager._store.load_agents()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE agents DROP COLUMN display_color')
            db.execute('DROP TABLE agent_name_history')
            db.execute('PRAGMA user_version=2')
        store = AgentStore(self.path)
        self.assertEqual(store.load_agents(), before)
        self.assertEqual([x['name'] for x in store.name_history(key)], ['שם נוכחי'])
        self.assertEqual(len(store.name_history(child)), 1)
        history = store.name_history(key)
        self.assertEqual(AgentStore(self.path).name_history(key), history)
        self.assertEqual(AgentStore(self.path).load_agents(), before)

    def test_reply_and_redirect_keep_color_and_name_history(self):
        key, _ = self.create(sandbox='workspace-write')
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?\n', finish=True)
        manager.set_agent_color(key, 'cyan')
        manager.rename_agent(key, 'Review team')
        history = self.history(key)
        self.resume(key, lambda agent_id: manager.reply_agent(agent_id, 'Proceed'))
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(key, lambda agent_id: manager.redirect_agent(agent_id, 'Focus on tests'))
        self.assertEqual(self.history(key), history)
        detail = manager.get_agent(key)
        self.assertEqual(detail['display_color'], 'cyan')
        self.assertEqual(detail['session_id'], key)
        self.assertEqual(detail['sandbox'], 'workspace-write')
        self.assertEqual(detail['display_name'], 'Review team')

    def test_storage_rejects_arbitrary_css_without_changing_records(self):
        key, _ = self.create()
        before = manager._store.load_agents()[0]
        with self.assertRaises(ValueError):
            manager._store.save_agent({**before, 'display_color': '#ff0000'})
        with closing(sqlite3.connect(self.path)) as db, db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE agents SET display_color=? WHERE agent_id=?', ('url(x)', key))
        self.assertEqual(manager._store.load_agents()[0], before)
