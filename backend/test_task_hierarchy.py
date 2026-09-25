import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager
from app.persistence import AgentStore
import test_task_lifecycle


class TaskHierarchyTests(unittest.TestCase):
    setUp = test_task_lifecycle.TaskLifecycleTests.setUp
    tearDown = test_task_lifecycle.TaskLifecycleTests.tearDown
    create = test_task_lifecycle.TaskLifecycleTests.create
    replacement = test_task_lifecycle.TaskLifecycleTests.replacement
    output = test_task_lifecycle.TaskLifecycleTests.output
    request = test_task_lifecycle.TaskLifecycleTests.request
    start_request = test_task_lifecycle.TaskLifecycleTests.start_request
    task_for = test_task_lifecycle.TaskLifecycleTests.task_for

    def test_hierarchy_helpers_constraints_and_cycles(self):
        store = manager._store
        root = store.create_task('root')
        child = store.create_task('child', parent_task_id=root['task_id'])
        self.assertIsNone(root['parent_task_id'])
        self.assertEqual(store.get_task_parent(child['task_id']), root)
        self.assertEqual(store.get_task_children(root['task_id']), [child])
        with self.assertRaises(LookupError):
            store.create_task('missing', parent_task_id='missing')
        with patch('app.persistence.uuid4', return_value=root['task_id']), self.assertRaises(ValueError):
            store.create_task('self', parent_task_id=root['task_id'])
        with store._connection() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE tasks SET parent_task_id=task_id WHERE task_id=?', (root['task_id'],))
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (child['task_id'], root['task_id']))
        self.assertEqual(store.get_task_descendants(root['task_id']), [child])
        with self.assertRaises(ValueError):
            store.create_task('under corrupt cycle', parent_task_id=child['task_id'])

    def test_deep_iterative_traversal(self):
        store = manager._store
        with store._connection() as db:
            for n in range(1500):
                db.execute("INSERT INTO tasks(task_id,title,description,status,parent_task_id) VALUES (?,?,?,'pending',?)",
                           (str(n), 'work', 'work', str(n - 1) if n else None))
        result = store.get_task_descendants('0')
        self.assertEqual(len(result), 1499)
        self.assertEqual(len({t['task_id'] for t in result}), 1499)

    def test_child_context_identity_and_independent_lifecycle(self):
        parent, _ = self.create(task='Parent goal', kind='mock')
        child, process = self.create(parent=parent, task='Child contribution', sandbox='workspace-write')
        parent_task, child_task = self.task_for(parent), self.task_for(child)
        self.assertEqual(child_task['parent_task_id'], parent_task['task_id'])
        self.assertEqual(manager.agent_parents[child], parent)
        self.assertEqual(len({parent, child, parent_task['task_id'], child_task['task_id']}), 4)
        self.assertEqual(child_task['description'], 'Child contribution')
        self.assertEqual(manager.agent_tasks[child], 'Child contribution')
        self.assertIn('Parent work:\nParent goal', process.stdin.saved)
        self.assertIn('Your assigned contribution:\nChild contribution', process.stdin.saved)
        self.assertIn('CONTROL_CENTER_WAITING:', process.stdin.saved)
        self.assertEqual(manager.agent_sandboxes[child], 'workspace-write')
        self.output(child, 'CONTROL_CENTER_WAITING: Question', True)
        self.assertEqual(self.task_for(parent)['status'], 'in_progress')
        self.assertEqual(self.task_for(child)['status'], 'waiting')

    def test_restart_subtask_with_and_without_active_parent(self):
        parent, _ = self.create(kind='mock')
        child, _ = self.create(parent=parent, kind='mock')
        task = self.task_for(child)
        manager.stop_agent(child)
        first = self.start_request(f"/tasks/{task['task_id']}/start-agent", {})['agent_id']
        self.assertEqual(manager.agent_parents[first], parent)
        manager.stop_branch(parent)
        second = self.start_request(f"/tasks/{task['task_id']}/start-agent", {})['agent_id']
        self.assertIsNone(manager.agent_parents[second])
        self.assertEqual(self.task_for(second)['parent_task_id'], task['parent_task_id'])
        self.assertEqual(len(manager._store.list_task_assignments(task['task_id'])), 3)

    def test_child_spawn_failure_keeps_pending_subtask_and_parent(self):
        parent, _ = self.create(kind='mock')
        before = self.task_for(parent)
        with patch.object(manager, '_spawn_process', side_effect=OSError('spawn failed')):
            self.request(f'/agents/{parent}/children/start', {'task': 'Child'}, 503)
        child, = manager._store.get_task_children(before['task_id'])
        self.assertEqual(child['status'], 'pending')
        self.assertEqual(manager._store.list_task_assignments(child['task_id']), [])
        self.assertEqual(self.task_for(parent), before)

    def test_completion_and_stale_reader_do_not_roll_up_or_reparent(self):
        parent, _ = self.create(kind='mock')
        child, old = self.create(parent=parent, kind='mock')
        sibling, _ = self.create(parent=parent, kind='mock')
        child_task = self.task_for(child)
        self.output(parent, 'Parent done', True)
        self.assertEqual(self.task_for(child)['status'], 'in_progress')
        manager.stop_agent(child)
        replacement = self.start_request(f"/tasks/{child_task['task_id']}/start-agent", {})['agent_id']
        before = manager._store.list_tasks()
        manager._finalize_process(child, old)
        self.assertEqual(manager._store.list_tasks(), before)
        self.output(replacement, 'Child done', True)
        self.assertEqual(self.task_for(sibling)['status'], 'in_progress')
        self.assertEqual(self.task_for(replacement)['parent_task_id'], child_task['parent_task_id'])

    def test_child_commit_failure_compensates_without_changing_parent(self):
        parent, _ = self.create(kind='mock')
        before = self.task_for(parent)
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        save = manager._store.save_agent
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError('write failure')
            return save(*args, **kwargs)
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread, \
                patch.object(manager._store, 'save_agent', side_effect=fail_once), self.assertLogs(manager.logger):
            thread.return_value.is_alive.return_value = False
            self.request(f'/agents/{parent}/children/start', {'task': 'Child'}, 503)
        child, = manager._store.get_task_children(before['task_id'])
        self.assertEqual(child['status'], 'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_task(child['task_id']))
        self.assertEqual(self.task_for(parent), before)
        process.terminate.assert_called_once()

    def test_v5_migration_roots_preservation_idempotence_and_rollback(self):
        parent, _ = self.create(kind='mock')
        child, _ = self.create(parent=parent, kind='mock')
        manager.rename_agent(child, 'Child name')
        manager.set_agent_color(child, 'blue')
        self.output(child, 'Raw output')
        store = manager._store
        before = store.load_agents()
        history = store.name_history(child)
        with store._connection() as db:
            db.execute('DROP INDEX task_parent')
            db.execute('ALTER TABLE tasks DROP COLUMN parent_task_id')
            db.execute('PRAGMA user_version=5')
        original = AgentStore._migrate_task_hierarchy
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('migration failed')
        with patch.object(AgentStore, '_migrate_task_hierarchy', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 5)
            self.assertNotIn('parent_task_id', {r['name'] for r in db.execute('PRAGMA table_info(tasks)')})
        restored = AgentStore(self.path)
        self.assertTrue(all(t['parent_task_id'] is None for t in restored.list_tasks()))
        self.assertEqual(restored.load_agents(), before)
        self.assertEqual(restored.name_history(child), history)
        self.assertEqual(manager.agent_parents[child], parent)
        self.assertEqual(AgentStore(self.path).list_tasks(), restored.list_tasks())


if __name__ == '__main__':
    unittest.main()
