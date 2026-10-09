from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, dependencies, workspace_git as gitops
from app.main import app
from app.persistence import AgentStore
import test_work_control


class DependencyTests(unittest.TestCase):
    setUp = test_work_control.WorkControlTests.setUp
    tearDown = test_work_control.WorkControlTests.tearDown
    create = test_work_control.WorkControlTests.create
    replacement = test_work_control.WorkControlTests.replacement
    output = test_work_control.WorkControlTests.output
    recover = test_work_control.WorkControlTests.recover
    resume = test_work_control.WorkControlTests.resume
    task_for = test_work_control.WorkControlTests.task_for
    task = test_work_control.WorkControlTests.task
    workspace = test_work_control.WorkControlTests.workspace
    git_state = test_work_control.WorkControlTests.git_state
    waiting = test_work_control.WorkControlTests.waiting
    control = test_work_control.WorkControlTests.control
    state = test_work_control.WorkControlTests.state

    def edge(self, dependent, source, remove=False):
        return manager.change_dependency(dependent['task_id'], source['task_id'], remove)

    def status(self, task, status):
        # Internal fixture transitions. Real process finalization is tested separately.
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE tasks SET status=? WHERE task_id=?', (status, task['task_id']))
            dependencies.reconcile(db)

    def reasons(self, task):
        return {b['reason_code'] for b in self.state(task)['active_blockers']}

    def request(self, path, method='GET', body=None, expected=200):
        async def run():
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                       'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
                       'headers': [(b'content-type', b'application/json')], 'root_path': '',
                       'client': ('127.0.0.1', 1), 'server': ('localhost', 8000)}, receive, send)
            self.assertEqual(messages[0]['status'], expected, messages)
            return json.loads(b''.join(m.get('body', b'') for m in messages))
        return asyncio.run(run())

    def test_same_project_siblings_parent_child_and_cross_tree(self):
        parent, other = self.task(), self.task()
        a, b = self.task(parent), self.task(parent)
        self.edge(a, b)
        self.edge(parent, a)
        self.edge(other, a)
        self.assertEqual(self.state(parent)['status'], 'blocked')
        self.assertEqual(self.state(other)['status'], 'blocked')
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(len(manager._store.list_dependencies(a['task_id'], reverse=True)), 2)

    def test_cross_project_unknown_and_terminal_rejected(self):
        a, b = self.task(), self.task()
        path = Path(self.temp.name) / 'other'
        path.mkdir()
        project = manager._store.create_project('Other', path)
        foreign = manager._store.create_task('Elsewhere', project_id=project['project_id'])
        with self.assertRaisesRegex(ValueError, 'Project'):
            self.edge(a, foreign)
        with self.assertRaises(LookupError):
            manager.change_dependency(a['task_id'], 'missing')
        for state in ('completed', 'canceled'):
            terminal = manager._store.create_task('Terminal', status=state)
            with self.assertRaises(ValueError):
                self.edge(terminal, b)
        self.assertEqual(manager._store.list_dependencies(a['task_id']), [])

    def test_self_direct_deep_cycles_and_duplicate_are_safe(self):
        nodes = [self.task() for _ in range(40)]
        for source, dependent in zip(nodes, nodes[1:]):
            self.edge(dependent, source)
        for dependent, source in [(nodes[0], nodes[0]), (nodes[0], nodes[1]), (nodes[0], nodes[-1])]:
            with self.assertRaisesRegex(ValueError, 'cycle'):
                self.edge(dependent, source)
        before = manager._store.list_dependencies(nodes[1]['task_id'])
        self.edge(nodes[1], nodes[0])
        self.assertEqual(manager._store.list_dependencies(nodes[1]['task_id']), before)

    def test_inverse_edge_race_uses_independent_store_transactions(self):
        a, b = self.task(), self.task()
        barrier = Barrier(2)
        def add(pair):
            store = AgentStore(self.path)
            barrier.wait()
            try:
                store.change_dependency(pair[0]['task_id'], pair[1]['task_id'])
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(add, [(a, b), (b, a)]))
        self.assertEqual(sorted(results), [False, True])

    def test_pending_blocks_and_real_completion_unblocks_before_publication(self):
        key, _ = self.create(kind='mock')
        a, b = self.task_for(key), self.task()
        self.edge(b, a)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(self.reasons(b), {'dependency_incomplete'})
        states = []
        with patch.object(manager.changes, 'publish', side_effect=lambda _: states.append(self.state(b)['status'])), patch.object(manager, '_spawn_process') as spawn:
            self.output(key, 'Done', True)
            spawn.assert_not_called()
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(states[-1], 'pending')
        self.assertEqual(self.reasons(b), set())

    def test_waiting_restoration_keeps_question_session_and_reply(self):
        key, _, b = self.waiting()
        a = self.task()
        workspace = manager._store.get_task_workspace(b['task_id'])
        assignment = manager._store.get_active_assignment_for_task(b['task_id'])
        self.edge(b, a)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')
        self.recover()
        self.status(a, 'completed')
        self.assertEqual(self.state(b)['status'], 'waiting')
        self.assertEqual(manager._store.get_active_assignment_for_task(b['task_id']), assignment)
        self.resume(key, lambda k: manager.reply_agent(k, 'A'))
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager._store.get_task_workspace(b['task_id']), workspace)

    def test_running_invalidation_stops_drains_ends_blocked_and_restores_pending(self):
        key, process = self.create(kind='mock')
        b, a = self.task_for(key), self.task()
        workspace = manager._store.get_task_workspace(b['task_id'])
        def stop():
            self.assertEqual(self.reasons(b), {'dependency_incomplete'})
            self.assertTrue(self.state(b)['stop_required'])
            process.poll.return_value = 0
        process.terminate.side_effect = stop
        manager.agent_readers[key].join.side_effect = lambda **_: self.output(key, 'final buffered line')
        self.edge(b, a)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(manager._store.list_task_assignments(b['task_id'])[0]['ended_reason'], 'blocked')
        self.assertIn('final buffered line', manager._store.full_output(key))
        self.assertEqual(manager._store.get_task_workspace(b['task_id']), workspace)
        self.edge(b, a, True)
        self.assertEqual(self.state(b)['status'], 'pending')

    def test_satisfied_dependency_does_not_stop_or_disturb_runtime(self):
        key, process = self.create(kind='mock')
        b = self.task_for(key)
        a = manager._store.create_task('Done', status='completed')
        self.edge(b, a)
        process.terminate.assert_not_called()
        self.assertEqual(self.state(b)['status'], 'in_progress')
        self.assertEqual(self.reasons(b), set())

    def test_multiple_dependencies_and_source_specific_removal(self):
        a, b, c = self.task(), self.task(), self.task()
        self.edge(c, a)
        self.edge(c, b)
        self.status(a, 'completed')
        self.assertEqual(self.state(c)['status'], 'blocked')
        self.control(b, 'canceled')
        self.assertTrue(self.state(c)['replanning_required'])
        self.assertEqual(self.reasons(c), {'dependency_canceled'})
        self.edge(c, b, True)
        self.assertEqual(self.state(c)['status'], 'pending')
        self.assertFalse(self.state(c)['replanning_required'])

    def test_pause_dependency_reason_changes_without_replanning(self):
        a, b = self.task(), self.task()
        self.edge(b, a)
        self.control(a, 'paused')
        self.assertEqual(self.reasons(b), {'dependency_paused'})
        self.assertFalse(self.state(b)['replanning_required'])
        self.control(a, 'active')
        self.assertEqual(self.reasons(b), {'dependency_incomplete'})
        self.status(a, 'completed')
        self.assertEqual(self.state(b)['status'], 'pending')

    def test_paused_blocked_rechecks_resolved_or_remaining_dependencies(self):
        for resolves in (False, True):
            a, b = self.task(), self.task()
            self.edge(b, a)
            self.control(b, 'paused')
            if resolves:
                self.status(a, 'completed')
            self.assertEqual(self.state(b)['status'], 'paused')
            self.control(b, 'active')
            self.assertEqual(self.state(b)['status'], 'pending' if resolves else 'blocked')

    def test_paused_blocked_waiting_restores_waiting(self):
        key, _, b = self.waiting()
        a = self.task()
        self.edge(b, a)
        self.control(b, 'paused')
        self.recover()
        self.status(a, 'completed')
        self.control(b, 'active')
        self.assertEqual(self.state(b)['status'], 'waiting')
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertIsNotNone(manager._store.get_active_assignment_for_task(b['task_id']))

    def test_generic_hard_blocker_and_unrelated_replan_survive_edge_removal(self):
        a, b = self.task(), self.task()
        self.control(a, 'canceled')
        self.edge(b, a)
        with manager._store._connection() as db:
            db.execute("INSERT INTO task_blockers(id,task_id,blocker_type,reason_code) VALUES (?,?,'resource','resource_busy')", (str(uuid4()), b['task_id']))
            db.execute("INSERT INTO task_replan_reasons(id,task_id,reason_type,reason_code) VALUES (?,?,'other','plan_review')", (str(uuid4()), b['task_id']))
        self.edge(b, a, True)
        self.assertEqual(self.reasons(b), {'resource_busy'})
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual([r['reason_code'] for r in self.state(b)['replanning_reasons']], ['plan_review'])

    def test_cancel_dependent_never_revives_when_dependency_resolves(self):
        a, b = self.task(), self.task()
        self.edge(b, a)
        self.control(b, 'canceled')
        self.status(a, 'completed')
        self.edge(b, a, True)
        self.assertEqual(self.state(b)['status'], 'canceled')

    def test_blockers_gate_all_continuations_even_if_status_is_not_blocked(self):
        key, _, b = self.waiting()
        a = self.task()
        self.edge(b, a)
        with manager._store._connection() as db:
            db.execute("UPDATE tasks SET status='waiting' WHERE task_id=?", (b['task_id'],))
        with patch.object(manager, '_spawn_process') as spawn:
            for action in [lambda: manager.start_task_agent(b['task_id']), lambda: manager.reply_agent(key, 'A'),
                           lambda: manager.redirect_agent(key, 'change'), lambda: manager.decide_agent(key),
                           lambda: manager.decide_similar_agent(key), lambda: manager.decide_always_agent(key),
                           lambda: manager.start_agent('child', 'mock', parent_id=key)]:
                with self.assertRaises(ValueError):
                    action()
            spawn.assert_not_called()

    def test_pause_subtree_preserves_terminal_and_waiting_members(self):
        parent, _ = self.create(kind='mock')
        pending = self.task(self.task_for(parent))
        running, process = self.create(parent, kind='mock')
        waiting, _ = self.create(parent)
        done, _ = self.create(parent, kind='mock')
        self.output(waiting, f'session id: {waiting}\nCONTROL_CENTER_WAITING: Keep?', True)
        self.output(done, 'Done', True)
        task = self.task_for(parent)
        self.control(task, 'paused')
        self.assertEqual(self.state(pending)['status'], 'paused')
        process.terminate.assert_called_once()
        self.assertEqual(self.task_for(done)['status'], 'completed')
        self.assertEqual(self.task_for(waiting)['status'], 'paused')
        self.assertEqual(manager.agent_waiting_questions[waiting], 'Keep?')
        self.control(task, 'active')
        self.assertEqual(self.task_for(waiting)['status'], 'waiting')
        self.assertEqual(self.task_for(running)['status'], 'pending')
        self.assertEqual(self.state(pending)['status'], 'pending')

    def test_independent_pause_and_nested_overlapping_operations(self):
        root = self.task()
        child = self.task(root)
        grandchild = self.task(child)
        self.control(child, 'paused')
        self.control(root, 'paused')
        self.control(root, 'active')
        self.assertEqual(self.state(root)['status'], 'pending')
        self.assertEqual(self.state(child)['status'], 'paused')
        self.assertEqual(self.state(grandchild)['status'], 'paused')
        self.control(child, 'active')
        self.assertEqual(self.state(grandchild)['status'], 'pending')

    def test_child_cannot_release_ancestor_pause_and_new_independent_pause_survives(self):
        root = self.task()
        child = self.task(root)
        self.control(root, 'paused')
        with self.assertRaises(ValueError):
            self.control(child, 'active')
        self.control(child, 'paused')
        self.control(root, 'active')
        self.assertEqual(self.state(child)['status'], 'paused')
        self.control(child, 'active')
        self.assertEqual(self.state(child)['status'], 'pending')

    def test_pause_subtree_blocks_external_without_pausing_or_replanning(self):
        root = self.task()
        child = self.task(root)
        external = self.task()
        self.edge(external, child)
        self.control(root, 'paused')
        self.assertEqual(self.state(external)['status'], 'blocked')
        self.assertEqual(self.state(external)['control_intent'], 'active')
        self.assertEqual(self.reasons(external), {'dependency_paused'})
        self.assertFalse(self.state(external)['replanning_required'])

    def test_cancel_subtree_preserves_completed_and_blocks_external(self):
        root, _ = self.create(kind='mock')
        child, process = self.create(root, kind='mock')
        waiting, _ = self.create(root)
        done, _ = self.create(root, kind='mock')
        self.output(waiting, 'CONTROL_CENTER_WAITING: Question?', True)
        self.output(done, 'Done', True)
        external = self.task()
        self.edge(external, self.task_for(child))
        self.control(self.task_for(root), 'canceled')
        process.terminate.assert_called_once()
        self.assertEqual(self.task_for(done)['status'], 'completed')
        self.assertEqual(self.task_for(waiting)['status'], 'canceled')
        self.assertEqual(self.state(external)['status'], 'blocked')
        self.assertTrue(self.state(external)['replanning_required'])
        self.assertEqual(self.state(external)['control_intent'], 'active')

    def test_deep_child_cancel_signals_ancestors_without_implicit_blocking(self):
        root = self.task()
        child = self.task(root)
        grandchild = self.task(child)
        self.control(grandchild, 'canceled')
        for ancestor in (root, child):
            self.assertEqual(self.state(ancestor)['status'], 'pending')
            self.assertTrue(self.state(ancestor)['replanning_required'])
            self.assertEqual(self.state(ancestor)['replanning_reasons'][0]['source_task_id'], grandchild['task_id'])
        self.edge(root, grandchild)
        self.edge(root, grandchild, True)
        self.assertEqual(self.state(root)['status'], 'pending')
        self.assertEqual([r['reason_code'] for r in self.state(root)['replanning_reasons']], ['child_canceled'])

    def test_preview_is_read_only_and_apply_recomputes_new_graph(self):
        root = self.task()
        before = manager._store.list_tasks()
        preview = manager._store.control_impact(root['task_id'], 'cancel')
        self.assertEqual(manager._store.list_tasks(), before)
        self.assertEqual(manager._store.control_operations(root['task_id']), [])
        child = self.task(root)
        b, c = self.task(), self.task()
        self.edge(b, child)
        self.edge(c, child)
        self.assertEqual(preview['descendant_task_ids'], [])
        current = manager._store.control_impact(root['task_id'], 'cancel')
        self.assertEqual(set(current['external_dependent_task_ids']), {b['task_id'], c['task_id']})
        self.control(root, 'canceled')
        self.assertEqual(self.state(child)['status'], 'canceled')
        self.assertTrue(self.state(b)['replanning_required'])

    def test_partial_subtree_stop_failure_continues_other_stops_and_recovers(self):
        root, p = self.create(kind='mock')
        child, failed = self.create(root, kind='mock')
        failed.terminate.side_effect = OSError('stop failure')
        task = self.task_for(root)
        with self.assertRaises(OSError):
            self.control(task, 'paused')
        p.terminate.assert_called_once()
        self.assertEqual(self.state(task)['status'], 'paused')
        self.assertEqual(self.task_for(child)['status'], 'in_progress')
        self.assertEqual(self.task_for(child)['control_intent'], 'paused')
        self.assertEqual(manager._store.control_operations(task['task_id'])[-1]['status'], 'recovery_required')
        with self.assertRaises(ValueError):
            self.control(task, 'active')
        self.recover()
        self.assertEqual(self.task_for(child)['status'], 'paused')
        self.assertEqual(manager._store.control_operations(task['task_id'])[-1]['status'], 'completed')

    def test_dependency_stop_failure_blocks_actions_and_later_exit_finalizes(self):
        key, process = self.create(kind='mock')
        b, a = self.task_for(key), self.task()
        process.terminate.side_effect = OSError('stop failure')
        with self.assertRaises(OSError):
            self.edge(b, a)
        self.assertEqual(self.state(b)['status'], 'in_progress')
        self.assertTrue(self.state(b)['stop_required'])
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.output(key, 'Done', True)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_removal_or_completion_during_stop_keeps_invalidation_authority(self):
        for remove in (False, True):
            key, process = self.create(kind='mock')
            b, a = self.task_for(key), self.task()
            def stop():
                if remove:
                    manager._store.change_dependency(b['task_id'], a['task_id'], True)
                else:
                    self.status(a, 'completed')
                self.assertTrue(self.state(b)['stop_required'])
                process.poll.return_value = 0
            process.terminate.side_effect = stop
            self.edge(b, a)
            self.assertEqual(self.state(b)['status'], 'pending')
            self.assertEqual(manager._store.list_task_assignments(b['task_id'])[0]['ended_reason'], 'blocked')

    def test_completion_first_rejects_edge_and_control_first_wins_child_finalizer(self):
        key, _ = self.create(kind='mock')
        self.output(key, 'Done', True)
        with self.assertRaises(ValueError):
            self.edge(self.task_for(key), self.task())
        for intent in ('paused', 'canceled'):
            root, _ = self.create(kind='mock')
            child, _ = self.create(root, kind='mock')
            manager._store.request_work_control(self.task_for(root)['task_id'], intent)
            self.output(child, 'Done', True)
            self.assertEqual(self.task_for(child)['status'], intent)

    def test_overlapping_pause_transactions_retain_both_owners(self):
        root = self.task()
        child = self.task(root)
        barrier = Barrier(2)
        def pause(task):
            store = AgentStore(self.path)
            barrier.wait()
            store.request_work_control(task['task_id'], 'paused')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(pause, [root, child]))
        manager._settle_tasks([root['task_id'], child['task_id']])
        self.control(root, 'active')
        self.assertEqual(self.state(child)['status'], 'paused')

    def test_restart_preserves_graph_multiple_reasons_and_independent_pauses(self):
        root = self.task()
        child = self.task(root)
        a, b = self.task(), self.task()
        self.edge(b, a)
        self.edge(b, child)
        self.control(child, 'paused')
        self.control(root, 'paused')
        self.control(a, 'canceled')
        before = self.state(b)
        self.recover()
        self.assertEqual(self.state(b), before)
        self.control(root, 'active')
        self.assertEqual(self.state(child)['status'], 'paused')
        self.assertTrue(self.state(b)['replanning_required'])

    def test_dependency_and_control_apis(self):
        a, b = self.task(), self.task()
        path = '/tasks/' + b['task_id']
        self.request(path + '/dependencies', 'POST', {'depends_on_task_id': a['task_id']})
        self.assertEqual(len(self.request(path + '/dependencies')['dependencies']), 1)
        self.assertEqual(len(self.request('/tasks/' + a['task_id'] + '/dependents')['dependents']), 1)
        preview = self.request(path + '/control-impact', 'POST', {'action': 'pause'})
        self.assertEqual(preview['target_task_id'], b['task_id'])
        self.request(path + '/control-impact', 'POST', {'action': 'unsafe'}, 422)
        self.request(path + '/dependencies/' + a['task_id'], 'DELETE')
        self.assertEqual(self.request(path)['status'], 'pending')
        self.request('/tasks/missing/dependencies', expected=404)

    def test_graph_commit_failure_has_no_worker_or_publication_side_effect(self):
        key, process = self.create(kind='mock')
        b, a = self.task_for(key), self.task()
        with manager._store._connection() as db:
            db.execute("CREATE TRIGGER reject_block BEFORE INSERT ON task_blockers BEGIN SELECT RAISE(ABORT,'fail'); END")
        with patch.object(manager.changes, 'publish') as publish:
            with self.assertRaises(sqlite3.Error):
                self.edge(b, a)
            publish.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(manager._store.list_dependencies(b['task_id']), [])

    def test_v11_migration_retains_existing_records_without_invented_graph(self):
        key, _, waiting = self.waiting()
        self.control(waiting, 'paused')
        workspace = manager._store.get_task_workspace(waiting['task_id'])
        manager.integrate_task(waiting['task_id'])
        history = manager._store.list_integrations(waiting['task_id'])
        context = manager._store.get_agent_source_context(key)
        child = self.task()
        with manager._store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (waiting['task_id'], child['task_id']))
            db.execute('UPDATE task_assignments SET rowid=100')
        records = manager._store.load_agents()
        assignments = manager._store.list_task_assignments(waiting['task_id'])
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ('task_control_members', 'task_control_operations', 'task_replan_reasons', 'task_blockers', 'task_dependencies'):
                db.execute('DROP TABLE ' + table)
            for column in ('block_resume_status', 'stop_required', 'legacy_pause'):
                db.execute('ALTER TABLE tasks DROP COLUMN ' + column)
            # Reconstruct the actual v11 assignment constraint, including a rowid
            # gap so migration cannot silently renumber assignment chronology.
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
            indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='task_assignments' AND type='index' AND sql IS NOT NULL")]
            sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v11', sql, count=1)
            db.execute(sql.replace("'blocked',", ''))
            columns = ','.join('"' + r[1] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
            db.execute(f'INSERT INTO assignments_v11(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
            db.execute('DROP TABLE task_assignments')
            db.execute('ALTER TABLE assignments_v11 RENAME TO task_assignments')
            for index in indexes:
                db.execute(index)
            db.execute('PRAGMA user_version=11')
        store = AgentStore(self.path)
        self.assertEqual(store.get_task(waiting['task_id'])['status'], 'paused')
        self.assertEqual(store.get_task(waiting['task_id'])['resume_status'], 'waiting')
        self.assertEqual(store.get_task_workspace(waiting['task_id']), workspace)
        self.assertEqual(store.list_integrations(waiting['task_id']), history)
        self.assertEqual(store.get_agent_source_context(key), context)
        self.assertEqual(store.load_agents(), records)
        self.assertEqual(store.list_task_assignments(waiting['task_id']), assignments)
        self.assertEqual(store.get_task(child['task_id'])['parent_task_id'], waiting['task_id'])
        self.assertEqual(store.list_dependencies(waiting['task_id']), [])
        self.assertEqual(store.get_task(waiting['task_id'])['active_blockers'], [])
        self.assertEqual(store.get_task(waiting['task_id'])['replanning_reasons'], [])
        self.assertEqual(store.list_tasks(), AgentStore(self.path).list_tasks())
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 19)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT rowid FROM task_assignments').fetchone()[0], 100)
        self.recover()
        # Repeated explicit Pause adopts the old independent pause into an op.
        self.control(waiting, 'paused')
        self.control(waiting, 'active')
        self.assertEqual(self.state(waiting)['status'], 'waiting')

    def test_migration_failure_rolls_back_graph_tables_and_version(self):
        task = self.task()
        with closing(sqlite3.connect(self.path)) as db, db:
            for table in ('task_control_members', 'task_control_operations', 'task_replan_reasons', 'task_blockers', 'task_dependencies'):
                db.execute('DROP TABLE ' + table)
            for column in ('block_resume_status', 'stop_required', 'legacy_pause'):
                db.execute('ALTER TABLE tasks DROP COLUMN ' + column)
            db.execute('PRAGMA user_version=11')
        original = dependencies.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('injected')
        with patch.object(dependencies, 'migrate', side_effect=fail):
            with self.assertRaises(RuntimeError):
                AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 11)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='task_dependencies'").fetchone())
            self.assertEqual(db.execute('SELECT task_id FROM tasks').fetchone()[0], task['task_id'])

    def test_multiple_dependencies_require_every_completion(self):
        a, b, c = self.task(), self.task(), self.task()
        self.edge(c, a)
        self.edge(c, b)
        self.status(a, 'completed')
        self.assertEqual(self.state(c)['status'], 'blocked')
        with patch.object(manager, '_spawn_process') as spawn:
            self.status(b, 'completed')
            self.assertEqual(self.state(c)['status'], 'pending')
            spawn.assert_not_called()

    def test_cancel_stop_failure_recovery_preserves_successes_and_workspace_bytes(self):
        root, process = self.create(kind='mock')
        child, failed = self.create(root, kind='mock')
        task = self.task_for(root)
        workspace = manager._store.get_task_workspace(task['task_id'])
        path = Path(workspace['workspace_path']) / 'source.txt'
        path.write_bytes(b'unsaved work must remain')
        canonical = self.git_state(manager._project_root)
        failed.terminate.side_effect = OSError('stop failed')
        with self.assertRaises(OSError):
            self.control(task, 'canceled')
        process.terminate.assert_called_once()
        self.assertEqual(self.state(task)['status'], 'canceled')
        self.assertEqual(self.task_for(child)['status'], 'in_progress')
        self.assertEqual(manager._store.control_operations(task['task_id'])[-1]['status'], 'recovery_required')
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            spawn.assert_not_called()
        self.assertEqual(self.task_for(child)['status'], 'canceled')
        self.assertEqual(manager._store.control_operations(task['task_id'])[-1]['status'], 'completed')
        self.assertEqual(path.read_bytes(), b'unsaved work must remain')
        self.assertEqual(self.git_state(manager._project_root), canonical)

    def test_prerequisite_cancel_before_dependent_exit_keeps_blocking_authority(self):
        key, process = self.create(kind='mock')
        b, a = self.task_for(key), self.task()
        # Phase 1 won before the output finalizer obtained authority.
        manager._store.change_dependency(b['task_id'], a['task_id'])
        manager._store.request_work_control(a['task_id'], 'canceled')
        self.output(key, 'Done', True)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(self.reasons(b), {'dependency_canceled'})
        self.assertTrue(self.state(b)['replanning_required'])
        self.assertEqual(manager._store.list_task_assignments(b['task_id'])[0]['ended_reason'], 'blocked')
        process.terminate.assert_not_called()

    def test_child_completion_after_preview_before_control_remains_completed(self):
        for intent in ('paused', 'canceled'):
            root, _ = self.create(kind='mock')
            child, _ = self.create(root, kind='mock')
            task = self.task_for(root)
            manager._store.control_impact(task['task_id'], 'pause' if intent == 'paused' else 'cancel')
            self.output(child, 'Done', True)
            self.control(task, intent)
            self.assertEqual(self.task_for(child)['status'], 'completed')
            operation = manager._store.control_operations(task['task_id'])[-1]
            self.assertIn(self.task_for(child)['task_id'], operation['impact']['completed_descendants'])

    def test_edge_removal_and_reconciliation_are_serialized(self):
        a, b = self.task(), self.task()
        self.edge(b, a)
        barrier = Barrier(2)
        def reconcile():
            store = AgentStore(self.path)
            barrier.wait()
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                dependencies.reconcile(db)
        def remove():
            store = AgentStore(self.path)
            barrier.wait()
            store.change_dependency(b['task_id'], a['task_id'], True)
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(reconcile), pool.submit(remove)]
            for future in futures:
                future.result()
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(self.reasons(b), set())

    def test_preview_reports_direct_replanning_and_running_workers_only(self):
        key, _ = self.create(kind='mock')
        root = self.task_for(key)
        completed = self.task(root)
        self.status(completed, 'completed')
        b, c = self.task(), self.task()
        self.edge(b, root)
        self.edge(c, b)
        before = manager._store.list_tasks()
        plan = manager._store.control_impact(root['task_id'], 'cancel')
        self.assertEqual(manager._store.list_tasks(), before)
        self.assertEqual(plan['workers_to_stop'], [{'task_id': root['task_id'], 'agent_id': key}])
        self.assertEqual(plan['completed_descendants'], [completed['task_id']])
        self.assertEqual(plan['blocked_task_ids'], [b['task_id']])
        self.assertEqual(plan['replanning_task_ids'], [b['task_id']])
        self.control(root, 'canceled')
        self.assertFalse(self.state(c)['replanning_required'])

    def test_direct_assignment_completion_cannot_override_dependency_latch(self):
        key, _ = self.create(kind='mock')
        b, a = self.task_for(key), self.task()
        manager._store.change_dependency(b['task_id'], a['task_id'])
        assignment = manager._store.get_active_assignment_for_task(b['task_id'])
        ended = manager._store.end_assignment(assignment['assignment_id'], 'completed')
        self.assertEqual(ended['ended_reason'], 'blocked')
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertFalse(self.state(b)['stop_required'])
        self.status(a, 'completed')
        self.assertEqual(self.state(b)['status'], 'pending')

    def test_dependency_changes_preserve_dirty_workspace_index_and_integration_history(self):
        key, _, b = self.waiting()
        a = self.task()
        workspace = manager._store.get_task_workspace(b['task_id'])
        manager.integrate_task(b['task_id'])
        history = manager._store.list_integrations(b['task_id'])
        path = Path(workspace['workspace_path']) / 'source.txt'
        path.write_bytes(b'dirty task content')
        canonical = self.git_state(manager._project_root)
        root = Path(workspace['workspace_path'])
        def snapshot():
            index = Path(gitops.git(root, 'rev-parse', '--git-path', 'index').decode().strip())
            return (gitops.git(root, 'rev-parse', 'HEAD'), index.read_bytes(),
                    gitops.git(root, 'status', '--porcelain=v1', '-z'), path.read_bytes())
        task_git = snapshot()
        self.edge(b, a)
        self.control(a, 'canceled')
        self.edge(b, a, True)
        self.assertEqual(self.git_state(manager._project_root), canonical)
        self.assertEqual(snapshot(), task_git)
        self.assertEqual(manager._store.list_integrations(b['task_id']), history)
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')

    def test_kill_timeout_does_not_skip_remaining_workers(self):
        root, root_process = self.create(kind='mock')
        child, child_process = self.create(root, kind='mock')
        pairs = sorted([(self.task_for(root), root_process), (self.task_for(child), child_process)],
                       key=lambda pair: pair[0]['task_id'])
        failed_task, failed = pairs[0]
        successful_task, successful = pairs[1]
        failed.terminate.side_effect = None
        failed.wait.side_effect = subprocess.TimeoutExpired('test shim', 2)
        failed.kill.side_effect = None
        with self.assertRaisesRegex(RuntimeError, 'termination'):
            self.control(self.task_for(root), 'paused')
        failed.kill.assert_called_once()
        successful.terminate.assert_called_once()
        self.assertEqual(self.state(failed_task)['status'], 'in_progress')
        self.assertEqual(self.state(failed_task)['control_intent'], 'paused')
        self.assertEqual(self.state(successful_task)['status'], 'paused')
        self.assertEqual(manager._store.control_operations(self.task_for(root)['task_id'])[-1]['status'], 'recovery_required')
