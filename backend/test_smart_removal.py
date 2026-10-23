"""Stage 2D.2: hierarchy-aware Agent removal and explicit responsibility transfer.

Temporary SQLite databases, temporary repositories and process doubles; a real
Windows class uses disposable supervised interpreters only (no Codex, no AI).
"""
from contextlib import closing
import json
import os
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager
from app import agent_removal, executions, execution_diagnostics as diagnostics, execution_attestation as attestation
from app import responsibility, work_control, delegation_orchestration as orchestration
from app.persistence import AgentStore
from execution_test_support import genuine_unsupervised, rebooted
import test_agent_removal
import test_delegation_hybrid as hybrid_tests
import test_executions
import test_persistence
from test_executions import alive_pid, until


class SmartRemovalTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    request = test_agent_removal.AgentRemovalTests.request
    worker = test_agent_removal.AgentRemovalTests.worker
    assignments = test_agent_removal.AgentRemovalTests.assignments
    task = test_agent_removal.AgentRemovalTests.task
    agent_row = test_agent_removal.AgentRemovalTests.agent_row
    blockers = test_agent_removal.AgentRemovalTests.blockers
    failing_stop = test_agent_removal.AgentRemovalTests.failing_stop
    working_stop = test_agent_removal.AgentRemovalTests.working_stop

    # -- helpers --------------------------------------------------------------
    def remove(self, key, mode='single', request_key=None, token=None, **options):
        token = token or manager.removal_preview(key, mode)['plan_token']
        return manager.remove_agent(key, request_key=request_key or str(uuid4()), plan_token=token, mode=mode, **options)

    def admit_only(self, key, mode='retire_branch', request_key='crash-key'):
        token = manager.removal_preview(key, mode)['plan_token']
        operation, created = manager._store.admit_removal(key, request_key=request_key, plan_token=token, mode=mode,
                                                          disposition='preserve', reason=None)
        self.assertTrue(created)
        return operation

    def family(self):
        """root -> child -> grandchild, plus an unrelated root."""
        root, _ = self.worker()
        child, _ = self.worker(parent=root)
        grandchild, _ = self.worker(parent=child)
        unrelated, _ = self.worker()
        return root, child, grandchild, unrelated

    def started(self, action):
        """Run a start path with a process double (no real worker)."""
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        with patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_codex_command', return_value='fixed command'), patch.object(manager, 'Thread'):
            result = action()
        for key, reader in manager.agent_readers.items():
            reader.is_alive.return_value = False
        return result

    def transfer(self, task_id, request_key=None, expected=None, **options):
        return self.started(lambda: manager.transfer_responsibility(
            task_id, request_key=request_key or str(uuid4()), expected_owner_assignment_id=expected, **options))

    def claim(self, task_id, key='db:main'):
        claim_id = manager._store.create_resource_claim(task_id, resource_type='generic', resource_key=key,
                                                        scope='project', lifetime='worker')
        return claim_id

    def claim_status(self, task_id, claim_id):
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)['status']

    def count_agents(self):
        with manager._store._connection() as db:
            return db.execute('SELECT COUNT(*) FROM agents').fetchone()[0]

    # -- individual removal -----------------------------------------------------
    def test_removing_a_parent_keeps_independent_children_working(self):
        root, child, grandchild, unrelated = self.family()
        before = self.count_agents()
        preview = manager.removal_preview(root)
        self.assertTrue(preview['options']['single']['available'])
        self.assertIn('children_continue', preview['consequences'])
        self.assertIn('task_awaits_responsibility_transfer', preview['consequences'])
        self.assertEqual({d['agent_id']: d['continuation'] for d in preview['descendants']},
                         {child: 'continues', grandchild: 'continues'})
        operation = self.remove(root, token=preview['plan_token'])
        self.assertEqual(operation['phase'], 'removed')
        self.assertEqual([m['agent_id'] for m in operation['members']], [root])  # only the selected Agent
        for key in (child, grandchild, unrelated):
            self.assertEqual(manager.agent_statuses[key], 'running')
            self.assertIsNone(self.agent_row(key)['removed_at'])
            self.assertIsNone(self.assignments(key)[0]['ended_at'])
        # Historical lineage is immutable: no reparenting and no replacement parent.
        self.assertEqual(self.agent_row(child)['parent_id'], root)
        self.assertEqual(self.agent_row(grandchild)['parent_id'], child)
        self.assertEqual(self.count_agents(), before)
        # Independent Children keep their own authority: a Child still finishes its own Task.
        self.output(grandchild, 'Done\n', finish=True)
        self.assertEqual(self.task(grandchild)['status'], 'completed')
        # The removed Agent's Task keeps its identity and waits for an explicit new owner.
        state = manager.responsibility_state(self.task(root)['task_id'])
        self.assertTrue(state['awaiting_transfer'] and state['transferable'])
        self.assertEqual(state['removed_owner']['agent_id'], root)

    def test_a_removed_agent_never_regains_authority(self):
        root, child, grandchild, _ = self.family()
        self.remove(root)
        refusals = {
            'new Child': lambda: self.worker(parent=root),
            'reply': lambda: manager.reply_agent(root, 'answer'),
            'redirect': lambda: manager.redirect_agent(root, 'new instruction'),
        }
        for name, attempt in refusals.items():
            with self.subTest(name), patch.object(manager, '_spawn_process', side_effect=AssertionError('no process')), \
                    self.assertRaises((ValueError, LookupError)):
                attempt()
        with manager._store._connection() as db:
            for check in (agent_removal.require_operational, agent_removal.require_parent_operational):
                with self.assertRaisesRegex(ValueError, 'removed'):
                    check(db, root)
        # Its living Child may still create its own Children.
        great, _ = self.worker(parent=grandchild)
        self.assertEqual(self.agent_row(great)['parent_id'], grandchild)

    def test_a_child_start_racing_admission_is_refused_in_its_admission_transaction(self):
        root, _ = self.worker()
        self.admit_only(root, mode='single')
        # Simulate the early check having passed before the removal was admitted:
        # the launch's own write transaction still refuses the new lineage.
        with patch.object(manager._store, 'require_agent_operational'), \
                self.assertRaisesRegex(ValueError, 'Parent Agent removal is in progress'):
            self.worker(parent=root)
        self.assertEqual([d['agent_id'] for d in manager.removal_preview(root)['descendants']], [])

    # -- branch removal ---------------------------------------------------------
    def test_branch_removal_retires_exactly_the_previewed_branch(self):
        root, child, grandchild, unrelated = self.family()
        preview = manager.removal_preview(root, 'retire_branch')
        self.assertTrue(preview['options']['retire_branch']['available'])
        self.assertEqual([(m['agent_id'], m['role'], m['depth']) for m in preview['members']],
                         [(root, 'target', 0), (child, 'descendant', 1), (grandchild, 'descendant', 2)])
        tasks = {key: self.task(key) for key in (root, child, grandchild)}
        operation = self.remove(root, 'retire_branch', token=preview['plan_token'])
        self.assertEqual(operation['phase'], 'removed')
        self.assertEqual([(m['agent_id'], m['phase'], m['stop_order']) for m in operation['members']],
                         [(root, 'removed', 0), (child, 'removed', 1), (grandchild, 'removed', 2)])
        for key, task in tasks.items():
            self.assertIsNotNone(self.agent_row(key)['removed_at'])
            self.assertEqual(self.agent_row(key)['removal_id'], operation['removal_id'])
            self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
            current = manager._store.get_task(task['task_id'])
            self.assertEqual((current['task_id'], current['description']), (task['task_id'], task['description']))
            self.assertNotIn(current['status'], ('completed', 'canceled'))  # never completed or deleted
        self.assertEqual(manager.agent_statuses[unrelated], 'running')  # unrelated work continues
        self.assertEqual((self.agent_row(child)['parent_id'], self.agent_row(grandchild)['parent_id']), (root, child))
        for key in (root, child, grandchild):
            self.assertEqual(manager.list_agent_removals(key), [manager.get_removal(operation['removal_id'])])

    def test_a_stale_branch_preview_never_expands_silently(self):
        root, child, grandchild, _ = self.family()
        token = manager.removal_preview(root, 'retire_branch')['plan_token']
        late, _ = self.worker(parent=child)
        with self.assertRaises(agent_removal.StalePreview) as error:
            self.remove(root, 'retire_branch', token=token)
        self.assertIn(late, [m['agent_id'] for m in error.exception.details['preview']['members']])
        self.assertEqual(manager.list_agent_removals(root), [])
        self.assertEqual(manager.agent_statuses[late], 'running')
        # After admission the member set is fixed and the branch cannot grow.
        operation = self.admit_only(root)
        with self.assertRaises(ValueError):
            self.worker(parent=grandchild)
        self.assertEqual(len(manager.get_removal(operation['removal_id'])['members']), 4)

    def test_previously_removed_descendants_are_not_removed_again(self):
        root, child, grandchild, _ = self.family()
        first = self.remove(grandchild)
        preview = manager.removal_preview(root, 'retire_branch')
        self.assertEqual([m['agent_id'] for m in preview['members']], [root, child])
        self.assertTrue(next(d for d in preview['descendants'] if d['agent_id'] == grandchild)['removed'])
        operation = self.remove(root, 'retire_branch', token=preview['plan_token'])
        self.assertEqual(len(operation['members']), 2)
        self.assertEqual(self.agent_row(grandchild)['removal_id'], first['removal_id'])  # history kept
        with manager._store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_removal_members WHERE agent_id=?', (grandchild,)).fetchone()[0], 1)

    def test_overlapping_removals_are_refused_until_the_first_settles(self):
        root, _ = self.worker()
        child, _ = self.worker(kind='codex', parent=root)
        with self.failing_stop():
            pending = self.remove(child)
        self.assertEqual(pending['phase'], 'recovery_required')
        preview = manager.removal_preview(root, 'retire_branch')
        self.assertEqual(preview['options']['retire_branch']['reasons'], ['descendant_removal_in_progress'])
        with self.assertRaises(agent_removal.RemovalError) as error:
            self.remove(root, 'retire_branch', token=preview['plan_token'])
        self.assertEqual(error.exception.code, 'not_available')
        # A database-level race is refused by the live-membership index as well.
        current = dict(preview, options={**preview['options'], 'retire_branch': {'available': True, 'reasons': []}})
        with self.assertRaises(agent_removal.RemovalError) as raced, patch.object(agent_removal, 'plan', return_value=current):
            # The store's transaction rolls back the whole admission on refusal.
            manager._store.admit_removal(root, request_key='race', plan_token=preview['plan_token'],
                                         mode='retire_branch', disposition='preserve', reason=None)
        self.assertEqual(raced.exception.code, 'descendant_removal_in_progress')
        self.assertEqual(manager.list_agent_removals(root), [])
        with self.working_stop():
            manager.retry_removal(pending['removal_id'])
        operation = self.remove(root, 'retire_branch')
        self.assertEqual([m['agent_id'] for m in operation['members']], [root])
        # A member of a live branch cannot be removed individually.
        other, _ = self.worker()
        other_child, _ = self.worker(kind='codex', parent=other)
        with self.failing_stop():
            branch = self.remove(other, 'retire_branch')
        self.assertEqual(manager.removal_preview(other_child)['options']['single']['reasons'], ['removal_in_progress'])
        with self.working_stop():
            self.assertEqual(manager.retry_removal(branch['removal_id'])['phase'], 'removed')

    def test_partial_branch_failure_keeps_ownership_and_retry_finishes_without_duplicates(self):
        root, _ = self.worker()
        stuck, _ = self.worker(kind='codex', parent=root)
        leaf, _ = self.worker(parent=stuck)
        stuck_task = self.task(stuck)['task_id']
        claim = self.claim(stuck_task)
        with self.failing_stop():
            operation = self.remove(root, 'retire_branch')
        self.assertEqual(operation['phase'], 'recovery_required')
        phases = {m['agent_id']: m['phase'] for m in operation['members']}
        self.assertEqual(phases, {root: 'removed', stuck: 'recovery_required', leaf: 'removed'})
        self.assertTrue(operation['last_error'].startswith('stop_not_confirmed'))
        # The unconfirmed member keeps its assignment, claims and gates; nothing is claimed as removed.
        self.assertIsNone(self.agent_row(stuck)['removed_at'])
        self.assertIsNone(self.assignments(stuck)[0]['ended_at'])
        self.assertEqual(self.claim_status(stuck_task, claim), 'active')
        with manager._store._connection() as db, self.assertRaises(ValueError):
            agent_removal.require_operational(db, stuck)
        # Settled members are never resurrected.
        self.assertIsNotNone(self.agent_row(root)['removed_at'])
        with self.working_stop():
            settled = manager.retry_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertEqual(len(settled['members']), 3)
        self.assertEqual(self.claim_status(stuck_task, claim), 'released')
        self.assertEqual(manager.retry_removal(operation['removal_id']), settled)  # idempotent

    def test_branch_member_with_unresolved_execution_settles_only_with_evidence_or_attestation(self):
        root, _ = self.worker()
        with genuine_unsupervised():
            child, _ = self.worker(parent=root)
            child_task = self.task(child)['task_id']
            claim = self.claim(child_task)
            self.output(child, 'Done\n', finish=True)  # an unsupervised end: never proof
        operation = self.remove(root, 'retire_branch')
        member = next(m for m in operation['members'] if m['agent_id'] == child)
        self.assertEqual(member['phase'], 'recovery_required')
        self.assertTrue(member['failure'].startswith('execution_unresolved'))
        self.assertEqual(self.claim_status(child_task, claim), 'active')
        self.assertNotEqual(manager._store.get_task(child_task)['status'], 'completed')
        # Retrying without evidence settles nothing.
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'recovery_required')
        # A P3 human attestation within its strict eligibility rules settles the execution.
        execution, = manager._store.agent_executions(child)
        with patch.object(diagnostics.DiagnosticProbe, 'scan', lambda: []):
            reviewed = manager.refresh_execution_diagnostics(execution['execution_id'])['attestation_offer']
            self.assertTrue(reviewed['eligible'])
            manager.attest_execution(execution['execution_id'], request_key='attest', statement=attestation.STATEMENT,
                                     reviewed_diagnostic_id=reviewed['reviewed_diagnostic_id'], actor={'channel': 'test'})
        settled = manager.retry_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertEqual(self.claim_status(child_task, claim), 'released')

    def test_branch_launch_in_progress_waits(self):
        root, child, _, _ = self.family()
        late_task = manager._store.create_task('Late child', parent_task_id=self.task(child)['task_id'])
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            reservation = executions.reserve(db, manager._controller, agent_id=str(uuid4()), task_id=late_task['task_id'],
                                             provider='mock', containment='none')
        preview = manager.removal_preview(root, 'retire_branch')
        self.assertIn('launch_in_progress', preview['options']['retire_branch']['reasons'])
        self.assertTrue(manager.removal_preview(root)['options']['single']['available'])  # Children continue anyway
        with manager._store._connection() as db:
            db.execute('DELETE FROM executions WHERE execution_id=?', (reservation,))
        self.assertTrue(manager.removal_preview(root, 'retire_branch')['options']['retire_branch']['available'])

    def test_crash_during_branch_removal_resumes_from_durable_state_only(self):
        root, child, grandchild, unrelated = self.family()
        operation = self.admit_only(root)
        with patch.object(manager, '_spawn_process', side_effect=AssertionError('never resumed')):
            self.recover()
        current = manager.get_removal(operation['removal_id'])
        # Running doubles of an earlier controller: their end is not established.
        self.assertEqual(current['phase'], 'recovery_required')
        self.assertTrue(all(m['phase'] == 'recovery_required' for m in current['members']))
        self.assertTrue(all(self.agent_row(k)['removed_at'] is None for k in (root, child, grandchild)))
        with manager._store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_removal_members WHERE removal_id=?',
                                        (operation['removal_id'],)).fetchone()[0], 3)
        with rebooted(), patch.object(manager, '_spawn_process', side_effect=AssertionError('never resumed')):
            self.recover()  # verified Windows restart: kernel-level evidence
        settled = manager.get_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertTrue(all(m['outcome'] == 'settled_by_restart_recovery' for m in settled['members']))
        self.assertIsNone(self.agent_row(unrelated)['removed_at'])
        with manager._store._connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM agents WHERE removed_at IS NOT NULL").fetchone()[0], 3)

    def test_repeated_branch_submission_is_idempotent(self):
        root, child, grandchild, _ = self.family()
        token = manager.removal_preview(root, 'retire_branch')['plan_token']
        first = self.remove(root, 'retire_branch', request_key='same', token=token)
        with patch.object(manager, 'stop_agent', side_effect=AssertionError('no second stop')):
            self.assertEqual(self.remove(root, 'retire_branch', request_key='same', token=token), first)
        with self.assertRaises(agent_removal.RemovalError) as conflict:
            self.remove(root, 'single', request_key='same', token=token)
        self.assertEqual(conflict.exception.code, 'request_key_conflict')
        with manager._store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_removals').fetchone()[0], 1)

    # -- responsibility transfer -------------------------------------------------
    def test_explicit_transfer_establishes_a_new_owner_and_keeps_history(self):
        key, _ = self.worker()
        task = self.task(key)
        removal = self.remove(key)
        before = self.count_agents()
        self.assertEqual(self.count_agents(), before)  # removal created no replacement
        state = manager.responsibility_state(task['task_id'])
        expected = state['removed_owner']['assignment_id']
        with self.assertRaises(responsibility.TransferError) as stale:
            self.transfer(task['task_id'], expected=str(uuid4()))
        self.assertEqual(stale.exception.code, 'stale_responsibility')
        transfer = self.transfer(task['task_id'], request_key='t1', expected=expected, reason='Take over the parser')
        self.assertEqual(transfer['phase'], 'completed')
        self.assertEqual((transfer['from_agent_id'], transfer['removal_id'], transfer['source']), (key, removal['removal_id'], 'user'))
        new = transfer['to_agent_id']
        self.assertNotEqual(new, key)
        history = manager._store.list_task_assignments(task['task_id'])
        self.assertEqual([(a['agent_id'], a['ended_reason']) for a in history], [(key, 'removed'), (new, None)])
        self.assertEqual(transfer['to_assignment_id'], history[1]['assignment_id'])
        self.assertEqual(self.count_agents(), before + 1)
        # Exact replay never starts a second owner; a new request finds the owner present.
        with patch.object(manager, 'start_agent', side_effect=AssertionError('no second start')):
            self.assertEqual(manager.transfer_responsibility(task['task_id'], request_key='t1', expected_owner_assignment_id=expected,
                                                             agent_type='mock', sandbox='read-only', reason='Take over the parser'), transfer)
        with self.assertRaises(responsibility.TransferError) as present:
            self.transfer(task['task_id'])
        self.assertIn('owner_present', [r['code'] for r in present.exception.details['reasons']])
        state = manager.responsibility_state(task['task_id'])
        self.assertEqual((state['awaiting_transfer'], state['owner']['agent_id']), (False, new))
        self.assertEqual([t['transfer_id'] for t in state['transfers']], [transfer['transfer_id']])

    def test_transfer_is_refused_while_ownership_is_unsafe(self):
        key, _ = self.worker(kind='codex')
        task_id = self.task(key)['task_id']
        with self.failing_stop():
            pending = self.remove(key)
        reasons = [r['code'] for r in manager.responsibility_state(task_id)['reasons']]
        self.assertIn('removal_in_progress', reasons)
        with self.assertRaises(responsibility.TransferError):
            self.transfer(task_id)
        with self.working_stop():
            manager.retry_removal(pending['removal_id'])
        manager.control_task(task_id, 'paused')
        self.assertIn('task_control', [r['code'] for r in manager.responsibility_state(task_id)['reasons']])
        manager.control_task(task_id, 'active')
        self.assertTrue(manager.responsibility_state(task_id)['transferable'])
        # An unresolved execution of the Task blocks any new owner.
        with genuine_unsupervised():
            other, _ = self.worker()
            other_task = self.task(other)['task_id']
            self.output(other, 'Done\n', finish=True)
        self.remove(other)  # recovery_required: the execution is unresolved
        self.assertIn('execution_unresolved', [r['code'] for r in manager.responsibility_state(other_task)['reasons']])
        with patch.object(manager, 'start_agent', side_effect=AssertionError('no start')), \
                self.assertRaises(responsibility.TransferError):
            self.transfer(other_task)

    def test_failed_start_keeps_the_task_waiting_and_a_new_request_can_succeed(self):
        key, _ = self.worker()
        task_id = self.task(key)['task_id']
        self.remove(key)
        with patch.object(manager, 'start_agent', side_effect=ValueError('resource conflict')), \
                self.assertRaises(responsibility.TransferError) as failed:
            manager.transfer_responsibility(task_id, request_key='fails')
        self.assertEqual(failed.exception.details['transfer']['phase'], 'failed')
        state = manager.responsibility_state(task_id)
        self.assertTrue(state['awaiting_transfer'] and state['transferable'])
        self.assertEqual(self.transfer(task_id)['phase'], 'completed')
        self.assertEqual([t['phase'] for t in manager.responsibility_state(task_id)['transfers']], ['failed', 'completed'])

    def test_start_agent_after_removal_is_recorded_as_a_transfer(self):
        key, _ = self.worker()
        task_id = self.task(key)['task_id']
        self.remove(key)
        result = self.started(lambda: manager.start_task_agent(task_id))
        transfer, = manager.responsibility_state(task_id)['transfers']
        self.assertEqual((transfer['source'], transfer['to_agent_id'], transfer['phase']), ('start_agent', result['agent_id'], 'completed'))

    def test_concurrent_transfers_have_exactly_one_owner(self):
        key, _ = self.worker()
        task_id = self.task(key)['task_id']
        self.remove(key)
        barrier, outcomes = threading.Barrier(2), []

        def attempt(name):
            barrier.wait()
            try:
                outcomes.append(self.transfer(task_id, request_key=name)['phase'])
            except (ValueError, LookupError) as exc:
                outcomes.append(type(exc).__name__)
        threads = [threading.Thread(target=attempt, args=(name,)) for name in ('a', 'b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(outcomes.count('completed'), 1, outcomes)
        self.assertEqual(len([a for a in manager._store.list_task_assignments(task_id) if a['ended_at'] is None]), 1)

    def test_interrupted_transfers_are_settled_from_durable_assignments(self):
        key, _ = self.worker()
        task_id = self.task(key)['task_id']
        self.remove(key)
        admitted, _ = manager._store.admit_transfer(task_id, request_key='crash', expected_from_assignment_id=None,
                                                    agent_type='mock', sandbox=None, reason=None)
        self.recover()
        self.assertEqual(manager._store.transfer(admitted['transfer_id'])['phase'], 'failed')
        self.assertTrue(manager.responsibility_state(task_id)['transferable'])
        # A crash after the new owner's assignment committed completes the record.
        second, _ = manager._store.admit_transfer(task_id, request_key='crash-2', expected_from_assignment_id=None,
                                                  agent_type='mock', sandbox=None, reason=None)
        agent_id = self.started(lambda: manager.start_agent('', 'mock', task_id=task_id))
        self.recover()
        recovered = manager._store.transfer(second['transfer_id'])
        self.assertEqual((recovered['phase'], recovered['to_agent_id']), ('completed', agent_id))

    def test_children_of_a_removed_parent_can_get_their_own_new_owner(self):
        root, child, _, _ = self.family()
        self.remove(root)
        child_task = self.task(child)['task_id']
        self.remove(child)  # removed while owning its Task: the Task awaits an explicit new owner
        transfer = self.transfer(child_task)
        new = transfer['to_agent_id']
        # The replacement does not invent lineage under the removed Agent.
        self.assertIsNone(self.agent_row(new)['parent_id'])
        self.assertEqual(manager._store.get_task(child_task)['parent_task_id'], self.task(root)['task_id'])

    # -- read models and API ---------------------------------------------------
    def test_read_models_show_removal_and_waiting_responsibility(self):
        root, child, grandchild, _ = self.family()
        stuck, _ = self.worker(kind='codex')
        self.remove(root)
        with self.failing_stop():
            self.remove(stuck)
        nodes = {n['agent_id']: n for n in self.request('/workspace')['agents']}
        self.assertTrue(nodes[root]['removed'])
        self.assertEqual(nodes[root]['removal']['state'], 'removed')
        self.assertEqual(nodes[root]['waiting_context'], {'kind': 'removed', 'label': 'Removed'})
        self.assertTrue(nodes[root]['responsibility_awaiting'])
        self.assertFalse(nodes[child]['removed'])
        self.assertEqual(nodes[stuck]['removal']['state'], 'blocked')
        self.assertEqual(nodes[stuck]['waiting_context'], {'kind': 'recovery', 'label': 'Removal blocked'})
        detail = manager.get_agent(root, include_output=False)
        self.assertTrue(detail['removed'])
        self.assertEqual(detail['removal']['mode'], 'single')

    def test_api_routes_validate_and_transfer(self):
        root, child, _, _ = self.family()
        token = self.request(f'/agents/{root}/removal-preview', {'mode': 'retire_branch'})['plan_token']
        operation = self.request(f'/agents/{root}/remove', {'request_key': 'api-branch', 'plan_token': token,
                                                            'mode': 'retire_branch'})
        self.assertEqual((operation['phase'], len(operation['members'])), ('removed', 3))
        task_id = self.task(child)['task_id']
        state = self.request(f'/tasks/{task_id}/responsibility')
        self.assertTrue(state['transferable'])
        path = f'/tasks/{task_id}/responsibility/transfer'
        self.request(path, {'request_key': 'x', 'agent_type': 'other'}, 422)
        self.request(path, {'request_key': 'x', 'force': True}, 422)  # no override field exists
        self.assertEqual(self.request(path, {'request_key': 'x', 'expected_owner_assignment_id': 'stale'}, 409)['detail']['code'],
                         'stale_responsibility')
        self.request('/tasks/missing/responsibility', expected=404)
        result = self.started(lambda: self.request(path, {'request_key': 'api-transfer',
                                                          'expected_owner_assignment_id': state['removed_owner']['assignment_id']}))
        self.assertEqual(result['phase'], 'completed')


class HybridRemovalTests(unittest.TestCase):
    """Hybrid exception: a removed Hybrid Parent's continuation is disabled, never moved."""
    setUp = hybrid_tests.HybridDomainTests.setUp
    agent = hybrid_tests.HybridDomainTests.agent
    task = hybrid_tests.HybridDomainTests.task
    worker = hybrid_tests.HybridDomainTests.worker
    origin_context = hybrid_tests.HybridDomainTests.origin_context
    accept = hybrid_tests.HybridDomainTests.accept
    snapshot = hybrid_tests.HybridDomainTests.snapshot
    attach = hybrid_tests.HybridDomainTests.attach
    ready = hybrid_tests.HybridDomainTests.ready
    admit = hybrid_tests.HybridDomainTests.admit
    enable = hybrid_tests.HybridDomainTests.enable
    current = hybrid_tests.HybridDomainTests.current
    rows = hybrid_tests.HybridDomainTests.rows
    save_status = hybrid_tests.HybridDomainTests.save_status
    complete_child = hybrid_tests.HybridDomainTests.complete_child

    def remove_parent(self):
        key = self.origin['agent_id']
        preview = self.store.removal_preview(key)
        self.assertTrue(preview['options']['single']['available'])
        self.assertTrue(preview['orchestration']['hybrid'])
        self.assertEqual(preview['orchestration']['kind'], 'hybrid')
        self.assertIn('orchestration_disabled', preview['consequences'])
        self.assertIn('task_waits_hybrid_orchestration_bound', preview['consequences'])
        operation, _ = self.store.admit_removal(key, request_key='hybrid', plan_token=preview['plan_token'],
                                                mode='single', disposition='preserve', reason=None)
        self.assertTrue(self.store.settle_removal(operation['removal_id'], key, 'no_active_execution'))
        return operation

    def running_child(self, identifier):
        child_task = self.ready(identifier)
        child_key, _ = self.admit(identifier)
        record = next(r for r in self.store.load_agents() if r['agent_id'] == self.origin['agent_id'])
        record.update(agent_id=child_key, task=self.store.get_task(child_task)['description'], status='running',
                      parent_id=self.origin['agent_id'])
        self.store.save_agent(record, assignment_task_id=child_task)
        with self.store._connection() as db:
            db.execute("UPDATE delegation_materializations SET phase='started' WHERE delegation_id=?", (identifier,))
        return child_task, child_key

    def test_hybrid_parent_removal_disables_continuation_and_children_continue(self):
        first, second = self.enable(keys=('a', 'b'))
        child_task, child_key = self.running_child(first)
        self.assertEqual(self.current()['phase'], 'waiting')
        self.remove_parent()
        continuation = self.current()
        self.assertEqual(continuation['phase'], 'stopped')
        self.assertEqual(continuation['last_error'], work_control.OWNER_REMOVED)
        parent_assignment = self.store.list_task_assignments(self.parent['task_id'])[-1]
        self.assertEqual(parent_assignment['ended_reason'], 'removed')
        parent = self.store.get_task(self.parent['task_id'])
        self.assertNotIn(parent['status'], ('completed', 'canceled'))
        # The running delegated Child keeps its own assignment and identity.
        self.assertIsNone(self.store.get_active_assignment_for_task(child_task)['ended_at'])
        # Nothing can deliver, acknowledge or materialize for the removed owner.
        with self.store._connection() as db, self.assertRaises(ValueError):
            orchestration.owner(db, self.parent['task_id'])
        # Its Task waits, but its orchestration is not transferable (no silent model change).
        state = self.store.responsibility(self.parent['task_id'])
        self.assertTrue(state['awaiting_transfer'])
        codes = [r['code'] for r in state['reasons']]
        self.assertIn('hybrid_orchestration_bound', codes)
        self.assertNotIn('delegation_handoff_unsettled', codes)  # Hybrid takes precedence, deterministically
        self.assertEqual(state['orchestration_kind'], 'hybrid')
        self.assertEqual(state['integration_candidates'], [])  # no manual-integration guidance for Hybrid
        with self.assertRaises(responsibility.TransferError):
            self.store.admit_transfer(self.parent['task_id'], request_key='t', expected_from_assignment_id=None,
                                      agent_type='mock', sandbox=None, reason=None)
        # A requested, not yet materialized Child depends on the removed owner: it waits.
        with self.assertRaisesRegex(ValueError, 'stale or unavailable'):
            self.ready(second)
            self.admit(second)

    def test_finished_children_never_complete_the_removed_parents_task(self):
        identifier, = self.enable()
        child, key = self.running_child(identifier)
        self.remove_parent()
        assignment = self.store.get_active_assignment_for_task(child)
        self.save_status(key, 'finished', output_lines=['Child output'], completion_evidence=dict(
            assignment_id=assignment['assignment_id'], returncode=0, validated=True))
        self.assertEqual(self.store.get_task(child)['status'], 'completed')  # the Child's own work completes
        result, = self.rows()
        self.assertEqual(result['outcome'], 'completed')  # recorded, preserved, not delivered
        self.assertEqual(result['phase'], 'recorded')
        self.assertNotEqual(self.store.get_task(self.parent['task_id'])['status'], 'completed')
        with self.store._connection() as db:
            self.assertTrue(orchestration.outstanding(db, self.parent['task_id']))


class OrdinaryDelegationClarityTests(unittest.TestCase):
    """Stage 2D.2 clarity: ordinary (non-Hybrid) Delegations are reported precisely and stay non-transferable."""
    setUp = hybrid_tests.HybridDomainTests.setUp
    agent = hybrid_tests.HybridDomainTests.agent
    task = hybrid_tests.HybridDomainTests.task
    worker = hybrid_tests.HybridDomainTests.worker
    origin_context = hybrid_tests.HybridDomainTests.origin_context
    accept = hybrid_tests.HybridDomainTests.accept
    snapshot = hybrid_tests.HybridDomainTests.snapshot
    attach = hybrid_tests.HybridDomainTests.attach
    ready = hybrid_tests.HybridDomainTests.ready
    admit = hybrid_tests.HybridDomainTests.admit
    save_status = hybrid_tests.HybridDomainTests.save_status
    remove_parent = HybridRemovalTests.remove_parent
    running_child = HybridRemovalTests.running_child

    def accept_v1(self, *keys):
        with self.store._connection() as db:
            db.execute('UPDATE agents SET child_materialization_enabled=1 WHERE agent_id=?', (self.origin['agent_id'],))
        return self.accept(message=hybrid_tests.v1.envelope(*(keys or ('a',))))['delegation_ids']

    def remove_owner(self):
        key = self.origin['agent_id']
        preview = self.store.removal_preview(key)
        self.assertEqual(preview['orchestration']['kind'], 'delegation_handoff')
        self.assertNotIn('orchestration_disabled', preview['consequences'])  # no continuation exists to disable
        self.assertIn('task_waits_delegation_handoff_unsettled', preview['consequences'])
        operation, _ = self.store.admit_removal(key, request_key='v1', plan_token=preview['plan_token'],
                                                mode='single', disposition='preserve', reason=None)
        self.assertTrue(self.store.settle_removal(operation['removal_id'], key, 'no_active_execution'))

    def blockers(self):
        return {b['reason_code'] for b in self.store.get_task(self.parent['task_id'])['active_blockers']}

    def refuse_transfer(self):
        with self.assertRaises(responsibility.TransferError) as refused:
            self.store.admit_transfer(self.parent['task_id'], request_key=str(uuid4()), expected_from_assignment_id=None,
                                      agent_type='mock', sandbox=None, reason=None)
        self.assertEqual(refused.exception.code, 'not_transferable')
        return [r['code'] for r in refused.exception.details['reasons']]

    def test_ordinary_delegations_report_an_unsettled_handoff_not_hybrid(self):
        first, second = self.accept_v1('a', 'b')
        child, child_key = self.running_child(first)
        self.remove_owner()
        state = self.store.responsibility(self.parent['task_id'])
        codes = [r['code'] for r in state['reasons']]
        self.assertEqual((state['awaiting_transfer'], state['transferable'], state['orchestration_kind']),
                         (True, False, 'delegation_handoff'))
        self.assertIn('delegation_handoff_unsettled', codes)
        self.assertNotIn('hybrid_orchestration_bound', codes)
        message = next(r['message'] for r in state['reasons'] if r['code'] == 'delegation_handoff_unsettled')
        self.assertIn('acknowledgement protocol is not implemented', message)
        self.assertIn('delegation_handoff_unsettled', self.refuse_transfer())
        # The receipt, Delegations and blocker are untouched: nothing is settled or completed.
        self.assertIn('delegation_requested', self.blockers())
        self.assertEqual({d['status'] for d in self.store.list_delegations(self.parent['task_id'])}, {'materialized', 'requested'})
        self.assertIsNotNone(self.store.get_task(self.parent['task_id'])['orchestration_handoff'])
        self.assertNotIn(self.store.get_task(self.parent['task_id'])['status'], ('completed', 'canceled'))
        self.assertIsNone(self.store.get_active_assignment_for_task(child)['ended_at'])  # the Child continues

    def test_historical_receipt_with_closed_delegations_stays_unsettled(self):
        identifier, = self.accept_v1()
        self.store.transition_delegation(identifier, 'canceled')  # a closed historical record, not acknowledged
        self.remove_owner()
        self.assertIn('delegation_handoff_unsettled', self.refuse_transfer())
        with self.store._connection() as db:
            from app import agent_removal as removal
            self.assertEqual(removal.orchestration_kind(db, self.parent['task_id']), 'delegation_handoff')

    def test_completed_child_integration_guidance_never_acknowledges(self):
        identifier, = self.accept_v1()
        child, child_key = self.running_child(identifier)
        self.remove_owner()
        assignment = self.store.get_active_assignment_for_task(child)
        self.save_status(child_key, 'finished', output_lines=['Child output'], completion_evidence=dict(
            assignment_id=assignment['assignment_id'], returncode=0, validated=True))
        self.assertEqual(self.store.get_task(child)['status'], 'completed')
        state = self.store.responsibility(self.parent['task_id'])
        candidate, = state['integration_candidates']
        self.assertEqual((candidate['child_task_id'], candidate['delegation_status']), (child, 'materialized'))
        # The parent Task in this fixture has no workspace: the guidance says so instead of offering the action.
        self.assertFalse(candidate['integration_available'])
        self.assertIn('parent_workspace_missing', [b['code'] for b in candidate['integration_blockers']])
        workspace = dict(workspace_id=str(uuid4()), task_id=self.parent['task_id'], project_id=self.project['project_id'],
                         workspace_path=str(Path(self.temp.name) / 'parent'), workspace_path_key=str(uuid4()),
                         base_snapshot='fixture', origin_kind='project_snapshot', source_task_id=None)
        self.store.save_task_workspace(workspace)
        candidate, = self.store.responsibility(self.parent['task_id'])['integration_candidates']
        self.assertEqual((candidate['integration_available'], candidate['integration_blockers']), (True, []))
        # Guidance is read-only: nothing is acknowledged, released or completed by reporting it.
        state = self.store.responsibility(self.parent['task_id'])
        self.assertFalse(state['transferable'])
        self.assertIn('delegation_requested', self.blockers())
        self.assertEqual(self.store.list_delegations(self.parent['task_id'])[0]['status'], 'materialized')
        self.assertNotEqual(self.store.get_task(self.parent['task_id'])['status'], 'completed')

    def test_transfer_eligibility_is_unchanged_for_plain_tasks(self):
        # No coordination at all: the same Task kind as before stays transferable after removal.
        key = self.origin['agent_id']
        # A worker waiting for the user keeps its assignment (no live process for this store-level fixture).
        self.store.save_agent({**next(r for r in self.store.load_agents() if r['agent_id'] == key), 'status': 'waiting',
                               'waiting_question': 'Which file?'})
        preview = self.store.removal_preview(key)
        self.assertIsNone(preview['orchestration']['kind'])
        self.assertIn('task_awaits_responsibility_transfer', preview['consequences'])
        operation, _ = self.store.admit_removal(key, request_key='plain', plan_token=preview['plan_token'],
                                                mode='single', disposition='preserve', reason=None)
        self.store.settle_removal(operation['removal_id'], key, 'no_active_execution')
        state = self.store.responsibility(self.parent['task_id'])
        self.assertEqual((state['orchestration_kind'], state['integration_candidates']), (None, []))
        self.assertNotIn('hybrid_orchestration_bound', [r['code'] for r in state['reasons']])
        self.assertNotIn('delegation_handoff_unsettled', [r['code'] for r in state['reasons']])

    def test_responsibility_api_reports_the_precise_reason(self):
        from test_task_lifecycle import TaskLifecycleTests
        self.accept_v1()
        self.remove_owner()
        with patch.object(manager, '_store', self.store):
            detail = TaskLifecycleTests.request(self, f"/tasks/{self.parent['task_id']}/responsibility")
            refused = TaskLifecycleTests.request(self, f"/tasks/{self.parent['task_id']}/responsibility/transfer",
                                                 {'request_key': 'api'}, 409)['detail']
        self.assertEqual(detail['orchestration_kind'], 'delegation_handoff')
        self.assertIn('delegation_handoff_unsettled', [r['code'] for r in detail['reasons']])
        self.assertEqual(refused['code'], 'not_transferable')
        self.assertIn('delegation_handoff_unsettled', [r['code'] for r in refused['reasons']])


class ResponsibilityMigrationTests(unittest.TestCase):
    def test_v29_to_v30_is_additive_and_idempotent(self):
        import tempfile
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'v29.sqlite3'
        store = AgentStore(path)
        task = store.create_task('Kept')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DROP TABLE responsibility_transfers')
            db.execute('PRAGMA user_version=29')
        AgentStore(path)
        AgentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 36)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM responsibility_transfers').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT task_id FROM tasks').fetchall(), [(task['task_id'],)])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue({'responsibility_transfer_live', 'responsibility_transfer_task'} <= names)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Objects')
