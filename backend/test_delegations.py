from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID

from app import agent_manager as manager, delegations
from app.persistence import AgentStore
import test_tasks
import test_dependencies


class DelegationTests(unittest.TestCase):
    agent = test_tasks.TaskTests.agent
    request = test_dependencies.DependencyTests.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'delegations.sqlite3'
        self.store = AgentStore(self.path)
        root = Path(self.temp.name) / 'project'
        root.mkdir()
        self.project = self.store.create_project('Project', root)
        self.parent = self.task()
        self.origin = self.worker(self.parent)

    def task(self, parent=None, project=None):
        return self.store.create_task('Work', parent_task_id=parent['task_id'] if parent else None,
                                      project_id=(project or self.project)['project_id'])

    def worker(self, task):
        key = self.agent('running')
        return self.store.create_assignment(task['task_id'], key)

    def create(self, task=None, origin=None, **options):
        task, origin = task or self.parent, origin or self.origin
        args = dict(project_id=task['project_id'], requested_by_agent_id=origin['agent_id'],
                    requested_by_assignment_id=origin['assignment_id'], request_key='part-1',
                    instruction='Implement the assigned part.\nKeep existing behavior. שלום')
        args.update(options)
        return self.store.create_delegation(task['task_id'], **args)

    def snapshot(self, exclude=()):
        with self.store._connection() as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'") if r[0] not in exclude]
            return {t: [tuple(r) for r in db.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid')] for t in tables}

    def test_root_request_uuid_and_complete_provenance(self):
        row = self.create()
        UUID(row['delegation_id'])
        self.assertEqual(row['status'], 'requested')
        self.assertEqual(row['parent_task_id'], self.parent['task_id'])
        self.assertEqual(row['project_id'], self.project['project_id'])
        self.assertEqual(row['requested_by_agent_id'], self.origin['agent_id'])
        self.assertEqual(row['requested_by_assignment_id'], self.origin['assignment_id'])
        self.assertIsNone(row['child_task_id'])
        self.assertIsNone(row['materialized_at'])
        self.assertIsNone(row['closed_at'])
        self.assertTrue(row['created_at'].endswith('Z'))

    def test_child_can_delegate_to_grandchild_without_hierarchy_changes(self):
        child = self.task(self.parent)
        grandchild = self.task(child)
        source = self.worker(child)
        parent_request = self.create()
        child_request = self.create(child, source)
        self.store.attach_delegation_child(parent_request['delegation_id'], child['task_id'])
        self.store.attach_delegation_child(child_request['delegation_id'], grandchild['task_id'])
        self.assertEqual(self.store.get_task(grandchild['task_id'])['parent_task_id'], child['task_id'])
        self.assertEqual(self.store.list_delegations(child['task_id'])[0]['child_task_id'], grandchild['task_id'])

    def test_duplicate_request_is_identical_and_changed_instruction_conflicts(self):
        row = self.create()
        self.assertEqual(self.create(), row)
        with self.assertRaisesRegex(ValueError, 'different delegation instruction'):
            self.create(instruction='Different work')
        self.assertEqual(self.store.list_delegations(self.parent['task_id']), [row])

    def test_replacement_retry_preserves_original_origin_and_terminal_record(self):
        row = self.create()
        row = self.store.transition_delegation(row['delegation_id'], 'canceled')
        self.store.end_assignment(self.origin['assignment_id'], 'stopped')
        replacement = self.worker(self.parent)
        self.assertEqual(self.create(origin=replacement), row)
        self.assertEqual(self.store.get_delegation(row['delegation_id']), row)
        with self.assertRaisesRegex(ValueError, 'current active assignment'):
            self.create()  # Old Worker cannot submit even a duplicate.

    def test_same_key_different_parents_and_projects(self):
        first = self.create()
        other = self.task()
        second = self.create(other, self.worker(other))
        root = Path(self.temp.name) / 'other-project'
        root.mkdir()
        project = self.store.create_project('Other', root)
        foreign = self.task(project=project)
        third = self.create(foreign, self.worker(foreign))
        self.assertEqual(len({r['delegation_id'] for r in (first, second, third)}), 3)

    def test_invalid_parent_and_project(self):
        with self.assertRaises(LookupError):
            self.create(dict(self.parent, task_id='missing'))
        for project in ('missing', None):
            with self.subTest(project=project), self.assertRaises(ValueError):
                self.create(project_id=project)
        self.assertEqual(self.store.list_delegations(self.parent['task_id']), [])

    def test_assignment_and_agent_must_match_parent(self):
        other = self.task()
        other_origin = self.worker(other)
        for args in (dict(requested_by_assignment_id='missing'),
                     dict(requested_by_assignment_id=other_origin['assignment_id']),
                     dict(requested_by_agent_id=other_origin['agent_id'])):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.create(**args)

    def test_ended_or_inactive_origin_is_rejected(self):
        with self.store._connection() as db:
            db.execute("UPDATE agents SET status='stopped' WHERE agent_id=?", (self.origin['agent_id'],))
        with self.assertRaises(ValueError):
            self.create()
        self.store.end_assignment(self.origin['assignment_id'], 'stopped')
        with self.assertRaises(ValueError):
            self.create()

    def test_waiting_assignment_can_request_but_control_gates_remain_authoritative(self):
        with self.store._connection() as db:
            db.execute("UPDATE agents SET status='waiting' WHERE agent_id=?", (self.origin['agent_id'],))
            db.execute("UPDATE tasks SET status='waiting' WHERE task_id=?", (self.parent['task_id'],))
        self.create()
        self.store.request_work_control(self.parent['task_id'], 'paused')
        with self.assertRaises(ValueError):
            self.create(request_key='another')

    def test_input_bounds_and_exact_key_identity(self):
        for key in ('', ' ', ' key', 'key ', 'a\nkey', 'a'*129, None):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.create(request_key=key)
        for text in ('', ' \n ', 'a'*32769, 'a\x00b', None):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.create(instruction=text)
        self.assertNotEqual(self.create(request_key='Key')['delegation_id'], self.create(request_key='key')['delegation_id'])

    def test_attachment_is_idempotent_and_does_not_create_anything(self):
        child = self.task(self.parent)
        row = self.create()
        before = self.snapshot(exclude=('delegations',))
        attached = self.store.attach_delegation_child(row['delegation_id'], child['task_id'])
        self.assertEqual(attached['status'], 'materialized')
        self.assertIsNotNone(attached['materialized_at'])
        self.assertEqual(attached, self.store.attach_delegation_child(row['delegation_id'], child['task_id']))
        self.store.transition_delegation(row['delegation_id'], 'canceled')
        self.assertEqual(self.snapshot(exclude=('delegations',)), before)

    def test_cross_project_wrong_parent_and_self_attachment_rejected(self):
        root = Path(self.temp.name) / 'foreign'
        root.mkdir()
        foreign = self.task(project=self.store.create_project('Foreign', root))
        unrelated = self.task()
        wrong_child = self.task(unrelated)
        row = self.create()
        for child in (foreign, wrong_child, self.parent):
            with self.subTest(child=child['task_id']), self.assertRaises(ValueError):
                self.store.attach_delegation_child(row['delegation_id'], child['task_id'])
        with self.assertRaises(LookupError):
            self.store.attach_delegation_child(row['delegation_id'], 'missing')
        self.assertEqual(self.store.get_delegation(row['delegation_id']), row)

    def test_child_unique_even_after_cancellation_and_one_child_per_delegation(self):
        child, second_child = self.task(self.parent), self.task(self.parent)
        one, two = self.create(), self.create(request_key='two')
        self.store.attach_delegation_child(one['delegation_id'], child['task_id'])
        with self.assertRaises(ValueError):
            self.store.attach_delegation_child(one['delegation_id'], second_child['task_id'])
        self.store.transition_delegation(one['delegation_id'], 'canceled')
        with self.assertRaises(ValueError):
            self.store.attach_delegation_child(two['delegation_id'], child['task_id'])
        with self.assertRaises(ValueError):
            self.store.attach_delegation_child(one['delegation_id'], child['task_id'])

    def test_lifecycle_rejects_backward_and_reserved_result_transitions(self):
        row = self.create()
        for status in ('requested', 'materialized', 'result_ready', 'acknowledged', 'finished', 'invalid'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.transition_delegation(row['delegation_id'], status)
        canceled = self.store.transition_delegation(row['delegation_id'], 'canceled')
        self.assertIsNotNone(canceled['closed_at'])
        self.assertEqual(self.store.transition_delegation(row['delegation_id'], 'canceled'), canceled)
        with self.assertRaises(ValueError):
            self.store.attach_delegation_child(row['delegation_id'], self.task(self.parent)['task_id'])

    def test_child_completion_does_not_infer_result_delivery(self):
        child = self.task(self.parent)
        source = self.worker(child)
        row = self.store.attach_delegation_child(self.create()['delegation_id'], child['task_id'])
        self.store.end_assignment(source['assignment_id'], 'completed')
        self.assertEqual(self.store.get_delegation(row['delegation_id']), row)

    def test_restart_preserves_all_states_without_recovery_actions(self):
        self.create()
        linked = self.create(request_key='linked')
        self.store.attach_delegation_child(linked['delegation_id'], self.task(self.parent)['task_id'])
        self.store.transition_delegation(self.create(request_key='canceled')['delegation_id'], 'canceled')
        before = self.store.list_delegations(self.parent['task_id'])
        reopened = AgentStore(self.path)
        reopened.reconcile_task_recovery()
        self.assertEqual(reopened.list_delegations(self.parent['task_id']), before)
        self.assertEqual(len(reopened.list_tasks()), 2)
        self.assertTrue(all(reopened.get_task_workspace(t['task_id']) is None for t in reopened.list_tasks()))

    def test_get_endpoints_are_read_only_and_missing_records_are_404(self):
        row = self.create()
        before = self.snapshot()
        with patch.object(manager, '_task_store', return_value=self.store):
            self.assertEqual(self.request('/delegations/' + row['delegation_id']), row)
            self.assertEqual(self.request('/tasks/' + self.parent['task_id'] + '/delegations'), {'delegations': [row]})
            self.request('/delegations/missing', expected=404)
            self.request('/tasks/missing/delegations', expected=404)
        self.assertEqual(self.snapshot(), before)

    def test_request_does_not_mutate_tasks_dependencies_intents_or_workspaces(self):
        before = self.snapshot(exclude=('delegations',))
        self.create()
        self.assertEqual(self.snapshot(exclude=('delegations',)), before)

    def test_concurrent_duplicate_creation_uses_database_identity(self):
        stores = [AgentStore(self.path), AgentStore(self.path)]
        barrier = Barrier(2)
        def create(store):
            barrier.wait()
            return store.create_delegation(self.parent['task_id'], project_id=self.project['project_id'],
                requested_by_agent_id=self.origin['agent_id'], requested_by_assignment_id=self.origin['assignment_id'],
                request_key='race', instruction='Same request')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, stores))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.store.list_delegations(self.parent['task_id'])), 1)

    def test_concurrent_child_attachment_has_one_winner(self):
        one, two = self.create(), self.create(request_key='two')
        child = self.task(self.parent)
        stores = [AgentStore(self.path), AgentStore(self.path)]
        barrier = Barrier(2)
        def attach(pair):
            store, row = pair
            barrier.wait()
            try:
                return store.attach_delegation_child(row['delegation_id'], child['task_id'])
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(attach, zip(stores, (one, two))))
        self.assertEqual(sum(r is not None for r in result), 1)

    def test_database_uniqueness_and_shape_constraints(self):
        row = self.create()
        other = self.create(request_key='other')
        for sql, args in (
            ('UPDATE delegations SET request_key=? WHERE delegation_id=?', (row['request_key'], other['delegation_id'])),
            ("UPDATE delegations SET status='materialized' WHERE delegation_id=?", (row['delegation_id'],)),
            ("UPDATE delegations SET status='canceled' WHERE delegation_id=?", (row['delegation_id'],)),
            ('UPDATE delegations SET requested_by_assignment_id=? WHERE delegation_id=?', ('missing', row['delegation_id'])),
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), self.store._connection() as db:
                db.execute(sql, args)

    def test_failed_mutation_rolls_back(self):
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_delegation AFTER INSERT ON delegations BEGIN SELECT RAISE(ABORT,'test'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.Error):
            self.create()
        self.assertEqual(self.snapshot(), before)

    def test_failed_attachment_preserves_requested_state_and_timestamps(self):
        row = self.create()
        child = self.task(self.parent)
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_link AFTER UPDATE ON delegations BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.Error):
            self.store.attach_delegation_child(row['delegation_id'], child['task_id'])
        self.assertEqual(self.store.get_delegation(row['delegation_id']), row)

    def downgrade(self):
        with self.store._connection() as db:
            db.execute('DROP TABLE delegations')
            db.execute('PRAGMA user_version=17')

    def test_migration_preserves_all_historical_rows_without_fabrication(self):
        self.store.create_work_intent(self.parent['task_id'], namespace='component', key='auth')
        self.downgrade()
        before = self.snapshot()
        AgentStore(self.path)
        self.assertEqual(self.snapshot(exclude=('delegations',)), before)
        self.assertEqual(self.store.list_delegations(self.parent['task_id']), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 18)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            delegations.migrate(db)
        self.assertEqual(self.snapshot(exclude=('delegations',)), before)

    def test_migration_rollback_and_future_schema_rejection(self):
        self.downgrade()
        before = self.snapshot()
        original = delegations.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('interrupted migration')
        with patch.object(delegations, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        self.assertEqual(self.snapshot(), before)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 17)
            db.execute('PRAGMA user_version=19')
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 19)


if __name__ == '__main__':
    unittest.main()
