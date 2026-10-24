"""Stage 2D.1: durable single-Agent removal (temporary SQLite and repositories only)."""
from contextlib import closing
import shutil
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager
from app import agent_removal, recovery_actions
from app.persistence import AgentStore
import test_persistence
from execution_test_support import rebooted
import test_task_lifecycle
import test_delegation_protocol as protocol_tests
import test_planning as planning_tests
import test_promotion as promotion_tests
import test_replanning as replanning_tests
from app import planning as planning_domain


class AgentRemovalTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    request = test_task_lifecycle.TaskLifecycleTests.request
    start_request = test_task_lifecycle.TaskLifecycleTests.start_request

    # -- helpers --------------------------------------------------------------
    def worker(self, kind='mock', parent=None):
        return self.create(parent=parent, kind=kind)

    def remove(self, key, request_key=None, token=None, **options):
        token = token or manager.removal_preview(key)['plan_token']
        return manager.remove_agent(key, request_key=request_key or str(uuid4()), plan_token=token, **options)

    def admit_only(self, key, request_key='crash-key'):
        """T1 only: the journal commits, nothing has touched the process yet."""
        token = manager.removal_preview(key)['plan_token']
        operation, created = manager._store.admit_removal(key, request_key=request_key, plan_token=token,
                                                          mode='single', disposition='preserve', reason=None)
        self.assertTrue(created)
        return operation

    def assignments(self, key):
        with manager._store._connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM task_assignments WHERE agent_id=? ORDER BY rowid', (key,))]

    def task(self, key):
        return manager._store.get_task(self.assignments(key)[-1]['task_id'])

    def agent_row(self, key):
        with manager._store._connection() as db:
            return dict(db.execute('SELECT * FROM agents WHERE agent_id=?', (key,)).fetchone())

    def blockers(self, task_id):
        return {b['reason_code'] for b in manager._store.get_task(task_id)['active_blockers']}

    def failing_stop(self):
        return patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('Could not terminate; stop was not confirmed.'))

    def working_stop(self):
        return patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0))

    # -- single removal -------------------------------------------------------
    def test_running_worker_removed_with_distinct_reason_and_preserved_task(self):
        key, process = self.worker()
        self.output(key, 'partial line\n')
        task = self.task(key)
        workspace = manager._store.get_task_workspace(task['task_id'])
        preview = manager.removal_preview(key)
        self.assertTrue(preview['options']['single']['available'])
        self.assertEqual(preview['options']['retire_branch'], {'available': True, 'reasons': []})  # Stage 2D.2
        self.assertEqual(preview['options']['continue_children'], {'available': False, 'reasons': ['not_supported']})
        self.assertIn('stop_running_worker', preview['consequences'])
        operation = self.remove(key, reason='No longer needed', token=preview['plan_token'])
        self.assertEqual(operation['phase'], 'removed')
        self.assertEqual(operation['source'], 'user')
        self.assertEqual(operation['authorization_basis'], 'local_api')
        self.assertEqual(operation['reason'], 'No longer needed')
        member, = operation['members']
        self.assertEqual((member['phase'], member['outcome']), ('removed', 'stop_confirmed'))
        assignment, = self.assignments(key)
        self.assertEqual(assignment['ended_reason'], 'removed')
        current = manager._store.get_task(task['task_id'])
        self.assertEqual(current['status'], 'pending')
        self.assertEqual(current['description'], task['description'])
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        row = self.agent_row(key)
        self.assertEqual((row['status'], row['removal_id']), ('stopped', operation['removal_id']))
        self.assertIsNotNone(row['removed_at'])
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIn('partial line', manager.get_agent(key)['output'])
        process.terminate.assert_called_once()
        self.assertEqual(manager.list_agent_removals(key), [operation])
        self.assertEqual(manager.get_removal(operation['removal_id']), operation)

    def test_preview_is_read_only(self):
        key, _ = self.worker()
        before = self.path.read_bytes()
        for mode in agent_removal.MODES:
            manager.removal_preview(key, mode)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(manager.agent_statuses[key], 'running')
        with self.assertRaises(LookupError):
            manager.removal_preview('missing')

    def test_duplicate_request_returns_same_operation_without_second_stop(self):
        key, process = self.worker()
        token = manager.removal_preview(key)['plan_token']
        first = self.remove(key, 'same-key', token)
        with patch.object(manager, 'stop_agent') as stop:
            self.assertEqual(self.remove(key, 'same-key', token), first)
            stop.assert_not_called()
        with self.assertRaises(agent_removal.RemovalError) as error:
            self.remove(key, 'same-key', token, reason='different')
        self.assertEqual(error.exception.code, 'request_key_conflict')
        with self.assertRaises(agent_removal.RemovalError) as error:
            self.remove(key, 'other-key')
        self.assertEqual(error.exception.code, 'already_removed')
        process.terminate.assert_called_once()
        self.assertEqual(len(manager.list_agent_removals(key)), 1)

    def test_different_key_while_removal_pending_is_rejected(self):
        key, _ = self.worker(kind='codex')
        with self.failing_stop():
            pending = self.remove(key)
        self.assertEqual(pending['phase'], 'recovery_required')
        with self.assertRaises(agent_removal.RemovalError) as error:
            manager.remove_agent(key, request_key='second', plan_token='x' * 64)
        self.assertEqual(error.exception.code, 'removal_in_progress')
        self.assertEqual(error.exception.details['removal_id'], pending['removal_id'])

    def test_stale_preview_never_executes(self):
        key, _ = self.worker()
        token = manager.removal_preview(key)['plan_token']
        self.output(key, 'session id: 11111111-1111-1111-1111-111111111111\n')
        manager.rename_agent(key, 'Renamed only')  # display data is not impact
        self.assertEqual(manager.removal_preview(key)['plan_token'], token)
        child, _ = self.worker(parent=key)
        with self.assertRaises(agent_removal.StalePreview) as error:
            self.remove(key, token=token)
        fresh = error.exception.details['preview']
        self.assertEqual(fresh['options']['single'], {'available': True, 'reasons': []})  # Stage 2D.2: Children continue
        self.assertIn('children_continue', fresh['consequences'])
        self.assertEqual([d['agent_id'] for d in fresh['descendants']], [child])
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(manager.list_agent_removals(key), [])
        self.assertIsNone(self.assignments(key)[0]['ended_at'])

    def test_unsupported_modes_fail_explicitly_and_active_descendants_continue(self):
        parent, _ = self.worker()
        child, _ = self.worker(parent=parent)
        # Stage 2D.2: only the superseded continue_children mode and Task cancellation remain unsupported.
        for options, code in (({'mode': 'continue_children'}, 'unsupported_mode'),
                              ({'task_disposition': 'cancel'}, 'unsupported_task_disposition')):
            with self.subTest(options=options), self.assertRaises(agent_removal.Unsupported) as error:
                self.remove(parent, **options)
            self.assertEqual(error.exception.code, code)
        self.assertEqual(manager.agent_statuses[parent], 'running')
        # Single removal of the parent: its active Child keeps its identity, lineage and execution.
        self.assertEqual(self.remove(parent)['phase'], 'removed')
        self.assertEqual(manager.agent_statuses[child], 'running')
        self.assertEqual(self.agent_row(child)['parent_id'], parent)
        self.assertIsNone(self.agent_row(child)['removed_at'])
        self.assertEqual(self.remove(child)['phase'], 'removed')

    def test_agent_coordinating_delegated_work_is_removed_without_transferable_coordination(self):
        # Stage 2D.2: removable, but ordinary Delegation coordination is never transferred.
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        assignment = self.assignments(key)[0]
        manager._store.create_delegation(task['task_id'], project_id=task['project_id'], requested_by_agent_id=key,
                                         requested_by_assignment_id=assignment['assignment_id'],
                                         request_key='child-work', instruction='Child work')
        preview = manager.removal_preview(key)
        self.assertEqual(preview['options']['single']['reasons'], [])
        self.assertTrue(preview['orchestration']['coordinates_delegated_work'])
        self.assertEqual(preview['orchestration']['kind'], 'delegation_handoff')  # not Hybrid: nothing to disable
        self.assertNotIn('orchestration_disabled', preview['consequences'])
        self.assertIn('task_waits_delegation_handoff_unsettled', preview['consequences'])
        with self.working_stop():
            self.assertEqual(self.remove(key, token=preview['plan_token'])['phase'], 'removed')
        state = manager.responsibility_state(task['task_id'])
        self.assertTrue(state['awaiting_transfer'])
        self.assertFalse(state['transferable'])
        self.assertIn('delegation_handoff_unsettled', [r['code'] for r in state['reasons']])

    # -- races ----------------------------------------------------------------
    def test_stop_before_removal_keeps_stopped_reason(self):
        key, _ = self.worker()
        manager.stop_agent(key)
        operation = self.remove(key)
        self.assertEqual(operation['members'][0]['outcome'], 'no_active_execution')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'stopped')

    def test_stop_during_pending_removal_finalizes_as_removed(self):
        key, process = self.worker(kind='codex')
        with self.failing_stop():
            operation = self.remove(key)
        self.assertEqual(operation['phase'], 'recovery_required')
        self.assertTrue(operation['last_error'].startswith('stop_not_confirmed'))
        self.assertIsNone(self.assignments(key)[0]['ended_at'])  # ownership retained
        with self.working_stop():
            manager.stop_agent(key)
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'removed')

    def test_cancel_before_and_during_removal(self):
        key, _ = self.worker()
        task = self.task(key)
        manager.control_task(task['task_id'], 'canceled')
        self.assertEqual(self.remove(key)['phase'], 'removed')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'canceled')
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'canceled')

        other, _ = self.worker(kind='codex')
        other_task = self.task(other)
        with self.failing_stop():
            pending = self.remove(other)
        with self.working_stop():
            manager.control_task(other_task['task_id'], 'canceled')
        # Cancel intent governs its own termination; the removal still settles.
        self.assertEqual(self.assignments(other)[0]['ended_reason'], 'canceled')
        settled = manager.retry_removal(pending['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertIn('assignment_ended_canceled', settled['members'][0]['outcome'])

    def test_replacement_waits_for_settled_removal(self):
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        with self.failing_stop():
            pending = self.remove(key)
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_task_agent(task['task_id'])
        spawn.assert_not_called()
        with self.working_stop():
            manager.retry_removal(pending['removal_id'])
        replacement = self.start_request(f"/tasks/{task['task_id']}/start-agent", {})
        self.assertNotEqual(replacement['agent_id'], key)
        history = manager._store.list_task_assignments(task['task_id'])
        self.assertEqual([(a['agent_id'], a['ended_reason']) for a in history],
                         [(key, 'removed'), (replacement['agent_id'], None)])

    def test_completion_committed_first_is_preserved(self):
        key, _ = self.worker(kind='codex')
        self.output(key, 'Done\n', finish=True)
        task = self.task(key)
        self.assertEqual(task['status'], 'completed')
        operation = self.remove(key)
        self.assertEqual(operation['members'][0]['outcome'], 'no_active_execution')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'completed')
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'completed')
        self.assertEqual(self.agent_row(key)['status'], 'finished')

    def test_completion_after_admission_never_completes_task(self):
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        operation = self.admit_only(key)
        self.output(key, 'Done\n', finish=True)  # the watcher's late successful exit
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'pending')
        self.assertIn('Done', manager.get_agent(key)['output'])
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'removed')
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'pending')

    def test_waiting_agent_session_is_ended_not_reused(self):
        key, _ = self.worker(kind='codex')
        self.output(key, f'session id: {uuid4()}\nCONTROL_CENTER_WAITING: Which file?\n', True)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        operation = self.remove(key)
        self.assertEqual(operation['members'][0]['outcome'], 'waiting_session_ended')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        row = self.agent_row(key)
        self.assertEqual((row['status'], row['waiting_question']), ('stopped', None))
        self.assertEqual(self.task(key)['status'], 'pending')
        with self.assertRaises(ValueError):
            manager.reply_agent(key, 'answer')

    # -- failures, crashes and recovery -----------------------------------------
    def test_failed_termination_keeps_ownership_and_gates(self):
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        manager._store.create_resource_claim(task['task_id'], resource_type='generic', resource_key='db:main', scope='project')
        with self.failing_stop():
            operation = self.remove(key)
        self.assertEqual(operation['members'][0]['phase'], 'recovery_required')
        self.assertIsNone(self.agent_row(key)['removed_at'])
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        claim, = manager._store.resource_claims(task['task_id'])
        self.assertEqual(claim['status'], 'active')
        with manager._store._connection() as db, self.assertRaises(ValueError):
            agent_removal.require_operational(db, key)
        self.request(f'/agents/{key}/remove', {'request_key': 'k', 'plan_token': 't'}, 409)
        with self.working_stop():
            settled = manager.retry_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')

    def test_api_reports_unsettled_removal_as_503(self):
        key, _ = self.worker(kind='codex')
        token = self.request(f'/agents/{key}/removal-preview', {})['plan_token']
        with self.failing_stop():
            detail = self.request(f'/agents/{key}/remove', {'request_key': 'api', 'plan_token': token}, 503)['detail']
        self.assertEqual(detail['code'], 'removal_recovery_required')
        removal_id = detail['removal']['removal_id']
        self.assertEqual(self.request(f'/agent-removals/{removal_id}')['phase'], 'recovery_required')
        with self.working_stop():
            self.assertEqual(self.request(f'/agent-removals/{removal_id}/retry', {})['phase'], 'removed')
        self.assertEqual(len(self.request(f'/agents/{key}/removals')['removals']), 1)
        self.request('/agent-removals/missing', expected=404)
        self.request('/agents/missing/removals', expected=404)
        self.request(f'/agents/{key}/removal-preview', {'mode': 'other'}, 422)
        self.assertEqual(self.request(f'/agents/{key}/remove', {'request_key': 'api', 'plan_token': token,
                                                                'mode': 'retire_branch'}, 409)['detail']['code'],
                         'request_key_conflict')
        self.assertEqual(self.request(f'/agents/{key}/remove', {'request_key': 'new', 'plan_token': token,
                                                                'mode': 'continue_children'}, 422)['detail']['code'],
                         'unsupported_mode')

    def test_crash_after_admission_requires_kernel_evidence_and_never_resumes(self):
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        operation = self.admit_only(key)
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            spawn.assert_not_called()
        current = manager.get_removal(operation['removal_id'])
        self.assertEqual(current['phase'], 'recovery_required')
        self.assertTrue(current['last_error'].startswith('execution_unresolved'))
        self.assertIsNone(self.agent_row(key)['removed_at'])
        self.assertIn('process_ownership_unknown', self.blockers(task['task_id']))
        # Stage 2D.1b: neither a confirmation nor a clean scan is proof that it ended.
        for confirm in (False, True):
            with patch.object(recovery_actions, 'scan_processes', return_value=[]):
                retried = manager.retry_removal(operation['removal_id'], confirm_ended=confirm)
            self.assertEqual(retried['phase'], 'recovery_required')
            self.assertTrue(retried['last_error'].startswith('execution_unresolved'))
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        with rebooted():  # controlled evidence: a verified Windows restart since that execution
            settled = manager.retry_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        # The same authoritative settlement lifted the Task's ownership blocker.
        self.assertNotIn('process_ownership_unknown', self.blockers(task['task_id']))

    def claim(self, task_id, key='db:main', lifetime='worker'):
        claim_id = manager._store.create_resource_claim(task_id, resource_type='generic', resource_key=key,
                                                        scope='project', lifetime=lifetime)
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)

    def claim_status(self, task_id, claim_id):
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)['status']

    def test_restart_keeps_unverified_worker_ownership_until_confirmed(self):
        # Review regression: restart reconciliation must not end the assignment
        # or release worker-lifetime ownership before termination is confirmed.
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        claim = self.claim(task['task_id'])
        self.assertEqual(claim['status'], 'active')
        operation = self.admit_only(key)
        self.recover()
        current = manager.get_removal(operation['removal_id'])
        self.assertEqual(current['phase'], 'recovery_required')
        self.assertTrue(current['last_error'].startswith('execution_unresolved'))
        assignment, = self.assignments(key)
        self.assertIsNone(assignment['ended_at'])
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        self.assertIn('process_ownership_unknown', self.blockers(task['task_id']))
        # New work cannot bypass the unresolved ownership.
        other = manager._store.create_task('Competing work')
        competing = self.claim(other['task_id'], lifetime='task')
        self.assertEqual(competing['status'], 'waiting')
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_task_agent(task['task_id'])
        spawn.assert_not_called()
        with self.assertRaises(ValueError):
            manager._store.create_assignment(task['task_id'], str(uuid4()))
        # Ordinary saves of the retiring Agent cannot settle it either.
        manager.rename_agent(key, 'Still unverified')
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'recovery_required')
        self.recover()  # still unconfirmed after another restart
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        self.assertEqual(manager.get_removal(operation['removal_id'])['phase'], 'recovery_required')
        # A clean scan is not proof; only kernel or reboot evidence settles it.
        with patch.object(recovery_actions, 'scan_processes', return_value=[]):
            self.assertEqual(manager.retry_removal(operation['removal_id'], confirm_ended=True)['phase'],
                             'recovery_required')
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        with rebooted():
            settled = manager.retry_removal(operation['removal_id'])
        self.assertEqual(settled['phase'], 'removed')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'released')
        self.assertEqual(self.claim_status(other['task_id'], competing['claim_id']), 'active')
        snapshot = (manager.get_removal(operation['removal_id']), self.assignments(key),
                    manager._store.resource_claims(task['task_id']))
        self.recover()
        self.assertEqual((manager.get_removal(operation['removal_id']), self.assignments(key),
                          manager._store.resource_claims(task['task_id'])), snapshot)

    def test_restart_after_failed_stop_requires_kernel_evidence(self):
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        claim = self.claim(task['task_id'])
        with self.failing_stop():
            operation = self.remove(key)
        self.assertTrue(operation['last_error'].startswith('stop_not_confirmed'))
        self.recover()
        current = manager.get_removal(operation['removal_id'])
        self.assertTrue(current['last_error'].startswith('execution_unresolved'))
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        retried = manager.retry_removal(operation['removal_id'], confirm_ended=True)
        self.assertEqual((retried['phase'], retried['last_error'].split(':')[0]), ('recovery_required', 'execution_unresolved'))
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')

    def test_restart_after_verified_stop_releases_worker_ownership(self):
        key, _ = self.worker()
        task = self.task(key)
        claim = self.claim(task['task_id'])
        operation = self.admit_only(key)
        manager._store.mark_removal_stopping(operation['removal_id'], key)
        manager.stop_agent(key)  # termination verified and durably recorded before the crash
        self.recover()
        current = manager.get_removal(operation['removal_id'])
        self.assertEqual((current['phase'], current['members'][0]['outcome']), ('removed', 'settled_by_restart_recovery'))
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'released')

    def test_crash_after_verified_stop_settles_deterministically(self):
        key, _ = self.worker()
        operation = self.admit_only(key)
        manager._store.mark_removal_stopping(operation['removal_id'], key)
        manager.stop_agent(key)
        self.recover()
        current = manager.get_removal(operation['removal_id'])
        self.assertEqual((current['phase'], current['members'][0]['outcome']), ('removed', 'settled_by_restart_recovery'))
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.recover()  # repeated recovery changes nothing
        self.assertEqual(manager.get_removal(operation['removal_id']), current)
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')
        self.assertEqual(len(manager.list_agent_removals(key)), 1)

    def test_crash_with_waiting_agent_settles_without_resuming(self):
        key, _ = self.worker(kind='codex')
        self.output(key, f'session id: {uuid4()}\nCONTROL_CENTER_WAITING: Which file?\n', True)
        operation = self.admit_only(key)
        self.recover()
        self.assertEqual(manager.get_removal(operation['removal_id'])['phase'], 'removed')
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')

    def test_failure_while_recording_settlement_is_retryable(self):
        key, process = self.worker()
        with patch.object(agent_removal, 'settle', side_effect=sqlite3.OperationalError('disk I/O error')), \
             self.assertRaises(sqlite3.OperationalError):
            self.remove(key, 'settle-fail')
        operation, = manager.list_agent_removals(key)
        self.assertEqual((operation['phase'], operation['members'][0]['phase']), ('stopping', 'stopping'))
        self.assertIsNone(self.agent_row(key)['removed_at'])
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'removed')
        process.terminate.assert_called_once()

    def test_failure_while_recording_admission_changes_nothing(self):
        key, process = self.worker()
        with patch.object(agent_removal, 'get', side_effect=sqlite3.OperationalError('disk I/O error')), \
             self.assertRaises(sqlite3.OperationalError):
            self.remove(key)
        self.assertEqual(manager.list_agent_removals(key), [])
        self.assertEqual(manager.agent_statuses[key], 'running')
        process.terminate.assert_not_called()

    # -- preservation and gates -------------------------------------------------
    def test_dependency_and_integration_history_preserved(self):
        key, _ = self.worker()
        task = self.task(key)
        dependent = manager._store.create_task('Consumer')
        manager.change_dependency(dependent['task_id'], task['task_id'])
        with manager._store._connection() as db:
            db.execute('''INSERT INTO integrations(integration_id,source_task_id,destination_kind,destination_id,destination_key,
                base_snapshot,source_snapshot,destination_before_snapshot,status) VALUES ('i1',?,'project',?,?,'b','s','d','conflict')''',
                       (task['task_id'], task['project_id'], 'project:' + task['project_id']))
        before = manager._store.list_integrations(task['task_id'])
        preview = manager.removal_preview(key)
        self.assertEqual(preview['external_dependents'], [{'task_id': dependent['task_id'], 'status': 'blocked'}])
        self.assertEqual(preview['integrations'][0]['status'], 'conflict')
        self.remove(key)
        self.assertIn('dependency_incomplete', self.blockers(dependent['task_id']))
        self.assertEqual(manager._store.get_task(dependent['task_id'])['status'], 'blocked')
        self.assertEqual(manager._store.list_integrations(task['task_id']), before)

    def test_removed_agent_cannot_resume_or_parent_new_work(self):
        key, _ = self.worker(kind='codex')
        self.output(key, f'session id: {uuid4()}\nCONTROL_CENTER_WAITING: Which file?\n', True)
        task = self.task(key)
        self.remove(key)
        with patch.object(manager, '_spawn_process') as spawn:
            for action in (lambda: manager.reply_agent(key, 'x'), lambda: manager.decide_agent(key),
                           lambda: manager.decide_similar_agent(key), lambda: manager.decide_always_agent(key),
                           lambda: manager.redirect_agent(key, 'x'),
                           lambda: manager.start_agent('Child', 'mock', parent_id=key)):
                with self.subTest(action=action), self.assertRaises(ValueError):
                    action()
            spawn.assert_not_called()
        self.request(f'/agents/{key}/children/start', {'task': 'Child'}, 422)
        other = manager._store.create_task('Other work')
        with self.assertRaises(ValueError):
            manager._store.create_assignment(other['task_id'], key)
        with self.assertRaises(ValueError):
            manager._store.begin_delegation_execution(key, str(uuid4()))
        record = {'agent_id': key, 'parent_id': None, 'task': manager.agent_tasks[key], 'agent_type': 'codex',
                  'sandbox': 'read-only', 'status': 'running', 'session_id': None, 'waiting_question': None,
                  'similar_decisions_enabled': False, 'similar_examples': [], 'always_decide_enabled': False,
                  'always_decide_configured': False}
        with self.assertRaises(sqlite3.IntegrityError):
            manager._store.save_agent(record)
        with self.assertRaises(ValueError):
            manager._spawn_with_resources(task['task_id'], key, 'cmd', 'codex', self.temp.name)
        # Ordinary metadata and Stop remain harmless.
        manager.rename_agent(key, 'Historical worker')
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        self.assertIsNotNone(self.agent_row(key)['removed_at'])

    def test_late_watcher_cannot_continue_pending_removal(self):
        key, _ = self.worker(kind='codex')
        self.admit_only(key)
        manager.agent_always_decisions[key].enabled = True
        with patch.object(manager, '_resume_agent') as resume:
            self.output(key, f'session id: {uuid4()}\nCONTROL_CENTER_WAITING: Next?\n', True)
            resume.assert_not_called()
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIsNone(manager.agent_waiting_questions[key])
        manager.rename_agent(key, 'Later save')
        self.assertIsNone(self.agent_row(key)['waiting_question'])
        self.assertEqual(self.assignments(key)[0]['ended_reason'], 'removed')

    def test_direct_end_assignment_cannot_bypass_unverified_removal(self):
        # Review regression (P2): only verified removal settlement may close it.
        key, _ = self.worker(kind='codex')
        task = self.task(key)
        claim = self.claim(task['task_id'])
        intent, _ = manager._store.create_work_intent(task['task_id'], namespace='component', key='parser', mode='single_owner')
        operation = self.admit_only(key)
        self.recover()
        assignment, = self.assignments(key)
        with self.assertRaises(ValueError):
            manager._store.end_assignment(assignment['assignment_id'], 'stopped')
        self.assertIsNone(self.assignments(key)[0]['ended_at'])
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'active')
        own, = manager._store.work_intents(task['task_id'])
        self.assertEqual((own['intent_id'], own['status'], bool(own['holds_authority'])), (intent, 'active', True))
        other = manager._store.create_task('Competing work')
        competing = self.claim(other['task_id'], lifetime='task')
        self.assertEqual(competing['status'], 'waiting')
        rival = manager._store.create_task('Competing scope')
        manager._store.create_work_intent(rival['task_id'], namespace='component', key='parser', mode='single_owner')
        self.assertFalse(manager._store.work_intents(rival['task_id'])[0]['holds_authority'])
        with rebooted():  # controlled evidence: only it may close the assignment
            self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'removed')
        ended = self.assignments(key)[0]
        self.assertEqual(ended['ended_reason'], 'removed')
        self.assertEqual(self.claim_status(task['task_id'], claim['claim_id']), 'released')
        self.assertEqual(self.claim_status(other['task_id'], competing['claim_id']), 'active')
        # Already-ended assignments stay idempotent for direct callers.
        self.assertEqual(manager._store.end_assignment(ended['assignment_id'], 'stopped'), ended)


class AgentRemovalMigrationTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement

    def v26_copy(self, name):
        """A representative v26 database: real rows, v26 assignment CHECK, no v27 artifacts."""
        path = Path(self.temp.name) / name
        shutil.copyfile(self.path, path)
        with closing(sqlite3.connect(path)) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            with db:
                db.execute('DROP TABLE agent_removal_members')
                db.execute('DROP TABLE agent_removals')
                for table, old, new in (
                        ('agents', ', removed_at TEXT, removal_id TEXT REFERENCES agent_removals(removal_id)', ''),
                        ('task_assignments', "'external_resource_unavailable','removed','canceled'",
                         "'external_resource_unavailable','canceled'")):
                    sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (table,)).fetchone()[0]
                    self.assertIn(old, sql)
                    indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]
                    columns = [r[1] for r in db.execute(f'PRAGMA table_info({table})') if r[1] not in ('removed_at', 'removal_id')]
                    db.execute(sql.replace(old, new).replace(f'CREATE TABLE "{table}"', f'CREATE TABLE {table}_old', 1)
                               .replace(f'CREATE TABLE {table} ', f'CREATE TABLE {table}_old ', 1))
                    names = ','.join(columns)
                    db.execute(f'INSERT INTO {table}_old(rowid,{names}) SELECT rowid,{names} FROM {table}')
                    db.execute(f'DROP TABLE {table}')
                    db.execute(f'ALTER TABLE {table}_old RENAME TO {table}')
                    for index in indexes:
                        db.execute(index)
                db.execute('PRAGMA user_version=26')
        return path

    def rows(self, path, table, columns='*'):
        with closing(sqlite3.connect(path)) as db:
            return db.execute(f'SELECT rowid,{columns} FROM {table} ORDER BY rowid').fetchall()

    def populate(self):
        key, _ = self.create(kind='mock')
        done, _ = self.create(kind='mock')
        manager.stop_agent(done)
        manager.create_task('Unassigned', 'Unassigned')
        return key

    def test_v26_to_v27_preserves_rows_constraints_and_indexes(self):
        self.populate()
        path = self.v26_copy('v26.sqlite3')
        columns = 'assignment_id,task_id,agent_id,assigned_at,ended_at,ended_reason'
        before = (self.rows(path, 'task_assignments', columns), self.rows(path, 'agents', 'agent_id,status,task'),
                  self.rows(path, 'tasks'))
        AgentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='task_assignments'")}
            self.assertTrue({'task_active_assignment', 'agent_active_task', 'task_assignment_history'} <= indexes)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agents WHERE removed_at IS NOT NULL OR removal_id IS NOT NULL').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agent_removals').fetchone()[0], 0)
            ended = db.execute('SELECT assignment_id FROM task_assignments WHERE ended_at IS NOT NULL').fetchone()[0]
            db.execute("UPDATE task_assignments SET ended_reason='removed' WHERE assignment_id=?", (ended,))
            db.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE task_assignments SET ended_reason='deleted' WHERE assignment_id=?", (ended,))
        after = (self.rows(path, 'task_assignments', columns), self.rows(path, 'agents', 'agent_id,status,task'),
                 self.rows(path, 'tasks'))
        self.assertEqual(after, before)
        snapshot = {t: self.rows(path, t) for t in ('agents', 'task_assignments', 'tasks')}
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA user_version=26')  # an idempotent re-run over v27 artifacts
        AgentStore(path)
        self.assertEqual({t: self.rows(path, t) for t in snapshot}, snapshot)

    def test_failed_migration_rolls_back_and_future_schema_rejected(self):
        self.populate()
        path = self.v26_copy('rollback.sqlite3')
        before = {t: self.rows(path, t) for t in ('agents', 'task_assignments', 'tasks')}
        original = agent_removal.migrate

        def fail(db):
            original(db)
            raise sqlite3.OperationalError('interrupted migration')
        with patch.object(agent_removal, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 26)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_removals'").fetchone())
            self.assertNotIn("'removed'", db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0])
        self.assertEqual({t: self.rows(path, t) for t in before}, before)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA user_version=38')
        with self.assertRaises(RuntimeError):
            AgentStore(path)



