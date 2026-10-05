import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID

from app import agent_manager as manager
from app.persistence import AgentStore
from app.project_domain import canonical_path, contains
from workspace_test_support import make_repository
import test_task_lifecycle


class ProjectTests(unittest.TestCase):
    setUp = test_task_lifecycle.TaskLifecycleTests.setUp
    tearDown = test_task_lifecycle.TaskLifecycleTests.tearDown
    create = test_task_lifecycle.TaskLifecycleTests.create
    replacement = test_task_lifecycle.TaskLifecycleTests.replacement
    output = test_task_lifecycle.TaskLifecycleTests.output
    recover = test_task_lifecycle.TaskLifecycleTests.recover
    resume = test_task_lifecycle.TaskLifecycleTests.resume
    request = test_task_lifecycle.TaskLifecycleTests.request
    start_request = test_task_lifecycle.TaskLifecycleTests.start_request
    task_for = test_task_lifecycle.TaskLifecycleTests.task_for

    def directory(self, name):
        root = Path(self.temp.name) / name
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_project_api_roundtrip_and_validation(self):
        root = self.directory('shop')
        result = self.request('/projects', {'name': ' Shop ', 'root_path': str(root)})
        UUID(result['project_id'])
        self.assertEqual(result['name'], 'Shop')
        self.assertEqual(result['root_path'], canonical_path(root)[0])
        self.assertEqual(set(result), {'project_id', 'name', 'root_path', 'created_at', 'updated_at'})
        self.assertEqual(self.request('/projects')['projects'], [result])
        self.assertEqual(self.request('/projects/' + result['project_id']), result)
        self.request('/projects/missing', expected=404)
        for name in (' ', 'x' * 101, 'line\nbreak'):
            self.request('/projects', {'name': name, 'root_path': str(root)}, 422)
        for path in ('', '\0', str(root / 'missing'), str(self.path)):
            error = self.request('/projects', {'name': 'Test', 'root_path': path}, 409)
            self.assertEqual(error['detail'], 'Project root must be a valid existing directory')
        self.request('/projects', {'name': 'Duplicate', 'root_path': str(root)}, 409)
        nested = self.directory('shop/backend')
        self.request('/projects', {'name': 'Overlap', 'root_path': str(nested)}, 409)

    def test_normalization_containment_and_nonprefix(self):
        store = manager._store
        root = self.directory('shop')
        nested = self.directory('shop/backend')
        equivalent = str(nested / '..') + os.sep
        project = store.create_project('Shop', equivalent)
        self.assertTrue(Path(project['root_path']).is_absolute())
        self.assertEqual(project['root_path'], canonical_path(root)[0])
        self.assertEqual(store.resolve_project_for_path(nested / 'future.py'), project)
        self.assertEqual(store.ensure_project_for_root(nested), project)
        self.assertEqual(store.ensure_project_for_root(root / '.'), project)
        unrelated = self.directory('shopping')
        self.assertIsNone(store.resolve_project_for_path(unrelated))
        self.assertNotEqual(store.create_project('Other', unrelated)['project_id'], project['project_id'])
        with self.assertRaises(ValueError):
            store.create_project('Ancestor', Path(self.temp.name))
        with self.assertRaises(ValueError):
            store.create_project('Descendant', nested)
        with self.assertRaises(ValueError):
            store.ensure_project_for_root(Path(self.temp.name))

    @unittest.skipUnless(os.name == 'nt', 'Windows path identity')
    def test_windows_case_slashes_trailing_separator_and_drives(self):
        root = self.directory('MixedCase')
        store = manager._store
        project = store.create_project('Windows', root)
        variant = str(root).swapcase().replace('\\', '/') + '/'
        self.assertEqual(store.ensure_project_for_root(variant), project)
        with self.assertRaises(ValueError):
            store.create_project('Duplicate', variant)
        self.assertFalse(contains(canonical_path('C:\\repos\\shop')[1], canonical_path('D:\\repos\\shop')[1]))

    def test_ensure_race_uses_database_transaction_and_unique_key(self):
        root = self.directory('race')
        stores = [AgentStore(self.path) for _ in range(4)]
        barrier = Barrier(len(stores))
        def ensure(store):
            barrier.wait()
            return store.ensure_project_for_root(root)['project_id']
        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(ensure, stores))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(manager._store.list_projects()), 1)
        with manager._store._connection() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO projects(project_id,name,root_path,root_path_key) SELECT 'duplicate',name,root_path,root_path_key FROM projects")

    def test_project_and_task_creation_rollback_together(self):
        store = manager._store
        with store._connection() as db:
            db.execute("CREATE TRIGGER fail_task BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'unavailable'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.create_task('Work', root_path=self.directory('rollback'))
        self.assertEqual(store.list_tasks(), [])
        self.assertEqual(store.list_projects(), [])

    def test_root_task_failure_does_not_spawn_or_publish(self):
        with manager._store._connection() as db:
            db.execute("CREATE TRIGGER fail_task BEFORE INSERT ON tasks BEGIN SELECT RAISE(ABORT,'unavailable'); END")
        with patch.object(manager, '_spawn_process') as spawn, patch.object(manager.changes, 'publish') as publish:
            self.request('/agents/start', {'task': 'Work'}, 503)
            spawn.assert_not_called()
            publish.assert_not_called()
        self.assertEqual(manager._store.list_projects(), [])
        self.assertEqual(manager._store.list_tasks(), [])

    def test_root_resolves_existing_project_containing_effective_cwd(self):
        root = make_repository(self.directory('host'))
        project = manager._store.create_project('Host', root)
        with patch.object(manager, '_project_root', root):
            key, _ = self.create(kind='mock')
        self.assertEqual(self.task_for(key)['project_id'], project['project_id'])
        self.assertNotEqual(manager._resume_cwd(key), root)

    def test_child_rejects_corrupt_parent_workspace_before_spawn(self):
        key, _ = self.create()
        parent = self.task_for(key)
        with manager._store._connection() as db:
            db.execute('UPDATE task_workspaces SET workspace_path=? WHERE task_id=?',
                       (str(self.directory('outside')), parent['task_id']))
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f'/agents/{key}/children/start', {'task': 'Child'}, 422)
            spawn.assert_not_called()
        self.assertEqual(self.task_for(key), parent)

    def test_root_agents_provider_independent_and_unrelated_roots(self):
        first, _ = self.create(kind='mock')
        second, _ = self.create(kind='codex', sandbox='workspace-write')
        project_id = self.task_for(first)['project_id']
        self.assertEqual(self.task_for(second)['project_id'], project_id)
        self.assertEqual(len(manager._store.list_projects()), 1)
        self.assertNotEqual(first, self.task_for(first)['task_id'])
        self.assertNotEqual(first, project_id)
        other = make_repository(self.directory('other'))
        with patch.object(manager, '_project_root', other):
            third, _ = self.create()
        self.assertNotEqual(self.task_for(third)['project_id'], project_id)
        self.assertEqual(len(manager._store.list_projects()), 2)

    def test_post_tasks_default_explicit_missing_project(self):
        default = self.request('/tasks', {'title': 'Default', 'description': 'Work'})
        self.assertIsNotNone(default['project_id'])
        other = manager._store.create_project('Other', self.directory('other'))
        task = self.request('/tasks', {'title': 'Explicit', 'description': 'Work', 'project_id': other['project_id']})
        self.assertEqual(task['project_id'], other['project_id'])
        self.request('/tasks', {'title': 'Missing', 'description': 'Work', 'project_id': 'missing'}, 404)
        self.assertEqual(len(manager._store.list_tasks()), 2)

    def test_children_inherit_exact_project_and_reject_cross_project(self):
        parent, _ = self.create()
        child, _ = self.create(parent=parent, kind='mock')
        grandchild, _ = self.create(parent=child)
        task = self.task_for(parent)
        for key in (child, grandchild):
            self.assertEqual(self.task_for(key)['project_id'], task['project_id'])
        other = manager._store.create_project('Other', self.directory('other'))
        with self.assertRaisesRegex(ValueError, 'parent Project'):
            manager._store.create_task('Wrong', parent_task_id=task['task_id'], project_id=other['project_id'])
        manager.stop_branch(parent)
        self.assertTrue(all(t['project_id'] == task['project_id'] for t in manager._store.list_tasks()))

    def test_existing_task_start_and_resume_use_project_not_current_default(self):
        root = make_repository(self.directory('assigned'))
        project = manager._store.create_project('Assigned', root)
        task = manager._store.create_task('Work', project_id=project['project_id'])
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process) as spawn, \
                patch.object(manager, '_codex_command', return_value='fixed'), patch.object(manager, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            key = manager.start_task_agent(task['task_id'], 'codex')['agent_id']
        workspace = Path(manager._store.get_task_workspace(task['task_id'])['workspace_path'])
        self.assertEqual(spawn.call_args.kwargs['cwd'], workspace)
        manager.agent_sessions[key] = '11111111-1111-1111-1111-111111111111'
        self.output(key, 'CONTROL_CENTER_WAITING: Which file?', True)
        _, _, _, popen = self.resume(key, lambda k: manager.reply_agent(k, 'Continue'))
        self.assertEqual(popen.call_args.kwargs['cwd'], workspace)
        self.assertEqual(self.task_for(key)['project_id'], project['project_id'])

    def test_runtime_resume_keeps_original_directory_with_enclosing_project(self):
        enclosing = make_repository(self.directory('enclosing'))
        nested = self.directory('enclosing/nested')
        project = manager._store.create_project('Enclosing', enclosing)
        with patch.object(manager, '_project_root', nested):
            key, _ = self.create()
        self.assertEqual(self.task_for(key)['project_id'], project['project_id'])
        manager.agent_sessions[key] = '11111111-1111-1111-1111-111111111111'
        self.output(key, 'CONTROL_CENTER_WAITING: Which file?', True)
        _, _, _, popen = self.resume(key, lambda k: manager.reply_agent(k, 'Continue'))
        self.assertEqual(popen.call_args.kwargs['cwd'], Path(manager._store.get_task_workspace(self.task_for(key)['task_id'])['workspace_path']))

    def test_unresolved_task_and_child_start_fail_before_spawn(self):
        parent, _ = self.create(kind='mock')
        task = self.task_for(parent)
        manager.stop_agent(parent)
        with manager._store._connection() as db:
            db.execute('UPDATE tasks SET project_id=NULL WHERE task_id=?', (task['task_id'],))
        before = manager._store.list_tasks()
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
            self.request(f'/agents/{parent}/children/start', {'task': 'Child'}, 422)
            spawn.assert_not_called()
        self.assertEqual(manager._store.list_tasks(), before)

    def test_missing_project_directory_rejects_before_spawn(self):
        root = self.directory('removed')
        project = manager._store.create_project('Removed', root)
        task = manager._store.create_task('Work', project_id=project['project_id'])
        root.rmdir()
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
            spawn.assert_not_called()

    def test_spawn_failure_retains_pending_task_with_project_no_assignment(self):
        with patch.object(manager, '_spawn_process', side_effect=OSError('failed')):
            self.request('/agents/start', {'task': 'Work'}, 503)
        task, = manager._store.list_tasks()
        self.assertEqual(task['status'], 'pending')
        self.assertIsNotNone(manager._store.get_project(task['project_id']))
        self.assertEqual(manager._store.list_task_assignments(task['task_id']), [])

    def test_recovery_and_worker_restart_preserve_project(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        self.recover()
        self.assertEqual(manager._store.get_task(task['task_id'])['project_id'], task['project_id'])
        replacement = self.start_request(f"/tasks/{task['task_id']}/start-agent", {})['agent_id']
        self.assertNotEqual(replacement, key)
        self.assertEqual(self.task_for(replacement)['project_id'], task['project_id'])

    def test_v6_migration_no_execution_evidence_preserves_history_and_hierarchy(self):
        parent, _ = self.create()
        child, _ = self.create(parent=parent)
        manager.rename_agent(child, 'Name')
        manager.set_agent_color(child, 'cyan')
        manager.agent_sessions[child] = '11111111-1111-1111-1111-111111111111'
        self.output(child, 'Output\nCONTROL_CENTER_WAITING: Question?', True)
        store = manager._store
        agents, tasks, names = store.load_agents(), store.list_tasks(), store.name_history(child)
        assignments = [store.list_task_assignments(t['task_id']) for t in tasks]
        with store._connection() as db:
            db.execute('DROP TABLE task_workspaces')
            db.execute('DROP INDEX task_project')
            db.execute('ALTER TABLE tasks DROP COLUMN project_id')
            db.execute('DROP TABLE projects')
            db.execute('PRAGMA user_version=6')
        migrate = AgentStore._migrate_projects
        def fail(db):
            migrate(db)
            raise sqlite3.OperationalError('interrupted')
        with patch.object(AgentStore, '_migrate_projects', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 6)
            self.assertNotIn('project_id', {row['name'] for row in db.execute('PRAGMA table_info(tasks)')})
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone())
        restored = AgentStore(self.path)
        self.assertEqual(restored.list_projects(), [])
        self.assertEqual(restored.list_tasks(), [{**t, 'project_id': None} for t in tasks])
        self.assertEqual(restored.load_agents(), agents)
        self.assertEqual(restored.name_history(child), names)
        self.assertEqual([restored.list_task_assignments(t['task_id']) for t in tasks], assignments)
        with restored._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertIn('projects', {row['table'] for row in db.execute('PRAGMA foreign_key_list(tasks)')})
        self.assertEqual(AgentStore(self.path).list_tasks(), restored.list_tasks())
        self.assertEqual(AgentStore(self.path).list_projects(), [])


if __name__ == '__main__':
    unittest.main()
