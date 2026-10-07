from contextlib import closing
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager, work_control
from app.persistence import AgentStore
import test_task_workspaces


class WorkControlTests(unittest.TestCase):
    setUp = test_task_workspaces.TaskWorkspaceTests.setUp
    tearDown = test_task_workspaces.TaskWorkspaceTests.tearDown
    create = test_task_workspaces.TaskWorkspaceTests.create
    replacement = test_task_workspaces.TaskWorkspaceTests.replacement
    output = test_task_workspaces.TaskWorkspaceTests.output
    recover = test_task_workspaces.TaskWorkspaceTests.recover
    resume = test_task_workspaces.TaskWorkspaceTests.resume
    request = test_task_workspaces.TaskWorkspaceTests.request
    task_for = test_task_workspaces.TaskWorkspaceTests.task_for
    task = test_task_workspaces.TaskWorkspaceTests.task
    workspace = test_task_workspaces.TaskWorkspaceTests.workspace
    git_state = test_task_workspaces.TaskWorkspaceTests.git_state

    def control(self, task, intent):
        return manager.control_task(task['task_id'], intent)

    def state(self, task):
        return manager._store.get_task(task['task_id'])

    def waiting(self):
        key, process = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which option?', True)
        return key, process, self.task_for(key)

    def test_pending_pause_resume_preserves_workspace_and_no_worker(self):
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path']) / 'source.txt'
        path.write_bytes(b'local work')
        canonical = self.git_state(manager._project_root)
        with patch.object(manager, '_spawn_process') as spawn:
            result = self.control(task, 'paused')
            self.assertEqual((result['status'], result['control_intent'], result['resume_status']), ('paused', 'paused', 'pending'))
            self.control(task, 'paused')
            result = self.control(task, 'active')
            self.assertEqual((result['status'], result['control_intent'], result['resume_status']), ('pending', 'active', None))
            spawn.assert_not_called()
        self.assertEqual(path.read_bytes(), b'local work')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(self.git_state(manager._project_root), canonical)
        self.assertEqual(manager._store.list_task_assignments(task['task_id']), [])

    def test_running_pause_drains_output_and_ends_assignment_paused(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        def stop():
            self.assertEqual(self.state(task)['control_intent'], 'paused')
            self.assertEqual(self.state(task)['status'], 'in_progress')
            process.poll.return_value = 0
        process.terminate.side_effect = stop
        def drain(**kwargs):
            self.output(key, 'last buffered output\n')
        manager.agent_readers[key].join.side_effect = drain
        result = self.control(task, 'paused')
        self.assertEqual(result['status'], 'paused')
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'paused')
        self.assertIn('last buffered output', manager._store.full_output(key))
        self.assertEqual(self.control(task, 'active')['status'], 'pending')

    def test_waiting_pause_restart_resume_reply_retains_session_question(self):
        key, _, task = self.waiting()
        assignment = manager._store.get_active_assignment_for_task(task['task_id'])
        workspace = manager._store.get_task_workspace(task['task_id'])
        self.control(task, 'paused')
        self.recover()
        self.assertEqual(self.state(task)['status'], 'paused')
        self.assertEqual(self.state(task)['resume_status'], 'waiting')
        self.assertEqual(manager._store.get_active_assignment_for_task(task['task_id']), assignment)
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(self.control(task, 'active')['status'], 'waiting')
        self.resume(key, lambda k: manager.reply_agent(k, 'Option A'))
        self.assertEqual(self.state(task)['status'], 'in_progress')
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_stop_failure_retains_intent_and_retry_finalizes(self):
        for intent in ('paused', 'canceled'):
            with self.subTest(intent=intent):
                key, process = self.create(kind='mock')
                task = self.task_for(key)
                process.terminate.side_effect = OSError('cannot stop')
                with self.assertRaises(OSError):
                    self.control(task, intent)
                self.assertEqual(self.state(task)['control_intent'], intent)
                self.assertEqual(self.state(task)['status'], 'in_progress')
                self.assertEqual(manager.agent_statuses[key], 'running')
                with patch.object(manager, '_spawn_process') as spawn:
                    with self.assertRaises(ValueError):
                        manager.start_task_agent(task['task_id'])
                    spawn.assert_not_called()
                process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
                self.assertEqual(self.control(task, intent)['status'], intent)

    def test_cancel_pending_blocked_paused_preserves_history(self):
        for status in ('pending', 'blocked', 'paused'):
            task = manager._store.create_task('Work', status=status)
            self.assertEqual(self.control(task, 'canceled')['status'], 'canceled')
            self.assertEqual(self.control(task, 'canceled')['control_intent'], 'canceled')
            self.assertEqual(manager._store.list_task_assignments(task['task_id']), [])

    def test_cancel_waiting_and_paused_waiting_ends_association_without_spawn(self):
        for paused in (False, True):
            key, _, task = self.waiting()
            if paused:
                self.control(task, 'paused')
            before = manager.get_agent_output(key)
            with patch.object(manager, '_spawn_process') as spawn:
                self.assertEqual(self.control(task, 'canceled')['status'], 'canceled')
                spawn.assert_not_called()
            self.assertEqual(manager.agent_statuses[key], 'stopped')
            self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'canceled')
            self.assertEqual(manager.get_agent_output(key), before)

    def test_cancel_running_uses_existing_windows_tree_stop(self):
        key, process = self.create()
        task = self.task_for(key)
        def stopped(p):
            self.assertIs(p, process)
            self.assertEqual(self.state(task)['control_intent'], 'canceled')
            p.poll.return_value = 0
        with patch.object(manager, '_stop_windows_tree', side_effect=stopped) as stop:
            self.control(task, 'canceled')
            stop.assert_called_once_with(process)
        self.assertEqual(self.state(task)['status'], 'canceled')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'canceled')

    def test_terminal_rejections_and_cancel_idempotency(self):
        for status in ('completed', 'canceled'):
            task = manager._store.create_task('Work', status=status)
            for intent in ('paused', 'active', 'canceled'):
                if status == 'canceled' and intent == 'canceled':
                    self.control(task, intent)
                else:
                    with self.assertRaises(ValueError):
                        self.control(task, intent)
        with self.assertRaises(ValueError):
            self.control(self.task(), 'active')

    def test_blocked_pause_resume_and_recovery(self):
        task = manager._store.create_task('System blocked', status='blocked')
        with self.assertRaises(ValueError):
            manager.start_task_agent(task['task_id'])
        self.assertEqual(self.control(task, 'paused')['resume_status'], 'blocked')
        self.assertEqual(self.control(task, 'active')['status'], 'blocked')
        self.recover()
        self.assertEqual(self.state(task)['status'], 'blocked')

    def test_stopping_paused_waiting_work_restores_pending_on_resume(self):
        key, _, task = self.waiting()
        self.control(task, 'paused')
        manager.stop_agent(key)
        self.assertEqual(self.state(task)['status'], 'paused')
        self.assertEqual(self.state(task)['resume_status'], 'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))
        self.assertEqual(self.control(task, 'active')['status'], 'pending')

    def test_output_drain_failure_retains_intent_until_finalizer(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        manager.agent_readers[key].is_alive.return_value = True
        with self.assertRaisesRegex(RuntimeError, 'output'):
            self.control(task, 'paused')
        self.assertEqual(self.state(task)['status'], 'in_progress')
        self.assertEqual(self.state(task)['control_intent'], 'paused')
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.output(key, 'final output', True)
        self.assertEqual(self.state(task)['status'], 'paused')
        self.assertIn('final output', manager._store.full_output(key))

    def test_final_persistence_failure_does_not_publish_success_and_can_retry(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        with patch.object(manager._store, 'save_agent', side_effect=sqlite3.OperationalError('disk')), patch.object(manager.changes, 'publish') as publish, self.assertLogs(manager.logger):
            with self.assertRaises(RuntimeError):
                self.control(task, 'canceled')
            publish.assert_not_called()
        self.assertEqual(self.state(task)['control_intent'], 'canceled')
        self.assertEqual(self.state(task)['status'], 'in_progress')
        self.assertEqual(self.control(task, 'canceled')['status'], 'canceled')

    def test_control_preserves_integration_provenance_workspace_and_canonical(self):
        task = self.task()
        workspace = self.workspace(task)
        result = manager.integrate_task(task['task_id'])
        history = manager._store.list_integrations(task['task_id'])
        canonical = self.git_state(manager._project_root)
        path = Path(workspace['workspace_path']) / 'source.txt'
        path.write_bytes(b'keep unfinished work')
        self.control(task, 'paused')
        self.control(task, 'active')
        self.control(task, 'canceled')
        self.recover()
        self.assertEqual(path.read_bytes(), b'keep unfinished work')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(manager._store.list_integrations(task['task_id']), history)
        self.assertEqual(result['status'], 'noop')
        self.assertEqual(self.git_state(manager._project_root), canonical)

    def test_blocked_and_completed_parent_gates_do_not_create_children(self):
        for status in ('blocked', 'completed'):
            key, _, task = self.waiting()
            with manager._store._connection() as db:
                db.execute('UPDATE tasks SET status=? WHERE task_id=?', (status, task['task_id']))
            before = manager._store.list_tasks()
            with patch.object(manager, '_spawn_process') as spawn:
                with self.assertRaises(ValueError):
                    manager.start_agent('child', parent_id=key)
                for action in (manager.decide_agent, manager.decide_similar_agent, manager.decide_always_agent):
                    with self.assertRaises(ValueError):
                        action(key)
                spawn.assert_not_called()
            self.assertEqual(manager._store.list_tasks(), before)

    def test_stop_remains_pending_but_intent_outranks_stop_and_branch(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        manager.stop_agent(key)
        self.assertEqual(self.state(task)['status'], 'pending')
        for intent in ('paused', 'canceled'):
            key, _ = self.create(kind='mock')
            task = self.task_for(key)
            manager._store.request_work_control(task['task_id'], intent)
            manager.stop_branch(key)
            self.assertEqual(self.state(task)['status'], intent)

    def test_completion_committed_first_rejects_control(self):
        for intent in ('paused', 'canceled'):
            key, _ = self.create()
            self.output(key, 'Done', True)
            task = self.task_for(key)
            with self.assertRaises(ValueError):
                self.control(task, intent)
            self.assertEqual(self.state(task)['status'], 'completed')

    def test_assignment_end_respects_committed_intent_and_is_idempotent(self):
        for intent in ('paused', 'canceled'):
            key, _, task = self.waiting()
            assignment = manager._store.get_active_assignment_for_task(task['task_id'])
            manager._store.request_work_control(task['task_id'], intent)
            ended = manager._store.end_assignment(assignment['assignment_id'], 'completed')
            self.assertEqual(ended['ended_reason'], intent)
            self.assertEqual(self.state(task)['status'], intent)
            self.assertEqual(manager._store.end_assignment(assignment['assignment_id'], 'stopped'), ended)

    def test_intent_committed_first_wins_finalizer_and_disables_automatic_decision(self):
        for intent in ('paused', 'canceled'):
            key, process = self.create()
            task = self.task_for(key)
            manager.agent_always_decisions[key].enabled = True
            manager._store.request_work_control(task['task_id'], intent)
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: New question')
            process.poll.return_value = 0
            with patch.object(manager, '_spawn_process') as spawn:
                manager._finalize_process(key, process)
                spawn.assert_not_called()
            self.assertEqual(self.state(task)['status'], intent)
            self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_all_action_gates_before_spawn_or_stdin(self):
        for intent in ('paused', 'canceled'):
            key, _, task = self.waiting()
            manager._store.request_work_control(task['task_id'], intent)
            actions = [lambda: manager.start_task_agent(task['task_id']),
                       lambda: manager.reply_agent(key, 'answer'), lambda: manager.redirect_agent(key, 'change'),
                       lambda: manager.decide_agent(key), lambda: manager.decide_similar_agent(key),
                       lambda: manager.decide_always_agent(key), lambda: manager.start_agent('child', parent_id=key)]
            with patch.object(manager, '_spawn_process') as spawn:
                for action in actions:
                    with self.assertRaises(ValueError):
                        action()
                spawn.assert_not_called()

    def test_subtree_control_replaces_temporary_descendant_guard(self):
        key, process = self.create(kind='mock')
        parent = self.task_for(key)
        child = self.task(parent)
        self.control(parent, 'paused')
        process.terminate.assert_called_once()
        self.assertEqual(self.state(parent)['status'], 'paused')
        self.assertEqual(self.state(child)['status'], 'paused')
        self.control(parent, 'active')
        self.assertEqual(self.state(child)['status'], 'pending')
        self.control(parent, 'canceled')
        self.assertEqual(self.state(child)['status'], 'canceled')

    def test_restart_finalizes_interrupted_intents_and_preserves_terminal_work(self):
        tasks = []
        for intent in ('paused', 'canceled'):
            key, _ = self.create(kind='mock')
            task = self.task_for(key)
            manager._store.request_work_control(task['task_id'], intent)
            tasks.append((task, intent))
        pending = self.task()
        self.control(pending, 'paused')
        tasks.append((pending, 'paused'))
        self.recover()
        for task, intent in tasks:
            self.assertEqual(self.state(task)['status'], intent)
            self.assertEqual(self.state(task)['control_intent'], intent)

    def test_api_representation_missing_conflict_and_no_arbitrary_block(self):
        task = self.task()
        path = '/tasks/' + task['task_id']
        self.assertEqual(self.request(path + '/pause', {})['status'], 'paused')
        self.assertEqual(self.request(path)['control_intent'], 'paused')
        self.assertEqual(self.request(path + '/resume', {})['status'], 'pending')
        self.assertEqual(self.request(path + '/cancel', {})['status'], 'canceled')
        self.request(path + '/resume', {}, 409)
        self.request('/tasks/missing/pause', {}, 404)
        self.request(path + '/block', {}, 404)

    def test_intent_persistence_failure_does_not_stop_or_publish(self):
        key, process = self.create(kind='mock')
        with patch.object(manager._store, 'request_work_control', side_effect=sqlite3.OperationalError('disk')), patch.object(manager.changes, 'publish') as publish:
            with self.assertRaises(sqlite3.Error):
                self.control(self.task_for(key), 'paused')
            process.terminate.assert_not_called()
            publish.assert_not_called()

    def test_final_publication_observes_durable_state(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        observed = []
        with patch.object(manager.changes, 'publish', side_effect=lambda _: observed.append(AgentStore(self.path).get_task(task['task_id'])['status'])):
            self.control(task, 'paused')
        self.assertTrue(observed)
        self.assertEqual(set(observed), {'paused'})

    def downgrade(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('ALTER TABLE tasks DROP COLUMN control_intent')
            db.execute('ALTER TABLE tasks DROP COLUMN resume_status')
            # Restore genuine v10 CHECK constraints, not just its version number.
            for table, old, new in (
                ('tasks', "'waiting','blocked','paused','completed'", "'waiting','completed'"),
                ('task_assignments', "'reassigned','paused','canceled'", "'reassigned','canceled'"),
            ):
                sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                indexes = [r[0] for r in db.execute('SELECT sql FROM sqlite_master WHERE type="index" AND tbl_name=? AND sql IS NOT NULL', (table,))]
                sql = re.sub(r'CREATE TABLE "?'+table+r'"?', 'CREATE TABLE '+table+'_old', sql.replace(old, new), count=1)
                db.execute(sql)
                db.execute(f'INSERT INTO {table}_old SELECT * FROM {table}')
                db.execute(f'DROP TABLE {table}')
                db.execute(f'ALTER TABLE {table}_old RENAME TO {table}')
                for index in indexes:
                    db.execute(index)
            db.execute('PRAGMA user_version=10')

    def test_v10_migration_preserves_history_defaults_and_reopen(self):
        key, _, waiting = self.waiting()
        completed = manager._store.create_task('Done', status='completed')
        canceled = manager._store.create_task('Canceled', status='canceled')
        pending = self.task()
        child = self.task(waiting)
        child_workspace = self.workspace(child, 'parent_task_snapshot')
        running, _ = self.create(kind='mock')
        manager.integrate_task(waiting['task_id'])
        integrations = manager._store.list_integrations(waiting['task_id'])
        projects = manager._store.list_projects()
        context = manager._store.get_agent_source_context(key)
        workspace = manager._store.get_task_workspace(waiting['task_id'])
        history = manager._store.list_task_assignments(waiting['task_id'])
        self.downgrade()
        store = AgentStore(self.path)
        self.assertEqual(store.get_task(waiting['task_id'])['control_intent'], 'active')
        self.assertEqual(store.get_task(completed['task_id'])['status'], 'completed')
        self.assertEqual(store.get_task(canceled['task_id'])['control_intent'], 'canceled')
        self.assertEqual(store.get_task_workspace(waiting['task_id']), workspace)
        self.assertEqual(store.list_task_assignments(waiting['task_id']), history)
        self.assertEqual(store.get_task(pending['task_id'])['status'], 'pending')
        self.assertEqual(store.get_task(child['task_id'])['parent_task_id'], waiting['task_id'])
        self.assertEqual(store.get_task_workspace(child['task_id']), child_workspace)
        self.assertEqual(store.get_task(self.task_for(running)['task_id'])['status'], 'in_progress')
        self.assertEqual(store.list_projects(), projects)
        self.assertEqual(store.list_integrations(waiting['task_id']), integrations)
        self.assertEqual(store.get_agent_source_context(key), context)
        self.assertEqual(store.create_task('Blocked', status='blocked')['status'], 'blocked')
        self.assertEqual(AgentStore(self.path).list_tasks(), store.list_tasks())
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 13)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_migration_rollback_is_atomic(self):
        self.task()
        self.downgrade()
        original = work_control.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('injected')
        with patch.object(work_control, 'migrate', side_effect=fail):
            with self.assertRaises(RuntimeError):
                AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 10)
            self.assertNotIn('control_intent', [r[1] for r in db.execute('PRAGMA table_info(tasks)')])
        AgentStore(self.path)