def admit(key, request_key='structured'):
    token = manager.removal_preview(key)['plan_token']
    operation, _ = manager._store.admit_removal(key, request_key=request_key, plan_token=token, mode='single',
                                                disposition='preserve', reason=None)
    return operation


class StructuredAuthorityTests(unittest.TestCase):
    """Review regression (P1): no new structured work once removal admission commits."""
    setUp = promotion_tests.PromotionTests.setUp
    patch = promotion_tests.PromotionTests.patch
    stream = promotion_tests.PromotionTests.stream
    finish = promotion_tests.PromotionTests.finish
    launch = promotion_tests.PromotionTests.launch
    plan = promotion_tests.PromotionTests.plan
    message = promotion_tests.PromotionTests.message

    def counts(self):
        with self.store._connection() as db:
            return {t: db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                    for t in ('planning_revisions', 'promotion_decisions', 'promotion_items', 'delegations',
                              'delegation_rounds', 'delegation_continuations', 'replan_decisions')}

    def assert_rejected_and_preserved(self, key, before, needle):
        self.assertEqual(self.counts(), before)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIsNone(self.store.get_task(self.task['task_id'])['orchestration_handoff'])
        assignment, = self.store.list_task_assignments(self.task['task_id'])
        self.assertEqual(assignment['ended_reason'], 'removed')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        output = self.store.full_output(key)
        self.assertTrue(any(needle in line for line in output), output)  # final output preserved

    def test_store_failure_after_admission_cannot_leak_plan_and_promotion(self):
        key, process = self.launch()
        before = self.counts()
        self.stream(key, process, [self.plan(), self.message()])
        with patch.object(self.store, 'removal', side_effect=sqlite3.OperationalError('disk I/O error')), \
             self.assertRaises(sqlite3.OperationalError):
            manager.remove_agent(key, request_key='lost-ack', plan_token=manager.removal_preview(key)['plan_token'])
        self.assertTrue(manager._store.agent_removal_pending(key))
        self.assertFalse(process._control_center_execution.revoked)  # the in-memory revocation never happened
        self.finish(key, process)
        self.assert_rejected_and_preserved(key, before, promotion_tests.promotion.PROTOCOL)
        operation, = manager.list_agent_removals(key)
        self.assertEqual(manager.retry_removal(operation['removal_id'])['phase'], 'removed')
        self.assertEqual(self.counts(), before)

    def test_late_planning_message_after_admission(self):
        key, process = self.launch()
        before = self.counts()
        self.stream(key, process, [self.plan(), 'Ordinary final answer'])
        admit(key)
        self.finish(key, process)
        self.assert_rejected_and_preserved(key, before, planning_domain.PROTOCOL)

    def test_late_delegation_message_after_admission(self):
        key, process = self.launch()
        before = self.counts()
        self.stream(key, process, [protocol_tests.envelope('late')])
        admit(key)
        self.finish(key, process)
        self.assert_rejected_and_preserved(key, before, 'request_delegations')

    def test_store_boundary_rejects_new_work_but_replays_committed_receipt(self):
        key, process = self.launch()
        self.stream(key, process, ['Work continues'])
        origin, session = process._control_center_execution.origin, manager.agent_sessions[key]
        first = planning_tests.decision(decision='propose', items=[planning_tests.item('a')])
        committed, = self.store.accept_planning_messages(origin, [('plan', first)], session_id=session)  # before admission: kept
        admit(key)
        before = self.counts()
        replay, = self.store.accept_planning_messages(origin, [('plan', first)], session_id=session)
        self.assertEqual(replay['revision'], committed['revision'])
        attempts = {
            'planning': lambda store: store.accept_planning_messages(
                origin, [('next', planning_tests.decision(decision_key='next', base_revision=1))], session_id=session),
            'promotion': lambda store: store.accept_promotion_message(
                origin, self.message(items=[promotion_tests.selection('a')]), session_id=session, message_id='promotion'),
            'delegation': lambda store: store.accept_delegation_message(
                origin, protocol_tests.envelope('late'), session_id=session, message_id='delegation'),
        }
        for name, attempt in attempts.items():
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, 'removal'):
                attempt(self.store)
        self.assertEqual(self.counts(), before)
        reopened = AgentStore(self.store.path)  # the restriction is durable across restart
        for name, attempt in attempts.items():
            with self.subTest(restart=name), self.assertRaisesRegex(ValueError, 'removal'):
                attempt(reopened)
        self.assertEqual(self.counts(), before)

    def test_structured_acceptance_committed_first_is_preserved(self):
        key, process = self.launch()
        self.stream(key, process, ['Work continues'])
        origin, session = process._control_center_execution.origin, manager.agent_sessions[key]
        self.store.accept_planning_messages(origin, [('plan', self.plan())], session_id=session)
        stale = manager.removal_preview(key)['plan_token']
        receipt = self.store.accept_promotion_message(origin, self.message(), session_id=session, message_id='promotion')
        with self.assertRaises(agent_removal.RemovalError):
            manager._store.admit_removal(key, request_key='late', plan_token=stale, mode='single',
                                         disposition='preserve', reason=None)
        fresh = manager.removal_preview(key)
        self.assertTrue(fresh['orchestration']['coordinates_delegated_work'])  # Stage 2D.2: not refused
        self.assertEqual(fresh['orchestration']['kind'], 'hybrid')
        self.assertIn('orchestration_disabled', fresh['consequences'])
        self.assertFalse(manager._store.agent_removal_pending(key))
        self.assertEqual(self.store.accept_promotion_message(origin, self.message(), session_id=session, message_id='promotion'), receipt)
        self.assertEqual(len(self.store.list_promotions(self.task['task_id'])), 1)

    def test_concurrent_admission_and_structured_acceptance_serialize(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        key, process = self.launch()
        self.stream(key, process, ['Work continues'])
        origin, session = process._control_center_execution.origin, manager.agent_sessions[key]
        self.store.accept_planning_messages(origin, [('plan', self.plan())], session_id=session)
        token = manager.removal_preview(key)['plan_token']
        barrier = Barrier(2)

        def removal():
            barrier.wait()
            return self.store.admit_removal(key, request_key='race', plan_token=token, mode='single',
                                            disposition='preserve', reason=None)

        def promote():
            barrier.wait()
            return self.store.accept_promotion_message(origin, self.message(), session_id=session, message_id='promotion')
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(removal), pool.submit(promote)]
            outcomes = []
            for future in futures:
                try:
                    future.result()
                    outcomes.append(True)
                except ValueError:
                    outcomes.append(False)
        self.assertEqual(sum(outcomes), 1, outcomes)  # exactly one side wins, transactionally
        if outcomes[0]:
            self.assertEqual(self.store.list_promotions(self.task['task_id']), [])
        else:
            self.assertEqual(len(self.store.list_promotions(self.task['task_id'])), 1)
            self.assertFalse(manager._store.agent_removal_pending(key))


