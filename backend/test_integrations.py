from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier, Event
import unittest
from unittest.mock import patch

from app import agent_manager as manager, integration_git as ig, integrations as service, workspace_git as gitops
from app.persistence import AgentStore
from app.workspace_freshness import evaluate_workspace_freshness
import test_task_workspaces


class IntegrationTests(unittest.TestCase):
    setUp = test_task_workspaces.TaskWorkspaceTests.setUp
    tearDown = test_task_workspaces.TaskWorkspaceTests.tearDown
    task = test_task_workspaces.TaskWorkspaceTests.task
    workspace = test_task_workspaces.TaskWorkspaceTests.workspace
    git_state = test_task_workspaces.TaskWorkspaceTests.git_state
    create = test_task_workspaces.TaskWorkspaceTests.create
    replacement = test_task_workspaces.TaskWorkspaceTests.replacement
    output = test_task_workspaces.TaskWorkspaceTests.output
    recover = test_task_workspaces.TaskWorkspaceTests.recover
    resume = test_task_workspaces.TaskWorkspaceTests.resume
    request = test_task_workspaces.TaskWorkspaceTests.request
    task_for = test_task_workspaces.TaskWorkspaceTests.task_for

    def edit(self, root, name, text):
        path = Path(root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    def root(self):
        task = self.task()
        return task, Path(self.workspace(task)['workspace_path'])

    def child(self, parent):
        task = self.task(parent)
        return task, Path(self.workspace(task, 'parent_task_snapshot')['workspace_path'])

    def integrate(self, task):
        return manager.integrate_task(task['task_id'])

    def source_bytes(self, root):
        return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob('*') if p.is_file() and '.git' not in p.parts}

    def seed(self):
        self.edit(manager._project_root, 'a.txt', 'original A\n')
        self.edit(manager._project_root, 'b.txt', 'original B\n')

    def test_child_combines_independent_work_and_retains_source_canonical_and_lifecycle(self):
        self.seed()
        parent, p = self.root()
        child, c = self.child(parent)
        self.edit(p, 'a.txt', 'parent work')
        self.edit(c, 'b.txt', 'child work')
        source, canonical = self.source_bytes(c), self.git_state(manager._project_root)
        metadata = manager._store.get_task_workspace(child['task_id'])
        result = self.integrate(child)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['validation_status'], 'not_run')
        self.assertEqual(result['destination_id'], parent['task_id'])
        self.assertEqual((p / 'a.txt').read_text(), 'parent work')
        self.assertEqual((p / 'b.txt').read_text(), 'child work')
        self.assertEqual(self.source_bytes(c), source)
        self.assertEqual(self.git_state(manager._project_root), canonical)
        self.assertEqual(manager._store.get_task_workspace(child['task_id']), metadata)
        self.assertEqual(manager._store.get_task(parent['task_id'])['status'], 'pending')
        self.assertEqual(manager.agents, {})

    def test_same_file_real_three_way_merge_combines_nonoverlapping_sections(self):
        base = ''.join(f'line {i}\n' for i in range(20))
        self.edit(manager._project_root, 'source.txt', base)
        parent, p = self.root()
        child, c = self.child(parent)
        self.edit(p, 'source.txt', base.replace('line 1\n', 'parent\n'))
        self.edit(c, 'source.txt', base.replace('line 18\n', 'child\n'))
        result = self.integrate(child)
        self.assertEqual(result['status'], 'applied')
        text = (p / 'source.txt').read_text()
        self.assertIn('parent\n', text)
        self.assertIn('child\n', text)
        self.assertNotIn('<<<<<<<', text)

    def test_child_true_conflict_preserves_all_files(self):
        parent, p = self.root()
        child, c = self.child(parent)
        self.edit(p, 'source.txt', 'parent incompatible\n')
        self.edit(c, 'source.txt', 'child incompatible\n')
        before = [self.source_bytes(path) for path in (p, c, manager._project_root)]
        result = self.integrate(child)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(result['conflict_paths'], ['source.txt'])
        self.assertEqual([self.source_bytes(path) for path in (p, c, manager._project_root)], before)
        self.assertEqual(manager._store.get_integration(result['integration_id'])['status'], 'conflict')

    def test_grandchild_integrates_only_direct_parent(self):
        parent, p = self.root()
        child, c = self.child(parent)
        grandchild, g = self.child(child)
        self.edit(g, 'new.py', 'grandchild')
        result = self.integrate(grandchild)
        self.assertEqual(result['destination_id'], child['task_id'])
        self.assertEqual((c / 'new.py').read_text(), 'grandchild')
        self.assertFalse((p / 'new.py').exists())
        self.assertFalse((manager._project_root / 'new.py').exists())
        self.assertEqual(self.integrate(child)['status'], 'applied')
        self.assertEqual((p / 'new.py').read_text(), 'grandchild')

    def test_root_dirty_destination_preserves_branch_head_index_and_user_edits(self):
        self.seed()
        task, source = self.root()
        root = manager._project_root
        self.edit(source, 'b.txt', 'Task result\n')
        self.edit(root, 'source.txt', 'staged user source\n')
        gitops.git(root, 'add', 'source.txt')
        self.edit(root, 'a.txt', 'unstaged user source\n')
        self.edit(root, 'user.py', 'untracked user source\n')
        git_before = self.git_state(root)
        cached, entries = gitops.git(root, 'diff', '--cached'), gitops.git(root, 'ls-files', '-s')
        source_before = self.source_bytes(source)
        result = self.integrate(task)
        self.assertEqual(result['status'], 'applied')
        after = self.git_state(root)
        for index in (0, 1, 2, 4):
            self.assertEqual(after[index], git_before[index])
        self.assertEqual(gitops.git(root, 'diff', '--cached'), cached)
        self.assertEqual(gitops.git(root, 'ls-files', '-s'), entries)
        for name, text in [('source.txt','staged user source\n'), ('a.txt','unstaged user source\n'),
                           ('user.py','untracked user source\n'), ('b.txt','Task result\n')]:
            self.assertEqual((root / name).read_text(), text)
        self.assertEqual(self.source_bytes(source), source_before)

    def test_root_conflict_keeps_index_and_bytes(self):
        task, source = self.root()
        self.edit(source, 'source.txt', 'Task edit')
        self.edit(manager._project_root, 'source.txt', 'user edit')
        gitops.git(manager._project_root, 'add', 'source.txt')
        before = self.git_state(manager._project_root)
        files = self.source_bytes(manager._project_root)
        result = self.integrate(task)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(self.git_state(manager._project_root), before)
        self.assertEqual(self.source_bytes(manager._project_root), files)

    def test_source_and_destination_races_obsolete_the_candidate(self):
        for side in ('source','destination'):
            with self.subTest(side=side):
                task, source = self.root()
                self.edit(source, 'task.py', 'Task work ' + side)
                build = ig.build_candidate
                def raced(*args):
                    value = build(*args)
                    self.edit(source if side == 'source' else manager._project_root, 'late.py', side)
                    return value
                with patch.object(ig, 'build_candidate', side_effect=raced):
                    result = self.integrate(task)
                self.assertEqual(result['status'], side + '_changed')
                self.assertFalse((manager._project_root / 'task.py').exists())
                self.assertEqual(((source if side == 'source' else manager._project_root) / 'late.py').read_text(), side)

    def test_same_destination_serializes_and_preserves_both_children(self):
        parent, p = self.root()
        a, ap = self.child(parent)
        b, bp = self.child(parent)
        self.edit(ap, 'a.py', 'A')
        self.edit(bp, 'b.py', 'B')
        barrier = Barrier(2)
        def run(task):
            barrier.wait(timeout=5)
            return self.integrate(task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, (a,b)))
        self.assertEqual([r['status'] for r in results], ['applied','applied'])
        self.assertEqual((p / 'a.py').read_text(), 'A')
        self.assertEqual((p / 'b.py').read_text(), 'B')
        ordered = sorted(results, key=lambda r: r['applied_at'])
        self.assertEqual(ordered[1]['destination_before_snapshot'], ordered[0]['result_snapshot'])

    def test_different_destinations_can_prepare_concurrently(self):
        a, _ = self.root()
        b, _ = self.root()
        child_a, ca = self.child(a)
        child_b, cb = self.child(b)
        self.edit(ca, 'a.py', 'A')
        self.edit(cb, 'b.py', 'B')
        barrier = Barrier(2)
        build = ig.build_candidate
        def together(*args):
            barrier.wait(timeout=10)
            return build(*args)
        with patch.object(ig, 'build_candidate', side_effect=together), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.integrate, (child_a, child_b)))
        self.assertTrue(all(r['status']=='applied' for r in results))

    def test_durable_active_claim_across_store_connections(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        build = ig.build_candidate
        def duplicate(*args):
            row = manager._store.list_integrations(task['task_id'])[0]
            row['integration_id'] = 'different-id'
            with self.assertRaisesRegex(ValueError, 'active integration'):
                AgentStore(self.path).claim_integration(row)
            return build(*args)
        with patch.object(ig, 'build_candidate', side_effect=duplicate):
            self.assertEqual(self.integrate(task)['status'], 'applied')

    def test_idempotent_retry_uses_persisted_source_identity(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        first = self.integrate(task)
        before = self.source_bytes(manager._project_root)
        manager._store = AgentStore(self.path)
        with patch.object(ig, 'apply_plan', side_effect=AssertionError('must not apply twice')):
            again = self.integrate(task)
        self.assertEqual(first, again)
        self.assertEqual(self.source_bytes(manager._project_root), before)
        self.assertEqual(len(manager._store.list_integrations(task['task_id'])), 1)

    def test_noop_keeps_source_destination_and_validation(self):
        task, _ = self.root()
        before = self.git_state(manager._project_root)
        with patch.object(ig, 'apply_plan') as apply:
            result = self.integrate(task)
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(result['validation_status'], 'not_run')
        self.assertEqual(result['changed_paths'], [])
        apply.assert_not_called()
        self.assertEqual(self.git_state(manager._project_root), before)

    def test_active_source_and_parent_are_rejected_without_stopping(self):
        parent_key, parent_process = self.create()
        parent = self.task_for(parent_key)
        child, source = self.child(parent)
        self.edit(source, 'new.py', 'work')
        with patch.object(manager, 'stop_agent') as stop, self.assertRaisesRegex(ValueError, 'live Agent'):
            self.integrate(child)
        stop.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'live Agent'):
            self.integrate(parent)
        self.assertEqual(manager._store.list_integrations(), [])
        self.assertIsNone(parent_process.poll())

    def test_multiple_child_integrations_invalidate_parent_same_session_and_stale_sibling(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which path?\n', True)
        parent = self.task_for(key)
        a, ap = self.child(parent)
        b, bp = self.child(parent)
        sibling, _ = self.child(parent)
        self.edit(ap, 'a.py', 'A')
        self.edit(bp, 'b.py', 'B')
        self.integrate(a)
        self.integrate(b)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(evaluate_workspace_freshness(manager._store, sibling['task_id'])['freshness'], 'stale')
        self.recover()
        _, process, command, _ = self.resume(key, lambda k: manager.reply_agent(k, 'continue'))
        self.assertIn('Work from child Tasks was integrated', process.stdin.saved)
        self.assertIn('- a.py', process.stdin.saved)
        self.assertIn('- b.py', process.stdin.saved)
        self.assertIn('MUST re-read', process.stdin.saved)
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager.agent_tasks[key], parent['description'])
        self.assertEqual(manager._store.session_integrations(key, parent['task_id']), [])

    def test_apply_failure_rolls_back_only_unchanged_applied_files(self):
        self.seed()
        task, source = self.root()
        self.edit(source, 'a.txt', 'new A')
        self.edit(source, 'b.txt', 'new B')
        before = self.source_bytes(manager._project_root)
        writer = ig.write_entry
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('injected write failure')
            return writer(*args, **kwargs)
        with patch.object(ig, 'write_entry', side_effect=fail_second):
            result = self.integrate(task)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.source_bytes(manager._project_root), before)
        self.assertEqual((source / 'a.txt').read_text(), 'new A')

    def test_apply_failure_preserves_concurrent_edits_and_blocks_destination(self):
        self.seed()
        task, source = self.root()
        self.edit(source, 'a.txt', 'new A')
        self.edit(source, 'b.txt', 'new B')
        writer = ig.write_entry
        calls = 0
        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.edit(manager._project_root, 'a.txt', 'external edit after first write')
                raise OSError('injected failure')
            return writer(*args, **kwargs)
        with patch.object(ig, 'write_entry', side_effect=fail_second):
            result = self.integrate(task)
        self.assertEqual(result['status'], 'recovery_required')
        self.assertEqual((manager._project_root / 'a.txt').read_text(), 'external edit after first write')
        with self.assertRaisesRegex(ValueError, 'recovery'):
            self.integrate(task)
        with self.assertRaisesRegex(ValueError, 'recovery'):
            manager._execution_workspace(task['task_id'])

    def interrupted(self, destination_state):
        task, source = self.root()
        self.edit(source, 'source.txt', 'new source')
        result = self.integrate(task)
        manager._store.update_integration(result['integration_id'], 'applying')
        if destination_state == 'before':
            record = manager._store.get_integration(result['integration_id'])
            for entry in record['plan']:
                ig.write_entry(manager._project_root, entry['path'], entry['before'])
        elif destination_state == 'other':
            self.edit(manager._project_root, 'source.txt', 'external unknown')
        manager.initialize_persistence(self.path)
        return task, manager._store.get_integration(result['integration_id'])

    def test_recovery_before_marks_failed(self):
        _, result = self.interrupted('before')
        self.assertEqual(result['status'], 'failed')

    def test_recovery_result_finalizes_applied(self):
        _, result = self.interrupted('result')
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['validation_status'], 'not_run')

    def test_recovery_uncertain_blocks_next_integration(self):
        task, result = self.interrupted('other')
        self.assertEqual(result['status'], 'recovery_required')
        with self.assertRaisesRegex(ValueError, 'recovery'):
            self.integrate(task)

    def test_ignored_secret_and_runtime_files_never_overwritten(self):
        self.edit(manager._project_root, '.gitignore', 'runtime.txt\n')
        task, source = self.root()
        root = manager._project_root
        for name in ('.env','runtime.txt','node_modules/a.js'):
            self.edit(root, name, 'keep private')
        self.edit(source, 'new.py', 'safe addition')
        self.assertEqual(self.integrate(task)['status'], 'applied')
        for name in ('.env','runtime.txt','node_modules/a.js'):
            self.assertEqual((root / name).read_text(), 'keep private')
        # A source addition cannot overwrite a locally ignored existing path.
        self.edit(source, '.gitignore', '')
        self.edit(source, 'runtime.txt', 'would overwrite')
        with self.assertRaises(ValueError):
            self.integrate(task)
        self.assertEqual((root / 'runtime.txt').read_text(), 'keep private')

    def test_apis_derive_destination_and_hide_internal_journal(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'safe')
        self.request(f"/tasks/{task['task_id']}/integrate", {'destination_id':'not-authoritative'}, 422)
        result = self.request(f"/tasks/{task['task_id']}/integrate", {})
        self.assertEqual(result['destination_id'], task['project_id'])
        self.assertEqual(result['destination_kind'], 'project')
        self.assertNotIn('plan', result)
        self.assertEqual(self.request('/integrations/' + result['integration_id']), result)
        self.assertEqual(self.request(f"/tasks/{task['task_id']}/integrations")['integrations'], [result])
        self.request('/integrations/missing', expected=404)
        self.request('/tasks/missing/integrations', expected=404)

    def test_api_conflict_is_structured(self):
        task, source = self.root()
        self.edit(source, 'source.txt', 'task')
        self.edit(manager._project_root, 'source.txt', 'user')
        result = self.request(f"/tasks/{task['task_id']}/integrate", {}, 409)
        self.assertEqual(result['detail']['status'], 'conflict')
        self.assertEqual(result['detail']['conflict_paths'], ['source.txt'])

    def test_missing_source_is_not_recreated(self):
        task = self.task()
        with self.assertRaisesRegex(ValueError, 'never provisions'):
            self.integrate(task)
        self.assertIsNone(manager._store.get_task_workspace(task['task_id']))

    def test_migration_v9_to_v10_is_empty_preserving_idempotent_and_transactional(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        workspace = manager._store.get_task_workspace(task['task_id'])
        context = manager._store.get_agent_source_context(key)
        with manager._store._connection() as db:
            db.execute('DROP TABLE integrations')
            db.execute('ALTER TABLE agent_source_context DROP COLUMN integration_order')
            db.execute('PRAGMA user_version=9')
        migrate = AgentStore._migrate_integrations
        def fail(db):
            migrate(db)
            raise sqlite3.OperationalError('migration failed')
        with patch.object(AgentStore, '_migrate_integrations', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 9)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='integrations'").fetchone())
        store = AgentStore(self.path)
        self.assertEqual(store.list_integrations(), [])
        self.assertEqual(store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(store.get_agent_source_context(key), context)
        self.assertEqual(AgentStore(self.path).get_task(task['task_id']), store.get_task(task['task_id']))
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='integration_destination_active'").fetchone())

    def test_apply_journal_is_committed_before_first_file_write(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        writer = ig.write_entry
        def inspect(*args, **kwargs):
            record = AgentStore(self.path).list_integrations(task['task_id'])[0]
            self.assertEqual(record['status'], 'applying')
            self.assertEqual(record['plan'][0]['path'], 'new.py')
            self.assertIsNotNone(record['result_snapshot'])
            return writer(*args, **kwargs)
        with patch.object(ig, 'write_entry', side_effect=inspect):
            self.assertEqual(self.integrate(task)['status'], 'applied')

    def test_database_failure_before_applying_never_mutates_destination(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        before = self.source_bytes(manager._project_root)
        update = manager._store.update_integration
        def fail(key, status, **fields):
            if status == 'applying':
                raise sqlite3.OperationalError('disk full')
            return update(key, status, **fields)
        with patch.object(manager._store, 'update_integration', side_effect=fail), self.assertRaises(RuntimeError):
            self.integrate(task)
        self.assertEqual(self.source_bytes(manager._project_root), before)
        self.assertEqual(manager._store.list_integrations(task['task_id'])[0]['status'], 'failed')

    def test_database_failure_after_apply_retains_recovery_claim(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        update = manager._store.update_integration
        def fail(key, status, **fields):
            if status == 'applied':
                raise sqlite3.OperationalError('disk full')
            return update(key, status, **fields)
        with patch.object(manager._store, 'update_integration', side_effect=fail), self.assertRaises(RuntimeError):
            self.integrate(task)
        record = manager._store.list_integrations(task['task_id'])[0]
        self.assertEqual(record['status'], 'recovery_required')
        self.assertEqual((manager._project_root / 'new.py').read_text(), 'source')
        other, other_source = self.root()
        self.edit(other_source, 'other.py', 'other')
        with self.assertRaisesRegex(ValueError, 'recovery'):
            self.integrate(other)

    def test_parent_becoming_live_during_prepare_rejects_before_apply(self):
        key, process = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Continue?\n', True)
        parent = self.task_for(key)
        child, source = self.child(parent)
        self.edit(source, 'new.py', 'source')
        build = ig.build_candidate
        def raced(*args):
            result = build(*args)
            process.poll.return_value = None
            return result
        with patch.object(ig, 'build_candidate', side_effect=raced), self.assertRaises(ValueError):
            self.integrate(child)
        parent_path = Path(manager._store.get_task_workspace(parent['task_id'])['workspace_path'])
        self.assertFalse((parent_path / 'new.py').exists())
        self.assertIsNone(process.poll())

    def test_preparing_recovery_never_applies_and_releases_claim(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        result = self.integrate(task)
        manager._store.update_integration(result['integration_id'], 'preparing')
        before = self.source_bytes(manager._project_root)
        manager.initialize_persistence(self.path)
        self.assertEqual(manager._store.get_integration(result['integration_id'])['status'], 'failed')
        self.assertEqual(self.source_bytes(manager._project_root), before)

    def test_delete_modify_and_binary_conflicts_are_not_resolved_by_guessing(self):
        task, source = self.root()
        (source / 'source.txt').unlink()
        self.edit(manager._project_root, 'source.txt', 'user modification')
        self.assertEqual(self.integrate(task)['status'], 'conflict')
        task, source = self.root()
        self.edit(source, 'binary.dat', 'source\0binary')
        self.edit(manager._project_root, 'binary.dat', 'user\0binary')
        result = self.integrate(task)
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual((manager._project_root / 'binary.dat').read_bytes(), b'user\0binary')

    def test_in_progress_git_operation_is_refused_without_mutation(self):
        task, source = self.root()
        self.edit(source, 'new.py', 'source')
        root = manager._project_root
        before = self.source_bytes(root)
        (root / '.git' / 'MERGE_HEAD').write_bytes(gitops.git(root, 'rev-parse', 'HEAD'))
        with self.assertRaisesRegex(ValueError, 'in-progress Git'):
            self.integrate(task)
        self.assertEqual(self.source_bytes(root), before)
        self.assertEqual(manager._store.list_integrations(), [])

    def test_worker_start_cannot_observe_an_upstream_integration_in_progress(self):
        parent, _ = self.root()
        source_task, source = self.child(parent)
        waiting_task, _ = self.child(parent)
        self.edit(source, 'new.py', 'child work')
        build = ig.build_candidate
        def during(*args):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(manager.start_task_agent, waiting_task['task_id'], 'mock', 'read-only')
                with self.assertRaisesRegex(ValueError, 'operation already in progress'):
                    future.result(timeout=5)
            return build(*args)
        with patch.object(ig, 'build_candidate', side_effect=during), patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(self.integrate(source_task)['status'], 'applied')
        spawn.assert_not_called()
        self.assertEqual(manager._store.list_task_assignments(waiting_task['task_id']), [])


if __name__ == '__main__':
    unittest.main()
