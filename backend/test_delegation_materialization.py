import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from app import agent_manager as manager, delegation_materialization as materialization, delegations
from app.persistence import AgentStore
from app.task_workspaces import provision_task_workspace
from app.workspace_freshness import ensure_workspace_current
from workspace_test_support import make_repository
import test_delegation_protocol as protocol_tests


class MaterializationTests(unittest.TestCase):
    setUp = protocol_tests.HandoffTests.setUp
    agent = protocol_tests.HandoffTests.agent
    task = protocol_tests.HandoffTests.task
    worker = protocol_tests.HandoffTests.worker
    origin_context = protocol_tests.HandoffTests.origin_context
    accept = protocol_tests.HandoffTests.accept
    snapshot = protocol_tests.HandoffTests.snapshot

    def accepted(self, *keys, instruction=None):
        with self.store._connection() as db:
            db.execute('UPDATE agents SET child_materialization_enabled=1 WHERE agent_id=?', (self.origin['agent_id'],))
        message = protocol_tests.envelope(*(keys or ('a',)))
        if instruction is not None:
            value = json.loads(message)
            value['requests'][0]['instruction'] = instruction
            message = json.dumps(value)
        return self.accept(message=message)['delegation_ids']

    def attach(self, identifier, limits=materialization.DEFAULT_LIMITS):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return materialization.attach(db, identifier, limits)

    def ready(self, identifier):
        record = self.attach(identifier)
        key = record['child_task_id']
        workspace = dict(workspace_id=str(uuid4()), task_id=key, project_id=self.project['project_id'],
                         workspace_path=str(Path(self.temp.name) / key), workspace_path_key=str(uuid4()),
                         base_snapshot='fixture', origin_kind='parent_task_snapshot', source_task_id=self.parent['task_id'])
        self.store.save_task_workspace(workspace)
        with self.store._connection() as db:
            db.execute("UPDATE delegation_materializations SET phase='workspace_ready' WHERE delegation_id=?", (identifier,))
        return key

    def admit(self, identifier, limits=materialization.DEFAULT_LIMITS):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return materialization.admit(db, identifier, limits)

    def test_valid_child_has_uuid_project_parent_and_exact_instruction(self):
        instruction = '  Preserve all whitespace\nשלום  '
        identifier, = self.accepted(instruction=instruction)
        record = self.attach(identifier)
        child = self.store.get_task(record['child_task_id'])
        self.assertEqual(child['description'], instruction)
        self.assertEqual(child['project_id'], self.parent['project_id'])
        self.assertEqual(child['parent_task_id'], self.parent['task_id'])
        self.assertEqual(record['status'], 'materialized')
        self.assertNotEqual(child['task_id'], self.origin['agent_id'])

    def test_atomic_attach_rolls_back_child_on_link_failure(self):
        identifier, = self.accepted()
        before = self.snapshot()
        with patch.object(delegations, 'attach_child', side_effect=sqlite3.IntegrityError('injected')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.attach(identifier)
        self.assertEqual(self.snapshot(), before)

    def test_idempotent_retry_and_crash_after_child_creation(self):
        identifier, = self.accepted()
        child = self.attach(identifier)['child_task_id']
        self.store = AgentStore(self.path)
        self.assertEqual(self.attach(identifier)['child_task_id'], child)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_concurrent_attach_creates_exactly_one_child(self):
        identifier, = self.accepted()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(self.attach, [identifier, identifier]))
        self.assertEqual(results[0]['child_task_id'], results[1]['child_task_id'])
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_unrelated_blocker_is_not_bypassed(self):
        source = self.task()
        identifier, = self.accepted()
        self.store.change_dependency(self.parent['task_id'], source['task_id'])
        with self.assertRaisesRegex(ValueError, 'unrelated'):
            self.attach(identifier)

    def test_normal_child_creation_still_rejects_handoff_parent(self):
        self.accepted()
        with self.assertRaises(ValueError):
            self.task(self.parent)

    def test_unaccepted_delegation_rejected(self):
        record = self.store.create_delegation(self.parent['task_id'], project_id=self.project['project_id'],
            requested_by_agent_id=self.origin['agent_id'], requested_by_assignment_id=self.origin['assignment_id'],
            request_key='a', instruction='Work')
        with self.assertRaisesRegex(ValueError, 'handoff'):
            self.attach(record['delegation_id'])

    def test_canceled_delegation_and_missing_delegation_rejected(self):
        identifier, = self.accepted()
        self.store.transition_delegation(identifier, 'canceled')
        with self.assertRaises(ValueError):
            self.attach(identifier)
        with self.assertRaises(LookupError):
            self.attach('missing')

    def test_stale_generation_rejected(self):
        identifier, = self.accepted()
        with self.store._connection() as db:
            db.execute('UPDATE agents SET execution_generation=?', (str(uuid4()),))
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.attach(identifier)

    def test_changed_project_rejected(self):
        identifier, = self.accepted()
        other = Path(self.temp.name) / 'other'
        other.mkdir()
        project = self.store.create_project('Other', other)
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET project_id=?', (project['project_id'],))
        with self.assertRaises(ValueError):
            self.attach(identifier)

    def test_parent_cancel_before_child_creation(self):
        identifier, = self.accepted()
        self.store.request_work_control(self.parent['task_id'], 'canceled')
        with self.assertRaises(ValueError):
            self.attach(identifier)
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_parent_pause_after_child_creation_prevents_admission(self):
        identifier, = self.accepted()
        self.ready(identifier)
        self.store.request_work_control(self.parent['task_id'], 'paused')
        with self.assertRaises(ValueError):
            self.admit(identifier)

    def test_activation_is_explicit(self):
        identifier, = self.accepted()
        with self.store._connection() as db:
            db.execute('UPDATE agents SET child_materialization_enabled=0')
        with self.assertRaisesRegex(ValueError, 'activation'):
            self.attach(identifier)

    def test_oversized_protocol_instruction_preserved_without_api_limit_change(self):
        instruction = 'x' * 32768
        identifier, = self.accepted(instruction=instruction)
        child = self.attach(identifier)['child_task_id']
        self.assertEqual(self.store.get_task(child)['description'], instruction)
        from app.task_domain import TASK_DESCRIPTION_LIMIT
        self.assertEqual(TASK_DESCRIPTION_LIMIT, 20000)

    def test_fanout_limit_keeps_excess_request(self):
        a, b = self.accepted('a', 'b')
        limit = materialization.Limits(children_per_parent=1)
        self.attach(a, limit)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.attach(b, limit)
        self.assertEqual(self.store.get_delegation(b)['status'], 'requested')

    def test_parent_stays_incomplete_and_no_result_delivery(self):
        identifier, = self.accepted()
        self.attach(identifier)
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'], 'blocked')
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']), self.origin)
        self.assertEqual(self.store.get_delegation(identifier)['status'], 'materialized')

    def test_admission_inherits_provider_and_permissions(self):
        identifier, = self.accepted()
        self.ready(identifier)
        key, provider = self.admit(identifier)
        self.assertEqual(provider['agent_type'], 'codex')
        self.assertEqual(provider['sandbox'], 'workspace-write')
        self.assertNotEqual(key, self.origin['agent_id'])

    def test_concurrent_admission_has_one_winner(self):
        identifier, = self.accepted()
        self.ready(identifier)
        def attempt(_):
            try:
                return self.admit(identifier)[0]
            except ValueError:
                return None
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_unknown_launch_fails_closed_after_restart(self):
        identifier, = self.accepted()
        task = self.ready(identifier)
        self.admit(identifier)
        self.store.reconcile_task_recovery()
        self.assertEqual(self.store.get_delegation(identifier)['materialization']['phase'], 'recovery_required')
        with self.assertRaises(ValueError):
            self.admit(identifier)
        with self.store._connection() as db, self.assertRaises(ValueError):
            materialization.check_worker(db, task)

    def test_siblings_without_declarations_are_staged(self):
        a, b = self.accepted('a', 'b')
        self.ready(a)
        self.ready(b)
        self.admit(a)
        with self.assertRaisesRegex(ValueError, 'non-overlapping'):
            self.admit(b)

    def test_disjoint_declared_siblings_can_be_admitted_in_parallel(self):
        a, b = self.accepted('a', 'b')
        for identifier, key in ((a, 'api'), (b, 'docs')):
            task = self.ready(identifier)
            self.store.create_work_intent(task, namespace='component', key=key, mode='single_owner')
        self.admit(a)
        self.admit(b)

    def test_concurrency_limit_even_with_disjoint_intents(self):
        a, b = self.accepted('a', 'b')
        for identifier, key in ((a, 'api'), (b, 'docs')):
            task = self.ready(identifier)
            self.store.create_work_intent(task, namespace='component', key=key, mode='single_owner')
        self.admit(a)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.admit(b, materialization.Limits(concurrent_children=1))

    def test_child_dependency_blocks_launch(self):
        source = self.task()
        identifier, = self.accepted()
        child = self.ready(identifier)
        self.store.change_dependency(child, source['task_id'])
        with self.assertRaises(ValueError):
            self.admit(identifier)

    def test_unrelated_root_work_intent_blocks_child(self):
        root = self.task()
        self.store.create_work_intent(root['task_id'], namespace='component', key='api', mode='single_owner')
        identifier, = self.accepted()
        child = self.ready(identifier)
        self.store.create_work_intent(child, namespace='component', key='api/auth', mode='single_owner')
        with self.assertRaises(ValueError):
            self.admit(identifier)

    def test_global_resource_conflict_across_projects(self):
        other_path = Path(self.temp.name) / 'other'
        other_path.mkdir()
        other = self.store.create_project('Other', other_path)
        root = self.task(project=other)
        claim = dict(resource_type='generic', resource_key='test:shared', scope='global', mode='exclusive', lifetime='task')
        self.store.create_resource_claim(root['task_id'], **claim)
        identifier, = self.accepted()
        child = self.ready(identifier)
        self.store.create_resource_claim(child, **claim)
        with self.assertRaises(ValueError):
            self.admit(identifier)

    def test_migration_v19_rollback_and_no_fabricated_state(self):
        with self.store._connection() as db:
            db.execute('DROP TABLE delegation_materializations')
            db.execute('ALTER TABLE agents DROP COLUMN child_materialization_enabled')
            db.execute('PRAGMA user_version=19')
        original = materialization.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('injected')
        with patch.object(materialization, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 19)
            self.assertNotIn('child_materialization_enabled', {r['name'] for r in db.execute('PRAGMA table_info(agents)')})
        self.store = AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delegation_materializations').fetchone()[0], 0)

    def test_worker_collision_rejected_without_stealing_assignment(self):
        identifier, = self.accepted()
        child = self.ready(identifier)
        other = self.agent('running')
        with self.store._connection() as db:
            db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id) VALUES (?,?,?)', (str(uuid4()), child, other))
        with self.assertRaisesRegex(ValueError, 'history'):
            self.admit(identifier)
        self.assertEqual(self.store.get_active_assignment_for_task(child)['agent_id'], other)

    def test_depth_limit_does_not_delete_accepted_request(self):
        # An accepted nested request is retained even when this activation only
        # permits the first generation of children.
        root = self.task()
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (root['task_id'], self.parent['task_id']))
        identifier, = self.accepted()
        with self.assertRaisesRegex(ValueError, 'depth'):
            self.attach(identifier)
        self.assertEqual(self.store.get_delegation(identifier)['status'], 'requested')

    def test_unrelated_live_root_without_intents_prevents_parallel_launch(self):
        root = self.task()
        self.worker(root)
        identifier, = self.accepted()
        self.ready(identifier)
        with self.assertRaisesRegex(ValueError, 'non-overlapping'):
            self.admit(identifier)

    def test_replaced_parent_assignment_cannot_materialize_old_receipt(self):
        identifier, = self.accepted()
        with self.store._connection() as db:
            db.execute("UPDATE task_assignments SET ended_at='ended',ended_reason='reassigned' WHERE assignment_id=?", (self.origin['assignment_id'],))
        with self.assertRaisesRegex(ValueError, 'ownership'):
            self.attach(identifier)