class RealWindowsBranchRemovalTests(unittest.TestCase):
    """Real supervised workers (a fake codex launcher running a local interpreter, no AI)."""
    make_launcher = test_executions.SupervisedExecutionTests.make_launcher
    setUp = test_executions.SupervisedExecutionTests.setUp
    tearDown = test_executions.SupervisedExecutionTests.tearDown
    launcher = test_executions.SupervisedExecutionTests.launcher
    lines = test_executions.SupervisedExecutionTests.lines
    DESCENDANT = test_executions.SupervisedExecutionTests.DESCENDANT

    def test_branch_removal_terminates_every_supervised_tree_with_kernel_evidence(self):
        self.launcher(self.DESCENDANT + 'import time\ntime.sleep(60)\n')
        root = manager.start_agent('Root', 'codex')
        child = manager.start_agent('Child', 'codex', parent_id=root)
        grandchildren = {key: int(self.lines(key, 2)[1]) for key in (root, child)}
        self.assertTrue(all(alive_pid(pid) for pid in grandchildren.values()))
        operation = manager.remove_agent(root, request_key='real-branch', mode='retire_branch',
                                         plan_token=manager.removal_preview(root, 'retire_branch')['plan_token'])
        self.assertEqual(operation['phase'], 'removed')
        for key, pid in grandchildren.items():
            self.assertTrue(until(lambda: not alive_pid(pid)), f'descendant of {key} survived')
            row, = manager._store.agent_executions(key)
            self.assertEqual(row['state'], 'ended_verified')  # kernel evidence, not a scan or a marker
            self.assertIsNotNone(self.agent_row(key)['removed_at'])

    def agent_row(self, key):
        with manager._store._connection() as db:
            return dict(db.execute('SELECT * FROM agents WHERE agent_id=?', (key,)).fetchone())


if __name__ == '__main__':
    unittest.main()
