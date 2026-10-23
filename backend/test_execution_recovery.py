"""Stage 2D.1b P2: authoritative execution recovery and ownership coordination.

Decision tests use explicit fake probes. Integration tests use temporary
databases and process doubles. Real Windows tests use only disposable local
interpreters created here; every cleanup verifies PID and creation time.
"""
from contextlib import suppress
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, execution_recovery as recovery, executions, process_supervision as ps
from app.persistence import AgentStore, ControllerConflict
import execution_test_support
from execution_test_support import rebooted, genuine_unsupervised
import test_persistence
from test_executions import PYTHON, BACKEND, alive, reap, rows, until, job_exists
from execution_test_support import use_attested_test_doubles

FLAGS = ps.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
CONFIG = {'limit_flags': FLAGS, 'kill_on_job_close': True, 'breakaway_ok': False, 'silent_breakaway_ok': False}
NOW = '2026-10-22T10:00:00.000Z'


def controller(**values):
    return {'instance_id': 'old', 'pid': 100, 'created_at': '500', 'session_id': 1, 'user_sid': 'S-1-5-21-1',
            'boot_id': 7, 'os_boot_time': '2026-10-20T08:00:00Z', 'started_at': '2026-10-21T09:00:00.000Z',
            **values}


def current(**values):
    return controller(instance_id='new', pid=200, created_at='900', started_at=NOW, **values)


def row(**values):
    return {'execution_id': 'e1', 'agent_id': 'a1', 'task_id': 't1', 'controller_instance_id': 'old',
            'containment': 'job_kill_on_close', 'state': 'contained', 'uncertainty': None,
            'job_name': 'Local\\ControlCenter-' + '0' * 32, 'job_config': dict(CONFIG), 'root_pid': 300,
            'root_created_at': '600', 'evidence': {'launch': {'is_process_in_job': True}}, **values}


class FakeJob:
    def __init__(self, limits=None, members=(300,), fail=None):
        self._limits, self._members, self.fail = dict(limits or CONFIG), list(members), fail
        self.terminated = self.closed = False

    def limits(self):
        return self._limits

    def members(self):
        return self._members

    def terminate(self):
        if self.fail:
            raise ps.SupervisionError(self.fail)
        self.terminated = True
        return {'active_processes': 0, 'total_processes': len(self._members), 'terminated_processes': 1}

    def close(self):
        self.closed = True


class FakeProbe:
    def __init__(self, statuses=None, job=None, created=None, terminate=False):
        self.statuses, self.job, self.created, self.terminated = statuses or {}, job, created or {}, terminate
        self.calls = []

    def process_status(self, pid, created_at):
        self.calls.append(('status', pid))
        return self.statuses.get(pid, 'gone')

    def open_job(self, name):
        self.calls.append(('open', name))
        if isinstance(self.job, Exception):
            raise self.job
        return self.job

    def terminate_exact(self, pid, created_at):
        self.calls.append(('terminate', pid))
        return self.terminated

    def member_created(self, pid):
        return self.created.get(pid, 700)