class RuntimeMaterializationTests(unittest.TestCase):
    setUp = protocol_tests.RuntimeTests.setUp
    patch = protocol_tests.RuntimeTests.patch
    read = protocol_tests.RuntimeTests.read
    finish = protocol_tests.RuntimeTests.finish

    def accepted(self, *keys, sandbox='read-only'):
        with patch.object(manager, '_schedule_materialization') as schedule:
            key = manager.start_agent('Parent work', 'codex', sandbox, task_id=self.task['task_id'],
                                      delegation_protocol_enabled=True, child_materialization_enabled=True)
            self.read(key, manager.agents[key], protocol_tests.envelope(*(keys or ('a',))))
            self.finish(key, manager.agents[key])
            schedule.assert_called_once_with(self.task['task_id'])
        def workspace(task_id, origin='legacy_project_snapshot'):
            task = self.store.get_task(task_id)
            if self.store.get_task_workspace(task_id) is None:
                key = str(uuid4())
                self.store.save_task_workspace(dict(workspace_id=key, task_id=task_id, project_id=task['project_id'],
                    workspace_path=str(self.root.parent / key), workspace_path_key=key,
                    base_snapshot='fixture', origin_kind=origin, source_task_id=task['parent_task_id']))
            return self.root
        self.patch(patch.object(manager, '_execution_workspace', side_effect=workspace))
        return key, self.store.list_delegations(self.task['task_id'])

    def test_child_launch_assignment_parent_permissions_and_no_recursion(self):
        parent, records = self.accepted(sandbox='workspace-write')
        result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'started', result)
        child = result['launch_agent_id']
        self.assertEqual(manager.agent_parents[child], parent)
        self.assertEqual(manager.agent_sandboxes[child], 'workspace-write')
        self.assertEqual(manager.agent_tasks[child], records[0]['instruction'])
        self.assertFalse(self.store.execution_settings(child))
        self.assertEqual(self.store.get_active_assignment_for_task(records[0]['child_task_id'] or self.store.get_delegation(records[0]['delegation_id'])['child_task_id'])['agent_id'], child)

    def test_retry_never_launches_second_process(self):
        self.accepted()
        first = manager.materialize_delegations(self.task['task_id'])
        count = len(self.commands)
        self.assertEqual(manager.materialize_delegations(self.task['task_id']), first)
        self.assertEqual(len(self.commands), count)

    def test_workspace_failure_retains_child_then_retry_reuses_it(self):
        _, records = self.accepted()
        with patch.object(manager, '_execution_workspace', side_effect=OSError('provision failed')):
            failed, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(failed['phase'], 'attached')
        child = self.store.get_delegation(records[0]['delegation_id'])['child_task_id']
        self.assertEqual(len(self.commands), 1)
        success, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(success['phase'], 'started', success)
        self.assertEqual(self.store.get_delegation(records[0]['delegation_id'])['child_task_id'], child)

    def test_crash_after_workspace_commit_reuses_workspace(self):
        _, records = self.accepted()
        service = materialization.Materializer(self.store, manager._launch_delegated_child, manager._emit_agent_change)
        child = service.prepare(records[0]['delegation_id'])['child_task_id']
        manager._execution_workspace(child, 'parent_task_snapshot')
        before = self.store.get_task_workspace(child)
        self.store = AgentStore(self.store.path)
        result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'started', result)
        self.assertEqual(self.store.get_task_workspace(child), before)

    def test_spawn_failure_is_visible_and_cannot_repeat_paid_attempt(self):
        self.accepted()
        with patch.object(manager, '_spawn_process', side_effect=OSError('spawn failed')) as spawn:
            result, = manager.materialize_delegations(self.task['task_id'])
            manager.materialize_delegations(self.task['task_id'], retry=True)
        self.assertEqual(result['phase'], 'recovery_required')
        spawn.assert_called_once()

    def test_cancel_during_workspace_preparation_never_spawns_child(self):
        self.accepted()
        original = manager._execution_workspace.side_effect
        def cancel(task_id, origin):
            path = original(task_id, origin)
            self.store.request_work_control(self.task['task_id'], 'canceled')
            return path
        with patch.object(manager, '_execution_workspace', side_effect=cancel):
            manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 1)

    def test_child_stop_retains_workspace_and_does_not_complete_parent(self):
        self.accepted()
        result, = manager.materialize_delegations(self.task['task_id'])
        child = result['launch_agent_id']
        task = self.store.agent_task_ids(child)[0]
        before = self.store.get_task_workspace(task)
        manager.stop_agent(child)
        self.assertEqual(self.store.get_task(task)['status'], 'pending')
        self.assertIsNone(self.store.get_active_assignment_for_task(task))
        self.assertEqual(self.store.get_task_workspace(task), before)
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')

    def test_mock_provider_is_inherited_by_generic_service(self):
        parent, _ = self.accepted()
        with self.store._connection() as db:
            db.execute("UPDATE agents SET agent_type='mock',sandbox=NULL WHERE agent_id=?", (parent,))
        result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'started', result)
        self.assertEqual(manager.agent_types[result['launch_agent_id']], 'mock')
        self.assertIsNone(manager.agent_sandboxes[result['launch_agent_id']])

    def test_restart_never_schedules_or_reattaches_workers(self):
        self.accepted()
        result, = manager.materialize_delegations(self.task['task_id'])
        for key in manager.agents:
            manager.agents[key] = None
        with patch.object(manager, '_schedule_materialization') as schedule, patch.object(manager, '_spawn_process') as spawn:
            manager.initialize_persistence(self.store.path)
        schedule.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(self.store.get_delegation(result['delegation_id'])['materialization']['phase'], 'recovery_required')

    def test_persistence_failure_after_spawn_stops_worker_and_never_retries_launch(self):
        self.accepted()
        save = self.store.save_agent
        def fail(record, *args, **kwargs):
            if record['status'] == 'running':
                raise sqlite3.OperationalError('injected start commit failure')
            return save(record, *args, **kwargs)
        with patch.object(self.store, 'save_agent', side_effect=fail):
            result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'recovery_required')
        child = result['launch_agent_id']
        self.assertEqual(manager.agent_statuses[child], 'stopped')
        task = self.store.get_delegation(result['delegation_id'])['child_task_id']
        self.assertIsNone(self.store.get_active_assignment_for_task(task))
        count = len(self.commands)
        manager.materialize_delegations(self.task['task_id'], retry=True)
        self.assertEqual(len(self.commands), count)

    def test_publication_observes_committed_child_and_start(self):
        self.accepted()
        seen = []
        def publish(_):
            record, = AgentStore(self.store.path).list_delegations(self.task['task_id'])
            if record['child_task_id']:
                seen.append((record['status'], record['materialization']['phase']))
        with patch.object(manager, '_emit_agent_change', side_effect=publish):
            result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'started', result)
        self.assertIn(('materialized', 'attached'), seen)
        self.assertIn(('materialized', 'started'), seen)

    def test_preparation_retries_are_bounded_and_explicit_reset_is_safe(self):
        self.accepted()
        with patch.object(manager, '_execution_workspace', side_effect=OSError('offline')) as workspace:
            for _ in range(5):
                manager.materialize_delegations(self.task['task_id'])
            self.assertEqual(workspace.call_count, 3)
            manager.materialize_delegations(self.task['task_id'], retry=True)
            self.assertEqual(workspace.call_count, 4)
        self.assertEqual(len(self.commands), 1)

    def test_completed_child_wakes_staged_sibling_without_parent_resume(self):
        parent, _ = self.accepted('a', 'b')
        results = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(results[0]['phase'], 'started', results)
        self.assertEqual(results[1]['phase'], 'attached', results)
        child = results[0]['launch_agent_id']
        process = manager.agents[child]
        process.stdout.close()
        process.poll.return_value = 0
        with patch.object(manager, '_schedule_materialization') as schedule:
            manager._finalize_process(child, process)
        schedule.assert_called_once_with(self.task['task_id'])
        results = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(results[1]['phase'], 'started', results)
        self.assertEqual(manager.agent_statuses[parent], 'stopped')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')

    def test_resume_gate_does_not_allow_direct_start_during_unknown_launch(self):
        self.accepted()
        with patch.object(manager, '_spawn_process', side_effect=OSError('unknown')):
            result, = manager.materialize_delegations(self.task['task_id'])
        child = self.store.get_delegation(result['delegation_id'])['child_task_id']
        with self.assertRaisesRegex(ValueError, 'controlled materialization'):
            manager.start_task_agent(child)

    def test_real_workspace_snapshots_parent_and_preserves_source(self):
        repository = self.root / 'source'
        make_repository(repository)
        # Replace this test's empty fixture Project before it owns workspaces.
        from app.project_domain import canonical_path
        path, key = canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?', (path, key, self.project['project_id']))
        self.accepted()
        with patch.dict(manager.os.environ, CONTROL_CENTER_WORKSPACE_ROOT=str(self.root / 'workspaces')):
            parent = provision_task_workspace(self.store, self.task['task_id'])
            (Path(parent['workspace_path']) / 'source.txt').write_text('Parent contribution', encoding='utf-8')
            def workspace(task_id, origin):
                return Path(provision_task_workspace(self.store, task_id, origin_kind=origin)['workspace_path'])
            with patch.object(manager, '_execution_workspace', side_effect=workspace), patch.object(manager, 'ensure_workspace_current', side_effect=ensure_workspace_current):
                result, = manager.materialize_delegations(self.task['task_id'])
            self.assertEqual(result['phase'], 'started', result)
            child = self.store.get_delegation(result['delegation_id'])['child_task_id']
            child_workspace = self.store.get_task_workspace(child)
            self.assertEqual(child_workspace['source_task_id'], self.task['task_id'])
            self.assertNotEqual(child_workspace['workspace_path'], parent['workspace_path'])
            self.assertEqual((Path(child_workspace['workspace_path']) / 'source.txt').read_text(), 'Parent contribution')
            self.assertEqual((repository / 'source.txt').read_text(), 'original\n')


if __name__ == '__main__':
    unittest.main()
