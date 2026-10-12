"""Synthetic multi-generation execution; no provider requests or user worktrees."""
import io
import json
import os
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, delegation_safety as safety
from app import delegation_materialization as materialization, delegation_orchestration as domain
from app import delegation_rounds as rounds, work_control
from app.persistence import AgentStore
from app.codex_events import CodexEvents
from app.task_workspaces import provision_task_workspace
from app.workspace_freshness import ensure_workspace_current
from workspace_test_support import make_repository
import test_delegation_protocol as baseline
import test_delegation_hybrid as hybrid


class RecursiveTests(unittest.TestCase):
    patch = baseline.RuntimeTests.patch
    read = baseline.RuntimeTests.read
    finish = baseline.RuntimeTests.finish

    def setUp(self):
        baseline.RuntimeTests.setUp(self)
        self.patch(patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH='4'))
        self.patch(patch.object(manager, '_schedule_materialization'))
        self.patch(patch.object(manager, '_schedule_hybrid_for_agent'))
        self.patch(patch.object(manager, '_notify_hybrid_child_end'))
        def workspace(task_id, origin='parent_task_snapshot'):
            task = self.store.get_task(task_id)
            if not self.store.get_task_workspace(task_id):
                identifier = str(uuid4())
                self.store.save_task_workspace(dict(workspace_id=identifier, task_id=task_id,
                    project_id=task['project_id'], workspace_path=str(self.root/identifier),
                    workspace_path_key=identifier, base_snapshot='fixture', origin_kind=origin,
                    source_task_id=task['parent_task_id']))
            return self.root
        self.patch(patch.object(manager, '_execution_workspace', side_effect=workspace))

    def root_agent(self, recursive=True, sandbox='workspace-write', planning=False):
        return manager.start_agent('Parent work', 'codex', sandbox, task_id=self.task['task_id'],
            delegation_protocol_enabled=True, child_materialization_enabled=True,
            hybrid_delegation_enabled=True, recursive_delegation_enabled=recursive, planning_enabled=planning)

    def task_for(self, agent):
        return self.store.agent_task_ids(agent)[0]

    def respond(self, agent, message, **options):
        process = manager.agents[agent]
        self.read(agent, process, message, session=manager.agent_sessions[agent], **options)
        self.finish(agent, process)

    def request(self, agent, *keys):
        self.respond(agent, hybrid.request(None, *(keys or ('child',))))
        return self.store.list_delegations(self.task_for(agent))

    def children(self, agent):
        task_id = self.task_for(agent)
        # Declare disjoint responsibilities, as required for parallel siblings.
        for record in self.store.list_delegations(task_id):
            if record['status'] not in ('requested', 'materialized'):
                continue
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                child = materialization.attach(db, record['delegation_id'])['child_task_id']
            if not self.store.work_intents(child):
                self.store.create_work_intent(child, namespace='component', key=child, mode='single_owner')
        progress = manager.materialize_delegations(task_id)
        return [p['launch_agent_id'] for p in progress if p.get('phase') == 'started']

    def chain(self, generations=2, **options):
        agents = [self.root_agent(**options)]
        for _ in range(generations):
            self.request(agents[-1])
            child, = self.children(agents[-1])
            agents.append(child)
        return agents

    def rows(self, agent):
        with self.store._connection() as db:
            return domain.result_rows(db, self.task_for(agent))

    def test_root_child_grandchild_and_four_edges(self):
        agents = self.chain(4)
        self.assertEqual(len(set(agents)), 5)
        for parent, child in zip(agents, agents[1:]):
            task = self.store.get_task(self.task_for(child))
            self.assertEqual(task['parent_task_id'], self.task_for(parent))
            self.assertEqual(self.store.get_task_workspace(task['task_id'])['source_task_id'], self.task_for(parent))
        with self.store._connection() as db:
            self.assertEqual(safety.occupied(db), {self.task_for(agents[-1])})

    def test_recursive_planning_inherits_through_depth_limit(self):
        agents = self.chain(4, planning=True, sandbox='read-only')
        for agent in agents:
            self.assertTrue(self.store.planning_enabled(agent))
            self.assertEqual(manager.agent_types[agent], 'codex')
            self.assertEqual(manager.agent_sandboxes[agent], 'read-only')
        count = len(self.commands)
        self.assertEqual(self.request(agents[-1]), [])
        self.assertEqual(len(self.commands), count)
        self.assertEqual(len(self.store.list_tasks()), 5)

    def test_activation_inherited_with_provider_sandbox(self):
        root = self.root_agent(sandbox='read-only')
        self.request(root)
        child, = self.children(root)
        with self.store._connection() as db:
            row = dict(db.execute('SELECT * FROM agents WHERE agent_id=?', (child,)).fetchone())
        self.assertEqual((row['agent_type'], row['sandbox']), ('codex', 'read-only'))
        self.assertTrue(all(row[k] for k in ('delegation_protocol_enabled', 'child_materialization_enabled',
                                           'hybrid_delegation_enabled', 'recursive_delegation_enabled')))
        self.assertFalse(row['planning_enabled'])
        self.assertIn('--json', self.commands[-1])
        self.assertEqual(len(self.request(child)), 1)

    def test_nonrecursive_hybrid_child_cannot_delegate(self):
        root = self.root_agent(recursive=False)
        self.request(root)
        child, = self.children(root)
        self.assertEqual(self.request(child), [])
        self.assertEqual(manager.agent_statuses[child], 'stopped')

    def test_mock_cannot_enable_recursive_capability(self):
        with self.assertRaises(ValueError):
            manager.start_agent('Mock', 'mock', delegation_protocol_enabled=True,
                child_materialization_enabled=True, hybrid_delegation_enabled=True, recursive_delegation_enabled=True)
        self.assertFalse(self.commands)

    def test_child_cannot_elevate_sandbox(self):
        root = self.root_agent(sandbox='read-only')
        record, = self.request(root)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            child = materialization.attach(db, record['delegation_id'])['child_task_id']
            with self.assertRaisesRegex(ValueError, 'capability'):
                safety.activation(db, child, 'codex', 'workspace-write')

    def test_fifth_edge_rejected_before_request_acceptance(self):
        leaf = self.chain(4)[-1]
        self.assertEqual(self.request(leaf), [])
        self.assertEqual(len(self.store.list_tasks()), 5)

    def test_lowered_depth_retains_history_and_exposes_reason(self):
        root, child = self.chain(1)
        record, = self.request(child)
        with patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH='1'):
            progress, = manager.materialize_delegations(self.task_for(child))
            self.assertIn('depth limit', progress['last_error'])
            self.assertIn('depth limit', self.store.get_delegation(record['delegation_id'])['admission_error'])
        self.assertEqual(len(self.store.list_tasks()), 2)
        self.assertEqual(len(self.children(child)), 1)
        self.assertIsNone(self.store.get_delegation(record['delegation_id'])['admission_error'])

    def test_configured_depth_above_default(self):
        with patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH='6'):
            agents = self.chain(5)
        self.assertEqual(len(agents), 6)

    def test_invalid_depth_configuration_fails_closed(self):
        for value in ('0', '-1', 'invalid', '1.5'):
            with self.subTest(value=value), patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH=value):
                with self.assertRaises(ValueError):
                    safety.maximum_depth()

    def test_cycle_is_rejected_before_acceptance_and_admission(self):
        root, child = self.chain(1)
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (self.task_for(child), self.task_for(root)))
        with self.store._connection() as db, self.assertRaisesRegex(ValueError, 'cycle'):
            safety.check_depth(db, self.task_for(child), child=True)
        self.assertEqual(self.request(child), [])

    def test_stop_parent_preserves_running_descendants(self):
        root, child, grandchild = self.chain(2)
        manager.stop_agent(child)
        self.assertEqual(manager.agent_statuses[grandchild], 'running')
        self.assertEqual(self.store.get_task(self.task_for(grandchild))['control_intent'], 'active')
        self.assertEqual(manager.agent_statuses[root], 'stopped')

    def test_child_result_survives_stopped_direct_parent(self):
        root, child, grandchild = self.chain(2)
        manager.stop_agent(child)
        self.respond(grandchild, 'Contribution complete')
        result, = self.rows(child)
        self.assertEqual(result['child_task_id'], self.task_for(grandchild))
        self.assertEqual(result['outcome'], 'completed')
        self.assertEqual(self.rows(root), [])
        count = len(self.commands)
        manager.advance_delegations(self.task_for(child))
        self.assertEqual(len(self.commands), count)
        self.assertNotEqual(self.store.get_task(self.task_for(root))['status'], 'completed')

    def test_uncertain_descendant_stop_retains_cancel_gate_and_stops_sibling(self):
        root, child = self.chain(1)
        self.request(child, 'a', 'b')
        stuck, sibling = self.children(child)
        process = manager.agents[stuck]
        stop = manager._stop_windows_tree.side_effect
        def fail_one(candidate):
            if candidate is process:
                raise OSError('termination uncertain')
            return stop(candidate)
        process.terminate.side_effect = OSError('termination uncertain')
        with patch.object(manager, '_stop_windows_tree', side_effect=fail_one), self.assertRaises(OSError):
            manager.control_task(self.task_for(root), 'canceled')
        self.assertEqual(manager.agent_statuses[stuck], 'running')
        self.assertEqual(manager.agent_statuses[sibling], 'stopped')
        self.assertTrue(all(t['control_intent'] == 'canceled' for t in self.store.list_tasks()))
        with self.store._connection() as db, self.assertRaisesRegex(ValueError, 'Ancestor'):
            work_control.require_ancestors(db, self.task_for(stuck))

    def test_cancel_root_covers_all_descendants_and_is_idempotent(self):
        agents = self.chain(3)
        manager.control_task(self.task_for(agents[0]), 'canceled')
        manager.control_task(self.task_for(agents[0]), 'canceled')
        for agent in agents:
            task = self.store.get_task(self.task_for(agent))
            self.assertEqual(task['control_intent'], 'canceled')
            self.assertEqual(task['status'], 'canceled')

    def test_cancel_before_grandchild_attach_prevents_creation(self):
        root, child = self.chain(1)
        record, = self.request(child)
        self.store.request_work_control(self.task_for(root), 'canceled')
        result, = manager.materialize_delegations(self.task_for(child))
        self.assertIn('last_error', result)
        self.assertIsNone(self.store.get_delegation(record['delegation_id'])['child_task_id'])

    def test_canceled_ancestor_cannot_be_bypassed_by_local_active_state(self):
        root, child = self.chain(1)
        with self.store._connection() as db:
            db.execute("UPDATE tasks SET control_intent='canceled' WHERE task_id=?", (self.task_for(root),))
        self.assertEqual(self.request(child), [])

    def test_cancel_during_workspace_preparation_prevents_late_spawn(self):
        root, child = self.chain(1)
        self.request(child)
        prepare = manager._execution_workspace.side_effect
        def cancel_then_prepare(*args, **kwargs):
            result = prepare(*args, **kwargs)
            self.store.request_work_control(self.task_for(root), 'canceled')
            return result
        count = len(self.commands)
        with patch.object(manager, '_execution_workspace', side_effect=cancel_then_prepare):
            result, = manager.materialize_delegations(self.task_for(child))
        self.assertEqual(len(self.commands), count)
        self.assertIn('last_error', result)
        self.assertTrue(all(t['control_intent'] == 'canceled' for t in self.store.list_tasks()))

    def test_cancel_after_popen_compensates_without_sending_prompt(self):
        root, child = self.chain(1)
        self.request(child)
        spawn = manager._spawn_process.side_effect
        spawned = []
        def racing(*args, **kwargs):
            process = spawn(*args, **kwargs)
            spawned.append(process)
            return process
        # Cancellation cannot acquire SQLite inside Popen's transaction. Put it
        # immediately after that transaction, before Agent/assignment commit.
        launch = manager._spawn_with_resources
        def cancel_after(*args, **kwargs):
            process = launch(*args, **kwargs)
            self.store.request_work_control(self.task_for(root), 'canceled')
            return process
        with patch.object(manager, '_spawn_process', side_effect=racing), patch.object(manager, '_spawn_with_resources', side_effect=cancel_after):
            result, = manager.materialize_delegations(self.task_for(child))
        self.assertIn('last_error', result)
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(spawned[0].stdin.getvalue(), '')

    def test_concurrent_cancel_and_attach_cannot_escape_subtree(self):
        root, child = self.chain(1)
        record, = self.request(child)
        separate_connection = AgentStore(self.store.path)
        go = Event()
        def attach():
            go.wait(5)
            try:
                with separate_connection._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    materialization.attach(db, record['delegation_id'])
            except ValueError:
                return
        def cancel():
            go.wait(5)
            self.store.request_work_control(self.task_for(root), 'canceled')
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(attach), pool.submit(cancel)]
            go.set()
            for future in futures:
                future.result(timeout=10)
        self.assertTrue(all(t['control_intent'] == 'canceled' for t in self.store.list_tasks()))

    def test_sibling_failure_does_not_cancel_other_branch(self):
        root = self.root_agent()
        self.request(root, 'a', 'b')
        first, second = self.children(root)
        self.request(first)
        grandchild, = self.children(first)
        self.respond(grandchild, 'Failure', ending='turn.failed')
        self.assertEqual(manager.agent_statuses[second], 'running')
        self.assertNotEqual(self.store.get_task(self.task_for(root))['status'], 'completed')
        self.assertNotEqual(self.store.get_task(self.task_for(first))['status'], 'completed')

    def test_global_capacity_counts_recursive_workers_and_releases_finished(self):
        root = self.root_agent()
        self.request(root, 'a', 'b', 'c', 'd', 'e')
        children = self.children(root)
        self.assertEqual(len(children), 4)
        self.respond(children[0], 'Done')
        manager.materialize_delegations(self.task_for(root))
        with self.store._connection() as db:
            self.assertEqual(len(safety.occupied(db)), 4)
        self.assertEqual(sum(r['materialization']['phase'] == 'started' for r in self.store.list_delegations(self.task_for(root))), 5)

    def test_uncertain_launch_reserves_capacity(self):
        root, child = self.chain(1)
        self.request(child, 'a', 'b', 'c', 'd')
        children = self.children(child)
        self.assertEqual(len(children), 4)
        self.store.reconcile_task_recovery()
        with self.store._connection() as db:
            self.assertGreaterEqual(len(safety.occupied(db)), 4)
            with self.assertRaisesRegex(ValueError, 'capacity'):
                safety.require_capacity(db, str(uuid4()))

    def test_restart_preserves_pending_recursive_work_without_spawn(self):
        root, child = self.chain(1)
        record, = self.request(child)
        count = len(self.commands)
        recovered = AgentStore(self.store.path)
        recovered.reconcile_task_recovery()
        self.assertEqual(len(self.commands), count)
        self.assertEqual(recovered.get_delegation(record['delegation_id'])['status'], 'requested')
        self.assertEqual(len(recovered.list_tasks()), 2)

    def test_restart_after_child_attach_retains_one_child(self):
        root, child = self.chain(1)
        record, = self.request(child)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            before = materialization.attach(db, record['delegation_id'])['child_task_id']
        self.store.reconcile_task_recovery()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.assertEqual(materialization.attach(db, record['delegation_id'])['child_task_id'], before)
        self.assertEqual(len(self.store.list_tasks()), 3)

    def test_nested_stale_generation_cannot_request(self):
        root, child = self.chain(1)
        old = manager.agents[child]._control_center_execution.origin
        self.store.begin_delegation_execution(child, str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.store.accept_delegation_message(old, hybrid.request(), session_id=str(uuid4()), message_id='old')
        self.assertEqual(self.store.list_delegations(self.task_for(child)), [])

    def test_lowered_depth_rechecked_at_reserved_launch(self):
        root, child = self.chain(1)
        record, = self.request(child)
        prepare = manager._spawn_with_resources
        def lower(*args, **kwargs):
            with patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH='1'):
                return prepare(*args, **kwargs)
        count = len(self.commands)
        with patch.object(manager, '_spawn_with_resources', side_effect=lower):
            result, = manager.materialize_delegations(self.task_for(child))
        self.assertEqual(len(self.commands), count)
        self.assertIn('depth limit', result['last_error'])
        self.assertEqual(result['phase'], 'recovery_required')
        self.assertIsNotNone(self.store.get_delegation(record['delegation_id'])['child_task_id'])

    def test_activation_cannot_bypass_unauthorized_parent(self):
        root = self.root_agent(recursive=False)
        record, = self.request(root)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            child = materialization.attach(db, record['delegation_id'])['child_task_id']
            with self.assertRaisesRegex(ValueError, 'capability'):
                safety.activation(db, child, 'codex', 'workspace-write')

    def test_nested_parallel_scope_collision_defers_launch(self):
        root = self.root_agent()
        self.request(root, 'a', 'b')
        child, sibling = self.children(root)
        record, = self.request(child)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            grand = materialization.attach(db, record['delegation_id'])['child_task_id']
        self.store.create_work_intent(grand, namespace='component', key=self.task_for(sibling), mode='single_owner')
        count = len(self.commands)
        result, = manager.materialize_delegations(self.task_for(child))
        self.assertEqual(len(self.commands), count)
        self.assertIn('last_error', result)
        self.assertEqual(manager.agent_statuses[sibling], 'running')

    def test_nested_parent_cannot_complete_with_unresolved_grandchild(self):
        root, child, grandchild = self.chain(2)
        assignment = self.store.get_active_assignment_for_agent(child)
        with self.assertRaises(ValueError):
            self.store.end_assignment(assignment['assignment_id'], 'completed')
        self.assertNotEqual(self.store.get_task(self.task_for(child))['status'], 'completed')
        self.assertEqual(self.rows(root), [])

    def test_lost_receipt_replay_keeps_one_nested_child(self):
        root, child = self.chain(1)
        self.read(child, manager.agents[child], hybrid.request(), session=manager.agent_sessions[child])
        origin = manager.agents[child]._control_center_execution.origin
        self.finish(child, manager.agents[child])
        before = self.store.list_delegations(self.task_for(child))
        for _ in range(3):
            self.store.accept_delegation_message(origin, hybrid.request(), session_id=manager.agent_sessions[child], message_id='item_1')
        self.assertEqual(self.store.list_delegations(self.task_for(child)), before)
        self.children(child)
        self.assertEqual(len(self.store.list_tasks()), 3)

    def test_nested_parent_unknown_resume_blocks_replacement(self):
        root, child, grandchild = self.chain(2)
        with self.store._connection() as db:
            db.execute("UPDATE delegation_continuations SET phase='launching' WHERE task_id=?", (self.task_for(child),))
        self.store.reconcile_task_recovery()
        count = len(self.commands)
        with self.assertRaises(ValueError):
            manager.start_task_agent(self.task_for(child), 'codex')
        self.assertEqual(len(self.commands), count)
        with self.store._connection() as db:
            self.assertEqual(domain.state(db, self.task_for(child))['phase'], 'recovery_required')

    def test_sse_observes_committed_capability(self):
        observations = []
        def publish(key):
            if key:
                with self.store._connection() as db:
                    row = db.execute('SELECT recursive_delegation_enabled FROM agents WHERE agent_id=?', (key,)).fetchone()
                    observations.append(bool(row and row[0]))
        with patch.object(manager, '_emit_agent_change', side_effect=publish):
            self.root_agent()
        self.assertTrue(observations)
        self.assertTrue(all(observations))

    def test_root_rate_guard_aggregates_descendant_rounds_without_lifetime_cap(self):
        root, child = self.chain(1)
        task_id = self.task_for(child)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            assignment = self.store.get_active_assignment_for_agent(child)
            for i in range(20):
                receipt = dict(assignment_id=assignment['assignment_id'], agent_id=child,
                    generation=str(uuid4()), message_id=str(i), delegation_ids=[])
                rounds.record(db, task_id if i % 2 else self.task_for(root), receipt,
                              policy=rounds.Policy(burst=100, tree_burst=8))
            history = rounds.history(db, task_id)
            self.assertEqual(len(history), 10)
            self.assertGreater(history[-1]['not_before'], rounds.now(db) + 59)
            self.assertEqual(len(rounds.history(db, self.task_for(root))), 11)

    def test_concurrent_rounds_share_durable_root_capacity(self):
        root = self.root_agent()
        self.request(root, 'a', 'b')
        agents = self.children(root)
        origins = [(self.task_for(a), self.store.get_active_assignment_for_agent(a), a) for a in agents]
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        def reserve(index):
            task_id, assignment, agent = origins[index]
            with stores[index]._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                receipt = dict(assignment_id=assignment['assignment_id'], agent_id=agent,
                    generation=str(uuid4()), message_id='batch', delegation_ids=[])
                rounds.record(db, task_id, receipt, policy=rounds.Policy(burst=4, tree_burst=1))
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(reserve, (0, 1)))
        with self.store._connection() as db:
            slots = sorted(rounds.history(db, task_id)[0]['not_before'] for task_id, _, _ in origins)
        self.assertGreaterEqual(slots[1] - slots[0], 60)

    def test_unknown_nested_execution_prevents_filesystem_integration(self):
        self.real_workspaces()
        root, child, grandchild = self.chain(2)
        self.store.reconcile_task_recovery()
        with self.assertRaisesRegex(ValueError, 'ownership requires inspection'):
            manager.integrate_task(self.task_for(grandchild))

    def test_provider_limit_is_durable_and_does_not_retry(self):
        root, child = self.chain(1)
        process = manager.agents[child]
        events = baseline.events('Not completed', manager.agent_sessions[child])
        events[-1] = dict(type='turn.failed', error=dict(code='rate_limit_exceeded', message='Limited'))
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
        manager._read_output(child, process)
        self.finish(child, process)
        count = len(self.commands)
        for _ in range(3):
            manager.get_agent(child)
            manager.materialize_delegations(self.task_for(root))
        self.assertEqual(len(self.commands), count)
        self.assertIn('reset time unknown', '\n'.join(self.store.full_output(child)))

    def test_error_text_alone_does_not_invent_provider_limit(self):
        events = CodexEvents()
        events.feed(json.dumps(dict(type='turn.failed', error=dict(message='rate limit maybe'))))
        self.assertEqual(events.error, 'Codex turn failed')

    def test_v22_migration_rollback_and_default_off(self):
        with self.store._connection() as db:
            db.execute('ALTER TABLE agents DROP COLUMN recursive_delegation_enabled')
            db.execute('ALTER TABLE delegations DROP COLUMN admission_error')
            db.execute('PRAGMA user_version=22')
        original = safety.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('injected')
        with patch.object(safety, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 22)
            self.assertNotIn('recursive_delegation_enabled', {r['name'] for r in db.execute('PRAGMA table_info(agents)')})
        AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 25)
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def real_workspaces(self):
        repository = self.root/'repository'
        make_repository(repository)
        from app.project_domain import canonical_path
        path, canonical = canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?', (path, canonical, self.project['project_id']))
        self.patch(patch.dict(os.environ, CONTROL_CENTER_WORKSPACE_ROOT=str(self.root/'workspaces')))
        def workspace(task_id, origin='legacy_project_snapshot'):
            return Path(provision_task_workspace(self.store, task_id, origin_kind=origin)['workspace_path'])
        self.patch(patch.object(manager, '_execution_workspace', side_effect=workspace))
        self.patch(patch.object(manager, 'ensure_workspace_current', side_effect=ensure_workspace_current))
        return repository

    def review_and_finish(self, agent):
        task_id = self.task_for(agent)
        state = manager.advance_delegations(task_id)
        self.assertEqual(state['phase'], 'reviewing', state)
        rows = [r for r in self.rows(agent) if r['phase'] == 'delivered']
        ack = json.dumps(dict(protocol=hybrid.protocol.PROTOCOL, action='acknowledge_results',
            delivery_id=state['delivery_id'], results=[dict(result_id=r['result_id'], decision='accept', reason='Reviewed') for r in rows]))
        self.respond(agent, ack)
        self.assertEqual(manager.advance_delegations(task_id)['phase'], 'finishing')
        self.respond(agent, 'Overall task verified and complete.')
        self.assertEqual(self.store.get_task(task_id)['status'], 'completed')

    def test_nested_real_integration_and_ack_then_root_completion(self):
        repository = self.real_workspaces()
        root, child, grandchild = self.chain(2)
        grand_ws = Path(self.store.get_task_workspace(self.task_for(grandchild))['workspace_path'])
        child_ws = Path(self.store.get_task_workspace(self.task_for(child))['workspace_path'])
        root_ws = Path(self.store.get_task_workspace(self.task_for(root))['workspace_path'])
        (grand_ws/'source.txt').write_text('Nested contribution\n', encoding='utf-8')
        self.respond(grandchild, 'Done')
        self.assertEqual(len(self.rows(child)), 1)
        self.assertEqual(self.rows(root), [])
        self.review_and_finish(child)
        self.assertEqual((child_ws/'source.txt').read_text(), 'Nested contribution\n')
        self.assertEqual((root_ws/'source.txt').read_text(), 'original\n')
        self.review_and_finish(root)
        self.assertEqual((root_ws/'source.txt').read_text(), 'Nested contribution\n')
        self.assertEqual((repository/'source.txt').read_text(), 'original\n')
        self.assertEqual(len(self.store.list_integrations()), 2)

    def test_nested_no_change_results_and_repeated_delivery(self):
        self.real_workspaces()
        root, child, grandchild = self.chain(2)
        self.respond(grandchild, 'No changes needed')
        self.review_and_finish(child)
        self.review_and_finish(root)
        before = self.store.list_integrations()
        for agent in (child, root):
            manager.advance_delegations(self.task_for(agent))
        self.assertEqual(self.store.list_integrations(), before)
        self.assertTrue(all(r['status'] == 'noop' for r in before))

    def test_crash_after_nested_integration_reuses_journal(self):
        self.real_workspaces()
        root, child, grandchild = self.chain(2)
        path = Path(self.store.get_task_workspace(self.task_for(grandchild))['workspace_path'])/'source.txt'
        path.write_text('Nested change\n', encoding='utf-8')
        self.respond(grandchild, 'Done')
        integrate = manager.integrate_task
        def crash(task_id):
            integrate(task_id)
            raise sqlite3.OperationalError('lost result persistence')
        with patch.object(manager, 'integrate_task', side_effect=crash), self.assertRaises(sqlite3.OperationalError):
            manager.advance_delegations(self.task_for(child))
        self.assertEqual(self.rows(child)[0]['phase'], 'recorded')
        self.store.reconcile_task_recovery()
        from app import integrations
        with patch.object(integrations.applygit, 'apply_plan') as apply:
            state = manager.advance_delegations(self.task_for(child))
        apply.assert_not_called()
        self.assertEqual(state['phase'], 'reviewing')
        self.assertEqual(len(self.store.list_integrations()), 1)

    def test_nested_round_after_ack_and_restart_retains_old_results(self):
        self.real_workspaces()
        root, child, grandchild = self.chain(2)
        self.respond(grandchild, 'Done')
        state = manager.advance_delegations(self.task_for(child))
        row, = self.rows(child)
        ack = json.dumps(dict(protocol=hybrid.protocol.PROTOCOL, action='acknowledge_results',
            delivery_id=state['delivery_id'], results=[dict(result_id=row['result_id'], decision='accept', reason='Verified')]))
        self.respond(child, ack)
        self.store.reconcile_task_recovery()  # No execution at this quiet boundary.
        self.assertEqual(manager.advance_delegations(self.task_for(child))['phase'], 'finishing')
        self.request(child, 'next-round')
        with self.store._connection() as db:
            self.assertEqual(len(rounds.history(db, self.task_for(child))), 2)
            self.assertEqual(len(rounds.history(db, self.task_for(root))), 1)
        self.assertEqual(self.rows(child)[0]['phase'], 'accepted')
        self.assertEqual(self.rows(root), [])
        self.assertEqual(len(self.children(child)), 1)
        self.assertEqual(len(self.store.list_delegations(self.task_for(child))), 2)

    def test_nested_rejected_result_cannot_complete_or_reach_root(self):
        self.real_workspaces()
        root, child, grandchild = self.chain(2)
        self.respond(grandchild, 'Done')
        state = manager.advance_delegations(self.task_for(child))
        row, = self.rows(child)
        ack = json.dumps(dict(protocol=hybrid.protocol.PROTOCOL, action='acknowledge_results',
            delivery_id=state['delivery_id'], results=[dict(result_id=row['result_id'], decision='reject', reason='Needs correction')]))
        self.respond(child, ack)
        count = len(self.commands)
        manager.advance_delegations(self.task_for(child))
        self.assertEqual(len(self.commands), count)
        self.assertEqual(self.rows(child)[0]['phase'], 'rejected')
        self.assertEqual(self.rows(root), [])


if __name__ == '__main__':
    unittest.main()
