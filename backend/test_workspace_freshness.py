import sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import unittest
from unittest.mock import patch

from app import agent_manager as manager, workspace_git as gitops, workspace_freshness as freshness
from app.persistence import AgentStore
import test_task_workspaces


class WorkspaceFreshnessTests(unittest.TestCase):
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
    start_request = test_task_workspaces.TaskWorkspaceTests.start_request
    task_for = test_task_workspaces.TaskWorkspaceTests.task_for

    def check(self, task):
        return freshness.evaluate_workspace_freshness(manager._store, task['task_id'])

    def refresh(self, task):
        return freshness.ensure_workspace_current(manager._store, task['task_id'])

    def edit(self, root=None, name='source.txt', text='upstream new\n'):
        root = root or manager._project_root
        (root / name).write_bytes(text.encode('utf-8'))

    def waiting(self):
        key, process = self.create(sandbox='workspace-write')
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which path?\n', True)
        task = self.task_for(key)
        return key, process, task, manager._store.get_task_workspace(task['task_id'])

    def test_external_source_changes_without_head_or_events(self):
        root = manager._project_root
        for change in ('tracked', 'staged', 'addition', 'deletion', 'rename', 'untracked_removal'):
            with self.subTest(change=change):
                if change == 'untracked_removal':
                    self.edit(name='temporary.py')
                task = self.task()
                self.workspace(task)
                head = gitops.git(root, 'rev-parse', 'HEAD')
                if change in ('tracked', 'staged'):
                    self.edit(text=change)
                    if change == 'staged':
                        gitops.git(root, 'add', 'source.txt')
                elif change == 'addition':
                    self.edit(name='added.py')
                elif change == 'deletion':
                    (root / 'source.txt').unlink()
                elif change == 'rename':
                    (root / 'added.py').rename(root / 'renamed.py')
                else:
                    (root / 'temporary.py').unlink()
                before = self.git_state(root)
                state = self.check(task)
                self.assertEqual(state['freshness'], 'stale')
                self.assertFalse(state['local_dirty'])
                self.assertTrue(state['changed_upstream_files'])
                self.assertEqual(head, gitops.git(root, 'rev-parse', 'HEAD'))
                self.assertEqual(self.git_state(root), before)

    def test_identity_is_tree_content_not_commit_timestamp_and_local_work_is_fresh(self):
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path'])
        first = self.check(task)
        self.assertEqual(first['freshness'], 'fresh')
        self.assertEqual(first['base_source_snapshot'], first['upstream_snapshot'])
        self.edit(path, text='local work')
        self.edit(path, name='new.py')
        gitops.git(path, 'add', 'source.txt')
        state = self.check(task)
        self.assertEqual(state['freshness'], 'fresh')
        self.assertTrue(state['local_dirty'])
        self.assertEqual(self.refresh(task)['base_snapshot'], workspace['base_snapshot'])

    def test_clean_refresh_before_new_worker_retains_identity_and_canonical_state(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit(text='latest\r\n')
        self.edit(name='new.py')
        gitops.git(manager._project_root, 'add', 'source.txt')
        before = self.git_state(manager._project_root)
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process) as spawn, patch.object(manager, 'Thread'), \
                patch.object(manager, '_codex_command', return_value='fixed'):
            key = manager.start_task_agent(task['task_id'], 'codex', 'read-only')['agent_id']
        after = self.check(task)
        for field in ('workspace_id', 'workspace_path', 'task_id', 'project_id'):
            self.assertEqual(after[field], workspace[field])
        self.assertNotEqual(after['base_snapshot'], workspace['base_snapshot'])
        self.assertEqual(after['freshness'], 'fresh')
        self.assertEqual((Path(after['workspace_path']) / 'source.txt').read_bytes(), b'latest\r\n')
        self.assertEqual(spawn.call_args.kwargs['cwd'], Path(workspace['workspace_path']))
        self.assertNotIn('source-context invalidation', process.stdin.saved)
        self.assertEqual(manager._store.get_agent_source_context(key), after['base_snapshot'])
        self.assertEqual(self.git_state(manager._project_root), before)

    def test_dirty_stale_blocks_start_without_agent_assignment_or_file_loss(self):
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path'])
        self.edit(path, text='valuable local work')
        self.edit()
        before = self.git_state(manager._project_root)
        with patch.object(manager, '_spawn_process') as spawn:
            error = self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
        spawn.assert_not_called()
        self.assertIn('reconciliation', error['detail'])
        self.assertEqual(manager.agents, {})
        self.assertEqual(manager._store.list_task_assignments(task['task_id']), [])
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'pending')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual((path / 'source.txt').read_text(), 'valuable local work')
        self.assertEqual(self.check(task)['freshness'], 'reconciliation_required')
        self.assertEqual(self.git_state(manager._project_root), before)

    def test_direct_parent_grandchild_sibling_and_root_independence(self):
        parent, independent = self.task(), self.task()
        p = Path(self.workspace(parent)['workspace_path'])
        self.workspace(independent)
        child, sibling = self.task(parent), self.task(parent)
        c = Path(self.workspace(child, 'parent_task_snapshot')['workspace_path'])
        self.workspace(sibling, 'parent_task_snapshot')
        grandchild = self.task(child)
        self.workspace(grandchild, 'parent_task_snapshot')
        self.edit(c, text='child work')
        self.assertEqual(self.check(sibling)['freshness'], 'fresh')
        self.assertEqual(self.check(independent)['freshness'], 'fresh')
        self.assertEqual(self.check(grandchild)['freshness'], 'stale')
        self.refresh(grandchild)
        self.edit(p, text='parent work')
        self.assertEqual(self.check(grandchild)['freshness'], 'fresh')
        self.assertEqual(self.check(child)['freshness'], 'reconciliation_required')
        self.assertEqual(self.check(sibling)['freshness'], 'stale')
        state = self.refresh(sibling)
        self.assertEqual((Path(state['workspace_path']) / 'source.txt').read_text(), 'parent work')
        self.edit(text='canonical work')
        self.assertEqual(self.check(sibling)['freshness'], 'fresh')
        self.assertEqual(self.check(independent)['freshness'], 'stale')

    def test_reply_refreshes_same_session_with_internal_context_and_keeps_task(self):
        key, _, task, workspace = self.waiting()
        self.edit()
        before = self.git_state(manager._project_root)
        result, process, command, popen = self.resume(key, lambda k: manager.reply_agent(k, 'Use the new path'))
        self.assertEqual(result['agent_id'], key)
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(command.call_args.args, ('workspace-write', key))
        self.assertEqual(popen.call_args.kwargs['cwd'], Path(workspace['workspace_path']))
        self.assertIn('Internal Control Center source-context invalidation:', process.stdin.saved)
        self.assertIn('MUST re-read', process.stdin.saved)
        self.assertIn('- source.txt', process.stdin.saved)
        self.assertLess(process.stdin.saved.index('source-context invalidation'), process.stdin.saved.index('User request:'))
        self.assertEqual(manager.agent_tasks[key], task['description'])
        self.assertEqual(self.git_state(manager._project_root), before)

    def test_dirty_waiting_rejects_all_manual_resume_actions_unchanged(self):
        key, process, task, workspace = self.waiting()
        self.edit(Path(workspace['workspace_path']), text='local')
        self.edit()
        history = list(manager.agent_outputs[key])
        with patch.object(manager, '_codex_command', return_value='fixed'), patch.object(manager, '_spawn_process') as spawn:
            for action in (lambda k: manager.reply_agent(k, 'answer'), manager.decide_agent,
                           manager.decide_similar_agent, manager.decide_always_agent):
                with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'reconciliation'):
                    action(key)
        spawn.assert_not_called()
        self.assertIs(manager.agents[key], process)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which path?')
        self.assertEqual(list(manager.agent_outputs[key]), history)
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'waiting')

    def test_dirty_redirect_rejects_before_stopping_running_process(self):
        key, process = self.create()
        manager.agent_sessions[key] = key
        workspace = manager._store.get_task_workspace(self.task_for(key)['task_id'])
        self.edit(Path(workspace['workspace_path']), text='local')
        self.edit()
        with patch.object(manager, '_codex_command', return_value='fixed'), patch.object(manager, '_stop_windows_tree') as stop, \
                patch.object(manager, '_spawn_process') as spawn, self.assertRaisesRegex(ValueError, 'reconciliation'):
            manager.redirect_agent(key, 'correct course')
        stop.assert_not_called()
        spawn.assert_not_called()
        self.assertIs(manager.agents[key], process)
        self.assertEqual(manager.agent_statuses[key], 'running')

    def test_clean_redirect_stops_before_refresh_and_invalidates_context(self):
        key, process = self.create()
        manager.agent_sessions[key] = key
        workspace = manager._store.get_task_workspace(self.task_for(key)['task_id'])
        self.edit()
        def stop(old):
            self.assertIs(old, process)
            self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'original\n')
            old.poll.return_value = 0
        with patch.object(manager, '_stop_windows_tree', side_effect=stop):
            _, replacement, _, _ = self.resume(key, lambda k: manager.redirect_agent(k, 'correct course'))
        self.assertIn('source-context invalidation', replacement.stdin.saved)
        self.assertEqual(manager.agent_sessions[key], key)

    def test_failed_spawn_after_refresh_keeps_context_warning_across_recovery(self):
        key, _, task, workspace = self.waiting()
        self.edit()
        with patch.object(manager, '_spawn_process', side_effect=OSError('failed')), \
                patch.object(manager, '_codex_command', return_value='fixed'), self.assertRaises(OSError):
            manager.reply_agent(key, 'answer')
        self.assertEqual(self.check(task)['freshness'], 'fresh')
        self.assertEqual(manager._store.get_agent_source_context(key), workspace['base_snapshot'])
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.recover()
        _, process, _, _ = self.resume(key, lambda k: manager.reply_agent(k, 'answer'))
        self.assertIn('source-context invalidation', process.stdin.saved)
        self.assertEqual(manager._store.get_agent_source_context(key), self.check(task)['base_snapshot'])

    def test_changed_paths_are_bounded_relative_sorted_and_do_not_leak_contents(self):
        task = self.task()
        self.workspace(task)
        (manager._project_root / 'source.txt').unlink()
        for i in range(freshness.CHANGED_FILES_LIMIT + 5):
            self.edit(name=f'file-{i:03}.py', text='SUPER SECRET CONTENT')
        state = self.check(task)
        paths = state['changed_upstream_files']
        self.assertEqual(len(paths), freshness.CHANGED_FILES_LIMIT)
        self.assertEqual(state['changed_upstream_files_remaining'], 6)
        self.assertEqual(paths, sorted(paths))
        self.assertFalse(any(Path(p).is_absolute() for p in paths))
        self.assertNotIn('SUPER SECRET CONTENT', str(state))
        before = state['base_source_snapshot']
        with patch.object(gitops, 'git', return_value=b'line\ninjection.py\0bad\x1bname.py\0'):
            names, _ = freshness.changed_files(manager._project_root, before, state['upstream_snapshot'])
        self.assertFalse(any('\n' in n or '\x1b' in n for n in names))

    def test_local_race_revalidation_aborts_before_any_overwrite(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit()
        refresh = gitops.refresh_source
        def raced(root, base, tree, revalidate):
            self.edit(Path(root), text='late local work')
            return refresh(root, base, tree, revalidate)
        with patch.object(gitops, 'refresh_source', side_effect=raced), self.assertRaises(ValueError):
            self.refresh(task)
        self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'late local work')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(self.check(task)['freshness'], 'reconciliation_required')

    def test_upstream_race_revalidation_aborts_without_changing_workspace(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit()
        refresh = gitops.refresh_source
        def raced(root, base, tree, revalidate):
            self.edit(text='later upstream')
            return refresh(root, base, tree, revalidate)
        with patch.object(gitops, 'refresh_source', side_effect=raced), self.assertRaises(ValueError):
            self.refresh(task)
        self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'original\n')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_concurrent_starts_refresh_once_and_create_one_assignment(self):
        task = self.task()
        self.workspace(task)
        self.edit()
        barrier = Barrier(2)
        process = self.replacement()
        def start():
            barrier.wait(timeout=5)
            try:
                return manager.start_task_agent(task['task_id'], 'mock', 'read-only')
            except ValueError:
                return None
        with patch.object(manager, '_spawn_process', return_value=process) as spawn, patch.object(manager, 'Thread'), \
                ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: start(), range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(manager._store.list_task_assignments(task['task_id'])), 1)
        self.assertEqual(self.check(task)['freshness'], 'fresh')

    def test_recovery_and_get_recompute_freshness_without_refresh_or_provision(self):
        task, empty = self.task(), self.task()
        workspace = self.workspace(task)
        self.edit()
        manager._store = AgentStore(self.path)
        with patch.object(gitops, 'refresh_source') as refresh, patch.object(manager, '_execution_workspace') as provision:
            state = self.request(f"/tasks/{task['task_id']}/workspace")
            self.assertIsNone(self.request(f"/tasks/{empty['task_id']}/workspace"))
        refresh.assert_not_called()
        provision.assert_not_called()
        self.assertEqual(state['freshness'], 'stale')
        self.assertEqual(state['base_snapshot'], workspace['base_snapshot'])
        self.assertNotIn('workspace_path_key', state)
        self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'original\n')

    def test_refresh_database_failure_does_not_claim_fresh_or_spawn(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit()
        with patch.object(manager._store, 'update_workspace_base', side_effect=sqlite3.OperationalError('disk full')), \
                patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 503)
        spawn.assert_not_called()
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(self.check(task)['freshness'], 'reconciliation_required')

    def test_v8_migration_backfills_session_context_without_git_or_filesystem(self):
        key, _, _, workspace = self.waiting()
        with manager._store._connection() as db:
            db.execute('DROP TABLE agent_source_context')
            db.execute('PRAGMA user_version=8')
        with patch.object(gitops, 'git', side_effect=AssertionError('migration must not call Git')):
            store = AgentStore(self.path)
        self.assertEqual(store.get_agent_source_context(key), workspace['base_snapshot'])
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)

    def test_refresh_preserves_excluded_files_and_does_not_execute_filters(self):
        root = manager._project_root
        self.edit(name='.gitattributes', text='*.txt text eol=crlf filter=unsafe\n')
        self.edit(name='.gitignore', text='runtime.txt\n')
        gitops.git(root, 'config', 'filter.unsafe.smudge', 'command-that-must-never-run')
        gitops.git(root, 'config', 'filter.unsafe.required', 'true')
        task = self.task()
        path = Path(self.workspace(task)['workspace_path'])
        self.edit(path, name='runtime.txt', text='keep runtime')
        self.edit(path, name='.env', text='keep secret')
        self.edit(text='exact LF bytes\n')
        state = self.refresh(task)
        self.assertEqual(state['freshness'], 'fresh')
        self.assertEqual((path / 'source.txt').read_bytes(), b'exact LF bytes\n')
        self.assertEqual((path / 'runtime.txt').read_text(), 'keep runtime')
        self.assertEqual((path / '.env').read_text(), 'keep secret')

    def test_ignored_untracked_collision_is_not_overwritten(self):
        self.edit(name='.gitignore', text='collision.py\n')
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path'])
        self.edit(path, name='collision.py', text='valuable ignored local file')
        self.edit(name='.gitignore', text='')
        self.edit(name='collision.py', text='upstream addition')
        with self.assertRaises(ValueError):
            self.refresh(task)
        self.assertEqual((path / 'collision.py').read_text(), 'valuable ignored local file')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_change_during_refresh_does_not_publish_a_false_baseline(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit()
        refresh = gitops.refresh_source
        def raced(*args):
            snapshot = refresh(*args)
            self.edit(text='newer upstream after checkout')
            return snapshot
        with patch.object(gitops, 'refresh_source', side_effect=raced), self.assertRaises(ValueError):
            self.refresh(task)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(self.check(task)['freshness'], 'reconciliation_required')

    def test_git_guard_protects_local_write_after_optimistic_revalidation(self):
        task = self.task()
        workspace = self.workspace(task)
        self.edit()
        refresh = gitops.refresh_source
        def raced(root, base, tree, revalidate):
            def check_then_race():
                revalidate()
                self.edit(Path(root), text='late local change with different size')
            return refresh(root, base, tree, check_then_race)
        with patch.object(gitops, 'refresh_source', side_effect=raced), self.assertRaises(ValueError):
            self.refresh(task)
        self.assertEqual((Path(workspace['workspace_path']) / 'source.txt').read_text(), 'late local change with different size')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_failed_always_stdin_keeps_warning_for_retry(self):
        key, _, task, workspace = self.waiting()
        self.edit()
        replacement = self.replacement()
        replacement.stdin.write = lambda _: (_ for _ in ()).throw(OSError('delivery failed'))
        with patch.object(manager, '_spawn_process', return_value=replacement), \
                patch.object(manager, '_codex_command', return_value='fixed'), \
                patch.object(manager, '_stop_windows_tree'), patch.object(manager, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            with self.assertRaises(OSError):
                manager.decide_always_agent(key)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertFalse(manager.agent_always_decisions[key].enabled)
        self.assertEqual(manager._store.get_agent_source_context(key), workspace['base_snapshot'])
        _, process, _, _ = self.resume(key, manager.decide_always_agent)
        self.assertIn('source-context invalidation', process.stdin.saved)
        self.assertEqual(self.check(task)['freshness'], 'fresh')

    def test_automatic_similarity_and_always_reject_divergence_without_retry_loop(self):
        for policy_type in ('similar', 'always'):
            with self.subTest(policy=policy_type):
                key, process, task, workspace = self.waiting()
                self.edit(Path(workspace['workspace_path']), text='local divergence')
                self.edit(text=policy_type + ' upstream')
                manager.agent_statuses[key] = 'running'
                if policy_type == 'similar':
                    manager.agent_similar_decisions[key].enabled = True
                    manager.agent_similar_decisions[key].examples = ['Which path?']
                else:
                    manager.agent_always_decisions[key].enabled = True
                with patch.object(manager, '_codex_command', return_value='fixed'), \
                        patch.object(manager, '_spawn_process') as spawn, \
                        self.assertLogs(manager.logger, level='ERROR'):
                    manager._finalize_process(key, process)
                spawn.assert_not_called()
                self.assertEqual(manager.agent_statuses[key], 'waiting')
                self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'waiting')
                with patch.object(manager, '_resume_agent') as resume:
                    manager.get_agent(key)
                    manager.get_agent(key)
                resume.assert_not_called()

    def test_decide_and_similar_refresh_with_same_context_warning(self):
        for index, action in enumerate((manager.decide_agent, manager.decide_similar_agent)):
            with self.subTest(action=action):
                key, _, _, _ = self.waiting()
                self.edit(text=f'upstream decision {index}')
                _, process, _, _ = self.resume(key, action)
                self.assertIn('source-context invalidation', process.stdin.saved)
                self.assertEqual(manager.agent_sessions[key], key)

    def test_clean_refresh_applies_additions_deletions_and_renames(self):
        self.edit(name='untracked.py', text='previous untracked source')
        task = self.task()
        path = Path(self.workspace(task)['workspace_path'])
        (manager._project_root / 'source.txt').rename(manager._project_root / 'renamed.txt')
        (manager._project_root / 'untracked.py').unlink()
        self.edit(name='addition.py', text='new file')
        state = self.check(task)
        self.assertEqual(state['changed_upstream_files'], ['addition.py', 'renamed.txt', 'source.txt', 'untracked.py'])
        state = self.refresh(task)
        self.assertEqual(state['freshness'], 'fresh')
        self.assertFalse((path / 'source.txt').exists())
        self.assertFalse((path / 'untracked.py').exists())
        self.assertEqual((path / 'renamed.txt').read_text(), 'original\n')
        self.assertEqual((path / 'addition.py').read_text(), 'new file')


if __name__ == '__main__':
    unittest.main()
