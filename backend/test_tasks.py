from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from app.persistence import AgentStore
from app.task_domain import task_title, TASK_TITLE_LIMIT
from app import agent_manager as manager


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tasks.sqlite3'
        self.store = AgentStore(self.path)

    def agent(self, status='stopped', parent_id=None):
        key = str(uuid4())
        self.store.save_agent(dict(agent_id=key, parent_id=parent_id, task='  שלום 🐍  work\nsecond line',
            agent_type='codex', sandbox='workspace-write', status=status, session_id=str(uuid4()),
            waiting_question='Question?', similar_decisions_enabled=True, similar_examples=['Example'],
            always_decide_enabled=True, always_decide_configured=True,
            display_name='Worker name', display_color='blue'), ['first', 'שלום', 'last'])
        return key

    def legacy(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE task_assignments')
            db.execute('DROP TABLE tasks')
            db.execute('PRAGMA user_version=3')

    def test_task_creation_validation_unicode_and_round_trip(self):
        task = self.store.create_task('תיאור 🐍', title='כותרת 🐍')
        UUID(task['task_id'])
        self.assertEqual(task['status'], 'pending')
        self.assertTrue(task['created_at'].endswith('Z'))
        self.assertEqual(AgentStore(self.path).get_task(task['task_id']), task)
        self.assertEqual(self.store.list_tasks(), [task])
        self.assertIsNone(self.store.get_task('missing'))
        for status in ['pending', 'completed', 'canceled']:
            self.assertEqual(self.store.create_task('work', status=status)['status'], status)
        for status in ['in_progress', 'waiting']:
            with self.assertRaisesRegex(ValueError, 'active assignment'):
                self.store.create_task('work', status=status)
        with self.assertRaises(ValueError):
            self.store.create_task('work', status='failed')
        with self.store._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE tasks SET status='failed'")

    def test_assignment_identity_uniqueness_and_durable_history(self):
        agent = self.agent()
        other = self.agent()
        task = self.store.create_task('Work')['task_id']
        second = self.store.create_task('Other')['task_id']
        assignment = self.store.create_assignment(task, agent)
        UUID(assignment['assignment_id'])
        self.assertNotEqual(task, agent)
        self.assertEqual(self.store.get_active_assignment_for_task(task), assignment)
        self.assertEqual(self.store.get_active_assignment_for_agent(agent), assignment)
        for pair in [(task, other), (second, agent)]:
            with self.assertRaises(ValueError):
                self.store.create_assignment(*pair)
            with self.store._connection() as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id) VALUES (?,?,?)',
                               (str(uuid4()), *pair))
        with self.assertRaises(ValueError):
            self.store.end_assignment(assignment['assignment_id'], 'failed')
        ended = self.store.end_assignment(assignment['assignment_id'], 'reassigned')
        self.assertTrue(ended['ended_at'].endswith('Z'))
        self.assertEqual(ended['ended_reason'], 'reassigned')
        self.assertEqual(self.store.end_assignment(assignment['assignment_id'], 'stopped'), ended)
        replacement = self.store.create_assignment(task, other)
        self.assertEqual(AgentStore(self.path).list_task_assignments(task), [ended, replacement])
        self.assertIsNone(self.store.get_active_assignment_for_agent(agent))
        self.store.create_assignment(second, agent)

    def test_assignment_reference_and_end_constraints(self):
        task = self.store.create_task('work')['task_id']
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_assignment(task, 'missing')
        self.assertEqual(self.store.list_task_assignments(task), [])
        assignment = self.store.create_assignment(task, self.agent())
        with self.store._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE task_assignments SET ended_reason='stopped' WHERE assignment_id=?", (assignment['assignment_id'],))

    def test_title_derivation(self):
        self.assertEqual(task_title('\n  שלום   🐍 \nOther work'), 'שלום 🐍')
        self.assertEqual(task_title('  \n\t'), 'Untitled task')
        self.assertEqual(task_title('🐍' * 200), '🐍' * TASK_TITLE_LIMIT)

    def test_v3_migration_preserves_records_and_maps_all_statuses(self):
        keys = {}
        for status in ['running', 'waiting', 'finished', 'stopped']:
            keys[status] = self.agent(status, keys.get('running'))
        before = self.store.load_agents()
        histories = {key: self.store.name_history(key) for key in keys.values()}
        self.legacy()
        store = AgentStore(self.path)
        self.assertEqual(store.load_agents(), before)
        self.assertEqual(len(store.list_tasks()), 4)
        for record, task in zip(before, store.list_tasks()):
            UUID(task['task_id'])
            self.assertNotEqual(task['task_id'], record['agent_id'])
            self.assertEqual(task['description'], record['task'])
            self.assertEqual(task['title'], 'שלום 🐍 work')
            self.assertEqual(store.name_history(record['agent_id']), histories[record['agent_id']])
            assignment, = store.list_task_assignments(task['task_id'])
            self.assertEqual(assignment['agent_id'], record['agent_id'])
            expected = {'running': ('in_progress', None), 'waiting': ('waiting', None),
                        'finished': ('completed', 'completed'), 'stopped': ('pending', 'stopped')}
            self.assertEqual((task['status'], assignment['ended_reason']), expected[record['status']])
            self.assertEqual(assignment['ended_at'] is None, record['status'] in ['running', 'waiting'])
        self.assertEqual(AgentStore(self.path).list_tasks(), store.list_tasks())
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 14)

    def test_startup_reconciles_running_and_retains_waiting_without_processes(self):
        # Use the real startup path but restore global runtime registries afterward.
        running = self.agent('running')
        waiting = self.agent('waiting')
        self.legacy()
        store = AgentStore(self.path)
        identities = [t['task_id'] for t in store.list_tasks()]
        registries = ['agents', 'agent_parents', 'agent_statuses', 'agent_outputs', 'agent_tasks',
                      'agent_names', 'agent_colors', 'agent_types', 'agent_sandboxes', 'agent_sessions',
                      'agent_waiting_questions', 'agent_readers', 'agent_similar_decisions', 'agent_always_decisions']
        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in registries:
                stack.enter_context(patch.object(manager, name, {}))
            stack.enter_context(patch.object(manager, '_store', None))
            stack.enter_context(patch.object(manager, '_shutting_down', False))
            with patch.object(manager, '_spawn_process') as spawn:
                manager.initialize_persistence(self.path)
                self.assertEqual(manager.agent_statuses[running], 'stopped')
                self.assertEqual(manager.agent_statuses[waiting], 'waiting')
                self.assertIsNone(manager.agents[running])
                spawn.assert_not_called()
                manager.initialize_persistence(self.path)
        self.assertEqual([t['task_id'] for t in store.list_tasks()], identities)
        self.assertEqual([t['status'] for t in store.list_tasks()], ['pending', 'waiting'])
        self.assertIsNone(store.get_active_assignment_for_agent(running))
        self.assertIsNotNone(store.get_active_assignment_for_agent(waiting))
        self.assertEqual(store.list_task_assignments(identities[0])[0]['ended_reason'], 'stopped')

    def test_migration_failure_rolls_back_tables_rows_and_version(self):
        self.agent('waiting')
        self.agent('stopped')
        before = self.store.load_agents()
        self.legacy()
        with patch('app.persistence.task_title', side_effect=['First', RuntimeError('migration interrupted')]):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 3)
            self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name IN ('tasks','task_assignments')").fetchall())
        self.assertEqual(AgentStore(self.path).load_agents(), before)
        self.assertEqual(len(AgentStore(self.path).list_tasks()), 2)

    def test_future_schema_is_untouched(self):
        task = self.store.create_task('keep')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA user_version=15')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertEqual(db.execute('SELECT task_id FROM tasks').fetchone()[0], task['task_id'])

    def test_recovery_failure_rolls_back_task_and_assignment_together(self):
        self.agent('running')
        self.legacy()
        store = AgentStore(self.path)
        task, = store.list_tasks()
        assignment, = store.list_task_assignments(task['task_id'])
        with store._connection() as db:
            db.execute("""CREATE TRIGGER reject_assignment_end BEFORE UPDATE ON task_assignments
                          BEGIN SELECT RAISE(ABORT, 'unavailable'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            store.reconcile_task_recovery()
        self.assertEqual(store.get_task(task['task_id']), task)
        self.assertEqual(store.list_task_assignments(task['task_id']), [assignment])