class DecisionTests(unittest.TestCase):
    def decide(self, row_values=None, probe=None, ctrl=None, cur=None):
        return recovery.decide(row(**(row_values or {})), ctrl if ctrl is not None else controller(),
                               cur if cur is not None else current(), probe or FakeProbe())

    def test_reboot_evidence_must_be_positive_and_consistent(self):
        rebooted_now = current(boot_id=8, os_boot_time='2026-10-22T09:55:00Z')
        self.assertIsNotNone(recovery.rebooted_since(controller(), rebooted_now))
        for name, value in (('same counter', current(boot_id=7, os_boot_time='2026-10-22T09:55:00Z')),
                            ('counter went back', current(boot_id=6, os_boot_time='2026-10-22T09:55:00Z')),
                            ('Fast Startup: counter up, boot time unchanged', current(boot_id=8)),
                            ('boot before the controller started', current(boot_id=8, os_boot_time='2026-10-21T08:00:00Z')),
                            ('no counter', current(boot_id=None, os_boot_time='2026-10-22T09:55:00Z')),
                            ('no boot time', current(boot_id=8, os_boot_time=None)),
                            ('unparsable time', current(boot_id=8, os_boot_time='yesterday'))):
            with self.subTest(name):
                self.assertIsNone(recovery.rebooted_since(controller(), value))
        self.assertIsNone(recovery.rebooted_since(controller(boot_id=None), rebooted_now))
        self.assertIsNone(recovery.rebooted_since(controller(started_at=None), rebooted_now))

    def test_reboot_settles_any_containment_including_reservations(self):
        cur = current(boot_id=8, os_boot_time='2026-10-22T09:55:00Z')
        for values in ({'state': 'launching', 'root_pid': None}, {'containment': 'none', 'state': 'running'},
                       {'containment': 'legacy_unknown', 'state': 'uncertain', 'uncertainty': 'legacy'}, {}):
            with self.subTest(values=values):
                probe = FakeProbe()
                self.assertEqual(self.decide(values, probe, cur=cur)[0], 'ended_by_reboot')
                self.assertEqual(probe.calls, [])  # Nothing is opened or terminated.

    def test_a_launching_reservation_is_never_settled_from_a_missing_job(self):
        probe = FakeProbe(job=None)
        state, reason, _ = self.decide({'state': 'launching', 'root_pid': None, 'root_created_at': None}, probe)
        self.assertEqual((state, reason), ('uncertain', 'launching_unresolved'))
        self.assertEqual(probe.calls, [])

    def test_contained_is_decided_like_running_from_kernel_evidence(self):
        for state in ('contained', 'running'):
            with self.subTest(state=state):
                decided, reason, details = self.decide({'state': state}, FakeProbe(job=None))
                self.assertEqual((decided, reason), ('ended_kernel_guaranteed', None))
                self.assertEqual((details['job'], details['root']), ('not_found', 'gone'))

    def test_a_missing_job_alone_is_never_enough(self):
        cases = {
            'root still alive': ({}, FakeProbe(statuses={300: 'alive'}), 'root_alive_without_job'),
            'root unknown': ({}, FakeProbe(statuses={300: 'unknown'}), 'root_state_unknown'),
            'controller alive or unknown': ({}, FakeProbe(statuses={100: 'unknown'}), 'controller_state_unknown'),
            'no recorded flags': ({'job_config': None}, FakeProbe(), 'containment_unverified'),
            'breakaway allowed': ({'job_config': {**CONFIG, 'breakaway_ok': True}}, FakeProbe(), 'containment_unverified'),
            'assignment never verified': ({'evidence': {}}, FakeProbe(), 'containment_unverified'),
            'no root identity': ({'root_created_at': None}, FakeProbe(), 'containment_unverified'),
            'unsupervised': ({'containment': 'none'}, FakeProbe(), 'unsupervised_restart'),
            'legacy': ({'containment': 'legacy_unknown', 'state': 'uncertain', 'uncertainty': 'legacy'}, FakeProbe(),
                       'legacy'),
        }
        for name, (values, probe, reason) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.decide(values, probe)[:2], ('uncertain', reason))

    def test_another_namespace_is_never_searched(self):
        for cur in (current(session_id=2), current(user_sid='S-1-5-21-2'), current(session_id=None)):
            probe = FakeProbe(job=None)
            with self.subTest(cur=cur):
                self.assertEqual(self.decide(probe=probe, cur=cur)[:2], ('uncertain', 'namespace_mismatch'))
                self.assertNotIn('open', [c[0] for c in probe.calls])

    def test_an_existing_job_is_verified_on_one_handle_before_termination(self):
        job = FakeJob(members=[300, 301])
        state, _, details = self.decide(probe=FakeProbe(statuses={300: 'alive'}, job=job))
        self.assertEqual((state, job.terminated, job.closed, details['members']), ('ended_verified', True, True, [300, 301]))
        mismatches = {
            'other limits': (FakeJob(limits={**CONFIG, 'limit_flags': FLAGS | 0x800, 'breakaway_ok': True}), {}),
            'member older than the root': (FakeJob(members=[300, 301]), {301: 500}),
            'live root outside the Job': (FakeJob(members=[301]), {}),
        }
        for name, (job, created) in mismatches.items():
            with self.subTest(name):
                probe = FakeProbe(statuses={300: 'alive'}, job=job, created=created)
                self.assertEqual(self.decide(probe=probe)[:2], ('uncertain', 'job_identity_unverified'))
                self.assertFalse(job.terminated)  # A name match never authorizes termination.
                self.assertTrue(job.closed)
        job = FakeJob(fail='not confirmed')
        self.assertEqual(self.decide(probe=FakeProbe(job=job))[:2], ('uncertain', 'termination_unconfirmed'))

    def test_a_never_resumed_root_outside_any_job(self):
        values = {'containment': 'none', 'state': 'uncertain', 'uncertainty': 'launch_abort_unverified',
                  'evidence': {'end': {'worker_code_may_have_run': False}}}
        self.assertEqual(self.decide(values, FakeProbe())[0], 'launch_aborted')  # its exact identity is gone
        probe = FakeProbe(statuses={300: 'alive'}, terminate=True)
        self.assertEqual(self.decide(values, probe)[0], 'launch_aborted')
        self.assertIn(('terminate', 300), probe.calls)
        self.assertEqual(self.decide(values, FakeProbe(statuses={300: 'alive'}))[:2],
                         ('uncertain', 'root_alive_outside_job'))
        resumed = {**values, 'evidence': {'end': {'worker_code_may_have_run': True}}}
        self.assertEqual(self.decide(resumed, FakeProbe())[:2], ('uncertain', 'launch_abort_unverified'))

    def test_a_failing_probe_is_never_evidence(self):
        self.assertEqual(self.decide(probe=FakeProbe(job=OSError('access denied')))[:2],
                         ('uncertain', 'resolution_failed'))
        self.assertEqual(recovery.decide(row(), None, current())[:2], ('uncertain', 'controller_unknown'))


class ControllerOwnershipTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'owner.sqlite3')

    def identity(self, **values):
        return {**ps.controller_identity(), 'instance_id': str(uuid4()), **values}

    def test_only_a_controller_proven_gone_lets_another_claim_the_database(self):
        first = self.identity(pid=4242, created_at='1')
        self.store.claim_controller(first, status=lambda pid, created: 'gone')
        for state in ('alive', 'unknown'):
            with self.subTest(state=state), self.assertRaises(ControllerConflict):
                self.store.claim_controller(self.identity(), status=lambda pid, created, s=state: s)
        self.store.claim_controller(self.identity(), status=lambda pid, created: 'gone')

    def test_an_earlier_instance_of_this_process_is_superseded_and_legacy_rows_ignored(self):
        mine = self.identity()
        self.store.claim_controller(mine)
        with self.store._connection() as db, db:
            executions.legacy_controller(db, mine)
        self.store.claim_controller(self.identity(), status=lambda pid, created: self.fail('no liveness probe'))
        self.assertIsNotNone(self.store.controller_instance(mine['instance_id'])['stopped_at'])


class RestartPolicyTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover

    def task_of(self, key):
        return manager._store.agent_task_ids(key)[-1]

    def claim(self, task_id, key='db:main', lifetime='worker'):
        claim_id = manager._store.create_resource_claim(task_id, resource_type='generic', resource_key=key,
                                                        scope='project', lifetime=lifetime)
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)

    def claim_status(self, task_id, claim_id):
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)['status']

    def blocked(self, task_id):
        return any(b['reason_code'] == 'process_ownership_unknown' for b in manager._store.get_task(task_id)['active_blockers'])

    def test_an_unresolved_execution_keeps_every_kind_of_ownership_across_restarts(self):
        key, _ = self.create()
        task_id = self.task_of(key)
        claim = self.claim(task_id)
        intent, _ = manager._store.create_work_intent(task_id, namespace='component', key='parser', mode='single_owner')
        self.recover()
        execution, = manager._store.agent_executions(key)
        self.assertEqual((execution['state'], execution['uncertainty']), ('uncertain', 'unsupervised_restart'))
        assignment = manager._store.get_active_assignment_for_agent(key)
        self.assertIsNotNone(assignment)
        self.assertTrue(self.blocked(task_id))
        self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'active')
        own, = manager._store.work_intents(task_id)
        self.assertEqual((own['intent_id'], bool(own['holds_authority'])), (intent, True))
        rival = manager._store.create_task('Rival')
        manager._store.create_work_intent(rival['task_id'], namespace='component', key='parser', mode='single_owner')
        self.assertFalse(manager._store.work_intents(rival['task_id'])[0]['holds_authority'])
        competing = self.claim(manager._store.create_task('Competing')['task_id'], lifetime='task')
        self.assertEqual(competing['status'], 'waiting')
        # No replacement worker, no direct assignment end, no Cancel release.
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_task_agent(task_id)
        spawn.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'confirmed ended'):
            manager._store.end_assignment(assignment['assignment_id'], 'stopped')
        with self.assertRaisesRegex(ValueError, 'could not be confirmed ended'):
            manager._spawn_with_resources(None, key, ['ignored'], 'mock', '.')  # the execution gate itself
        manager._store.request_work_control(task_id, 'canceled')
        with suppress(Exception):
            manager._settle_tasks([task_id])
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
        self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'active')
        # Repeated recovery changes nothing.
        snapshot = (manager._store.agent_executions(key), manager._store.get_active_assignment_for_agent(key))
        self.recover()
        self.assertEqual((manager._store.agent_executions(key)[0]['state'],
                          manager._store.get_active_assignment_for_agent(key)), ('uncertain', snapshot[1]))
        # Only authoritative evidence settles it, and then ownership follows normal rules.
        with rebooted():
            self.recover()
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_by_reboot')
        self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
        self.assertFalse(self.blocked(task_id))
        self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'released')

    def test_attested_test_doubles_restart_exactly_as_before(self):
        # Controlled evidence: the fixture attests each test double's end (see execution_test_support).
        key, _ = self.create(kind='mock')
        self.output(key, 'Done\n', finish=True)
        waiting, _ = self.create()
        self.output(waiting, f'session id: {waiting}\nCONTROL_CENTER_WAITING: Which file?\n', True)
        stopped, _ = self.create(kind='mock')
        manager.stop_agent(stopped)
        self.recover()
        for agent in (key, waiting, stopped):
            with self.subTest(agent=agent):
                execution, = manager._store.agent_executions(agent)
                self.assertEqual(execution['state'], 'attested_ended')
                self.assertFalse(self.blocked(self.task_of(agent)))
        self.assertEqual(manager.get_agent(waiting)['status'], 'waiting')
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(waiting))

    def test_legacy_work_without_a_journal_stays_uncertain_once_until_a_verified_reboot(self):
        key, _ = self.create()
        task_id = self.task_of(key)
        with manager._store._connection() as db:
            db.execute('DELETE FROM executions WHERE agent_id=?', (key,))  # pre-journal history
        self.recover()
        legacy, = manager._store.agent_executions(key)
        self.assertEqual((legacy['containment'], legacy['state'], legacy['uncertainty']),
                         ('legacy_unknown', 'uncertain', 'legacy'))
        self.assertTrue(legacy['controller_instance_id'].startswith('legacy-'))
        self.assertIsNone(legacy['job_name'])
        self.assertTrue(self.blocked(task_id))
        self.recover()
        self.assertEqual(len(manager._store.agent_executions(key)), 1)  # classified once
        with rebooted():
            self.recover()
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_by_reboot')
        self.assertFalse(self.blocked(task_id))

    def test_an_old_ownership_blocker_without_journal_becomes_legacy_uncertainty(self):
        key, _ = self.create(kind='mock')
        self.output(key, 'Done\n', finish=True)
        task_id = self.task_of(key)
        with manager._store._connection() as db:
            db.execute('DELETE FROM executions WHERE agent_id=?', (key,))
            db.execute('''INSERT INTO task_blockers(id,task_id,blocker_type,reason_code,active,hard)
                VALUES (?,?,'process_ownership','process_ownership_unknown',1,1)''', (str(uuid4()), task_id))
        self.recover()
        legacy, = manager._store.agent_executions(key)
        self.assertEqual(legacy['uncertainty'], 'legacy')
        self.assertTrue(self.blocked(task_id))
        with patch.object(manager.recovery_actions, 'scan_processes', return_value=[]), \
             self.assertRaisesRegex(manager.recovery_actions.RecoveryRefused, 'not established'):
            manager.confirm_process_ownership_stopped(task_id, True)

    def test_an_admission_cannot_slip_in_while_recovery_decides(self):
        key, _ = self.create()
        task_id = self.task_of(key)
        self.recover()
        deciding, release, outcome = threading.Event(), threading.Event(), {}
        real = recovery.decide

        def slow(*args, **kwargs):
            deciding.set()
            release.wait(10)
            return real(*args, **kwargs)

        def reconcile():
            with rebooted():
                outcome['results'] = manager.reconcile_executions(task_ids=[task_id])
        with patch.object(recovery, 'decide', slow), rebooted():
            worker = threading.Thread(target=reconcile)
            worker.start()
            self.assertTrue(deciding.wait(10))
            with patch.object(manager, '_spawn_process', side_effect=AssertionError('no process')), \
                 self.assertRaisesRegex(ValueError, 'could not be confirmed ended'):
                manager._spawn_with_resources(None, key, ['ignored'], 'mock', '.')
            release.set()
            worker.join(15)
        self.assertEqual([state for _, state, _ in outcome['results']], ['ended_by_reboot'])
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_by_reboot')

    def test_an_unsupervised_end_is_blocked_before_its_outcome_is_saved(self):
        # Genuine production rules for this double: its end is observed, never attested.
        with genuine_unsupervised():
            key, process = self.create(kind='mock')
            task_id = self.task_of(key)
            claim = self.claim(task_id)
            observed = []
            real_save = manager._save_agent

            def save(agent_id, *args, **kwargs):
                if agent_id == key and manager.agent_statuses.get(agent_id) != 'running':
                    observed.append(self.blocked(task_id))  # at the moment the outcome is persisted
                return real_save(agent_id, *args, **kwargs)
            with patch.object(manager, '_save_agent', save):
                self.output(key, 'Done\n', finish=True)
        self.assertTrue(observed and all(observed))
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'unsupervised'))
        self.assertNotEqual(manager._store.get_task(task_id)['status'], 'completed')
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
        self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'active')
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_task_agent(task_id)
        spawn.assert_not_called()
        # An unrelated Task is unaffected.
        other, _ = self.create(kind='mock')
        self.assertFalse(self.blocked(self.task_of(other)))

    def test_a_failed_uncertainty_write_never_authorizes_completion(self):
        # Review: a one-shot SQLite failure while recording the unsupervised end.
        real, failed = executions.transition, []

        def transition(db, execution_id, state, *args, **kwargs):
            if state == 'uncertain' and not failed:
                failed.append(execution_id)
                raise sqlite3.OperationalError('disk I/O error')
            return real(db, execution_id, state, *args, **kwargs)
        with genuine_unsupervised():
            key, _ = self.create(kind='mock')
            task_id = self.task_of(key)
            claim = self.claim(task_id)
            intent, _ = manager._store.create_work_intent(task_id, namespace='component', key='parser', mode='single_owner')
            other, _ = self.create(kind='mock')
            with patch.object(executions, 'transition', transition):
                self.output(key, 'Done\n', finish=True)
            self.assertEqual(len(failed), 1)

            def held():
                self.assertNotEqual(manager._store.get_task(task_id)['status'], 'completed')
                self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
                self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'active')
                self.assertTrue(manager._store.work_intents(task_id)[0]['holds_authority'])
                with patch.object(manager, '_spawn_process', side_effect=AssertionError('no process')):
                    with self.assertRaises(ValueError):
                        manager.start_task_agent(task_id)
                    with self.assertRaisesRegex(ValueError, 'could not be confirmed ended'):
                        manager._spawn_with_resources(None, key, ['ignored'], 'mock', '.')
            held()
            self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')  # write lost
            manager.rename_agent(key, 'Later save')  # a later successful database operation
            held()
            manager.get_agent(key)  # reads and retries never settle it
            manager._refresh_status(key)  # the retry records the uncertainty durably
            held()
            row, = manager._store.agent_executions(key)
            self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'unsupervised'))
            self.assertTrue(self.blocked(task_id))
            self.assertFalse(self.blocked(self.task_of(other)))  # unrelated work is untouched

    def test_a_stop_whose_uncertainty_cannot_be_recorded_keeps_all_ownership(self):
        # Review: an unsupervised Stop with a failed uncertain write.
        real, failing = executions.transition, {'on': True}

        def transition(db, execution_id, state, *args, **kwargs):
            if state == 'uncertain' and failing['on']:
                raise sqlite3.OperationalError('disk I/O error')
            return real(db, execution_id, state, *args, **kwargs)
        with genuine_unsupervised():
            key, _ = self.create(kind='mock')
            task_id = self.task_of(key)
            claim = self.claim(task_id)
            manager._store.create_work_intent(task_id, namespace='component', key='parser', mode='single_owner')
            other, _ = self.create(kind='mock')

            def held():
                self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
                self.assertEqual(self.claim_status(task_id, claim['claim_id']), 'active')
                self.assertTrue(manager._store.work_intents(task_id)[0]['holds_authority'])
                self.assertNotEqual(manager._store.get_task(task_id)['status'], 'completed')
                with patch.object(manager, '_spawn_process', side_effect=AssertionError('no process')), \
                     self.assertRaisesRegex(ValueError, 'could not be confirmed ended'):
                    manager._spawn_with_resources(None, key, ['ignored'], 'mock', '.')
            with patch.object(executions, 'transition', transition):
                for _ in range(2):  # repeated Stop never releases it
                    with self.assertRaisesRegex(RuntimeError, 'not confirmed'):
                        manager.stop_agent(key)
                    held()
                    self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')
                # Task control settles nothing either.
                manager._store.request_work_control(task_id, 'paused')
                with suppress(Exception):
                    manager._settle_tasks([task_id])
                held()  # the Pause stays unsettled while ownership is unresolved
            failing['on'] = False
            # Database back: Stop retries the write, the blocker holds it, ownership stays.
            self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
            held()
            row, = manager._store.agent_executions(key)
            self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'unsupervised'))
            self.assertTrue(self.blocked(task_id))
            self.assertFalse(self.blocked(self.task_of(other)))
            with rebooted():  # authoritative evidence (a verified restart) settles it
                self.recover()
            self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_by_reboot')
            self.assertFalse(self.blocked(task_id))

    def test_attested_test_doubles_settle_by_explicit_test_attestation_only(self):
        key, _ = self.create(kind='mock')
        self.output(key, 'Done\n', finish=True)
        row, = manager._store.agent_executions(key)
        self.assertEqual(row['state'], 'attested_ended')
        self.assertEqual(row['containment'], 'none')  # no kernel evidence is claimed
        self.assertEqual(row['evidence']['end']['attested_by'], execution_test_support.ATTESTATION)
        self.assertEqual(row['attestation'], {'kind': 'test_fixture', 'by': execution_test_support.ATTESTATION,
                                              'technically_verified': False})
        self.assertEqual(manager._store.get_task(self.task_of(key))['status'], 'completed')
        self.assertFalse(execution_test_support.is_test_double(subprocess.Popen.__new__(subprocess.Popen)))
        # Production never writes the fixture kind, and its schema refuses it; the
        # only production write of attested_ended is the audited human attestation (P3).
        sources = '\n'.join(path.read_text(encoding='utf-8') for path in Path(manager.__file__).parent.glob('*.py'))
        self.assertNotIn('test_fixture', sources)
        self.assertNotIn('execution_test_support', sources)
        self.assertEqual(sources.count("'attested_ended', ("), 1)  # execution_attestation.settle only

    def test_a_platform_without_trustworthy_supervision_refuses_before_any_process(self):
        task = manager._store.create_task('Unsupported platform')
        with patch.object(ps, 'platform_supported', return_value=False), \
             patch.object(manager, '_spawn_process', side_effect=AssertionError('no process may be created')):
            for launch in (lambda: manager.start_task_agent(task['task_id']),
                           lambda: manager._spawn_with_resources(None, str(uuid4()), ['ignored'], 'mock', '.')):
                with self.subTest(launch=launch), \
                     self.assertRaisesRegex(ps.SupervisionError, 'Process supervision unavailable'):
                    launch()
        with manager._store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))

    def test_removal_settles_automatically_from_authoritative_evidence(self):
        key, _ = self.create()
        token = manager.removal_preview(key)['plan_token']
        operation, _ = manager._store.admit_removal(key, request_key='crash', plan_token=token, mode='single',
                                                    disposition='preserve', reason=None)
        with rebooted():
            self.recover()
        current = manager.get_removal(operation['removal_id'])
        self.assertEqual((current['phase'], current['members'][0]['outcome']), ('removed', 'settled_by_restart_recovery'))


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class KernelRecoveryTests(unittest.TestCase):
    """Real processes: a disposable helper "backend" launches a worker and is killed."""
    HELPER = textwrap.dedent('''
        import json, sys, time
        sys.path.insert(0, {backend!r})
        from app import agent_manager as m, process_supervision as ps
        from app.persistence import AgentStore
        m._store = AgentStore(sys.argv[1])
        grandchild = sys.argv[3]
        worker = [sys.executable, "-c", (
            "import subprocess, sys, time\\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x00000008)\\n"
            "open(%r, 'w').write(str(g.pid))\\n"
            "time.sleep(60)\\n") % grandchild]

        def report_and_hang(launch):
            print(json.dumps(dict(pid=launch.process.pid, created_at=launch.root_created_at, job_name=launch.job.name,
                                  execution_id=launch.execution_id)), flush=True)
            time.sleep(60)
        if sys.argv[2] == 'before_resume':
            ps.PendingLaunch.resume = report_and_hang
        else:
            real = ps.PendingLaunch.resume
            def resumed(launch):
                real(launch)  # the worker really runs
                report_and_hang(launch)  # ...but `running` is never written
            ps.PendingLaunch.resume = resumed
        m._spawn_with_resources(None, 'crash-agent', worker, 'mock', sys.argv[4])
    ''').format(backend=BACKEND)
    HOLDER = textwrap.dedent('''
        import ctypes, sys, time
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.OpenJobObjectW.restype = ctypes.c_void_p
        handle = kernel32.OpenJobObjectW(0x0004, False, sys.argv[1])
        print('held' if handle else 'missing', flush=True)
        time.sleep(60)
    ''')

    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        self.directory = tempfile.mkdtemp(prefix='kernel recovery ')
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.path = Path(self.directory) / 'recovery.sqlite3'
        self.store = AgentStore(self.path)
        self.current = {**ps.controller_identity(), 'instance_id': str(uuid4())}

    def spawn(self, script, *args):
        process = subprocess.Popen([PYTHON, '-I', '-B', '-c', script, *map(str, args)], stdout=subprocess.PIPE, text=True)
        identity = ps.process_created_at(int(process._handle))
        self.addCleanup(lambda: (process.kill(), process.wait(5), process.stdout.close()))
        self.addCleanup(reap, process.pid, identity)
        return process, identity

    def crash(self, boundary, hold=False):
        grandchild = Path(self.directory) / 'grandchild.txt'
        backend, _ = self.spawn(self.HELPER, self.path, boundary, grandchild, self.directory)
        launch = json.loads(backend.stdout.readline())
        self.addCleanup(reap, launch['pid'], launch['created_at'])
        holder = None
        if hold:
            holder, holder_identity = self.spawn(self.HOLDER, launch['job_name'])
            self.assertEqual(holder.stdout.readline().strip(), 'held')
            holder = (holder, holder_identity)
        descendant = None
        if boundary == 'resumed':
            self.assertTrue(until(lambda: grandchild.exists() and grandchild.read_text()))
            pid = int(grandchild.read_text())
            descendant = (pid, self.created(pid))
            self.addCleanup(reap, *descendant)
        backend.kill()  # TerminateProcess: no cleanup code runs in the "backend"
        backend.wait(5)
        return launch, holder, descendant

    def created(self, pid):
        handle = ps._OpenProcess(ps.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        self.assertTrue(handle)
        try:
            return ps.process_created_at(handle)
        finally:
            ps._CloseHandle(handle)

    def resolve(self):
        return recovery.resolve(self.store, self.current)

    def execution(self, execution_id):
        return self.store.get_execution(execution_id)

    def test_contained_crash_before_resume_settles_from_the_destroyed_job(self):
        launch, _, _ = self.crash('before_resume')
        self.assertTrue(until(lambda: not alive(launch['pid'], launch['created_at'])))
        self.assertEqual(self.resolve(), [(launch['execution_id'], 'ended_kernel_guaranteed', None)])
        row = self.execution(launch['execution_id'])
        self.assertEqual(row['evidence']['recovery']['job'], 'not_found')
        self.assertEqual(self.resolve(), [])  # idempotent: nothing left to decide

    def test_a_contained_row_whose_worker_really_ran_is_still_settled_only_by_kernel_evidence(self):
        launch, _, descendant = self.crash('resumed')
        self.assertEqual(self.execution(launch['execution_id'])['state'], 'contained')  # `running` was never written
        self.assertTrue(until(lambda: not alive(*descendant)))  # kill-on-close ended the whole tree
        self.assertEqual(self.resolve()[0][1], 'ended_kernel_guaranteed')

    def test_a_job_kept_alive_by_another_handle_is_verified_then_terminated(self):
        launch, (holder, holder_identity), descendant = self.crash('resumed', hold=True)
        time.sleep(0.5)
        self.assertTrue(alive(*descendant))  # the Job survived the controller
        self.assertTrue(job_exists(launch['job_name']))
        (execution_id, state, _), = self.resolve()
        self.assertEqual(state, 'ended_verified')
        self.assertTrue(until(lambda: not alive(*descendant) and not alive(launch['pid'], launch['created_at'])))
        self.assertTrue(alive(holder.pid, holder_identity))  # The other handle's owner is untouched.
        self.assertIn(descendant[0], self.execution(execution_id)['evidence']['recovery']['members'])

    def test_an_unverifiable_job_or_namespace_is_never_terminated(self):
        launch, (holder, _), descendant = self.crash('resumed', hold=True)
        with self.store._connection() as db, db:
            db.execute('UPDATE executions SET job_config=? WHERE execution_id=?',
                       (json.dumps({**CONFIG, 'limit_flags': FLAGS | 0x100}), launch['execution_id']))
        self.assertEqual(self.resolve()[0][1:], ('uncertain', 'job_identity_unverified'))
        self.assertTrue(alive(*descendant))
        other_session = {**self.current, 'session_id': (self.current['session_id'] or 0) + 1}
        self.assertEqual(recovery.resolve(self.store, other_session)[0][1:], ('uncertain', 'namespace_mismatch'))
        self.assertTrue(alive(*descendant))
        holder.kill()  # cleanup: its last handle closes and kill-on-close ends the tree
        holder.wait(5)
        self.assertTrue(until(lambda: not alive(*descendant)))

    def test_a_reused_pid_neither_blocks_nor_is_touched(self):
        launch, _, _ = self.crash('before_resume')
        bystander, identity = self.spawn('import time; time.sleep(30)')
        with self.store._connection() as db, db:
            db.execute('UPDATE executions SET root_pid=? WHERE execution_id=?', (bystander.pid, launch['execution_id']))
        self.assertEqual(self.resolve()[0][1], 'ended_kernel_guaranteed')
        self.assertTrue(alive(bystander.pid, identity))

    CLAIM = textwrap.dedent('''
        import os, sys, time
        sys.path.insert(0, {backend!r})
        from uuid import uuid4
        from app import process_supervision as ps
        from app.persistence import AgentStore, ControllerConflict
        store = AgentStore(sys.argv[1])
        while not os.path.exists(sys.argv[2]):
            time.sleep(0.005)
        try:
            store.claim_controller({{**ps.controller_identity(), 'instance_id': str(uuid4())}})
            print('claimed', flush=True)
        except ControllerConflict:
            print('refused', flush=True)
        time.sleep(30)
    ''').format(backend=BACKEND)

    def test_two_controllers_starting_together_cannot_both_claim_the_database(self):
        go = Path(self.directory) / 'go'
        first, _ = self.spawn(self.CLAIM, self.path, go)
        second, _ = self.spawn(self.CLAIM, self.path, go)
        time.sleep(1.0)
        go.write_text('go')
        results = sorted(p.stdout.readline().strip() for p in (first, second))
        self.assertEqual(results, ['claimed', 'refused'])
        # This process is a third controller: the live one refuses it too.
        with self.assertRaises(ControllerConflict):
            self.store.claim_controller(self.current)
        for process in (first, second):
            process.kill()
            process.wait(5)
        self.store.claim_controller(self.current)  # proven gone (PID plus creation time)


if __name__ == '__main__':
    unittest.main()