class HybridAuthorityTests(unittest.TestCase):
    """Replanning and Hybrid ownership of a coordinating Parent. Admission is
    refused for such Agents, so a live removal row is written directly to
    exercise the transactional gates in isolation."""
    setUp = replanning_tests.ReplanningTests.setUp
    patch = replanning_tests.ReplanningTests.patch
    stream = replanning_tests.ReplanningTests.stream
    finish = replanning_tests.ReplanningTests.finish
    launch = replanning_tests.ReplanningTests.launch
    plan = replanning_tests.ReplanningTests.plan
    message = replanning_tests.ReplanningTests.message
    rows = replanning_tests.ReplanningTests.rows
    workspace_fixture = replanning_tests.ReplanningTests.workspace_fixture
    accepted = replanning_tests.ReplanningTests.accepted
    change = replanning_tests.ReplanningTests.change
    decision = replanning_tests.ReplanningTests.decision
    submit = replanning_tests.ReplanningTests.submit
    transitions = replanning_tests.ReplanningTests.transitions

    def mark_removal_pending(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO agent_removals(removal_id,request_key,target_agent_id,mode,task_disposition,source,
                authorization_basis,plan,plan_token,phase) VALUES ('forced','forced',?,'single','preserve','user','local_api','{}','t','admitted')""",
                       (self.key,))
            db.execute("INSERT INTO agent_removal_members(removal_id,agent_id,role,phase) VALUES ('forced',?,'target','admitted')", (self.key,))

    def test_replanning_and_hybrid_ownership_rejected_but_receipt_replays(self):
        message = self.decision()
        receipt = self.submit(message)
        self.mark_removal_pending()
        transitions = self.transitions()
        self.assertEqual(self.submit(message), receipt)  # read-only replay
        with self.assertRaisesRegex(ValueError, 'removal'):
            self.submit(self.decision(decision_key='another'))
        self.assertEqual(self.transitions(), transitions)
        from app import delegation_orchestration
        with self.store._connection() as db, self.assertRaisesRegex(ValueError, 'removal'):
            delegation_orchestration.owner(db, self.task['task_id'], allow_running=True)


if __name__ == '__main__':
    unittest.main()
