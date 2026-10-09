import os
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

from app import agent_manager as manager, workspace_git as gitops
from app.persistence import AgentStore
from app.project_domain import canonical_path
from app.task_workspaces import provision_task_workspace, storage_root
from workspace_test_support import make_repository
import test_task_lifecycle


class TaskWorkspaceTests(unittest.TestCase):
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

    def task(self, parent=None):
        return manager._store.create_task('Work', parent_task_id=parent['task_id'] if parent else None)

    def workspace(self, task, origin='project_snapshot'):
        return provision_task_workspace(manager._store, task['task_id'], origin_kind=origin)

    def git_state(self, root):
        return (gitops.git(root, 'symbolic-ref', 'HEAD'), gitops.git(root, 'rev-parse', 'HEAD'),
                (root / '.git' / 'index').read_bytes(), gitops.git(root, 'status', '--porcelain=v1', '-z'),
                gitops.git(root, 'show-ref', '--heads'))

    def test_snapshot_captures_real_source_without_mutating_user_git_state(self):
        root = manager._project_root
        (root / 'source.txt').write_bytes(b'staged source\n')
        (root / 'staged.txt').write_bytes(b'staged only\n')
        gitops.git(root, 'add', 'source.txt', 'staged.txt')
        (root / 'source.txt').write_bytes(b'unstaged current source\r\n')
        (root / 'new.py').write_bytes(b'new source\n')
        (root / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
        for name in ['ignored.txt', '.env', 'secret.key', 'node_modules/a.js', '.venv/bin/tool', 'dist/bundle.js']:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('excluded', encoding='utf-8')
        before = self.git_state(root)
        original = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}
        workspace = self.workspace(self.task())
        path = Path(workspace['workspace_path'])
        self.assertEqual(self.git_state(root), before)
        self.assertEqual({str(p.relative_to(root)): p.read_bytes() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}, original)
        self.assertEqual((path / 'source.txt').read_bytes(), b'unstaged current source\r\n')
        self.assertEqual((path / 'staged.txt').read_bytes(), b'staged only\n')
        self.assertEqual((path / 'new.py').read_bytes(), b'new source\n')
        for name in ['ignored.txt', '.env', 'secret.key', 'node_modules', '.venv', 'dist']:
            self.assertFalse((path / name).exists(), name)
        self.assertEqual(gitops.git(path, 'rev-parse', 'HEAD').decode().strip(), workspace['base_snapshot'])
        self.assertEqual(gitops.git(path, 'branch', '--show-current').strip(), b'')

    def test_tracked_secrets_ignored_files_and_deletions_are_excluded(self):
        root = manager._project_root
        for name in ['.env', 'ignored.txt', 'deleted.txt']:
            (root / name).write_text('tracked', encoding='utf-8')
        gitops.git(root, 'add', '.env', 'ignored.txt', 'deleted.txt')
        (root / '.gitignore').write_text('ignored.txt\n', encoding='utf-8')
        (root / 'deleted.txt').unlink()
        path = Path(self.workspace(self.task())['workspace_path'])
        for name in ['.env', 'ignored.txt', 'deleted.txt']:
            self.assertFalse((path / name).exists())

    def test_independent_roots_have_independent_files(self):
        a, b = self.workspace(self.task()), self.workspace(self.task())
        self.assertNotEqual(a['workspace_id'], b['workspace_id'])
        self.assertNotEqual(a['workspace_path'], b['workspace_path'])
        self.assertEqual(gitops.git(a['workspace_path'], 'rev-parse', 'HEAD^{tree}'),
                         gitops.git(b['workspace_path'], 'rev-parse', 'HEAD^{tree}'))
        for workspace, text in [(a, 'A'), (b, 'B')]:
            (Path(workspace['workspace_path']) / 'source.txt').write_text(text, encoding='utf-8')
        self.assertEqual((Path(a['workspace_path']) / 'source.txt').read_text(), 'A')
        self.assertEqual((Path(b['workspace_path']) / 'source.txt').read_text(), 'B')
        self.assertEqual((manager._project_root / 'source.txt').read_text(), 'original\n')

    def test_child_sibling_and_grandchild_snapshot_current_parent_only(self):
        parent = self.task()
        p = Path(self.workspace(parent)['workspace_path'])
        (p / 'source.txt').write_text('Parent edit', encoding='utf-8')
        (p / 'helper.py').write_text('Parent untracked', encoding='utf-8')
        parent_status = gitops.git(p, 'status', '--porcelain=v1', '-z')
        child = self.task(parent)
        c = Path(self.workspace(child, 'parent_task_snapshot')['workspace_path'])
        sibling = Path(self.workspace(self.task(parent), 'parent_task_snapshot')['workspace_path'])
        self.assertEqual((c / 'source.txt').read_text(), 'Parent edit')
        self.assertEqual((c / 'helper.py').read_text(), 'Parent untracked')
        (c / 'source.txt').write_text('Child edit', encoding='utf-8')
        (c / 'child.py').write_text('Child untracked', encoding='utf-8')
        g = Path(self.workspace(self.task(child), 'parent_task_snapshot')['workspace_path'])
        self.assertEqual((g / 'source.txt').read_text(), 'Child edit')
        self.assertEqual((g / 'child.py').read_text(), 'Child untracked')
        (g / 'source.txt').write_text('Grandchild edit', encoding='utf-8')
        self.assertEqual((c / 'source.txt').read_text(), 'Child edit')
        self.assertEqual((sibling / 'source.txt').read_text(), 'Parent edit')
        self.assertEqual((p / 'source.txt').read_text(), 'Parent edit')
        self.assertEqual(gitops.git(p, 'status', '--porcelain=v1', '-z'), parent_status)
        self.assertEqual((manager._project_root / 'source.txt').read_text(), 'original\n')

    def test_worker_replacement_and_stop_branch_retain_exact_workspace(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        workspace = manager._store.get_task_workspace(task['task_id'])
        path = Path(workspace['workspace_path'])
        (path / 'source.txt').write_text('Unfinished work', encoding='utf-8')
        manager.stop_branch(key)
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process) as spawn, patch.object(manager, 'Thread'):
            replacement = manager.start_task_agent(task['task_id'])['agent_id']
        self.assertNotEqual(key, replacement)
        self.assertEqual(spawn.call_args.kwargs['cwd'], path)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual((path / 'source.txt').read_text(), 'Unfinished work')
        self.assertEqual(len(manager._store.list_task_assignments(task['task_id'])), 2)

    def test_all_resume_actions_and_recovered_wait_use_durable_workspace(self):
        for action in [lambda k: manager.reply_agent(k, 'Continue'), manager.decide_agent,
                       manager.decide_similar_agent, manager.decide_always_agent]:
            key, _ = self.create(sandbox='workspace-write')
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?', True)
            workspace = manager._store.get_task_workspace(self.task_for(key)['task_id'])
            self.recover()
            _, _, command, popen = self.resume(key, action)
            self.assertEqual(popen.call_args.kwargs['cwd'], Path(workspace['workspace_path']))
            command.assert_called_with('workspace-write', key)
            self.assertEqual(manager._store.get_task_workspace(self.task_for(key)['task_id']), workspace)

    def test_redirect_read_only_and_completion_retain_workspace(self):
        key, old = self.create()
        self.output(key, f'session id: {key}\n')
        workspace = manager._store.get_task_workspace(self.task_for(key)['task_id'])
        with patch.object(manager, '_stop_windows_tree'):
            _, _, command, popen = self.resume(key, lambda k: manager.redirect_agent(k, 'Correction'))
        self.assertEqual(popen.call_args.kwargs['cwd'], Path(workspace['workspace_path']))
        command.assert_called_with('read-only', key)
        old.poll.return_value = 0
        manager._finalize_process(key, old)
        self.assertEqual(manager._store.get_task_workspace(self.task_for(key)['task_id']), workspace)
        self.output(key, 'Done', True)
        self.assertEqual(manager._store.get_task_workspace(self.task_for(key)['task_id']), workspace)
        self.assertTrue(Path(workspace['workspace_path']).is_dir())

    def test_legacy_parent_bootstrap_then_child_snapshot(self):
        parent = self.task()
        child = self.task(parent)
        self.assertIsNone(manager._store.get_task_workspace(parent['task_id']))
        workspace = self.workspace(child, 'parent_task_snapshot')
        p = manager._store.get_task_workspace(parent['task_id'])
        self.assertEqual(p['origin_kind'], 'legacy_project_snapshot')
        self.assertEqual(workspace['source_task_id'], parent['task_id'])
        self.assertNotEqual(p['workspace_path'], workspace['workspace_path'])

    def test_non_git_unborn_and_unresolved_fail_before_spawn(self):
        for name in ['non-git', 'unborn']:
            root = Path(self.temp.name) / name
            root.mkdir()
            if name == 'unborn':
                gitops.git(root, 'init')
            project = manager._store.create_project(name, root)
            task = manager._store.create_task('Work', project_id=project['project_id'])
            with patch.object(manager, '_spawn_process') as spawn:
                self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
                spawn.assert_not_called()
            self.assertIsNone(manager._store.get_task_workspace(task['task_id']))
        task = self.task()
        with manager._store._connection() as db:
            db.execute('UPDATE tasks SET project_id=NULL WHERE task_id=?', (task['task_id'],))
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
            spawn.assert_not_called()

    def test_corrupt_workspace_never_rebuilds_or_spawns(self):
        other = make_repository(Path(self.temp.name) / 'other')
        ordinary = Path(self.temp.name) / 'ordinary'
        ordinary.mkdir()
        for target in [Path(self.temp.name) / 'missing', ordinary, other, manager._project_root]:
            task = self.task()
            workspace = self.workspace(task)
            before = manager._store.get_task(task['task_id'])
            with manager._store._connection() as db:
                db.execute('UPDATE task_workspaces SET workspace_path=?,workspace_path_key=? WHERE task_id=?',
                           (str(target), canonical_path(target)[1], task['task_id']))
            with patch.object(manager, '_spawn_process') as spawn, patch.object(gitops, 'materialize') as materialize:
                self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
                spawn.assert_not_called()
                materialize.assert_not_called()
            self.assertEqual(manager._store.get_task(task['task_id']), before)
            self.assertTrue(Path(workspace['workspace_path']).is_dir())

    def test_workspace_root_overlap_is_rejected(self):
        task = self.task()
        for root in [manager._project_root, manager._project_root / 'nested', manager._project_root.parent]:
            with patch.dict(os.environ, {'CONTROL_CENTER_WORKSPACE_ROOT': str(root)}), self.assertRaises(ValueError):
                self.workspace(task)
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))

    def test_workspace_cannot_be_nested_inside_another_task_workspace(self):
        first = self.workspace(self.task())
        task = self.task()
        with patch.dict(os.environ, {'CONTROL_CENTER_WORKSPACE_ROOT': first['workspace_path']}), \
                self.assertRaisesRegex(ValueError, 'paths must not overlap'):
            self.workspace(task)
        self.assertEqual((Path(first['workspace_path']) / 'source.txt').read_text(), 'original\n')
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))

    def test_corrupt_workspace_redirect_leaves_current_process_untouched(self):
        key, process = self.create()
        self.output(key, f'session id: {key}\n')
        with manager._store._connection() as db:
            db.execute('UPDATE task_workspaces SET workspace_path=? WHERE task_id=?',
                       (str(Path(self.temp.name) / 'missing'), self.task_for(key)['task_id']))
        with patch.object(manager, '_codex_command', return_value='fixed'), \
                patch.object(manager, '_stop_windows_tree') as stop, patch.object(manager, '_spawn_process') as spawn:
            with self.assertRaises(ValueError):
                manager.redirect_agent(key, 'Correct')
            stop.assert_not_called()
            spawn.assert_not_called()
        self.assertIs(manager.agents[key], process)
        self.assertEqual(manager.agent_statuses[key], 'running')

    def test_corrupt_workspace_automatic_decisions_fall_back_to_waiting_once(self):
        for mode in ['similar', 'always']:
            key, process = self.create()
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?')
            if mode == 'similar':
                manager.agent_similar_decisions[key].enabled = True
                manager.agent_similar_decisions[key].examples = ['Approved']
            else:
                manager.agent_always_decisions[key].enabled = True
            with manager._store._connection() as db:
                db.execute('UPDATE task_workspaces SET workspace_path=? WHERE task_id=?',
                           (str(Path(self.temp.name) / 'missing'), self.task_for(key)['task_id']))
            process.poll.return_value = 0
            with patch.object(manager, '_codex_command', return_value='fixed'), \
                    patch.object(manager, '_spawn_process') as spawn, self.assertLogs(manager.logger):
                manager._finalize_process(key, process)
                manager.get_agents()
                spawn.assert_not_called()
            self.assertEqual(manager.agent_statuses[key], 'waiting')
            self.assertEqual(manager.agent_waiting_questions[key], 'Question?')

    def test_snapshot_and_materialize_failures_do_not_leave_ready_workspaces(self):
        for operation in ['snapshot_worktree_source', 'materialize']:
            task = self.task()
            before = self.git_state(manager._project_root)
            with patch.object(gitops, operation, side_effect=ValueError('failure')), self.assertRaises(ValueError):
                self.workspace(task)
            self.assertIsNone(manager._store.get_task_workspace(task['task_id']))
            self.assertFalse((storage_root() / task['project_id'] / task['task_id']).exists())
            self.assertEqual(self.git_state(manager._project_root), before)
            self.assertIsNotNone(self.workspace(task))

    def test_db_failure_cleans_only_new_workspace_and_retry_is_idempotent(self):
        task = self.task()
        before = gitops.git(manager._project_root, 'worktree', 'list', '--porcelain')
        with patch.object(manager._store, 'save_task_workspace', side_effect=sqlite3.OperationalError('disk full')), self.assertRaises(sqlite3.Error):
            self.workspace(task)
        self.assertEqual(gitops.git(manager._project_root, 'worktree', 'list', '--porcelain'), before)
        self.assertFalse((storage_root() / task['project_id'] / task['task_id']).exists())
        workspace = self.workspace(task)
        self.assertEqual(self.workspace(task), workspace)

    def test_uncertain_commit_preserves_durable_workspace(self):
        task = self.task()
        save = manager._store.save_task_workspace
        def uncertain(record):
            save(record)
            raise sqlite3.OperationalError('ack lost')
        with patch.object(manager._store, 'save_task_workspace', side_effect=uncertain), self.assertRaises(sqlite3.Error):
            self.workspace(task)
        workspace = manager._store.get_task_workspace(task['task_id'])
        self.assertTrue(Path(workspace['workspace_path']).is_dir())
        self.assertEqual(self.workspace(task), workspace)

    def test_spawn_failure_preserves_valid_workspace_and_unfinished_work(self):
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path'])
        (path / 'unfinished.py').write_text('keep', encoding='utf-8')
        with patch.object(manager, '_spawn_process', side_effect=OSError('spawn failed')):
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 503)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual((path / 'unfinished.py').read_text(), 'keep')
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))

    def test_initial_git_provisioning_does_not_hold_agent_state_lock(self):
        snapshot = gitops.snapshot_worktree_source
        def checked(root):
            self.assertFalse(manager._state_lock._is_owned())
            return snapshot(root)
        with patch.object(gitops, 'snapshot_worktree_source', side_effect=checked):
            self.create(kind='mock')

    def test_shutdown_during_provisioning_prevents_late_spawn(self):
        snapshot = gitops.snapshot_worktree_source
        def shut_down(root):
            manager.shutdown_agents()
            return snapshot(root)
        with patch.object(gitops, 'snapshot_worktree_source', side_effect=shut_down), \
                patch.object(manager, '_spawn_process') as spawn:
            self.request('/agents/start', {'task': 'Work'}, 503)
            spawn.assert_not_called()
        task, = manager._store.list_tasks()
        self.assertEqual(task['status'], 'pending')
        self.assertIsNotNone(manager._store.get_task_workspace(task['task_id']))
        self.assertEqual(manager._store.list_task_assignments(task['task_id']), [])

    def test_partial_materialization_failure_removes_only_its_worktree(self):
        existing = self.workspace(self.task())
        task = self.task()
        materialize = gitops.materialize
        def partial(*args):
            materialize(*args)
            raise OSError('interrupted after worktree registration')
        before = gitops.git(manager._project_root, 'worktree', 'list', '--porcelain')
        with patch.object(gitops, 'materialize', side_effect=partial), self.assertRaises(OSError):
            self.workspace(task)
        self.assertEqual(gitops.git(manager._project_root, 'worktree', 'list', '--porcelain'), before)
        self.assertTrue(Path(existing['workspace_path']).is_dir())
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))

    def test_retry_failed_child_provisioning_still_snapshots_parent(self):
        key, _ = self.create(kind='mock')
        parent = self.task_for(key)
        path = Path(manager._store.get_task_workspace(parent['task_id'])['workspace_path'])
        (path / 'source.txt').write_text('Parent work', encoding='utf-8')
        with patch.object(gitops, 'snapshot_worktree_source', side_effect=ValueError('failed')):
            self.request(f'/agents/{key}/children/start', {'task': 'Child'}, 422)
        child, = manager._store.get_task_children(parent['task_id'])
        self.start_request(f"/tasks/{child['task_id']}/start-agent", {})
        workspace = manager._store.get_task_workspace(child['task_id'])
        self.assertEqual(workspace['origin_kind'], 'parent_task_snapshot')
        self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'Parent work')

    def test_unknown_existing_path_is_never_deleted_or_adopted(self):
        task = self.task()
        target = storage_root() / task['project_id'] / task['task_id']
        target.mkdir(parents=True)
        (target / 'valuable.txt').write_text('keep', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'manual recovery'):
            self.workspace(task)
        self.assertEqual((target / 'valuable.txt').read_text(), 'keep')
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))

    def test_workspace_reuse_ignores_storage_root_configuration_changes(self):
        task = self.task()
        workspace = self.workspace(task)
        with patch.dict(os.environ, {'CONTROL_CENTER_WORKSPACE_ROOT': str(Path(self.temp.name) / 'new-storage')}):
            self.assertEqual(self.workspace(task), workspace)

    def test_project_registration_cannot_turn_task_workspace_into_canonical(self):
        workspace = self.workspace(self.task())
        path = Path(workspace['workspace_path'])
        for root in [path, path.parent]:
            with self.assertRaisesRegex(ValueError, 'Task Workspace'):
                manager._store.create_project('Overlapping', root)

    def test_externally_deleted_workspace_fails_without_rebuilding(self):
        task = self.task()
        workspace = self.workspace(task)
        gitops.git(manager._project_root, 'worktree', 'remove', '--force', workspace['workspace_path'])
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
            spawn.assert_not_called()
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertFalse(Path(workspace['workspace_path']).exists())

    def test_concurrent_provisioning_has_one_record_and_path(self):
        task = self.task()
        stores = [AgentStore(self.path) for _ in range(3)]
        barrier = Barrier(3)
        def provision(store):
            barrier.wait()
            return provision_task_workspace(store, task['task_id'])
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(provision, stores))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(len(list((storage_root() / task['project_id']).iterdir())), 1)

    def test_observability_is_read_only_and_legacy_start_is_lazy(self):
        task = self.task()
        self.assertIsNone(self.request(f"/tasks/{task['task_id']}/workspace"))
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))
        self.request('/tasks/missing/workspace', expected=404)
        self.start_request(f"/tasks/{task['task_id']}/start-agent", {})
        result = self.request(f"/tasks/{task['task_id']}/workspace")
        self.assertEqual(result['origin_kind'], 'legacy_project_snapshot')
        self.assertNotIn('workspace_path_key', result)
        self.assertEqual(result['task_id'], task['task_id'])

    def test_v7_migration_preserves_data_without_snapshots_and_rolls_back(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?', True)
        manager.rename_agent(key, 'Preserved')
        manager.set_agent_color(key, 'blue')
        store = manager._store
        before = (store.load_agents(), store.list_tasks(), store.list_projects(), store.name_history(key),
                  store.list_task_assignments(self.task_for(key)['task_id']))
        with store._connection() as db:
            db.execute('DROP TABLE task_workspaces')
            db.execute('PRAGMA user_version=7')
        migrate = AgentStore._migrate_workspaces
        def fail(db):
            migrate(db)
            raise sqlite3.OperationalError('fail')
        with patch.object(AgentStore, '_migrate_workspaces', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 7)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='task_workspaces'").fetchone())
        with patch.object(gitops, 'git', side_effect=AssertionError('Migration must not invoke Git')):
            restored = AgentStore(self.path)
            self.assertEqual((restored.load_agents(), restored.list_tasks(), restored.list_projects(), restored.name_history(key),
                              restored.list_task_assignments(self.task_for(key)['task_id'])), before)
            self.assertIsNone(restored.get_task_workspace(self.task_for(key)['task_id']))
            self.assertIsNone(AgentStore(self.path).get_task_workspace(self.task_for(key)['task_id']))
        with restored._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 19)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
