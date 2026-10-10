from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, resources, dependencies, resource_probes
from app.persistence import AgentStore
import test_dependencies


class ResourceTests(unittest.TestCase):
    def setUp(self):
        test_dependencies.DependencyTests.setUp(self)
        # Managed-coordination unit fixtures use symbolic developer ports.
        # Real ephemeral-socket behavior is covered by test_resource_probes.
        probe = patch.object(resource_probes, 'probe_resource', return_value=resource_probes.Observation('available', 'test_available'))
        self.probe = probe.start()
        self.addCleanup(probe.stop)
        reservation = patch.object(resource_probes, "reserve_port")
        self.reservation = reservation.start()
        self.addCleanup(reservation.stop)
    tearDown = test_dependencies.DependencyTests.tearDown
    create = test_dependencies.DependencyTests.create
    replacement = test_dependencies.DependencyTests.replacement
    output = test_dependencies.DependencyTests.output
    recover = test_dependencies.DependencyTests.recover
    resume = test_dependencies.DependencyTests.resume
    task_for = test_dependencies.DependencyTests.task_for
    task = test_dependencies.DependencyTests.task
    workspace = test_dependencies.DependencyTests.workspace
    git_state = test_dependencies.DependencyTests.git_state
    waiting = test_dependencies.DependencyTests.waiting
    control = test_dependencies.DependencyTests.control
    state = test_dependencies.DependencyTests.state
    status = test_dependencies.DependencyTests.status
    reasons = test_dependencies.DependencyTests.reasons
    edge = test_dependencies.DependencyTests.edge
    request = test_dependencies.DependencyTests.request

    def claim(self, task, key='tcp:8000', kind='port', **kwargs):
        return manager.create_resource_claim(task['task_id'], resource_type=kind, resource_key=key, **kwargs)

    def release(self, claim):
        return manager.release_resource_claim(claim['task_id'], claim['claim_id'])

    def current(self, claim):
        return next(c for c in manager._store.resource_claims(claim['task_id']) if c['claim_id'] == claim['claim_id'])

    def test_compatibility_matrix_and_same_task(self):
        for left in resources.MODES:
            for right in resources.MODES:
                with self.subTest(left=left, right=right):
                    a, b = self.task(), self.task()
                    first = self.claim(a, mode=left)
                    second = self.claim(b, mode=right)
                    conflict = left != 'advisory' and right != 'advisory' and 'exclusive' in (left, right)
                    self.assertEqual(second['status'], 'waiting' if conflict else 'active')
                    self.assertEqual(self.state(b)['status'], 'blocked' if conflict else 'pending')
                    self.assertFalse(self.state(b)['replanning_required'])
                    self.release(first)
                    self.release(second)
        a = self.task()
        self.claim(a)
        self.assertEqual(self.claim(a, mode='shared')['status'], 'active')
        self.assertEqual(self.state(a)['status'], 'pending')

    def test_file_default_advisory_logical_identity_and_recursive_overlap(self):
        a, b = self.task(), self.task()
        aw, bw = self.workspace(a), self.workspace(b)
        self.assertNotEqual(aw['workspace_path'], bw['workspace_path'])
        first = self.claim(a, './backend/app/main.py', 'file_path')
        second = self.claim(b, 'backend\\app\\main.py', 'file_path')
        self.assertEqual(first['resource_key'], second['resource_key'])
        self.assertEqual(second['mode'], 'advisory')
        self.assertTrue(second['potential_overlap'])
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(self.reasons(b), set())
        self.claim(a, 'backend', 'file_path', mode='exclusive', recursive=True)
        blocked = self.claim(b, 'backend/app/main.py', 'file_path', mode='exclusive')
        self.assertEqual(blocked['status'], 'waiting')
        other = self.claim(self.task(), 'backend-other/x', 'file_path', mode='exclusive')
        self.assertEqual(other['status'], 'active')

    def test_file_windows_case_identity_and_unsafe_paths(self):
        a, b = self.task(), self.task()
        first = self.claim(a, 'Backend/Main.py', 'file_path', mode='exclusive')
        second = self.claim(b, 'backend/main.py', 'file_path', mode='exclusive')
        self.assertEqual(second['status'], 'waiting' if os.name == 'nt' else 'active')
        for key in ('../secret', 'foo/../../secret', 'C:\\repo\\x', '/absolute', 'x:stream', 'nul.txt', 'foo./bar'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.claim(a, key, 'file_path')
        self.assertEqual(first['scope'], 'project')

    def test_cross_project_global_conflicts_but_files_are_separate(self):
        a = self.task()
        root = Path(self.temp.name) / 'other-project'
        root.mkdir()
        project = manager._store.create_project('Other', root)
        b = manager._store.create_task('Other task', project_id=project['project_id'])
        self.claim(a, 'main.py', 'file_path', mode='exclusive')
        self.assertEqual(self.claim(b, 'main.py', 'file_path', mode='exclusive')['status'], 'active')
        owner = self.claim(a)
        # A held file does not forbid waiting for a global resource.
        self.assertEqual(self.claim(b)['status'], 'waiting')
        for c in manager._store.resource_claims(b['task_id']):
            self.release(c)
        waiter = self.claim(b)
        self.assertEqual(waiter['status'], 'waiting')
        self.release(owner)
        self.assertEqual(self.current(waiter)['status'], 'active')

    def test_protocol_namespaces_and_explicit_scope(self):
        a, b = self.task(), self.task()
        self.claim(a, 'TCP:08000')
        self.assertEqual(self.claim(b, 'udp:8000')['status'], 'active')
        for kind, key in [('database', 'postgres:localhost:5432/appdb'), ('generic', 'build:cache')]:
            with self.assertRaises(ValueError):
                self.claim(a, key, kind)
            self.assertEqual(self.claim(a, key, kind, scope='global')['status'], 'active')
        self.assertEqual(self.claim(a, 'container:api', 'docker_resource')['scope'], 'global')
        for key in ('postgres://user:pass@host/db', 'postgres:host/db?password=x', 'postgres:password:secret'):
            with self.assertRaises(ValueError):
                self.claim(a, key, 'database', scope='global')
        for key in ('tcp:0', 'udp:65536', 'http:8000'):
            with self.assertRaises(ValueError):
                self.claim(a, key)

    def test_idempotent_creation_release_and_owner_validation(self):
        a, b = self.task(), self.task()
        claim = self.claim(a, '8000')
        self.assertEqual(self.claim(a)['claim_id'], claim['claim_id'])
        with self.assertRaises(LookupError):
            manager.release_resource_claim(b['task_id'], claim['claim_id'])
        self.release(claim)
        self.release(claim)
        self.assertEqual(self.current(claim)['status'], 'released')
        self.assertIsNotNone(self.current(claim)['released_at'])
        self.assertNotEqual(self.claim(a)['claim_id'], claim['claim_id'])

    def test_hold_and_wait_retains_owned_resources_until_available(self):
        a, b = self.task(), self.task()
        held = self.claim(a, '8000')
        owner = self.claim(b, '8001')
        waiter = self.claim(a, '8001')
        self.assertEqual(waiter['status'], 'waiting')
        self.assertEqual(self.current(held)['status'], 'active')
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.release(owner)
        self.assertEqual(self.current(waiter)['status'], 'active')
        self.assertEqual(self.state(a)['status'], 'pending')

    def test_pending_bundle_never_acquires_a_free_subset_while_waiting(self):
        a, b = self.task(), self.task()
        holder = self.claim(a)
        wait = self.claim(b)
        free = self.claim(b, '8001')
        self.assertEqual(free['status'], 'waiting')
        self.release(holder)
        self.assertEqual(self.current(wait)['status'], 'active')
        self.assertEqual(self.current(free)['status'], 'active')

    def test_running_request_blocks_before_stop_drains_and_ends_resource_reason(self):
        holder = self.claim(self.task())
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        workspace = manager._store.get_task_workspace(task['task_id'])
        def stop():
            self.assertEqual(self.reasons(task), {'resource_conflict'})
            self.assertTrue(self.state(task)['stop_required'])
            process.poll.return_value = 0
        process.terminate.side_effect = stop
        manager.agent_readers[key].join.side_effect = lambda **_: self.output(key, 'last output')
        waiter = self.claim(task)
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'resource_conflict')
        self.assertIn('last output', manager._store.full_output(key))
        with patch.object(manager, '_spawn_process') as spawn:
            self.release(holder)
            spawn.assert_not_called()
        self.assertEqual(self.current(waiter)['status'], 'active')
        self.assertEqual(self.state(task)['status'], 'pending')
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_waiting_resource_block_restores_same_session_and_reply(self):
        holder = self.claim(self.task())
        key, _, task = self.waiting()
        workspace = manager._store.get_task_workspace(task['task_id'])
        assignment = manager._store.get_active_assignment_for_task(task['task_id'])
        self.claim(task)
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.recover()
        self.release(holder)
        self.assertEqual(self.state(task)['status'], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')
        self.assertEqual(manager._store.get_active_assignment_for_task(task['task_id']), assignment)
        self.resume(key, lambda k: manager.reply_agent(k, 'A'))
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_dependency_and_resource_blockers_resolve_independently(self):
        for resource_first in (False, True):
            a, b, holder_task = self.task(), self.task(), self.task()
            holder = self.claim(holder_task)
            self.edge(b, a)
            claim = self.claim(b)
            self.assertEqual(self.reasons(b), {'dependency_incomplete', 'resource_conflict'})
            if resource_first:
                self.release(holder)
                self.assertEqual(self.reasons(b), {'dependency_incomplete'})
                self.status(a, 'completed')
            else:
                self.status(a, 'completed')
                self.assertEqual(self.reasons(b), {'resource_conflict'})
                self.release(holder)
            self.assertEqual(self.state(b)['status'], 'pending')
            self.assertEqual(self.current(claim)['status'], 'active')
            self.release(claim)

    def test_all_continuation_gates_and_required_claim_defense(self):
        self.claim(self.task())
        key, _, task = self.waiting()
        self.claim(task)
        with patch.object(manager, '_spawn_process') as spawn:
            for action in [lambda: manager.start_task_agent(task['task_id']), lambda: manager.reply_agent(key, 'A'),
                           lambda: manager.redirect_agent(key, 'change'), lambda: manager.decide_agent(key),
                           lambda: manager.decide_similar_agent(key), lambda: manager.decide_always_agent(key),
                           lambda: manager.start_agent('child', 'mock', parent_id=key)]:
                with self.assertRaises(ValueError):
                    action()
            # Even a corrupt/stale status and missing blocker cannot bypass the
            # durable missing-claim gate before spawn.
            with manager._store._connection() as db:
                db.execute("UPDATE tasks SET status='waiting' WHERE task_id=?", (task['task_id'],))
                db.execute("UPDATE task_blockers SET active=0 WHERE task_id=?", (task['task_id'],))
            with self.assertRaises(ValueError):
                manager.reply_agent(key, 'A')
            spawn.assert_not_called()

    def test_pause_suspends_and_resume_reacquires_or_blocks(self):
        a, b = self.task(), self.task()
        first, second = self.claim(a), self.claim(b)
        self.control(a, 'paused')
        self.assertEqual(self.current(first)['status'], 'suspended')
        self.assertEqual(self.current(second)['status'], 'active')
        self.control(a, 'active')
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.release(second)
        self.assertEqual(self.state(a)['status'], 'pending')
        self.assertEqual(self.current(first)['status'], 'active')
        self.control(a, 'paused')
        self.control(a, 'active')
        self.assertEqual(self.current(first)['status'], 'active')

    def test_pause_stop_failure_does_not_give_resource_to_another_worker(self):
        key, process = self.create(kind='mock')
        a, b = self.task_for(key), self.task()
        first, second = self.claim(a), self.claim(b)
        process.terminate.side_effect = OSError('stop failed')
        with self.assertRaises(OSError):
            self.control(a, 'paused')
        self.assertEqual(self.current(first)['status'], 'active')
        self.assertNotEqual(self.current(second)['status'], 'active')
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        self.control(a, 'paused')
        self.assertEqual(self.current(second)['status'], 'active')

    def test_dependency_block_suspends_holder_and_reacquires_after_resolution(self):
        a, b, prerequisite = self.task(), self.task(), self.task()
        first, second = self.claim(a), self.claim(b)
        self.edge(a, prerequisite)
        self.assertEqual(self.current(first)['status'], 'suspended')
        self.assertEqual(self.current(second)['status'], 'active')
        self.status(prerequisite, 'completed')
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.assertEqual(self.reasons(a), {'resource_conflict'})
        self.release(second)
        self.assertEqual(self.state(a)['status'], 'pending')

    def test_worker_scope_released_on_assignment_end_task_scope_survives_stop(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        durable = self.claim(task)
        worker = self.claim(task, '8001', lifetime='worker')
        self.assertEqual(worker['assignment_id'], manager._store.get_active_assignment_for_task(task['task_id'])['assignment_id'])
        manager.stop_agent(key)
        self.assertEqual(self.current(worker)['status'], 'released')
        self.assertEqual(self.current(durable)['status'], 'active')
        with patch.object(manager, '_spawn_process', return_value=self.replacement()), patch.object(manager, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            manager.start_task_agent(task['task_id'])
        new = self.claim(task, '8001', lifetime='worker')
        self.assertNotEqual(new['assignment_id'], worker['assignment_id'])
        self.assertNotEqual(new['claim_id'], worker['claim_id'])

    def test_complete_and_cancel_release_all_claims_without_deleting_history(self):
        for cancel in (False, True):
            key, _ = self.create(kind='mock')
            task = self.task_for(key)
            first = self.claim(task)
            worker = self.claim(task, '8001', lifetime='worker')
            waiter = self.claim(self.task())
            if cancel:
                self.control(task, 'canceled')
            else:
                self.output(key, 'Done', True)
            self.assertEqual(self.current(first)['status'], 'released')
            self.assertEqual(self.current(worker)['status'], 'released')
            self.assertEqual(self.current(waiter)['status'], 'active')
            self.release(waiter)

    def test_arbitration_grants_compatible_shared_waiters_but_not_exclusive(self):
        owner = self.claim(self.task())
        a = self.claim(self.task(), mode='shared')
        b = self.claim(self.task(), mode='shared')
        c = self.claim(self.task())
        self.release(owner)
        self.assertEqual(self.current(a)['status'], 'active')
        self.assertEqual(self.current(b)['status'], 'active')
        self.assertEqual(self.current(c)['status'], 'waiting')
        self.release(a)
        self.assertEqual(self.current(c)['status'], 'waiting')
        self.release(b)
        self.assertEqual(self.current(c)['status'], 'active')

    def test_concurrent_exclusive_acquisition_is_serialized(self):
        a, b = self.task(), self.task()
        barrier = Barrier(2)
        def acquire(task):
            store = AgentStore(self.path)
            barrier.wait()
            return store.create_resource_claim(task['task_id'], resource_type='port', resource_key='8000')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(acquire, (a, b)))
        states = [manager._store.resource_claims(t['task_id'])[0]['status'] for t in (a, b)]
        self.assertEqual(sorted(states), ['active', 'waiting'])

    def test_release_request_race_has_one_owner_and_no_phantom_blocker(self):
        a, b = self.task(), self.task()
        claim = self.claim(a)
        barrier = Barrier(2)
        def action(release):
            store = AgentStore(self.path)
            barrier.wait()
            if release:
                store.release_resource_claim(a['task_id'], claim['claim_id'])
            else:
                store.create_resource_claim(b['task_id'], resource_type='port', resource_key='8000')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(action, (False, True)))
        self.assertEqual(manager._store.resource_claims(b['task_id'])[0]['status'], 'active')
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(self.reasons(b), set())

    def test_recovery_preserves_mixed_claims_and_blockers_without_spawn(self):
        active = self.claim(self.task())
        a, b = self.task(), self.task()
        self.edge(b, a)
        waiting = self.claim(b)
        paused_task = self.task()
        paused = self.claim(paused_task, '8001')
        self.control(paused_task, 'paused')
        released = self.claim(self.task(), '8002')
        self.release(released)
        key, _ = self.create(kind='mock')
        stale = self.claim(self.task_for(key), '8003', lifetime='worker')
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            spawn.assert_not_called()
        self.assertEqual(self.current(active)['status'], 'active')
        self.assertEqual(self.current(waiting)['status'], 'suspended')
        self.assertEqual(self.reasons(b), {'resource_conflict', 'dependency_incomplete'})
        self.assertEqual(self.current(paused)['status'], 'suspended')
        self.assertEqual(self.current(released)['status'], 'released')
        self.assertEqual(self.current(stale)['status'], 'released')

    def test_validation_and_api_inspection(self):
        task = self.task()
        path = '/tasks/' + task['task_id'] + '/resource-claims'
        claim = self.request(path, 'POST', {'resource_type': 'file_path', 'resource_key': 'src/main.py'})
        self.assertEqual(claim['mode'], 'advisory')
        self.assertEqual(self.request(path)['claims'], [claim])
        self.request(path, 'POST', {'resource_type': 'invalid', 'resource_key': 'x'}, 422)
        self.request(path, 'POST', {'resource_type': 'port', 'resource_key': '8000', 'lifetime': 'worker'}, 409)
        self.request(path + '/' + claim['claim_id'], 'DELETE')
        self.request('/tasks/missing/resource-claims', expected=404)
        self.control(task, 'canceled')
        self.request(path, 'POST', {'resource_type': 'port', 'resource_key': '8000'}, 409)

    def test_claim_transaction_failure_never_stops_or_publishes(self):
        self.claim(self.task())
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        with manager._store._connection() as db:
            db.execute("CREATE TRIGGER reject_resource BEFORE INSERT ON task_blockers WHEN NEW.blocker_type='resource' BEGIN SELECT RAISE(ABORT,'fail'); END")
        with patch.object(manager.changes, 'publish') as publish, self.assertRaises(sqlite3.Error):
            self.claim(task)
        publish.assert_not_called()
        process.terminate.assert_not_called()
        self.assertEqual(manager._store.resource_claims(task['task_id']), [])

    def test_multiple_resource_blockers_retain_claim_provenance(self):
        a, b, c = self.task(), self.task(), self.task()
        owner1, owner2 = self.claim(a, mode='shared'), self.claim(b, mode='shared')
        waiter = self.claim(c)
        blockers = self.state(c)['active_blockers']
        self.assertEqual(len(blockers), 2)
        self.assertEqual({r['waiting_claim_id'] for r in blockers}, {waiter['claim_id']})
        self.assertEqual({r['source_id'] for r in blockers}, {owner1['claim_id'], owner2['claim_id']})
        self.release(owner1)
        self.assertEqual(len(self.state(c)['active_blockers']), 1)
        self.assertEqual(self.state(c)['status'], 'blocked')

    def race(self, *actions):
        barrier = Barrier(len(actions))
        def run(action):
            store = AgentStore(self.path)
            barrier.wait()
            try:
                action(store)
                return 'ok'
            except ValueError:
                return 'rejected'
        with ThreadPoolExecutor(len(actions)) as pool:
            return list(pool.map(run, actions))

    def test_pause_acquisition_race_never_leaves_paused_ownership(self):
        task = self.task()
        self.race(lambda s: s.request_work_control(task['task_id'], 'paused'),
                  lambda s: s.create_resource_claim(task['task_id'], resource_type='port', resource_key='8000'))
        manager._settle_tasks([task['task_id']])
        self.assertEqual(self.state(task)['status'], 'paused')
        self.assertEqual(manager._store.resource_claims(task['task_id'])[0]['status'], 'suspended')

    def test_worker_exit_acquisition_race_has_no_phantom_worker_claim(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        assignment = manager._store.get_active_assignment_for_task(task['task_id'])
        self.race(lambda s: s.end_assignment(assignment['assignment_id'], 'completed'),
                  lambda s: s.create_resource_claim(task['task_id'], resource_type='port', resource_key='8000', lifetime='worker'))
        self.assertEqual(self.state(task)['status'], 'completed')
        self.assertTrue(all(c['status'] == 'released' for c in manager._store.resource_claims(task['task_id'])))

    def test_cancel_waiter_release_race_does_not_reactivate_canceled_work(self):
        a, b = self.task(), self.task()
        first, second = self.claim(a), self.claim(b)
        self.race(lambda s: s.request_work_control(b['task_id'], 'canceled'),
                  lambda s: s.release_resource_claim(a['task_id'], first['claim_id']))
        manager._settle_tasks([b['task_id']])
        self.assertEqual(self.current(second)['status'], 'released')
        self.assertEqual(self.state(b)['status'], 'canceled')

    def test_release_and_dependency_invalidation_race_preserves_dependency_gate(self):
        a, b, prerequisite = self.task(), self.task(), self.task()
        first, second = self.claim(a), self.claim(b)
        self.race(lambda s: s.change_dependency(b['task_id'], prerequisite['task_id']),
                  lambda s: s.release_resource_claim(a['task_id'], first['claim_id']))
        self.assertEqual(self.current(second)['status'], 'suspended')
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(self.reasons(b), {'dependency_incomplete'})

    def test_release_during_stop_keeps_resource_end_reason_and_stop_authority(self):
        first = self.claim(self.task())
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        def stop():
            manager._store.release_resource_claim(first['task_id'], first['claim_id'])
            self.assertTrue(self.state(task)['stop_required'])
            self.assertEqual(self.state(task)['stop_reason'], 'resource_conflict')
            process.poll.return_value = 0
        process.terminate.side_effect = stop
        self.claim(task)
        self.assertEqual(self.state(task)['status'], 'pending')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'resource_conflict')

    def test_resource_stop_failure_retains_gate_and_retry_is_idempotent(self):
        self.claim(self.task())
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        process.terminate.side_effect = OSError('cannot stop')
        with self.assertRaises(OSError):
            self.claim(task)
        claim = manager._store.resource_claims(task['task_id'])[0]
        self.assertEqual(self.state(task)['status'], 'in_progress')
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(self.reasons(task), {'resource_conflict'})
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_task_agent(task['task_id'])
        spawn.assert_not_called()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        self.assertEqual(self.claim(task)['claim_id'], claim['claim_id'])
        self.assertEqual(self.state(task)['status'], 'blocked')

    def test_resume_bundle_does_not_partially_hold_resources(self):
        a, b = self.task(), self.task()
        first, second = self.claim(a), self.claim(a, '8001')
        self.control(a, 'paused')
        owner = self.claim(b)
        self.control(a, 'active')
        self.assertEqual(self.current(first)['status'], 'waiting')
        self.assertEqual(self.current(second)['status'], 'waiting')
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.release(owner)
        self.assertEqual(self.current(first)['status'], 'active')
        self.assertEqual(self.current(second)['status'], 'active')

    def test_advisory_claim_does_not_stop_worker_or_count_as_held_lock(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        self.claim(self.task(), 'source.txt', 'file_path', mode='exclusive')
        self.claim(task, 'source.txt', 'file_path')
        process.terminate.assert_not_called()
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.claim(self.task())
        self.claim(task)
        process.terminate.assert_called_once()

    def test_subtree_ownership_and_resource_reacquisition_coexist(self):
        root = self.task()
        child = self.task(root)
        parent_claim, child_claim = self.claim(root), self.claim(child, '8001')
        self.control(child, 'paused')
        self.control(root, 'paused')
        self.recover()
        self.control(root, 'active')
        self.assertEqual(self.current(parent_claim)['status'], 'active')
        self.assertEqual(self.current(child_claim)['status'], 'suspended')
        self.assertEqual(self.state(child)['status'], 'paused')
        self.control(child, 'active')
        self.assertEqual(self.current(child_claim)['status'], 'active')

    def test_claims_do_not_change_workspace_canonical_or_integration_history(self):
        key, _, task = self.waiting()
        workspace = manager._store.get_task_workspace(task['task_id'])
        manager.integrate_task(task['task_id'])
        history = manager._store.list_integrations(task['task_id'])
        context = manager._store.get_agent_source_context(key)
        path = Path(workspace['workspace_path']) / 'source.txt'
        path.write_bytes(b'dirty task content')
        canonical = self.git_state(manager._project_root)
        first = self.claim(self.task())
        self.claim(task)
        self.control(task, 'paused')
        self.release(first)
        self.control(task, 'active')
        self.assertEqual(path.read_bytes(), b'dirty task content')
        self.assertEqual(self.git_state(manager._project_root), canonical)
        self.assertEqual(manager._store.list_integrations(task['task_id']), history)
        self.assertEqual(manager._store.get_agent_source_context(key), context)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_completion_arbitration_commits_before_sse_publication(self):
        key, _ = self.create(kind='mock')
        self.claim(self.task_for(key))
        waiting = self.claim(self.task())
        seen = []
        with patch.object(manager.changes, 'publish', side_effect=lambda _: seen.append(self.current(waiting)['status'])):
            self.output(key, 'Done', True)
        self.assertEqual(seen[-1], 'active')

    def test_unrelated_trees_coordinate_without_dependency_edges(self):
        root_a, root_b = self.task(), self.task()
        a, b = self.task(root_a), self.task(root_b)
        owner, waiter = self.claim(a), self.claim(b)
        self.assertEqual(waiter['status'], 'waiting')
        self.assertEqual(manager._store.list_dependencies(b['task_id']), [])
        self.assertEqual(self.state(root_b)['status'], 'pending')
        self.release(owner)
        self.assertEqual(self.current(waiter)['status'], 'active')

    def test_stop_branch_releases_worker_claims_and_stale_finalizer_cannot_release_replacement(self):
        root, old = self.create(kind='mock')
        child, _ = self.create(root, kind='mock')
        task = self.task_for(root)
        durable = self.claim(task)
        worker = self.claim(self.task_for(child), '8001', lifetime='worker')
        manager.stop_branch(root)
        self.assertEqual(self.current(durable)['status'], 'active')
        self.assertEqual(self.current(worker)['status'], 'released')
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread:
            thread.return_value.is_alive.return_value = False
            result = manager.start_task_agent(task['task_id'])
        fresh = self.claim(task, '8002', lifetime='worker')
        manager._finalize_process(root, old)
        self.assertEqual(self.current(fresh)['status'], 'active')
        self.assertEqual(manager.agent_statuses[result['agent_id']], 'running')

    def test_resource_release_does_not_clear_dependency_replanning(self):
        prerequisite, task = self.task(), self.task()
        owner = self.claim(self.task())
        self.claim(task)
        self.edge(task, prerequisite)
        self.control(prerequisite, 'canceled')
        self.release(owner)
        self.assertEqual(self.reasons(task), {'dependency_canceled'})
        self.assertTrue(self.state(task)['replanning_required'])
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.edge(task, prerequisite, True)
        self.assertEqual(self.state(task)['status'], 'pending')
        self.assertFalse(self.state(task)['replanning_required'])

    def test_arbitration_preserves_unrelated_generic_resource_blockers(self):
        a, b = self.task(), self.task()
        owner, waiter = self.claim(a), self.claim(b)
        with manager._store._connection() as db:
            db.execute("INSERT INTO task_blockers(id,task_id,blocker_type,reason_code) VALUES (?,?,'resource','resource_busy')", (str(uuid4()), b['task_id']))
        self.release(owner)
        self.assertEqual(self.reasons(b), {'resource_busy'})
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(self.current(waiter)['status'], 'suspended')
        # The generic blocker's own producer resolves it; acquisition now resumes.
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE task_blockers SET active=0 WHERE task_id=? AND reason_code='resource_busy'", (b['task_id'],))
            dependencies.reconcile(db)
        self.assertEqual(self.current(waiter)['status'], 'active')
        self.assertEqual(self.state(b)['status'], 'pending')

    def legacy_snapshot(self):
        with manager._store._connection() as db:
            result = {}
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'resource_claims'"):
                table = row[0]
                columns = [r['name'] for r in db.execute(f'PRAGMA table_info({table})')
                           if r['name'] not in ('stop_reason', 'source_type', 'source_id', 'waiting_claim_id', 'owning_claim_id')]
                result[table] = [tuple(r) for r in db.execute(f'SELECT rowid,{",".join(columns)} FROM {table} ORDER BY rowid')]
            return result

    def downgrade_v12(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP INDEX external_resource_blocker')
            db.execute('DROP INDEX resource_blocker_active')
            db.execute('DROP INDEX task_blockers_active')
            for column in ('source_type', 'source_id', 'waiting_claim_id', 'owning_claim_id'):
                db.execute('ALTER TABLE task_blockers DROP COLUMN ' + column)
            db.execute('DROP TABLE resource_claims')
            db.execute('ALTER TABLE tasks DROP COLUMN stop_reason')
            db.execute("CREATE UNIQUE INDEX task_blockers_active ON task_blockers(task_id,blocker_type,COALESCE(source_task_id,''),reason_code) WHERE active=1")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
            indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_assignments' AND sql IS NOT NULL")]
            sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v12', sql, count=1)
            db.execute(sql.replace(",'resource_conflict'", ''))
            columns = ','.join('"' + r[1] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
            db.execute(f'INSERT INTO assignments_v12(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
            db.execute('DROP TABLE task_assignments')
            db.execute('ALTER TABLE assignments_v12 RENAME TO task_assignments')
            for index in indexes:
                db.execute(index)
            db.execute('PRAGMA user_version=12')

    def test_real_v12_migration_preserves_all_foundation_data_without_claims(self):
        key, _, task = self.waiting()
        child = self.task(task)
        other = self.task()
        manager.integrate_task(task['task_id'])
        self.edge(other, child)
        self.control(child, 'canceled')
        self.control(task, 'paused')
        before = self.legacy_snapshot()
        self.downgrade_v12()
        store = AgentStore(self.path)
        self.assertEqual(self.legacy_snapshot(), before)
        self.assertEqual(store.resource_claims(task['task_id']), [])
        self.assertEqual(AgentStore(self.path).list_tasks(), store.list_tasks())
        self.assertEqual(store.load_agents()[0]['session_id'], key)
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_claims').fetchone()[0], 0)

    def test_migration_failure_rolls_back_columns_table_and_version(self):
        task = self.task()
        self.downgrade_v12()
        original = resources.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('migration failure')
        with patch.object(resources, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 12)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='resource_claims'").fetchone())
            self.assertEqual(db.execute('SELECT task_id FROM tasks').fetchone()[0], task['task_id'])
            self.assertNotIn('stop_reason', [r[1] for r in db.execute('PRAGMA table_info(tasks)')])


if __name__ == '__main__':
    unittest.main()
