"""Stage 2D.1b P3: diagnostics, human responsibility attestation and its audit.

Diagnosis tests use explicit fake observations. Flow tests use temporary
databases, process doubles and a chosen process scan, so the host's own
processes never influence them. Real Windows tests use only disposable local
interpreters created here; every cleanup verifies PID and creation time.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, execution_attestation as attestation, execution_diagnostics as diagnostics
from app import executions, presentation, process_supervision as ps, work_control
from app.persistence import AgentStore
from app.task_workspaces import task_lock
from execution_test_support import genuine_unsupervised
import test_persistence
from test_api_security import ALLOWED_ORIGIN, call, headers
from test_executions import PYTHON, alive, reap, until

NOW = '2026-10-22T10:00:00.000Z'
START = diagnostics.filetime(NOW)
SECOND = 10_000_000
LIMITS = {'limit_flags': ps.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 'kill_on_job_close': True, 'breakaway_ok': False,
          'silent_breakaway_ok': False}
CODEX = r'"C:\Program Files\nodejs\node.exe" C:\npm\node_modules\@openai\codex\bin\codex.js exec --json'
ACTOR = {'channel': 'test', 'authenticated': False}


def ident(**values):
    return {'instance_id': 'c1', 'pid': 100, 'created_at': '500', 'session_id': 1, 'user_sid': 'S-1-5-21-1', **values}


CURRENT = ident(instance_id='c2', pid=200, created_at='900')


def row(**values):
    return {'execution_id': 'e1', 'agent_id': 'a1', 'task_id': 't1', 'controller_instance_id': 'c1',
            'containment': 'none', 'state': 'uncertain', 'uncertainty': 'unsupervised', 'job_name': None,
            'job_config': None, 'root_pid': 300, 'root_created_at': str(START + SECOND), 'created_at': NOW,
            'updated_at': NOW, **values}


def proc(pid, parent=1, name='python.exe', command='python worker.py'):
    return {'pid': pid, 'parent': parent, 'name': name, 'command': command}


def codex(pid, parent=1):
    return proc(pid, parent, 'node.exe', CODEX)


class Probe:
    """Fake read-only observations."""
    def __init__(self, statuses=None, job=None, processes=(), created=None, sessions=None, scan_error=None):
        self.statuses = {100: 'gone', **(statuses or {})}
        self.job, self.processes, self.scan_error = job, list(processes), scan_error
        self.created, self.sessions = created or {}, sessions or {}

    def process_status(self, pid, created_at):
        value = self.statuses.get(pid, 'gone')
        if isinstance(value, Exception):
            raise value
        return value

    def inspect_job(self, name):
        if isinstance(self.job, Exception):
            raise self.job
        return self.job

    def scan(self):
        if self.scan_error:
            raise self.scan_error
        return [dict(p) for p in self.processes]

    def created_at(self, pid):
        return self.created.get(pid)

    def session_of(self, pid):
        return self.sessions.get(pid, 1)


def diagnose(probe=None, controller=None, current=CURRENT, tracked_roots=(), **values):
    return diagnostics.diagnose(row(**values), ident() if controller is None else controller, current,
                                trigger='test', tracked_roots=tracked_roots, probe=probe or Probe())


def reasons(result):
    return [r['code'] for r in result['reasons']]


class DiagnosisTests(unittest.TestCase):
    def test_nothing_relevant_is_complete_but_never_called_proof(self):
        found = diagnose(Probe(processes=[proc(500, command='notepad.exe')]))
        self.assertEqual((found['status'], found['classification'], found['candidates']),
                         ('complete', 'no_relevant_candidate', []))
        self.assertIn('never proof', found['sources']['scan']['provenance'])
        self.assertEqual(diagnostics.eligibility(row(), found), {'eligible': True, 'reasons': []})

    def test_the_exact_live_root_is_verified_owned_and_refuses(self):
        found = diagnose(Probe(statuses={300: 'alive'}))
        self.assertEqual(found['classification'], 'verified_owned')
        candidate, = found['candidates']
        self.assertEqual((candidate['pid'], candidate['source'], candidate['classification']), (300, 'root_identity', 'owned'))
        self.assertEqual(reasons(diagnostics.eligibility(row(), found)), ['verified_owned_alive'])

    def test_a_root_that_cannot_be_checked_leaves_the_picture_incomplete(self):
        found = diagnose(Probe(statuses={300: 'unknown'}))
        self.assertEqual((found['status'], found['classification']), ('incomplete', 'ownership_uncertain'))
        self.assertEqual(reasons(diagnostics.eligibility(row(), found)),
                         ['diagnostics_incomplete', 'ownership_uncertain_candidate'])

    def test_creation_time_decides_pid_reuse_never_the_pid_alone(self):
        cases = {
            'created well before the execution: unrelated': (str(START - 10 * SECOND), 'unrelated'),
            'within the clock margin: uncertain': (str(START - SECOND), 'uncertain'),
            'created after it started: uncertain': (str(START + 10 * SECOND), 'uncertain'),
            'creation time unavailable: uncertain': (None, 'uncertain'),
        }
        for name, (created, expected) in cases.items():
            with self.subTest(name):
                found = diagnose(Probe(processes=[codex(700)], created={700: created}))
                candidate, = found['candidates']
                self.assertEqual(candidate['classification'], expected)
                eligible = diagnostics.eligibility(row(), found)['eligible']
                self.assertEqual(eligible, expected == 'unrelated')
                if created is None:
                    self.assertIn('creation time unavailable', candidate['basis'])

    def test_parent_chains_and_controller_children_are_never_proof_but_always_block(self):
        later = str(START + 5 * SECOND)
        found = diagnose(Probe(processes=[proc(400, parent=300), proc(401, parent=400), proc(402, parent=100),
                                          proc(403, parent=1)],
                               created={400: later, 401: later, 402: later, 403: later}))
        by_pid = {c['pid']: c for c in found['candidates']}
        self.assertEqual(set(by_pid), {400, 401, 402})  # 403 matches nothing and is not listed
        self.assertTrue(all(c['classification'] == 'uncertain' for c in by_pid.values()))
        self.assertIn('descendant of the root PID by parent chain (not proof)', by_pid[401]['basis'])
        self.assertIn('child of the recorded controller PID (not proof)', by_pid[402]['basis'])
        self.assertIn('ownership_uncertain_candidate', reasons(diagnostics.eligibility(row(), found)))

    def test_only_a_known_other_session_excludes_a_process(self):
        later = {700: str(START + 5 * SECOND)}
        other = diagnose(Probe(processes=[codex(700)], created=later, sessions={700: 2}))
        self.assertEqual(other['candidates'][0]['classification'], 'unrelated')
        unknown = diagnose(Probe(processes=[codex(700)], created=later, sessions={700: None}))
        self.assertEqual(unknown['candidates'][0]['classification'], 'uncertain')

    def test_trees_this_backend_still_supervises_are_skipped(self):
        found = diagnose(Probe(processes=[codex(800), codex(801, parent=800)]), tracked_roots=(800,))
        self.assertEqual(found['candidates'], [])

    def test_job_evidence(self):
        job_row = dict(containment='job_kill_on_close', job_name='Local\\ControlCenter-' + '1' * 32, job_config=LIMITS)
        member_created = {300: str(START + SECOND), 301: str(START + 2 * SECOND)}
        accounting = {'active_processes': 2, 'total_processes': 2, 'terminated_processes': 0}
        found = diagnose(Probe(job={'limits': LIMITS, 'members': [300, 301], 'accounting': accounting},
                               created=member_created), **job_row)
        self.assertEqual(found['classification'], 'verified_owned')
        self.assertEqual(found['sources']['job']['status'], 'found')
        self.assertEqual({c['pid']: c['classification'] for c in found['candidates']}, {300: 'owned', 301: 'owned'})
        self.assertEqual(set(reasons(diagnostics.eligibility(row(**job_row), found))), {'verified_owned_alive', 'job_present'})
        # A Job with this name but other limits is not proven to be ours: its members stay uncertain.
        other = diagnose(Probe(job={'limits': {**LIMITS, 'breakaway_ok': True}, 'members': [301],
                                    'accounting': accounting}, created=member_created), **job_row)
        self.assertEqual(other['candidates'][0]['classification'], 'uncertain')
        self.assertFalse(other['sources']['job']['limits_match'])
        # No Job: nothing inspected is alive, but the observation is still not proof.
        gone = diagnose(Probe(job=None), **job_row)
        self.assertEqual((gone['sources']['job']['status'], gone['classification']), ('not_found', 'no_relevant_candidate'))
        # A Job of another session or user cannot be inspected from here.
        elsewhere = diagnose(Probe(job=AssertionError('never opened')), controller=ident(session_id=2), **job_row)
        self.assertEqual((elsewhere['sources']['job']['status'], elsewhere['status']), ('not_inspectable', 'incomplete'))
        self.assertIn('diagnostics_incomplete', reasons(diagnostics.eligibility(row(**job_row), elsewhere)))
        # A failure while inspecting it is a failed diagnostic, never a clean one.
        failed = diagnose(Probe(job=OSError('access denied')), **job_row)
        self.assertEqual((failed['status'], failed['classification']), ('failed', 'unavailable'))
        self.assertIn('diagnostic_failed', reasons(diagnostics.eligibility(row(**job_row), failed)))

    def test_the_controller_must_be_this_exact_instance_or_verified_gone(self):
        cases = {'alive': (Probe(statuses={100: 'alive'}), None, False),
                 'unknown': (Probe(statuses={100: 'unknown'}), None, False),
                 'missing': (Probe(), False, False),
                 'verified exited': (Probe(), None, True),
                 'legacy': (Probe(statuses={100: 'alive'}), ident(instance_id='legacy-1'), True),
                 'the exact current instance': (Probe(statuses={100: 'alive'}),
                                                ident(instance_id='c2', pid=200, created_at='900'), True),
                 'an earlier instance in this OS process': (Probe(statuses={100: 'alive'}),
                                                            ident(instance_id='c0', pid=200, created_at='900'), False),
                 'same PID, other creation time (PID reuse)': (Probe(statuses={200: 'gone'}),
                                                              ident(instance_id='c0', pid=200, created_at='1'), True)}
        for name, (probe, controller, eligible) in cases.items():
            with self.subTest(name):
                found = diagnostics.diagnose(row(), None if controller is False else (controller or ident()), CURRENT,
                                             trigger='test', probe=probe)
                result = diagnostics.eligibility(row(), found)
                self.assertEqual(result['eligible'], eligible, result)
                if not eligible:
                    self.assertIn('controller_inconsistent', reasons(result))
        status = diagnostics.controller_status(ident(instance_id='c0', pid=200, created_at='900'), CURRENT, Probe())
        self.assertEqual(status, 'superseded_in_process')  # never probed as if it were another process

    def test_failures_never_look_clean_and_never_raise(self):
        scan = diagnose(Probe(scan_error=RuntimeError('WMI unavailable')))
        self.assertEqual((scan['status'], scan['classification'], scan['sources']['scan']['status']),
                         ('incomplete', 'unavailable', 'failed'))
        self.assertIn('diagnostics_incomplete', reasons(diagnostics.eligibility(row(), scan)))
        broken = diagnose(Probe(statuses={300: OSError('boom')}))
        self.assertEqual((broken['status'], broken['classification']), ('failed', 'unavailable'))
        self.assertIn('diagnostic_failed', reasons(diagnostics.eligibility(row(), broken)))

    def test_legacy_executions_have_no_start_time_that_could_exclude_a_process(self):
        found = diagnose(Probe(processes=[codex(700)], created={700: str(START - 3600 * SECOND)}),
                         containment='legacy_unknown', root_pid=None, root_created_at=None, uncertainty='legacy')
        self.assertEqual(found['candidates'][0]['classification'], 'uncertain')
        self.assertEqual(found['sources']['root'], {'status': 'not_recorded'})

    def test_eligibility_refusals_and_no_stored_command_lines(self):
        self.assertEqual(reasons(diagnostics.eligibility(row(state='ended_verified'), diagnose())), ['not_unresolved'])
        self.assertEqual(reasons(diagnostics.eligibility(row(), diagnose(), tracked=True)), ['tracked_live'])
        self.assertEqual(reasons(diagnostics.eligibility(row(), None)), ['no_diagnostic'])
        self.assertTrue(all(diagnostics.REFUSALS[code] for code in diagnostics.REFUSALS))
        # Matched in memory only: a command line may carry secrets and is never part of a diagnostic.
        found = diagnose(Probe(processes=[{**codex(700), 'command': CODEX + ' --api-key secret-token'}]))
        self.assertEqual(found['candidates'][0]['classification'], 'uncertain')
        self.assertNotIn('command', found['candidates'][0])
        self.assertNotIn('secret-token', json.dumps(found))


class AttestationFlowTests(unittest.TestCase):
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    tearDown = test_persistence.PersistenceTests.tearDown

    def setUp(self):
        test_persistence.PersistenceTests.setUp(self)
        self.processes = []  # the process scan every diagnostic in these tests observes
        patch.object(diagnostics.DiagnosticProbe, 'scan', lambda: [dict(p) for p in self.processes]).start()
        manager._diagnostic_requests.clear()

    def task_of(self, key):
        return manager._store.agent_task_ids(key)[-1]

    def blocked(self, task_id):
        return any(b['reason_code'] == 'process_ownership_unknown' for b in manager._store.get_task(task_id)['active_blockers'])

    def claim_status(self, task_id, claim_id):
        return next(c for c in manager._store.resource_claims(task_id) if c['claim_id'] == claim_id)['status']

    def unresolved(self):
        """An unsupervised end: no Job evidence, so it is uncertain and every ownership stays held."""
        with genuine_unsupervised():
            key, _ = self.create(kind='mock')
            task_id = self.task_of(key)
            claim_id = manager._store.create_resource_claim(task_id, resource_type='generic', resource_key='db:' + key,
                                                            scope='project', lifetime='worker')
            self.output(key, 'Done\n', finish=True)
        execution, = manager._store.agent_executions(key)
        self.assertEqual((execution['state'], execution['uncertainty']), ('uncertain', 'unsupervised'))
        return key, task_id, execution['execution_id'], claim_id

    def review(self, execution_id):
        return manager.refresh_execution_diagnostics(execution_id)['attestation_offer']['reviewed_diagnostic_id']

    def attest(self, execution_id, reviewed, key=None, statement=attestation.STATEMENT, reason=None):
        return manager.attest_execution(execution_id, request_key=key or str(uuid4()), reviewed_diagnostic_id=reviewed,
                                        statement=statement, reason=reason, actor=ACTOR)

    def refused(self, code, execution_id, reviewed, **kwargs):
        with self.assertRaises(manager.AttestationRefused) as caught:
            self.attest(execution_id, reviewed, **kwargs)
        self.assertIn(code, [r['code'] for r in caught.exception.reasons])
        self.assertEqual(caught.exception.detail['state'], 'uncertain')
        return caught.exception

    def history(self, execution_id):
        with manager._store._connection() as db:
            return attestation.history(db, execution_id)

    def held(self, task_id, key, claim_id, execution_id):
        self.assertEqual(executions_state(execution_id), 'uncertain')
        self.assertTrue(self.blocked(task_id))
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
        self.assertEqual(self.claim_status(task_id, claim_id), 'active')

    def test_an_eligible_execution_settles_only_by_explicit_audited_attestation(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        offer = manager.execution_detail(execution_id)['attestation_offer']
        self.assertEqual((offer['eligible'], reasons(offer)), (False, ['no_diagnostic']))
        detail = manager.refresh_execution_diagnostics(execution_id)
        self.assertEqual((detail['diagnostic']['status'], detail['diagnostic']['classification']),
                         ('complete', 'no_relevant_candidate'))
        self.assertTrue(detail['attestation_offer']['eligible'])
        self.assertEqual(detail['attestation_offer']['statement'], attestation.STATEMENT)
        self.held(task_id, key, claim_id, execution_id)  # Diagnostics alone never release anything.
        reviewed = detail['attestation_offer']['reviewed_diagnostic_id']
        result = self.attest(execution_id, reviewed, reason='Checked Task Manager: no node.exe left')
        self.assertEqual(result['outcome'], 'settled_by_attestation')
        settled = result['execution']
        self.assertEqual(settled['state'], 'attested_ended')
        self.assertNotIn(settled['state'], ('ended_verified', 'ended_kernel_guaranteed', 'ended_by_reboot'))
        record = result['attestation']
        self.assertEqual(settled['attestation'], {'kind': 'user_responsibility', 'attestation_id': record['attestation_id'],
                                                  'technically_verified': False})
        self.assertEqual((record['outcome'], record['prior_state'], record['prior_uncertainty']),
                         ('settled', 'uncertain', 'unsupervised'))
        self.assertEqual((record['agent_id'], record['task_id'], record['actor'], record['statement']),
                         (key, task_id, ACTOR, attestation.STATEMENT))
        self.assertEqual(record['controller_instance_id'], manager._controller['instance_id'])
        self.assertEqual(record['reviewed_diagnostic_id'], reviewed)
        self.assertNotIn(record['fresh_diagnostic_id'], (None, reviewed))
        with manager._store._connection() as db:
            fresh = attestation.diagnostic(db, record['fresh_diagnostic_id'])
        self.assertEqual((fresh['trigger'], fresh['conclusions']['eligible']), ('attestation_recheck', True))
        # Only now, and only what this execution held, is released.
        self.assertFalse(self.blocked(task_id))
        self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
        self.assertEqual(self.claim_status(task_id, claim_id), 'released')
        with manager._store._connection() as db:
            self.assertEqual(executions.blocking(db, task_id=task_id), [])

    def test_a_fresh_recheck_that_finds_a_candidate_refuses_and_changes_nothing(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)
        self.processes = [codex(999991)]  # appears after the review; its creation time is unreadable
        refusal = self.refused('ownership_uncertain_candidate', execution_id, reviewed)
        self.assertEqual(refusal.detail['diagnostic']['trigger'], 'attestation_recheck')
        self.held(task_id, key, claim_id, execution_id)
        record, = self.history(execution_id)
        self.assertEqual((record['outcome'], record['reviewed_diagnostic_id']), ('refused', reviewed))
        self.assertIsNotNone(record['fresh_diagnostic_id'])
        self.assertIn('ownership_uncertain_candidate', [r['code'] for r in record['refusal']])

    def test_stale_expired_foreign_and_ineligible_reviews_are_refused(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        other_key, _, other_id, _ = self.unresolved()
        first = self.review(execution_id)
        self.review(execution_id)
        self.refused('stale_review', execution_id, first)
        self.refused('unknown_review', execution_id, self.review(other_id))
        self.refused('unknown_review', execution_id, str(uuid4()))
        self.processes = [codex(999992)]
        self.refused('review_not_eligible', execution_id, self.review(execution_id))
        self.processes = []
        latest = self.review(execution_id)
        with patch.object(attestation, 'REVIEW_WINDOW_SECONDS', -1):
            self.refused('review_expired', execution_id, latest)
        self.held(task_id, key, claim_id, execution_id)
        self.assertEqual([r['outcome'] for r in self.history(execution_id)], ['refused'] * 5)
        self.assertEqual(self.history(other_id), [])
        self.assertEqual(executions_state(other_id), 'uncertain')

    def test_the_acknowledgment_must_be_exact_and_requests_well_formed(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)
        for statement in ('I agree', attestation.STATEMENT + ' ', '', None):
            with self.subTest(statement=statement), self.assertRaisesRegex(ValueError, 'exactly'):
                self.attest(execution_id, reviewed, statement=statement)
        for bad in ('', 'x' * 129, 'line\nbreak'):
            with self.subTest(key=bad), self.assertRaisesRegex(ValueError, 'request_key'):
                manager.attest_execution(execution_id, request_key=bad, reviewed_diagnostic_id=reviewed,
                                         statement=attestation.STATEMENT, actor=ACTOR)
        with self.assertRaisesRegex(ValueError, 'reason'):
            self.attest(execution_id, reviewed, reason='x' * 2001)
        with self.assertRaises(LookupError):
            self.attest(str(uuid4()), reviewed)
        self.assertEqual(self.history(execution_id), [])  # malformed requests decide nothing
        self.held(task_id, key, claim_id, execution_id)

    def test_requests_are_idempotent_and_settle_at_most_once(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)
        first = self.attest(execution_id, reviewed, key='request-1')
        again = self.attest(execution_id, reviewed, key='request-1')
        self.assertEqual(again['attestation']['attestation_id'], first['attestation']['attestation_id'])
        self.assertTrue(again['replayed'])
        with self.assertRaisesRegex(ValueError, 'already used'):
            self.attest(execution_id, str(uuid4()), key='request-1')
        with self.assertRaises(manager.AttestationRefused) as caught:
            self.attest(execution_id, reviewed, key='request-2')
        self.assertEqual([r['code'] for r in caught.exception.reasons], ['not_unresolved'])
        with self.assertRaises(manager.AttestationRefused) as replayed:  # a refusal replays as a refusal
            self.attest(execution_id, reviewed, key='request-2')
        self.assertEqual(replayed.exception.reasons, caught.exception.reasons)
        self.assertEqual([r['outcome'] for r in self.history(execution_id)], ['settled', 'refused'])
        with manager._store._connection() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute('BEGIN IMMEDIATE')
            attestation.record(db, outcome='settled', request_key='request-3', row=executions.get(db, execution_id),
                               controller_instance_id='c', reviewed_diagnostic_id=None, fresh_diagnostic_id=None,
                               actor=ACTOR, reason=None)

    def test_concurrent_decisions_are_refused_not_queued(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)
        held, free = threading.Event(), threading.Event()

        def diagnosing():  # a diagnostic in flight for this execution (another thread)
            with manager._execution_lock(execution_id):
                held.set()
                free.wait(10)
        other = threading.Thread(target=diagnosing)
        other.start()
        self.assertTrue(held.wait(10))
        try:
            self.refused('concurrent_decision', execution_id, reviewed)
        finally:
            free.set()
            other.join(10)
        # Another Task operation (a launch, removal or recovery action) holds the Task.
        holding, release = threading.Event(), threading.Event()

        def hold():
            with task_lock(manager._store, task_id):
                holding.set()
                release.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(holding.wait(10))
        try:
            self.refused('concurrent_decision', execution_id, reviewed)
        finally:
            release.set()
            holder.join(10)
        # Two attestations at once: the second is refused while the first decides.
        entered, proceed, results = threading.Event(), threading.Event(), []
        real = manager.run_diagnostics

        def slow(execution, trigger, **kwargs):
            stored = real(execution, trigger, **kwargs)
            if trigger == 'attestation_recheck':
                entered.set()
                proceed.wait(10)
            return stored
        with patch.object(manager, 'run_diagnostics', slow):
            first = threading.Thread(target=lambda: results.append(self.attest(execution_id, reviewed)))
            first.start()
            self.assertTrue(entered.wait(10))
            self.refused('concurrent_decision', execution_id, reviewed)
            proceed.set()
            first.join(10)
        self.assertEqual(results[0]['outcome'], 'settled_by_attestation')
        self.assertEqual([r['outcome'] for r in self.history(execution_id)], ['settled'])

    def recheck_then(self, change):
        """Run `change` right after the attestation's fresh check (a concurrent recovery or launch)."""
        real = manager.run_diagnostics

        def observed(execution, trigger, **kwargs):
            stored = real(execution, trigger, **kwargs)
            if trigger == 'attestation_recheck':
                with manager._store._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    change(db)
            return stored
        return patch.object(manager, 'run_diagnostics', observed)

    def exited_controller(self, execution_id):
        """Give the execution's controller the identity of a disposable interpreter that has exited."""
        process = subprocess.Popen([PYTHON, '-c', 'pass'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        created = ps.process_created_at(int(process._handle))
        process.wait(10)
        with manager._store._connection() as db:
            instance = executions.get(db, execution_id)['controller_instance_id']
            db.execute('UPDATE controller_instances SET pid=?, created_at=? WHERE instance_id=?',
                       (process.pid, created, instance))

    def paused_in_recheck(self, execution_id):
        """Run an attestation in a thread that pauses right after its fresh check; returns controls."""
        entered, proceed, results = threading.Event(), threading.Event(), []
        real = manager._run_diagnostics

        def slow(execution, trigger, probe):
            stored = real(execution, trigger, probe)
            if trigger == 'attestation_recheck':
                entered.set()
                proceed.wait(10)
            return stored
        patcher = patch.object(manager, '_run_diagnostics', slow)
        patcher.start()

        def run():
            try:
                results.append(self.attest(execution_id, self.reviewed))
            except Exception as exc:  # recorded for the test to inspect
                results.append(exc)
        worker = threading.Thread(target=run)
        return worker, entered, proceed, results, patcher

    def test_a_refresh_or_event_diagnostic_waits_for_an_attestation_in_progress(self):
        for name, observe in (('explicit refresh', lambda e: manager.refresh_execution_diagnostics(e)),
                              ('event-driven worker', lambda e: (manager.request_diagnostics(e, 'termination_unverified'),
                                                                 manager.process_diagnostic_requests()))):
            with self.subTest(name):
                key, task_id, execution_id, claim_id = self.unresolved()
                self.reviewed = self.review(execution_id)
                worker, entered, proceed, results, patcher = self.paused_in_recheck(execution_id)
                worker.start()
                try:
                    self.assertTrue(entered.wait(10))
                    observer = threading.Thread(target=observe, args=(execution_id,))
                    observer.start()
                    observer.join(0.5)
                    self.assertTrue(observer.is_alive())  # no diagnostic runs while the attestation decides
                finally:
                    proceed.set()
                    worker.join(10)
                    patcher.stop()
                observer.join(10)
                self.assertEqual(results[0]['outcome'], 'settled_by_attestation')
                with manager._store._connection() as db:
                    triggers = [r[0] for r in db.execute('SELECT trigger FROM execution_diagnostics WHERE execution_id=? '
                                                         'ORDER BY rowid', (execution_id,))]
                self.assertEqual(triggers[-1], 'attestation_recheck')  # nothing was stored after settlement

    def test_a_newer_adverse_diagnostic_committed_during_attestation_refuses_it(self):
        cases = {
            'verified owned process': {},
            'uncertain candidate': {'classification': 'ownership_uncertain', 'sources': {'controller': {'status': 'self'}},
                                    'candidates': [{'pid': 9, 'created_at': None, 'name': 'node.exe', 'parent': 1,
                                                    'source': 'process_scan', 'classification': 'uncertain',
                                                    'basis': ['test']}]},
            'live Job Object': {'classification': 'no_relevant_candidate', 'candidates': [],
                                'sources': {'controller': {'status': 'self'}, 'job': {'status': 'found'}}},
            'failed inspection': {'status': 'failed', 'classification': 'unavailable', 'candidates': [],
                                  'sources': {'controller': {'status': 'self'}}},
            'incomplete inspection': {'status': 'incomplete', 'classification': 'unavailable', 'candidates': [],
                                      'sources': {'controller': {'status': 'self'}, 'scan': {'status': 'failed'}}},
            'a newer eligible diagnostic (superseded all the same)': {
                'classification': 'no_relevant_candidate', 'candidates': [], 'sources': {'controller': {'status': 'self'}}},
        }
        for name, changes in cases.items():
            with self.subTest(name):
                key, task_id, execution_id, claim_id = self.unresolved()
                reviewed = self.review(execution_id)
                with patch.object(manager, '_tracked', side_effect=lambda e, real=manager._tracked: (
                         self.adverse(execution_id, **changes), real(e))[1]):
                    refusal = self.refused('diagnostics_superseded', execution_id, reviewed)
                self.held(task_id, key, claim_id, execution_id)
                record, = self.history(execution_id)
                self.assertEqual((record['outcome'], record['refusal'][0]['code']), ('refused', 'diagnostics_superseded'))
                self.assertEqual(refusal.detail['state'], 'uncertain')

    def test_a_stored_eligible_conclusion_is_never_trusted_alone(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        fresh = manager.run_diagnostics(execution_id, 'attestation_recheck')
        with manager._store._connection() as db:  # observations that forbid it, with a forged "eligible" conclusion
            db.execute('UPDATE execution_diagnostics SET observations=? WHERE diagnostic_id=?',
                       (json.dumps({'sources': {'controller': {'status': 'self'}, 'root': {'status': 'alive'}},
                                    'candidates': [{'pid': 1, 'classification': 'owned'}]}), fresh['diagnostic_id']))
        for fresh_id, trigger in ((fresh['diagnostic_id'], None), (None, 'explicit_refresh')):
            with self.subTest(trigger=trigger), manager._store._connection() as db, \
                 self.assertRaises(attestation.Changed) as caught:
                if trigger:
                    fresh_id = manager.run_diagnostics(execution_id, trigger)['diagnostic_id']
                db.execute('BEGIN IMMEDIATE')
                attestation.settle(db, request_key='r', row=executions.get(db, execution_id),
                                   controller_instance_id=manager._controller['instance_id'], reviewed_diagnostic_id=None,
                                   fresh_diagnostic_id=fresh_id, actor=ACTOR, reason=None)
            self.assertIn(caught.exception.code, ('diagnostics_not_eligible', 'diagnostics_superseded'))
        self.held(task_id, key, claim_id, execution_id)

    def test_a_state_change_during_diagnosis_makes_that_diagnostic_ineligible(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        real = diagnostics.diagnose

        def diagnose(row, *args, **kwargs):
            observed = real(row, *args, **kwargs)
            with manager._store._connection() as db:  # a concurrent journal write while observing
                db.execute("UPDATE executions SET updated_at='2099-01-01T00:00:00.000Z' WHERE execution_id=?",
                           (execution_id,))
            return observed
        with patch.object(diagnostics, 'diagnose', diagnose):
            stored = manager.run_diagnostics(execution_id, 'explicit_refresh')
        self.assertEqual(reasons(stored['conclusions']), ['execution_changed'])
        self.refused('review_not_eligible', execution_id, stored['diagnostic_id'])
        self.held(task_id, key, claim_id, execution_id)

    def test_launch_admission_racing_an_attestation_is_refused_and_ownership_kept(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        self.reviewed = self.review(execution_id)
        worker, entered, proceed, results, patcher = self.paused_in_recheck(execution_id)
        worker.start()
        self.assertTrue(entered.wait(10))
        try:
            with patch.object(manager, '_spawn_process', side_effect=AssertionError('no process')), \
                 self.assertRaisesRegex(ValueError, 'could not be confirmed ended'):
                manager._spawn_with_resources(None, key, ['ignored'], 'mock', '.')
            self.held(task_id, key, claim_id, execution_id)
        finally:
            proceed.set()
            worker.join(10)
            patcher.stop()
        self.assertEqual(results[0]['outcome'], 'settled_by_attestation')

    def test_technical_settlement_still_wins_during_an_attestation(self):
        # A Job-contained run whose Job is gone and whose exact root (a disposable interpreter) exited:
        # the reconciliation an attestation runs first settles it from kernel evidence instead.
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)  # eligible: no evidence could settle it yet
        root = subprocess.Popen([PYTHON, '-c', 'pass'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        created = ps.process_created_at(int(root._handle))
        root.wait(10)
        with manager._store._connection() as db:
            db.execute("""UPDATE executions SET containment='job_kill_on_close', job_name=?, job_config=?, root_pid=?,
                root_created_at=?, uncertainty='termination_unconfirmed', evidence=? WHERE execution_id=?""",
                       (ps.new_job_name(), json.dumps(LIMITS), root.pid, created,
                        json.dumps({'launch': {'is_process_in_job': True}}), execution_id))
        manager.agents[key] = None  # no longer a process object of this backend: reconciliation decides it
        result = self.attest(execution_id, reviewed)
        self.assertEqual((result['outcome'], result['attestation']), ('settled_by_evidence', None))
        self.assertEqual(executions_state(execution_id), 'ended_kernel_guaranteed')
        self.assertEqual(self.history(execution_id), [])
        self.assertFalse(self.blocked(task_id))

    def adverse(self, execution_id, **changes):
        """Commit a newer diagnostic that forbids attestation (as a concurrent refresh would)."""
        with manager._store._connection() as db:
            row = executions.get(db, execution_id)
        observation = {'execution_id': execution_id, 'trigger': 'explicit_refresh', 'observed_at': ps._now(),
                       'status': 'complete', 'classification': 'verified_owned',
                       'sources': {'controller': {'status': 'self'}, 'root': {'status': 'alive'}},
                       'candidates': [{'pid': 4321, 'created_at': '1', 'name': 'node.exe', 'parent': 1,
                                       'source': 'root_identity', 'classification': 'owned', 'basis': ['test']}]}
        observation.update(changes)
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return attestation.record_diagnostic(db, observation, diagnostics.eligibility(row, observation),
                                                 manager._controller['instance_id'])

    def test_settlement_refuses_when_newer_evidence_superseded_the_fresh_check(self):
        # Review reproduction: an eligible diagnostic, then a newer verified_owned one, then settle() on the older.
        key, task_id, execution_id, claim_id = self.unresolved()
        fresh = manager.run_diagnostics(execution_id, 'attestation_recheck')
        self.assertTrue(fresh['conclusions']['eligible'])
        self.adverse(execution_id)
        with manager._store._connection() as db, self.assertRaises(attestation.Changed):
            db.execute('BEGIN IMMEDIATE')
            attestation.settle(db, request_key='r', row=executions.get(db, execution_id),
                               controller_instance_id=manager._controller['instance_id'],
                               reviewed_diagnostic_id=fresh['diagnostic_id'],
                               fresh_diagnostic_id=fresh['diagnostic_id'], actor=ACTOR, reason=None)
        self.held(task_id, key, claim_id, execution_id)
        self.assertEqual(self.history(execution_id), [])

    def test_a_change_after_the_fresh_check_refuses_atomically(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        changes = {
            'a concurrent journal write': lambda db: db.execute(
                "UPDATE executions SET updated_at='2099-01-01T00:00:00.000Z' WHERE execution_id=?", (execution_id,)),
            'a launch reservation': lambda db: executions.reserve(db, manager._controller, agent_id=key, task_id=task_id,
                                                                  provider='mock', containment='none'),
        }
        for name, change in changes.items():
            with self.subTest(name):
                reviewed = self.review(execution_id)
                with self.recheck_then(change):
                    self.refused('execution_changed', execution_id, reviewed)
                self.held(task_id, key, claim_id, execution_id)
                with manager._store._connection() as db:
                    db.execute("DELETE FROM executions WHERE state='launching'")
        self.assertEqual([r['outcome'] for r in self.history(execution_id)], ['refused', 'refused'])

    def test_authoritative_evidence_is_never_replaced_by_an_attestation(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        reviewed = self.review(execution_id)

        def reboot(db):
            executions.transition(db, execution_id, 'ended_by_reboot', ('uncertain',),
                                  evidence={'recovery': {'outcome': 'ended_by_reboot'}})
        with self.recheck_then(reboot), self.assertRaises(manager.AttestationRefused) as caught:
            self.attest(execution_id, reviewed)
        self.assertEqual([r['code'] for r in caught.exception.reasons], ['execution_changed'])
        self.assertEqual(executions_state(execution_id), 'ended_by_reboot')
        self.assertFalse([r for r in self.history(execution_id) if r['outcome'] == 'settled'])

    def test_a_database_failure_during_settlement_changes_nothing(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        real_transition, real_sync = executions.transition, work_control.sync

        def transition(db, execution, state, *args, **kwargs):
            if state == 'attested_ended':
                raise sqlite3.OperationalError('disk I/O error')
            return real_transition(db, execution, state, *args, **kwargs)

        def sync(*args, **kwargs):
            raise sqlite3.OperationalError('disk I/O error')
        for target, replacement in ((executions, ('transition', transition)), (work_control, ('sync', sync))):
            with self.subTest(replacement[0]):
                reviewed = self.review(execution_id)
                with patch.object(target, *replacement), self.assertRaises(sqlite3.OperationalError):
                    self.attest(execution_id, reviewed)
                self.held(task_id, key, claim_id, execution_id)
                self.assertEqual(self.history(execution_id), [])  # the audit row rolled back with it
        # Database back: the same review cycle settles normally.
        self.assertEqual(self.attest(execution_id, self.review(execution_id))['outcome'], 'settled_by_attestation')

    def test_other_unresolved_executions_keep_the_task_held(self):
        key, task_id, first, claim_id = self.unresolved()
        second = str(uuid4())
        with manager._store._connection() as db:
            columns = [r['name'] for r in db.execute('PRAGMA table_info(executions)') if r['name'] != 'execution_id']
            db.execute(f"INSERT INTO executions(execution_id,{','.join(columns)}) SELECT ?,{','.join(columns)} "
                       'FROM executions WHERE execution_id=?', (second, first))
        self.assertEqual({e['execution_id'] for e in manager.unresolved_executions(agent_id=key)['executions']},
                         {first, second})
        self.attest(first, self.review(first))
        self.assertEqual(executions_state(first), 'attested_ended')
        self.held(task_id, key, claim_id, second)  # one settlement never releases another's ownership
        self.attest(second, self.review(second))
        self.assertFalse(self.blocked(task_id))
        self.assertEqual(self.claim_status(task_id, claim_id), 'released')
        self.assertEqual(manager.unresolved_executions(agent_id=key), {'executions': []})

    def test_diagnostics_are_event_driven_and_never_settle_or_attest(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        self.assertEqual(manager._diagnostic_requests, {execution_id: 'unsupervised_end'})
        self.recover()  # a controller restart re-queues every unresolved execution
        self.assertEqual(manager._diagnostic_requests, {execution_id: 'controller_restart'})
        manager.request_diagnostics(None, 'ignored')
        manager.request_diagnostics(str(uuid4()), 'unknown execution')  # logged, skipped
        with self.assertLogs('app.agent_manager', 'ERROR'):
            manager.process_diagnostic_requests()
        self.assertEqual(manager._diagnostic_requests, {})
        detail = manager.execution_detail(execution_id)
        self.assertEqual(detail['diagnostic']['trigger'], 'controller_restart')
        # recover() restarts inside this same OS process: that earlier instance is not verified gone.
        self.assertEqual(detail['diagnostic']['sources']['controller']['status'], 'superseded_in_process')
        self.assertIn('controller_inconsistent', reasons(detail['attestation_offer']))
        self.exited_controller(execution_id)  # what a real restart looks like: another OS process, now exited
        detail = manager.refresh_execution_diagnostics(execution_id)
        self.assertEqual(detail['diagnostic']['sources']['controller']['status'], 'gone')
        self.assertTrue(detail['attestation_offer']['eligible'])
        # Eligible is not accepted: no startup, reconcile or read ever attests.
        manager.reconcile_executions(task_ids=[task_id])
        self.recover()
        manager.process_diagnostic_requests()
        manager.unresolved_executions(task_id=task_id)
        self.held(task_id, key, claim_id, execution_id)
        self.assertEqual(self.history(execution_id), [])
        # The queue is bounded; a queued execution keeps its place.
        manager._diagnostic_requests.clear()
        for index in range(manager._DIAGNOSTIC_QUEUE_LIMIT):
            manager.request_diagnostics(f'e{index}', 'test')
        manager.request_diagnostics('overflow', 'test')
        manager.request_diagnostics('e0', 'again')
        self.assertNotIn('overflow', manager._diagnostic_requests)
        self.assertEqual(manager._diagnostic_requests['e0'], 'again')
        manager._diagnostic_requests.clear()

    def test_diagnostic_history_is_bounded_but_keeps_what_an_attestation_cites(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        cited = self.review(execution_id)
        self.processes = [codex(999993)]
        with self.assertRaises(manager.AttestationRefused):
            self.attest(execution_id, cited)
        self.processes = []
        for _ in range(attestation.HISTORY_LIMIT + 5):
            self.review(execution_id)
        with manager._store._connection() as db:
            stored = [r[0] for r in db.execute('SELECT diagnostic_id FROM execution_diagnostics WHERE execution_id=?',
                                               (execution_id,))]
        self.assertLessEqual(len(stored), attestation.HISTORY_LIMIT + 2)
        self.assertIn(cited, stored)

    def test_an_unverified_end_is_not_reported_as_a_lost_watcher(self):
        for flag in ('_control_center_end_unverified', '_control_center_uncertainty_unpersisted', None):
            with self.subTest(flag=flag):
                key, process = self.create()
                process.poll.return_value = 0
                if flag:
                    setattr(process, flag, True)
                with patch.object(manager, '_watcher_lost', return_value=True), \
                     patch.object(manager, '_end_unverified', side_effect=lambda p: p._control_center_end_unverified is True):
                    marked = manager.sweep_lost_watchers()
                self.assertEqual(key in marked, flag is None)
                self.assertEqual(manager.WATCHER_LOST_MARKER in manager.agent_outputs[key], flag is None)
                manager.agent_watcher_lost.pop(key, None)

    def test_presentation_names_an_unconfirmed_end_ahead_of_other_states(self):
        for task_status in ('running', 'paused', 'blocked'):
            with self.subTest(task_status=task_status):
                self.assertEqual(presentation.waiting_context('stopped', None, task_status,
                                                              ['process_ownership_unknown', 'dependency_waiting'], None, True),
                                 {'kind': 'recovery', 'label': 'Process end unconfirmed'})

    def api(self, method, path, body=None, token=True):
        request = headers(origin=ALLOWED_ORIGIN, content_type='application/json', extra=[('User-Agent', 'p3-test')],
                          **({} if token else {'token': None}))
        status, _, payload = call(method, path, request, json.dumps(body).encode() if body is not None else b'')
        return status, json.loads(payload) if payload else None

    def test_the_api_validates_refuses_and_records_only_request_context(self):
        key, task_id, execution_id, claim_id = self.unresolved()
        self.assertEqual(self.api('GET', '/executions/unresolved')[0], 422)
        self.assertEqual(self.api('GET', f'/executions/{uuid4()}')[0], 404)
        status, listed = self.api('GET', f'/executions/unresolved?agent_id={key}')
        self.assertEqual((status, [e['execution_id'] for e in listed['executions']]), (200, [execution_id]))
        self.assertEqual(self.api('POST', f'/executions/{execution_id}/diagnostics', token=False)[0], 403)
        status, detail = self.api('POST', f'/executions/{execution_id}/diagnostics')
        self.assertEqual(status, 200)
        reviewed = detail['attestation_offer']['reviewed_diagnostic_id']
        body = {'request_key': 'api-1', 'reviewed_diagnostic_id': reviewed, 'statement': attestation.STATEMENT}
        path = f'/executions/{execution_id}/attestation'
        self.assertEqual(self.api('POST', path, body, token=False)[0], 403)
        self.assertEqual(self.api('POST', path, {**body, 'confirmed': True})[0], 422)  # no client-side override field
        self.assertEqual(self.api('POST', path, {k: v for k, v in body.items() if k != 'statement'})[0], 422)
        status, error = self.api('POST', path, {**body, 'statement': 'yes'})
        self.assertEqual((status, error['detail']['code']), (409, 'attestation_invalid'))
        self.processes = [codex(999994)]
        status, error = self.api('POST', path, {**body, 'request_key': 'api-2'})
        self.assertEqual((status, error['detail']['code']), (409, 'attestation_refused'))
        self.assertIn('ownership_uncertain_candidate', [r['code'] for r in error['detail']['reasons']])
        self.assertEqual(error['detail']['execution']['state'], 'uncertain')
        self.processes = []
        status, detail = self.api('POST', f'/executions/{execution_id}/diagnostics')
        status, settled = self.api('POST', path, {**body, 'request_key': 'api-3',
                                                  'reviewed_diagnostic_id': detail['attestation_offer']['reviewed_diagnostic_id']})
        self.assertEqual((status, settled['outcome'], settled['execution']['state']),
                         (200, 'settled_by_attestation', 'attested_ended'))
        actor = settled['attestation']['actor']
        self.assertEqual((actor['channel'], actor['authenticated'], actor['origin'], actor['user_agent']),
                         ('local_api', False, ALLOWED_ORIGIN, 'p3-test'))
        self.assertIn('not a verified person', actor['note'])
        self.assertNotIn('user_id', actor)
        with patch.object(manager, 'attest_execution', side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(self.api('POST', path, {**body, 'request_key': 'api-4'})[0], 503)


def executions_state(execution_id):
    with manager._store._connection() as db:
        return executions.get(db, execution_id)['state']


class AttestationSchemaTests(unittest.TestCase):
    """The production schema refuses attested_ended without a settled human record (no fixture widening)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'schema.sqlite3')

    def test_attested_ended_requires_a_settled_record_of_the_same_execution(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            executions.register_controller(db, {**ident(), 'boot_id': 1, 'os_boot_time': None})
            for execution_id in ('e1', 'e2'):
                db.execute("""INSERT INTO executions(execution_id,agent_id,provider,controller_instance_id,containment,
                    state,uncertainty) VALUES (?,'a1','mock','c1','none','uncertain','unsupervised')""", (execution_id,))
            row = executions.get(db, 'e1')
            refused = attestation.record(db, outcome='refused', request_key='k1', row=row, controller_instance_id='c1',
                                         reviewed_diagnostic_id=None, fresh_diagnostic_id=None, actor=ACTOR, reason=None,
                                         refusal=[{'code': 'stale_review'}])
            other = attestation.record(db, outcome='settled', request_key='k2', row=executions.get(db, 'e2'),
                                       controller_instance_id='c1', reviewed_diagnostic_id=None, fresh_diagnostic_id=None,
                                       actor=ACTOR, reason=None)
        forged = (None, {'kind': 'user_responsibility', 'attestation_id': 'missing'},
                  {'kind': 'user_responsibility', 'attestation_id': refused},
                  {'kind': 'user_responsibility', 'attestation_id': other},  # another execution's record
                  {'kind': 'test_fixture'}, {'kind': 'kernel', 'technically_verified': True})
        for value in forged:
            with self.subTest(attestation=value), self.store._connection() as db, \
                 self.assertRaisesRegex(sqlite3.IntegrityError, 'settled human attestation'):
                db.execute("""UPDATE executions SET state='attested_ended', attestation=?, ended_at='x'
                    WHERE execution_id='e1'""", (json.dumps(value) if value else None,))
        with self.store._connection() as db, self.assertRaisesRegex(sqlite3.IntegrityError, 'settled human attestation'):
            db.execute("""INSERT INTO executions(execution_id,agent_id,provider,controller_instance_id,containment,state,
                attestation,ended_at) VALUES ('e3','a1','mock','c1','none','attested_ended',?, 'x')""",
                       (json.dumps({'kind': 'test_fixture'}),))
        with self.store._connection() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("""INSERT INTO execution_attestations(attestation_id,request_key,execution_id,agent_id,
                controller_instance_id,prior_state,prior_updated_at,actor,statement,outcome)
                VALUES ('x','k9','e1','a1','c1','uncertain','t','{}','s','refused')""")  # a refusal names its reasons
        with self.store._connection() as db:
            self.assertEqual(executions.get(db, 'e1')['state'], 'uncertain')

    def test_the_v28_to_v29_step_is_additive_and_idempotent(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('DROP TABLE execution_attestations')
            db.execute('DROP TABLE execution_diagnostics')
            for name, _ in attestation.TRIGGERS:
                db.execute(f'DROP TRIGGER {name}')
            db.execute('PRAGMA user_version=28')
        AgentStore(self.store.path)
        AgentStore(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 36)
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger','index')")}
            self.assertTrue({'execution_diagnostics', 'execution_attestations', 'execution_attestation_settled',
                             'execution_diagnostics_latest', *(n for n, _ in attestation.TRIGGERS)} <= names)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


@unittest.skipUnless(os.name == 'nt', 'Windows process identity')
class RealWindowsDiagnosticsTests(unittest.TestCase):
    """Disposable local interpreters only; nothing else is ever touched, and diagnostics end nothing."""

    class NoScan(diagnostics.DiagnosticProbe):
        scan = staticmethod(lambda: [])

    def setUp(self):
        self.identity = {**ps.controller_identity(), 'instance_id': 'real'}

    def sleeper(self):
        process = subprocess.Popen([PYTHON, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        created = ps.process_created_at(int(process._handle))
        self.addCleanup(process.wait, 10)
        self.addCleanup(reap, process.pid, created)
        return process, created

    def diagnose(self, probe=None, **values):
        return diagnostics.diagnose(row(controller_instance_id='real', **values), self.identity, self.identity,
                                    trigger='test', probe=probe or self.NoScan)

    def test_identity_helpers_read_this_process(self):
        self.assertEqual(ps.created_at_of(os.getpid()), self.identity['created_at'])
        self.assertEqual(ps.session_of(os.getpid()), self.identity['session_id'])
        self.assertIsNone(ps.created_at_of(None))
        self.assertIsNone(ps.session_of(0))

    def test_a_live_root_is_owned_only_with_its_exact_creation_time(self):
        start = ps._now()
        process, created = self.sleeper()
        owned = self.diagnose(root_pid=process.pid, root_created_at=created, created_at=start)
        self.assertEqual(owned['classification'], 'verified_owned')
        reused = self.diagnose(root_pid=process.pid, root_created_at=str(int(created) - 1), created_at=start)
        self.assertEqual((reused['sources']['root']['status'], reused['candidates']), ('gone', []))
        self.assertTrue(alive(process.pid, created))  # observed, never ended
        reap(process.pid, created)
        self.assertTrue(until(lambda: not alive(process.pid, created)))
        self.assertEqual(self.diagnose(root_pid=process.pid, root_created_at=created, created_at=start)
                         ['sources']['root']['status'], 'gone')

    def test_a_real_job_is_inspected_with_query_access_only(self):
        start = ps._now()
        name = ps.new_job_name()
        launch = ps.spawn([PYTHON, '-c', 'import time; time.sleep(60)'], job_name=name, stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process, job = launch.process, launch.job
        self.addCleanup(lambda: job.handle and job.close())
        self.addCleanup(reap, process.pid, launch.root_created_at)
        launch.resume()
        values = dict(containment='job_kill_on_close', job_name=name, job_config=job.limits(), root_pid=process.pid,
                      root_created_at=launch.root_created_at, created_at=start)
        found = self.diagnose(**values)
        self.assertEqual(found['sources']['job']['status'], 'found')
        self.assertTrue(found['sources']['job']['limits_match'])
        self.assertIn(process.pid, found['sources']['job']['members'])
        self.assertEqual(found['classification'], 'verified_owned')
        self.assertIn('job_present', reasons(diagnostics.eligibility(row(**values), found)))
        self.assertTrue(alive(process.pid, launch.root_created_at))  # query access cannot end it
        job.terminate()
        job.close()
        process.wait(10)
        gone = self.diagnose(**values)
        self.assertEqual((gone['sources']['job']['status'], gone['sources']['root']['status']), ('not_found', 'gone'))

    def test_a_surviving_descendant_found_by_the_real_scan_blocks_attestation(self):
        start = ps._now()
        root = subprocess.Popen([PYTHON, '-c', textwrap.dedent('''
            import subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x00000008,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(g.pid, flush=True)
        ''')], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        root_created = ps.process_created_at(int(root._handle))
        child = int(root.stdout.readline())
        child_created = ps.created_at_of(child)
        self.addCleanup(reap, child, child_created)
        root.wait(10)
        root.stdout.close()
        values = dict(root_pid=root.pid, root_created_at=root_created, created_at=start)
        found = self.diagnose(probe=diagnostics.DiagnosticProbe, **values)  # the real Win32_Process scan
        self.assertEqual(found['sources']['scan']['status'], 'complete')
        candidate = next(c for c in found['candidates'] if c['pid'] == child)
        self.assertEqual(candidate['classification'], 'uncertain')
        self.assertIn('descendant of the root PID by parent chain (not proof)', candidate['basis'])
        self.assertFalse(diagnostics.eligibility(row(**values), found)['eligible'])
        self.assertTrue(alive(child, child_created))  # never terminated by a diagnostic
        reap(child, child_created)
        self.assertTrue(until(lambda: not alive(child, child_created)))
        after = self.diagnose(probe=diagnostics.DiagnosticProbe, **values)
        self.assertNotIn(child, [c['pid'] for c in after['candidates']])


if __name__ == '__main__':
    unittest.main()
