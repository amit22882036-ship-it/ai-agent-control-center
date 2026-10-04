import asyncio
import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import UUID

from app import agent_manager as manager
from app.main import app
from app.persistence import AgentStore
import test_persistence


class TaskLifecycleTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    resume = test_persistence.PersistenceTests.resume

    def request(self, path, body=None, expected=200):
        async def run():
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'method': 'POST' if body is not None else 'GET', 'scheme': 'http', 'path': path,
                       'raw_path': path.encode(), 'query_string': b'', 'headers': [(b'content-type', b'application/json')],
                       'root_path': '', 'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000)}, receive, send)
            self.assertEqual(messages[0]['status'], expected, messages)
            return json.loads(b''.join(item.get('body', b'') for item in messages))
        return asyncio.run(run())

    def task_for(self, key):
        return next(t for t in manager._store.list_tasks()
                    if any(a['agent_id'] == key for a in manager._store.list_task_assignments(t['task_id'])))

    def start_request(self, path, body):
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            return self.request(path, body)

    def test_task_api_create_unicode_validation_and_missing(self):
        task = self.request('/tasks', {'title': '  שלום   🐍 ', 'description': '  Description\nשלום  '})
        UUID(task['task_id'])
        self.assertEqual(task['title'], 'שלום 🐍')
        self.assertEqual(task['description'], 'Description\nשלום')
        self.assertEqual(task['status'], 'pending')
        self.assertIsNone(task['current_assignment'])
        self.assertEqual(manager.agents, {})
        self.assertEqual(self.request('/tasks')['tasks'], [task])
        self.assertEqual(self.request('/tasks/' + task['task_id']), task)
        self.assertEqual(self.request('/tasks/' + task['task_id'] + '/assignments'), {'assignments': []})
        for body in [{}, {'title': ' ', 'description': 'text'}, {'title': 'a', 'description': '\n'},
                     {'title': 'a' * 101, 'description': 'text'}, {'title': 'a', 'description': 'x' * 20001}]:
            self.request('/tasks', body, 422)
        self.request('/tasks/missing', expected=404)
        self.request('/tasks/missing/assignments', expected=404)
        self.request('/tasks/missing/start-agent', {}, 404)

    def test_start_existing_task_uses_description_and_independent_identity(self):
        task = self.request('/tasks', {'title': 'Short title', 'description': 'Full work description'})
        result = self.start_request(f"/tasks/{task['task_id']}/start-agent", {})
        key = result['agent_id']
        self.assertNotEqual(key, task['task_id'])
        self.assertEqual(manager.agent_tasks[key], task['description'])
        detail = self.request('/tasks/' + task['task_id'])
        self.assertEqual(detail['status'], 'in_progress')
        self.assertEqual(detail['current_assignment']['agent_id'], key)
        self.assertEqual(detail['current_assignment']['agent_display_color'], 'neutral')
        self.assertEqual(detail['current_assignment']['agent_status'], 'running')
        self.assertEqual(manager.get_agent(key)['task_ids'], [task['task_id']])
        manager.rename_agent(key, 'Worker 🐍')
        self.assertEqual(self.request('/tasks')['tasks'][0]['current_assignment']['agent_display_name'], 'Worker 🐍')
        assignment, = self.request(f"/tasks/{task['task_id']}/assignments")['assignments']
        self.assertIsNone(assignment['ended_at'])
        self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
        for body in [{'agent_type': 'other'}, {'sandbox': 'unsafe'}]:
            self.request(f"/tasks/{task['task_id']}/start-agent", body, 422)

    def test_start_rejects_all_nonpending_and_active_conflict_before_spawn(self):
        for status in ['in_progress', 'waiting', 'completed', 'canceled']:
            task = manager._store.create_task('work')
            with manager._store._connection() as db:
                db.execute('UPDATE tasks SET status=? WHERE task_id=?', (status, task['task_id']))
            with patch.object(manager, '_spawn_process') as spawn:
                self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
                spawn.assert_not_called()
        key, _ = self.create()
        task = self.task_for(key)
        with manager._store._connection() as db:
            db.execute("UPDATE tasks SET status='pending' WHERE task_id=?", (task['task_id'],))
        with patch.object(manager, '_spawn_process') as spawn:
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 409)
            spawn.assert_not_called()

    def test_legacy_and_child_start_remain_compatible_with_independent_tasks(self):
        root = self.start_request('/agents/start', {'task': 'Legacy description'})
        child = self.start_request(f"/agents/{root['agent_id']}/children/start", {'task': 'Child description'})
        self.assertEqual(set(root), {'agent_id', 'status', 'parent_id', 'task', 'display_name', 'display_color', 'agent_type', 'sandbox'})
        self.assertEqual(child['parent_id'], root['agent_id'])
        tasks = manager._store.list_tasks()
        self.assertEqual(len(tasks), 2)
        self.assertEqual([t['description'] for t in tasks], ['Legacy description', 'Child description'])
        self.assertEqual(len({root['agent_id'], child['agent_id'], *(t['task_id'] for t in tasks)}), 4)
        self.assertTrue(all(t['status'] == 'in_progress' for t in tasks))
        self.assertIsNone(tasks[0]['parent_task_id'])
        self.assertEqual(tasks[1]['parent_task_id'], tasks[0]['task_id'])

    def test_wait_reply_decide_similar_always_preserve_assignment(self):
        actions = [lambda key: manager.reply_agent(key, 'Answer'), manager.decide_agent,
                   manager.decide_similar_agent, manager.decide_always_agent]
        for action in actions:
            key, _ = self.create()
            project_id = self.task_for(key)['project_id']
            assignment = manager._store.get_active_assignment_for_agent(key)
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question?\n', True)
            self.assertEqual(self.task_for(key)['status'], 'waiting')
            self.resume(key, action)
            self.assertEqual(self.task_for(key)['status'], 'in_progress')
            self.assertEqual(manager._store.get_active_assignment_for_agent(key), assignment)
            self.assertEqual(self.task_for(key)['project_id'], project_id)

    def test_automatic_similar_and_always_resume_same_task_assignment(self):
        for mode in ['similar', 'always']:
            key, _ = self.create()
            project_id = self.task_for(key)['project_id']
            assignment = manager._store.get_active_assignment_for_agent(key)
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Example\n', True)
            self.resume(key, manager.decide_similar_agent if mode == 'similar' else manager.decide_always_agent)
            self.output(key, 'CONTROL_CENTER_WAITING: New question\n')
            manager.agents[key].poll.return_value = 0
            self.resume(key, manager.get_agent)
            self.assertEqual(self.task_for(key)['status'], 'in_progress')
            self.assertEqual(manager._store.get_active_assignment_for_agent(key), assignment)
            self.assertEqual(self.task_for(key)['project_id'], project_id)

    def test_redirect_and_stale_process_cannot_change_task(self):
        key, old = self.create()
        project_id = self.task_for(key)['project_id']
        projects = manager._store.list_projects()
        self.output(key, f'session id: {key}\n')
        assignment = manager._store.get_active_assignment_for_agent(key)
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(key, lambda key: manager.redirect_agent(key, 'Correct'))
        old.stdout = io.StringIO('codex\nCONTROL_CENTER_WAITING: Stale\n')
        old.poll.return_value = 0
        with patch.object(manager.changes, 'publish') as publish:
            manager._read_output(key, old)
            manager._finalize_process(key, old)
            publish.assert_not_called()
        self.assertEqual(self.task_for(key)['status'], 'in_progress')
        self.assertEqual(manager._store.get_active_assignment_for_agent(key), assignment)
        self.assertEqual(self.task_for(key)['project_id'], project_id)
        self.assertEqual(manager._store.list_projects(), projects)

    def test_completion_stop_branch_and_later_new_worker_history(self):
        root, _ = self.create(kind='mock')
        child, _ = self.create(parent=root, kind='mock')
        finished, _ = self.create(kind='mock')
        self.output(finished, 'Done\n', True)
        self.assertEqual(self.task_for(finished)['status'], 'completed')
        finished_task = self.task_for(finished)['task_id']
        self.assertEqual(manager._store.list_task_assignments(finished_task)[0]['ended_reason'], 'completed')
        manager.stop_branch(root)
        for key in [root, child]:
            task = self.task_for(key)
            self.assertEqual(task['status'], 'pending')
            self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
            self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'stopped')
        task_id = self.task_for(root)['task_id']
        new = self.start_request(f'/tasks/{task_id}/start-agent', {})
        history = self.request(f'/tasks/{task_id}/assignments')['assignments']
        self.assertEqual([a['agent_id'] for a in history], [root, new['agent_id']])
        manager.stop_agent(root)  # Old worker cannot end the new worker's assignment.
        self.assertEqual(manager._store.get_task(task_id)['status'], 'in_progress')

    def test_recovery_waiting_and_running_tasks(self):
        running, _ = self.create()
        waiting, _ = self.create()
        self.output(waiting, f'session id: {waiting}\nCONTROL_CENTER_WAITING: Question\n', True)
        assignment = manager._store.get_active_assignment_for_agent(waiting)
        self.recover()
        self.assertEqual(self.task_for(running)['status'], 'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_agent(running))
        self.assertEqual(self.task_for(waiting)['status'], 'waiting')
        self.assertEqual(manager._store.get_active_assignment_for_agent(waiting), assignment)

    def test_spawn_failure_leaves_pending_without_assignment(self):
        task = self.request('/tasks', {'title': 'Work', 'description': 'Description'})
        with patch.object(manager, '_spawn_process', side_effect=OSError('cannot spawn')):
            self.request(f"/tasks/{task['task_id']}/start-agent", {}, 503)
            self.request('/agents/start', {'task': 'Legacy work'}, 503)
        self.assertTrue(all(t['status'] == 'pending' for t in manager._store.list_tasks()))
        self.assertTrue(all(not manager._store.list_task_assignments(t['task_id']) for t in manager._store.list_tasks()))
        self.assertEqual(manager.agents, {})

    def test_task_transition_failure_rolls_back_agent_output_and_assignment_before_sse(self):
        key, process = self.create(kind='mock')
        self.output(key, '')
        before = manager._store.load_agents()
        task = self.task_for(key)
        assignment = manager._store.get_active_assignment_for_agent(key)
        with manager._store._connection() as db:
            db.execute("""CREATE TRIGGER reject_task BEFORE UPDATE ON tasks
                          BEGIN SELECT RAISE(ABORT, 'unavailable'); END""")
        with patch.object(manager.changes, 'publish') as publish, self.assertLogs(manager.logger):
            manager.agent_outputs[key].append('Final output')
            process.poll.return_value = 0
            manager._finalize_process(key, process)
            publish.assert_not_called()
        self.assertEqual(manager._store.load_agents(), before)
        self.assertEqual(self.task_for(key), task)
        self.assertEqual(manager._store.get_active_assignment_for_agent(key), assignment)
        with manager._store._connection() as db:
            db.execute('DROP TRIGGER reject_task')
        def assert_durable(_):
            self.assertEqual(self.task_for(key)['status'], 'completed')
            self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
            self.assertEqual(manager._store.full_output(key), ['Final output'])
        with patch.object(manager.changes, 'publish', side_effect=assert_durable) as publish:
            manager.get_agent(key)
            publish.assert_called_once()

    def test_failed_initial_commit_stops_worker_and_keeps_pending(self):
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        save = manager._store.save_agent
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError('disk temporarily unavailable')
            return save(*args, **kwargs)
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread'), \
             patch.object(manager._store, 'save_agent', side_effect=fail_once), self.assertLogs(manager.logger):
            self.request('/agents/start', {'task': 'work'}, 503)
        process.terminate.assert_called_once()
        task, = manager._store.list_tasks()
        self.assertEqual(task['status'], 'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'stopped')

    def test_failed_always_prompt_preserves_durable_waiting_assignment(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Question\n', True)
        task = self.task_for(key)
        assignment = manager._store.get_active_assignment_for_agent(key)
        replacement = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed'), \
             patch.object(manager, '_spawn_process', return_value=replacement), patch.object(manager, 'Thread') as thread, \
             patch.object(replacement.stdin, 'write', side_effect=BrokenPipeError('closed')), \
             patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            thread.return_value.is_alive.return_value = False
            with self.assertRaises(BrokenPipeError):
                manager.decide_always_agent(key)
        self.assertEqual(self.task_for(key), task)
        self.assertEqual(manager._store.get_active_assignment_for_agent(key), assignment)

    def test_failed_start_with_already_exited_process_remains_pending(self):
        process = self.replacement()
        process.poll.return_value = 0
        process.stdout.close()
        save = manager._store.save_agent
        attempts = 0
        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError('initial commit failed')
            return save(*args, **kwargs)
        def assert_compensated(key):
            self.assertEqual(manager.agent_statuses[key], 'stopped')
            self.assertEqual(self.task_for(key)['status'], 'pending')
            self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread'), \
             patch.object(manager._store, 'save_agent', side_effect=fail_once), \
             patch.object(manager.changes, 'publish', side_effect=assert_compensated) as publish, self.assertLogs(manager.logger):
            self.request('/agents/start', {'task': 'Work'}, 503)
            publish.assert_called_once()
            self.notify.assert_not_called()
        task, = manager._store.list_tasks()
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'stopped')

    def test_existing_task_codex_prompt_uses_description_and_selected_sandbox(self):
        task = self.request('/tasks', {'title': 'Short title', 'description': 'Inspect "files" & preserve שלום'})
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread'), \
             patch.object(manager, '_codex_command', return_value='fixed') as command:
            result = self.request(f"/tasks/{task['task_id']}/start-agent", {'agent_type': 'codex', 'sandbox': 'workspace-write'})
        command.assert_called_once_with('workspace-write')
        self.assertEqual(process.stdin.saved, manager._codex_prompt(task['description']))
        self.assertEqual(manager.agent_sandboxes[result['agent_id']], 'workspace-write')

    def test_uncertain_initial_commit_compensates_without_duplicate_assignment(self):
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        save = manager._store.save_agent
        calls = 0
        def uncertain(*args, **kwargs):
            nonlocal calls
            calls += 1
            save(*args, **kwargs)
            if calls == 1:
                raise sqlite3.OperationalError('acknowledgment lost')
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread'), \
             patch.object(manager._store, 'save_agent', side_effect=uncertain), self.assertLogs(manager.logger):
            self.request('/agents/start', {'task': 'work'}, 503)
        task, = manager._store.list_tasks()
        assignment, = manager._store.list_task_assignments(task['task_id'])
        self.assertEqual(task['status'], 'pending')
        self.assertEqual(assignment['ended_reason'], 'stopped')
        process.terminate.assert_called_once()

    def test_startup_repairs_inconsistent_task_statuses(self):
        waiting, _ = self.create()
        self.output(waiting, f'session id: {waiting}\nCONTROL_CENTER_WAITING: Question\n', True)
        waiting_task = self.task_for(waiting)
        orphan = manager._store.create_task('unassigned')
        canceled, _ = self.create()
        self.output(canceled, 'CONTROL_CENTER_WAITING: Question\n', True)
        canceled_task = self.task_for(canceled)
        with manager._store._connection() as db:
            db.execute("UPDATE tasks SET status='pending' WHERE task_id=?", (waiting_task['task_id'],))
            db.execute("UPDATE tasks SET status='waiting' WHERE task_id=?", (orphan['task_id'],))
            db.execute("UPDATE tasks SET status='canceled' WHERE task_id=?", (canceled_task['task_id'],))
        self.recover()
        self.assertEqual(manager._store.get_task(waiting_task['task_id'])['status'], 'waiting')
        self.assertEqual(manager._store.get_task(orphan['task_id'])['status'], 'pending')
        self.assertEqual(manager._store.get_task(canceled_task['task_id'])['status'], 'canceled')
        self.assertIsNone(manager._store.get_active_assignment_for_agent(canceled))
        self.assertEqual(manager.agent_statuses[canceled], 'stopped')


class TaskV5MigrationTests(unittest.TestCase):
    setUp = TaskLifecycleTests.setUp
    tearDown = TaskLifecycleTests.tearDown
    create = TaskLifecycleTests.create
    replacement = TaskLifecycleTests.replacement
    output = TaskLifecycleTests.output
    task_for = TaskLifecycleTests.task_for
    def test_v4_mixed_backfill_is_idempotent_and_preserves_all_agent_data(self):
        represented, _ = self.create()
        missing, _ = self.create(parent=represented)
        self.output(missing, f'session id: {missing}\nCONTROL_CENTER_WAITING: Question\n', True)
        manager.rename_agent(missing, 'שלום')
        manager.set_agent_color(missing, 'green')
        before = manager._store.load_agents()
        task = self.task_for(represented)
        missing_task = self.task_for(missing)
        with manager._store._connection() as db:
            db.execute('DELETE FROM task_assignments WHERE task_id=?', (missing_task['task_id'],))
            db.execute('DELETE FROM tasks WHERE task_id=?', (missing_task['task_id'],))
            db.execute('PRAGMA user_version=4')
        store = AgentStore(self.path)
        self.assertEqual(store.load_agents(), before)
        self.assertEqual(store.get_task(task['task_id']), task)
        self.assertEqual(len(store.list_tasks()), 2)
        self.assertEqual(store.name_history(missing), manager._store.name_history(missing))
        self.assertEqual(store.get_task(store.get_active_assignment_for_agent(missing)['task_id'])['status'], 'waiting')
        self.assertEqual(AgentStore(self.path).list_tasks(), store.list_tasks())

    def test_v4_backfill_failure_rolls_back_without_touching_represented_tasks(self):
        represented, _ = self.create()
        missing, _ = self.create()
        task = self.task_for(represented)
        missing_task = self.task_for(missing)
        with manager._store._connection() as db:
            db.execute('DELETE FROM task_assignments WHERE task_id=?', (missing_task['task_id'],))
            db.execute('DELETE FROM tasks WHERE task_id=?', (missing_task['task_id'],))
            db.execute('PRAGMA user_version=4')
            db.execute("""CREATE TRIGGER reject_backfill BEFORE INSERT ON task_assignments
                          BEGIN SELECT RAISE(ABORT, 'unavailable'); END""")
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)
        self.assertEqual(manager._store.list_tasks(), [task])
        with manager._store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)

    def test_v4_backfill_maps_each_missing_agent_and_keeps_ended_history(self):
        keys = {}
        for status in ['running', 'waiting', 'finished', 'stopped']:
            key, _ = self.create()
            keys[status] = key
            manager.agent_statuses[key] = status
            manager._save_agent(key)
        represented_task = self.task_for(keys['stopped'])
        with manager._store._connection() as db:
            db.execute('DELETE FROM task_assignments WHERE agent_id<>?', (keys['stopped'],))
            db.execute('DELETE FROM tasks WHERE task_id<>?', (represented_task['task_id'],))
            db.execute('PRAGMA user_version=4')
        store = AgentStore(self.path)
        self.assertEqual(len(store.list_tasks()), 4)
        expected = {'running': 'in_progress', 'waiting': 'waiting', 'finished': 'completed', 'stopped': 'pending'}
        for status, key in keys.items():
            task = next(t for t in store.list_tasks() if store.list_task_assignments(t['task_id'])[0]['agent_id'] == key)
            self.assertEqual(task['status'], expected[status])
        self.assertEqual(store.get_task(represented_task['task_id']), represented_task)

