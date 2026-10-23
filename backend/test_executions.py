"""Stage 2D.1b P1: execution journal and launch evidence.

Temporary databases and repositories only. Real processes are harmless local
Python interpreters created by these tests; every cleanup verifies PID and
creation time before ending one. No provider (Codex/AI) ever runs.
"""
import ctypes
import json
import os
from contextlib import closing, nullcontext
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, executions, process_supervision as ps
from app.persistence import AgentStore
import test_agent_manager
import test_persistence
from execution_test_support import rebooted, genuine_unsupervised
from workspace_test_support import worker_output
from execution_test_support import use_attested_test_doubles

PYTHON = getattr(sys, '_base_executable', sys.executable)  # real interpreter, no venv launcher
BACKEND = str(Path(__file__).resolve().parent)
SYNCHRONIZE, PROCESS_TERMINATE, PROCESS_QUERY_LIMITED_INFORMATION = 0x00100000, 0x0001, 0x1000
JOB_OBJECT_QUERY, ERROR_FILE_NOT_FOUND = 0x0004, 2
JOB_NAME = re.compile(r'Local\\ControlCenter-[0-9a-f]{32}')


def _open_exact(pid, created_at, access):
    """A handle only to the very process with this PID and creation time."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(access | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return kernel32, None
    try:
        if ps.process_created_at(handle) == str(created_at):
            return kernel32, handle
    except OSError:
        pass
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return kernel32, None


def alive(pid, created_at):
    kernel32, handle = _open_exact(pid, created_at, SYNCHRONIZE)
    if handle is None:
        return False  # Gone, or the PID now names another process.
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def reap(pid, created_at):
    """Test hygiene: end a test-created process left by a failing assertion (identity verified)."""
    kernel32, handle = _open_exact(pid, created_at, PROCESS_TERMINATE)
    if handle is not None:
        kernel32.TerminateProcess(ctypes.c_void_p(handle), 1)
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def job_exists(name):
    """Read-only probe: is a Job Object with this name visible in this session?"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenJobObjectW.restype = ctypes.c_void_p
    kernel32.OpenJobObjectW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.OpenJobObjectW(JOB_OBJECT_QUERY, False, name)
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    if ctypes.get_last_error() != ERROR_FILE_NOT_FOUND:
        raise OSError(ctypes.get_last_error(), 'OpenJobObjectW')
    return False


def alive_pid(pid):
    """Assertion only (never used to end a process): is any process with this PID running?"""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0x102
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def rows(path, sql, *args):
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args)]


def marker_script(marker):
    return f'open({str(marker)!r}, "w").write("ran")'


class ExecutionSchemaTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.sqlite3'
        self.store = AgentStore(self.path)
        self.identity = {**ps.controller_identity(), 'instance_id': str(uuid4())}
        self.store.register_controller(self.identity)

    def insert(self, db, **values):
        row = dict(execution_id=str(uuid4()), agent_id='agent', provider='mock',
                   controller_instance_id=self.identity['instance_id'], containment='none', state='running',
                   job_name=None, job_config=None, root_pid=None, root_created_at=None, uncertainty=None,
                   ended_at=None)
        row.update(values)
        names = ','.join(row)
        db.execute(f'INSERT INTO executions({names}) VALUES ({",".join("?" * len(row))})', tuple(row.values()))
        return row['execution_id']

    def test_journal_constraints_admit_only_observed_evidence(self):
        job = dict(containment='job_kill_on_close', job_name=ps.new_job_name(), job_config='{}',
                   root_pid=1, root_created_at='1')
        refused = [
            dict(state='ended_verified', ended_at='now'),                   # unsupervised: never verified
            dict(state='ended_kernel_guaranteed', ended_at='now'),
            dict(containment='legacy_unknown', state='contained'),          # no fabricated containment
            dict(containment='job_kill_on_close', state='running'),         # Job without its evidence
            {**job, 'state': 'running', 'root_created_at': None},           # PID alone is not identity
            dict(state='uncertain'),                                        # uncertainty needs a reason
            dict(state='launch_aborted'),                                   # an end needs ended_at
            dict(state='running', ended_at='now'),                          # open rows have none
            dict(job_name=ps.new_job_name()),                               # a name implies a Job
            dict(controller_instance_id=str(uuid4())),                      # unknown controller
            dict(state='finished'),
        ]
        with self.store._connection() as db:
            for values in refused:
                with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                    self.insert(db, **values)
            self.insert(db, **job, state='contained')
            self.insert(db, state='uncertain', uncertainty='unsupervised')
            self.insert(db, containment='legacy_unknown', state='uncertain', uncertainty='legacy')
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert(db, **{**job, 'root_pid': 2}, state='running')  # job names are unique

    def test_transitions_are_conditional_and_keep_earlier_evidence(self):
        name = ps.new_job_name()
        with self.store._connection() as db:
            identifier = executions.reserve(db, self.identity, agent_id='agent', task_id=None, provider='mock',
                                            containment='job_kill_on_close', job_name=name)
            executions.record_launch(
                db, identifier, containment='job_kill_on_close', state='contained',
                evidence={'launch': {'is_process_in_job': True}}, job_name=name,
                job_config={'kill_on_job_close': True}, root_pid=10, root_created_at='123')
        self.assertTrue(self.store.transition_execution(identifier, 'running', ('contained',), evidence={'resume': {}}))
        self.assertFalse(self.store.transition_execution(identifier, 'running', ('contained',)))
        self.assertTrue(self.store.transition_execution(identifier, 'ended_verified', executions.LIVE,
                                                        evidence={'end': {'via': 'stop'}}))
        # First writer wins: a later uncertain report cannot overwrite verified evidence.
        self.assertFalse(self.store.transition_execution(identifier, 'uncertain', executions.LIVE,
                                                         uncertainty='termination_unconfirmed'))
        row = self.store.get_execution(identifier)
        self.assertEqual(row['state'], 'ended_verified')
        self.assertEqual(set(row['evidence']), {'launch', 'resume', 'end'})
        self.assertIsNotNone(row['ended_at'])
        self.assertIsNone(row['uncertainty'])

    def test_controller_identity_is_recorded_and_never_guessed(self):
        row = self.store.controller_instance(self.identity['instance_id'])
        self.assertEqual(row['pid'], os.getpid())
        self.assertIsNone(row['stopped_at'])
        if os.name == 'nt':
            self.assertEqual(row['created_at'], ps.process_created_at(ps._GetCurrentProcess()))
            self.assertIsInstance(row['session_id'], int)
            self.assertTrue(row['user_sid'].startswith('S-1-'))
            ps.require_identity(row)
            with patch.object(ps, '_user_sid', side_effect=ps.SupervisionError('token unavailable')), \
                 patch.object(ps, '_boot_id', side_effect=OSError('no boot counter')):
                partial = ps.controller_identity()
            self.assertIsNone(partial['user_sid'])
            self.assertIsNone(partial['boot_id'])
            self.assertEqual(len(partial['errors']), 2)
            with self.assertRaisesRegex(ps.SupervisionError, 'user_sid'):
                ps.require_identity(partial)
        self.store.register_controller(self.identity)  # idempotent
        self.store.controller_stopped(self.identity['instance_id'])
        self.assertIsNotNone(self.store.controller_instance(self.identity['instance_id'])['stopped_at'])


# v28 (journal) and v29 (diagnostics and attestations, which reference it), dependents first.
JOURNAL_TABLES = ('execution_attestations', 'execution_diagnostics', 'executions', 'controller_instances')


class ExecutionMigrationTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement

    def v27_copy(self, name):
        path = Path(self.temp.name) / name
        shutil.copyfile(self.path, path)
        with closing(sqlite3.connect(path)) as db, db:
            for table in JOURNAL_TABLES:
                db.execute(f'DROP TABLE {table}')
            db.execute('PRAGMA user_version=27')
        return path

    def snapshot(self, path):
        excluded = ','.join(f"'{table}'" for table in JOURNAL_TABLES)
        tables = [r['name'] for r in rows(path, "SELECT name FROM sqlite_master WHERE type='table' "
                                               f"AND name NOT IN ({excluded}) ORDER BY name")]
        return {t: rows(path, f'SELECT * FROM "{t}" ORDER BY rowid') for t in tables}

    def test_v27_to_v28_adds_only_the_journal_and_fabricates_nothing(self):
        running, _ = self.create(task='Still running')
        done, _ = self.create(task='Done', kind='mock')
        manager.stop_agent(done)
        path = self.v27_copy('v27.sqlite3')
        before = self.snapshot(path)
        AgentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 36)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            # No historical execution or controller is manufactured, even for a running Agent.
            self.assertEqual(db.execute('SELECT COUNT(*) FROM executions').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM controller_instances').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM execution_diagnostics').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM execution_attestations').fetchone()[0], 0)
            indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='executions'")}
            self.assertTrue({'executions_open', 'executions_agent'} <= indexes)
        self.assertEqual(self.snapshot(path), before)
        self.assertEqual(rows(path, 'SELECT status FROM agents WHERE agent_id=?', running)[0]['status'], 'running')
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA user_version=27')  # an idempotent re-run over v28 artifacts
        AgentStore(path)
        self.assertEqual(self.snapshot(path), before)

    def test_failed_migration_rolls_back_and_future_schema_rejected(self):
        self.create()
        path = self.v27_copy('rollback.sqlite3')
        before = self.snapshot(path)
        original = executions.migrate

        def fail(db):
            original(db)
            raise sqlite3.OperationalError('interrupted migration')
        with patch.object(executions, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 27)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='executions'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='controller_instances'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_attestations'").fetchone())
        self.assertEqual(self.snapshot(path), before)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA user_version=37')
        with self.assertRaises(RuntimeError):
            AgentStore(path)

    def test_startup_registers_a_controller_and_clean_shutdown_records_it(self):
        first = manager._controller['instance_id']
        row = manager._store.controller_instance(first)
        self.assertEqual(row['pid'], os.getpid())
        self.assertIsNone(row['stopped_at'])
        self.assertEqual(manager.shutdown_agents(), [])
        self.assertIsNotNone(manager._store.controller_instance(first)['stopped_at'])
        manager.initialize_persistence(self.path)
        self.assertNotEqual(manager._controller['instance_id'], first)
        self.assertIsNone(manager._store.controller_instance(manager._controller['instance_id'])['stopped_at'])

    def test_unsupervised_launch_is_journaled_without_containment_claims(self):
        process = self.replacement()
        process.pid = 4242
        with patch.object(manager.subprocess, 'Popen', return_value=process), \
             patch.object(manager, '_codex_command', return_value='fixed command'), patch.object(manager, 'Thread'):
            key = manager.start_agent('Double', 'codex')
        manager.agent_readers[key].is_alive.return_value = False
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['containment'], row['state'], row['root_pid']), ('none', 'running', 4242))
        self.assertIsNone(row['job_name'])
        self.assertEqual(row['assignment_id'], manager._store.get_active_assignment_for_agent(key)['assignment_id'])
        self.assertEqual(row['controller_instance_id'], manager._controller['instance_id'])
        process.poll.return_value = 0
        with genuine_unsupervised():  # production rules for this double, not the test attestation
            manager._record_end(process, 'root_exit')
        row, = manager._store.agent_executions(key)
        # The root exited, but nothing proves its descendants did: unresolved, and the Task is held.
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'unsupervised'))
        task = manager._store.get_task(row['task_id'])
        self.assertIn('process_ownership_unknown', {b['reason_code'] for b in task['active_blockers']})


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class JobIdentityTests(unittest.TestCase):
    def test_names_are_unpredictable_and_validated(self):
        names = {ps.new_job_name() for _ in range(64)}
        self.assertEqual(len(names), 64)
        self.assertTrue(all(JOB_NAME.fullmatch(name) for name in names))
        for name in ('Global\\ControlCenter-' + '0' * 32, 'Local\\ControlCenter-abc', 'Local\\Other', ''):
            with self.subTest(name=name), self.assertRaisesRegex(ps.SupervisionError, 'invalid Job Object name'):
                ps.Job(name)

    def test_name_collision_is_rejected_without_touching_the_existing_job(self):
        name = ps.new_job_name()
        existing = ps.popen([PYTHON, '-c', 'import time; time.sleep(30)'], job_name=name,
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        identity = ps.process_created_at(int(existing._handle))
        self.addCleanup(reap, existing.pid, identity)
        self.addCleanup(lambda: (existing.supervision_job.close(), existing.wait(5)))
        closed = []
        real_close = ps._CloseHandle
        with patch.object(ps, '_CloseHandle', side_effect=lambda h: closed.append(h) or real_close(h)), \
             patch.object(ps, '_SetInformationJobObject', side_effect=AssertionError('must not configure')):
            with self.assertRaisesRegex(ps.SupervisionError, 'name collision'):
                ps.Job(name)
        self.assertEqual(len(closed), 1)  # The handle to the other Job was closed unused.
        self.assertTrue(alive(existing.pid, identity))
        self.assertEqual(existing.supervision_job.active_processes(), 1)
        self.assertEqual(existing.supervision_job._read_config(), existing.supervision_job.config)

    def test_a_name_held_by_another_object_type_is_refused(self):
        name = ps.new_job_name()
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        mutex = kernel32.CreateMutexW(None, False, name)
        self.assertTrue(mutex)
        self.addCleanup(kernel32.CloseHandle, ctypes.c_void_p(mutex))
        with self.assertRaisesRegex(ps.SupervisionError, 'CreateJobObject'):
            ps.Job(name)

    def test_limits_are_read_back_and_breakaway_is_refused(self):
        real_query = ps._QueryInformationJobObject
        for flags in (ps.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | ps.JOB_OBJECT_LIMIT_BREAKAWAY_OK,
                      ps.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | ps.JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK, 0):
            def reported(handle, kind, info, size, returned, flags=flags):
                if kind != ps.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION:
                    return real_query(handle, kind, info, size, returned)
                info._obj.BasicLimitInformation.LimitFlags = flags
                return 1
            closed = []
            real_close = ps._CloseHandle
            with self.subTest(flags=flags), patch.object(ps, '_QueryInformationJobObject', side_effect=reported), \
                 patch.object(ps, '_CloseHandle', side_effect=lambda h: closed.append(h) or real_close(h)):
                with self.assertRaisesRegex(ps.SupervisionError, 'limits could not be verified'):
                    ps.Job(ps.new_job_name())
                self.assertEqual(len(closed), 1)
        job = ps.Job(ps.new_job_name())
        self.addCleanup(job.close)
        self.assertEqual(job.config, {'limit_flags': ps.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 'kill_on_job_close': True,
                                      'breakaway_ok': False, 'silent_breakaway_ok': False})

    def test_spawn_stops_at_verified_containment_with_root_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran.txt'
            launch = ps.spawn([PYTHON, '-c', marker_script(marker)], job_name=ps.new_job_name(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            self.addCleanup(reap, launch.process.pid, launch.root_created_at)
            handle = int(launch.process._handle)
            self.assertEqual(launch.root_created_at, ps.process_created_at(handle))
            self.assertTrue(launch.job.contains(handle))
            self.assertEqual({k: launch.evidence['job_members'][k] for k in ('active_processes', 'total_processes')},
                             {'active_processes': 1, 'total_processes': 1})
            time.sleep(0.3)
            self.assertFalse(marker.exists())  # Still suspended.
            state, evidence = launch.abort()
            self.assertEqual(state, 'launch_aborted')
            self.assertTrue(evidence['termination_verified'])
            self.assertIsNotNone(launch.process.poll())
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_an_abort_after_resume_never_reports_that_nothing_ran(self):
        launch = ps.spawn([PYTHON, '-c', 'import time; time.sleep(30)'], job_name=ps.new_job_name(),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.addCleanup(reap, launch.process.pid, launch.root_created_at)
        launch.resume()
        self.assertEqual(launch.abort()[0], 'ended_verified')
        # A root found already running is refused, and counted as possibly having run.
        launch = ps.spawn([PYTHON, '-c', 'pass'], job_name=ps.new_job_name(),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.addCleanup(reap, launch.process.pid, launch.root_created_at)
        with patch.object(ps, '_ResumeThread', return_value=0), \
             self.assertRaisesRegex(ps.SupervisionError, 'not suspended'):
            launch.resume()
        self.assertTrue(launch.resumed)
        self.assertEqual(launch.abort()[0], 'ended_verified')

    def unverified_abort(self, **patches):
        """Review finding 2: a never-resumed launch whose end cannot be confirmed."""
        launch = ps.spawn([PYTHON, '-c', 'pass'], job_name=ps.new_job_name(),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.addCleanup(reap, launch.process.pid, launch.root_created_at)
        with patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')), \
             (patch.multiple(ps, **patches) if patches else nullcontext()):
            state, evidence = launch.abort()
        self.assertFalse(launch.resumed)
        # No worker code ran, but that is not evidence that the process ended.
        self.assertEqual(state, 'uncertain')
        self.assertFalse(evidence['termination_verified'])
        self.assertFalse(evidence['worker_code_may_have_run'])
        return launch, evidence

    def test_failed_job_termination_is_never_a_verified_abort(self):
        launch, evidence = self.unverified_abort()
        self.assertTrue(evidence['root_exited'])  # TerminateProcess fallback worked; the Job did not confirm.

    def test_failed_root_termination_and_timeout_are_never_a_verified_abort(self):
        launch, evidence = self.unverified_abort(_TerminateProcess=lambda *_: 0)
        self.assertFalse(evidence['root_exited'])
        self.assertTrue(any('TerminateProcess' in f for f in evidence['failures']))
        launch = ps.spawn([PYTHON, '-c', 'pass'], job_name=ps.new_job_name(),
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        self.addCleanup(reap, launch.process.pid, launch.root_created_at)
        real_wait = launch.process.wait
        with patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')), \
             patch.object(launch.process, 'wait', side_effect=subprocess.TimeoutExpired('worker', 5)):
            state, evidence = launch.abort()
        real_wait(5)
        self.assertEqual((state, evidence['root_exited'], evidence['termination_verified']), ('uncertain', False, False))

    def test_popen_reports_an_unverified_abort_distinctly(self):
        with patch.object(ps, '_ResumeThread', return_value=0xFFFFFFFF), \
             patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')), \
             self.assertRaises(ps.TerminationUnverified):
            ps.popen([PYTHON, '-c', 'pass'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def test_a_closed_job_is_never_evidence_of_termination(self):
        job = ps.Job(ps.new_job_name())
        job.close()
        for probe in (job.terminate, job.accounting, job.active_processes, lambda: job.contains(ps._GetCurrentProcess())):
            with self.subTest(probe=probe), self.assertRaisesRegex(ps.SupervisionError, 'closed'):
                probe()


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class SupervisedExecutionTests(unittest.TestCase):
    """Real launcher path with a store and a fake codex.cmd (no AI)."""
    make_launcher = test_agent_manager.AgentTests.make_launcher

    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        test_persistence.PersistenceTests.setUp(self)
        self.addCleanup(test_persistence.PersistenceTests.tearDown, self)

    def tearDown(self):
        for agent_id, process in list(manager.agents.items()):
            if process is None:
                continue
            if process.poll() is None:
                manager.stop_agent(agent_id)
            watcher = getattr(process, '_control_center_watcher', None)
            if watcher is not None:
                watcher.join(10)

    def launcher(self, script):
        directory = tempfile.mkdtemp(prefix='codex journal ')
        self.addCleanup(shutil.rmtree, directory, True)
        self.make_launcher(directory, 'import sys\nsys.stdin.read()\n' + script)
        self.enterContext(patch.dict(os.environ, {'APPDATA': directory}))

    def lines(self, agent_id, count):
        self.assertTrue(until(lambda: len(worker_output(manager.agent_outputs[agent_id])) >= count))
        return worker_output(manager.agent_outputs[agent_id])

    def all_executions(self):
        return rows(self.path, 'SELECT * FROM executions ORDER BY rowid')

    def test_containment_evidence_commits_before_the_worker_may_run(self):
        self.launcher('import time\nprint("work", flush=True)\ntime.sleep(60)\n')
        seen, original = [], ps.PendingLaunch.resume

        def observed(launch):
            # Another connection sees only committed state.
            seen.append(rows(self.path, 'SELECT state FROM executions WHERE execution_id=?', launch.execution_id))
            self.assertIsNone(launch.process.poll())
            return original(launch)
        with patch.object(ps.PendingLaunch, 'resume', observed):
            key = manager.start_agent('Journal', 'codex')
        self.assertEqual(seen, [[{'state': 'contained'}]])
        process = manager.agents[key]
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['state'], row['containment'], row['provider']), ('running', 'job_kill_on_close', 'codex'))
        self.assertEqual(row['job_name'], process.supervision_job.name)
        self.assertTrue(JOB_NAME.fullmatch(row['job_name']))
        self.assertEqual(row['job_config'], process.supervision_job.config)
        self.assertEqual((row['root_pid'], row['root_created_at']),
                         (process.pid, ps.process_created_at(int(process._handle))))
        self.assertEqual(row['controller_instance_id'], manager._controller['instance_id'])
        assignment = manager._store.get_active_assignment_for_agent(key)
        self.assertEqual((row['assignment_id'], row['task_id']), (assignment['assignment_id'], assignment['task_id']))
        self.assertTrue(row['evidence']['launch']['is_process_in_job'])
        self.assertIn('resumed_at', row['evidence']['resume'])
        self.assertTrue(job_exists(row['job_name']))
        self.lines(key, 1)
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        row, = manager._store.agent_executions(key)
        self.assertEqual(row['state'], 'ended_verified')
        self.assertEqual((row['evidence']['end']['via'], row['evidence']['end']['active_processes']), ('stop', 0))
        self.assertIsNotNone(row['ended_at'])
        self.assertIsNone(process.supervision_job.handle)
        self.assertFalse(job_exists(row['job_name']))  # Our only handle is closed; the Job is gone.

    def test_normal_completion_verifies_that_the_whole_tree_ended(self):
        self.launcher(textwrap.dedent('''
            import ctypes, subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=0x00000008, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("codex", flush=True)
            print(g.pid, flush=True)
        '''))
        key = manager.start_agent('Leaves a descendant', 'codex')
        process = manager.agents[key]
        self.lines(key, 2)
        process._control_center_watcher.join(15)
        self.assertEqual(manager.get_agent(key)['status'], 'finished')
        row, = manager._store.agent_executions(key)
        self.assertEqual(row['state'], 'ended_verified')
        end = row['evidence']['end']
        self.assertEqual((end['via'], end['active_processes']), ('root_exit', 0))
        self.assertGreaterEqual(end['total_processes'], 3)  # launcher, interpreter and the descendant

    DESCENDANT = textwrap.dedent('''
        import subprocess, sys
        g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=0x00000008, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("codex", flush=True)
        print(g.pid, flush=True)
    ''')

    def unverified_completion(self):
        """Review finding 1: the root exits normally, but the Job's end cannot be confirmed."""
        self.launcher(self.DESCENDANT)
        real_terminate, self.failing = ps.Job.terminate, {'watcher': True, 'stop': False}

        def terminate(job, timeout=5.0):
            watcher = threading.current_thread() is not threading.main_thread()
            if self.failing['watcher' if watcher else 'stop']:
                raise ps.SupervisionError('not confirmed')
            return real_terminate(job, timeout)
        self.enterContext(patch.object(ps.Job, 'terminate', terminate))
        key = manager.start_agent('Unverified end', 'codex')
        process = manager.agents[key]
        self.addCleanup(lambda: process.supervision_job.handle and process.supervision_job.close())
        grandchild = int(self.lines(key, 2)[1])
        process._control_center_watcher.join(15)
        self.assertFalse(process._control_center_watcher.is_alive())
        task_id = manager._store.agent_task_ids(key)[0]
        return key, process, grandchild, task_id

    def assert_still_owned(self, key, process, task_id):
        self.assertEqual(manager.get_agent(key)['status'], 'running')
        self.assertEqual(manager.agent_statuses[key], 'running')
        assignment = manager._store.get_active_assignment_for_agent(key)
        self.assertIsNotNone(assignment)  # Not closed, so its ownership is not released.
        self.assertEqual(assignment['task_id'], task_id)
        self.assertNotEqual(manager._store.get_task(task_id)['status'], 'completed')
        self.assertIsNotNone(process.supervision_job.handle)  # Kept: Stop can still verify.
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'termination_unconfirmed'))
        self.assertIn('codex', manager.agent_outputs[key])  # Output is preserved.

    def test_unverified_completion_never_finalizes_the_agent(self):
        key, process, grandchild, task_id = self.unverified_completion()
        self.assertIsNotNone(task_id)
        self.assert_still_owned(key, process, task_id)
        # A lost-watcher recovery or a read cannot settle it either.
        with self.assertRaisesRegex(ValueError, 'unconfirmed'):
            manager.recover_lost_watcher(key)
        manager.agent_watcher_lost.pop(key, None)
        self.assert_still_owned(key, process, task_id)
        # Stop that cannot verify keeps every gate.
        self.failing['stop'] = True
        with self.assertRaisesRegex(RuntimeError, 'stop was not confirmed'):
            manager.stop_agent(key)
        self.assert_still_owned(key, process, task_id)
        self.assertTrue(alive_pid(grandchild))
        self.failing['stop'] = False
        # A verified Stop ends the tree; the execution is stopped, never completed.
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        self.assertTrue(until(lambda: not alive_pid(grandchild)))
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['state'], row['evidence']['end']['via']), ('ended_verified', 'stop'))
        self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
        self.assertEqual(manager._store.get_task(task_id)['status'], 'pending')
        # It ended while the Task carried the ownership blocker (added with the uncertainty).
        self.assertEqual(manager._store.list_task_assignments(task_id)[-1]['ended_reason'], 'blocked')

    def test_cancel_of_an_unverified_execution_first_verifies_termination(self):
        key, process, grandchild, task_id = self.unverified_completion()
        manager._store.request_work_control(task_id, 'canceled')
        self.failing['stop'] = True
        with self.assertRaises(RuntimeError):
            manager._settle_tasks([task_id])
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
        self.assertTrue(alive_pid(grandchild))
        self.failing['stop'] = False
        manager._settle_tasks([task_id])
        self.assertTrue(until(lambda: not alive_pid(grandchild)))
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIsNone(manager._store.get_active_assignment_for_agent(key))
        row, = manager._store.agent_executions(key)
        self.assertEqual(row['state'], 'ended_verified')

    def test_resume_failure_with_unverified_abort_keeps_ownership_evidence(self):
        self.launcher('print("never", flush=True)\n')
        with patch.object(ps, '_ResumeThread', return_value=0xFFFFFFFF), \
             patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')), \
             self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('Abort unverified', 'codex')
        row, = self.all_executions()
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'launch_abort_unverified'))
        end = json.loads(row['evidence'])['end']
        self.assertEqual((end['termination_verified'], end['worker_code_may_have_run']), (False, False))

    def test_unverified_abort_before_the_commit_is_still_journaled(self):
        self.launcher('print("never", flush=True)\n')
        real_record, calls = executions.record_launch, []

        def record_then_fail(*args, **kwargs):
            identifier = real_record(*args, **kwargs)
            calls.append(kwargs['state'])
            if len(calls) == 1:
                raise sqlite3.OperationalError('disk I/O error')  # only the admission write
            return identifier
        with patch.object(executions, 'record_launch', side_effect=record_then_fail), \
             patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')), \
             self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('Commit fails, end unverified', 'codex')
        row, = self.all_executions()
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'launch_abort_unverified'))
        self.assertTrue(JOB_NAME.fullmatch(row['job_name']))

    def coordination(self, task_id):
        """Durable ownership state that a refused launch must not release."""
        tables = ('task_assignments', 'resource_claims', 'resource_ownership', 'work_intents')
        return {t: rows(self.path, f'SELECT * FROM {t} WHERE task_id=? ORDER BY rowid', task_id) for t in tables}

    def partially_resumed_unverified(self):
        """Resume reports the root already running, and the Job's end cannot be confirmed."""
        return (patch.object(ps, '_ResumeThread', return_value=0),
                patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')))

    def test_an_unverified_failed_reply_blocks_the_next_reply_before_popen(self):
        session = str(uuid4())
        self.launcher(f'print("session id: {session}", flush=True)\nprint("codex", flush=True)\n'
                      'print("CONTROL_CENTER_WAITING: Which file?", flush=True)\n')
        key = manager.start_agent('Waiting', 'codex')
        manager.agents[key]._control_center_watcher.join(15)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        task_id = manager._store.agent_task_ids(key)[0]
        assignment = manager._store.get_active_assignment_for_agent(key)['assignment_id']
        self.assertEqual(self.coordination(task_id)['resource_claims'], [])  # no declared resources
        before = self.coordination(task_id)
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.assertRaises(ps.TerminationUnverified):
            manager.reply_agent(key, 'File A')
        failed = manager._store.agent_executions(key)[-1]
        self.assertEqual((failed['state'], failed['uncertainty']), ('uncertain', 'launch_abort_unverified'))
        self.assertTrue(failed['evidence']['end']['worker_code_may_have_run'])
        self.assertEqual(manager.agent_statuses[key], 'waiting')  # The old Agent was not replaced.
        count = len(self.all_executions())
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, self.REFUSED):
            manager.reply_agent(key, 'File B')
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, self.REFUSED):
            manager.decide_agent(key)
        self.assertEqual(len(self.all_executions()), count)  # No row for a refused launch.
        self.assertEqual(manager._store.get_active_assignment_for_agent(key)['assignment_id'], assignment)
        self.assertEqual(manager._store.agent_task_ids(key)[0], task_id)
        self.assertEqual(self.coordination(task_id), before)  # Nothing released or reassigned.
        self.assertEqual(manager.agent_statuses[key], 'waiting')

    def test_an_unverified_failed_initial_launch_blocks_the_task(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Blocked', 'Blocked work')
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        count = len(self.all_executions())
        before = self.coordination(task['task_id'])
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, self.REFUSED):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        self.assertEqual(len(self.all_executions()), count)
        self.assertEqual(self.coordination(task['task_id']), before)
        current = manager._store.get_task(task['task_id'])  # visibly held, never silently pending
        self.assertEqual(current['status'], 'blocked')
        self.assertIn('process_ownership_unknown', {b['reason_code'] for b in current['active_blockers']})
        # Another Task is unaffected.
        other = manager.create_task('Other', 'Other work')
        key = manager.start_agent('', 'codex', task_id=other['task_id'])
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')

    # -- Review: uncertainty persistence is best-effort; the block must not be --
    # The Task's ownership blocker (added with the uncertainty) or the execution gate refuses first.
    REFUSED = 'could not be confirmed ended|execution blockers'

    def cwd(self):
        if not hasattr(self, '_cwd'):
            self._cwd = tempfile.mkdtemp(prefix='journal cwd ')
            self.addCleanup(shutil.rmtree, self._cwd, True)
        return self._cwd

    def journal_down(self):
        """Injected failure: every execution-journal update fails."""
        return patch.object(executions, 'transition', side_effect=sqlite3.OperationalError('disk I/O error'))

    def refused(self, launch, *args, **kwargs):
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, self.REFUSED):
            launch(*args, **kwargs)

    def waiting_agent(self):
        session = str(uuid4())
        self.launcher(f'print("session id: {session}", flush=True)\nprint("codex", flush=True)\n'
                      'print("CONTROL_CENTER_WAITING: Which file?", flush=True)\n')
        key = manager.start_agent('Waiting', 'codex')
        manager.agents[key]._control_center_watcher.join(15)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        return key, manager._store.agent_task_ids(key)[0]

    def test_committed_launch_with_failed_uncertainty_write_still_blocks(self):
        # Scenario A: admission committed, resume and termination unverified, journal update fails.
        key, task_id = self.waiting_agent()
        before, count = self.coordination(task_id), len(self.all_executions())
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.journal_down(), self.assertRaises(ps.TerminationUnverified):
            manager.reply_agent(key, 'File A')
        failed = manager._store.agent_executions(key)[-1]
        self.assertEqual(failed['state'], 'contained')  # The uncertainty itself was never persisted.
        entry, = [e for e in manager._ownership_guard.values() if e['agent_id'] == key]
        self.assertEqual((entry['state'], entry['execution_id'], entry['root_pid'], entry['root_created_at']),
                         ('unverified', failed['execution_id'], failed['root_pid'], failed['root_created_at']))
        self.assertEqual(len(self.all_executions()), count + 1)
        # The journal is reachable again; that alone releases nothing.
        self.refused(manager.reply_agent, key, 'File B')
        self.refused(manager.decide_agent, key)
        self.refused(manager._spawn_with_resources, task_id, str(uuid4()), [PYTHON, '-c', 'pass'], 'mock',
                     self.cwd())  # another Agent on the same Task
        self.assertEqual(len(self.all_executions()), count + 1)  # No duplicate rows.
        self.assertEqual(self.coordination(task_id), before)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        # Unrelated Tasks remain usable.
        other = manager.create_task('Other', 'Other work')
        self.assertEqual(manager._store.agent_executions(
            manager.start_agent('', 'codex', task_id=other['task_id']))[0]['state'], 'running')

    def test_initial_launch_with_failed_uncertainty_write_still_blocks_the_task(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Blocked', 'Blocked work')
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.journal_down(), self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        row, = self.all_executions()
        self.assertEqual(row['state'], 'contained')
        self.refused(manager.start_agent, '', 'codex', task_id=task['task_id'])
        self.assertEqual(len(self.all_executions()), 1)
        self.assertEqual(manager._store.get_task(task['task_id'])['status'], 'pending')

    def test_rolled_back_admission_with_failed_fallback_journal_still_blocks(self):
        # Scenario B: the admission rolls back and every later journal write fails.
        # Phase 0: the reservation committed before Popen still survives.
        key, task_id = self.waiting_agent()
        count = len(self.all_executions())
        task = manager.create_task('Rolled back', 'Rolled back work')
        down = patch.object(executions, 'record_launch', side_effect=sqlite3.OperationalError('disk I/O error'))
        resume, terminate = self.partially_resumed_unverified()
        with terminate, down, self.journal_down(), self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        with resume, terminate, down, self.journal_down(), self.assertRaises(ps.TerminationUnverified):
            manager.reply_agent(key, 'File A')
        reservations = self.all_executions()[count:]
        self.assertEqual([r['state'] for r in reservations], ['launching', 'launching'])  # durable, never completed
        self.assertTrue(all(JOB_NAME.fullmatch(r['job_name']) for r in reservations))
        self.refused(manager.start_agent, '', 'codex', task_id=task['task_id'])
        self.refused(manager.reply_agent, key, 'File B')
        self.assertEqual(len(self.all_executions()), count + 2)
        self.assertEqual(manager.agent_statuses[key], 'waiting')

    def test_an_unconfirmed_discard_after_a_failed_spawn_blocks_with_its_identity(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Discard', 'Discard work')
        with patch.object(ps, '_AssignProcessToJobObject', lambda *_: 0), \
             patch.object(ps, '_TerminateProcess', lambda *_: 0), \
             self.assertRaises(ps.TerminationUnverified) as raised:
            manager.start_agent('', 'codex', task_id=task['task_id'])
        evidence = raised.exception.evidence
        self.addCleanup(reap, evidence['root_pid'], evidence['root_created_at'])  # suspended, never ran
        row, = self.all_executions()
        self.assertEqual((row['state'], row['uncertainty'], row['containment'], row['root_pid'], row['root_created_at']),
                         ('uncertain', 'launch_abort_unverified', 'none', evidence['root_pid'],
                          evidence['root_created_at']))  # never inside its Job
        entry, = [e for e in manager._ownership_guard.values() if e['task_id'] == task['task_id']]
        self.assertEqual((entry['state'], entry['root_pid'], entry['root_created_at']),
                         ('unverified', evidence['root_pid'], evidence['root_created_at']))
        self.refused(manager.start_agent, '', 'codex', task_id=task['task_id'])

    # -- P2 review: an unsupervised root exit is not proof the tree ended ----------
    def test_an_unsupervised_root_exit_with_a_surviving_child_keeps_all_ownership(self):
        # Genuine unsupervised execution (no test fixture): a real process without a Job.
        child_file = Path(self.cwd()) / 'child.txt'
        self.launcher(textwrap.dedent(f'''
            import subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x00000008,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open({str(child_file)!r}, "w").write(str(g.pid))
            print("codex", flush=True)
        '''))

        class Unsupervised(subprocess.Popen):
            """A real process the backend cannot supervise (not the real Popen class)."""
        with patch.object(manager.subprocess, 'Popen', Unsupervised):
            key = manager.start_agent('Unsupervised', 'codex')
            process = manager.agents[key]
            process._control_center_watcher.join(15)
        self.assertTrue(until(lambda: child_file.exists() and child_file.read_text()))
        child = int(child_file.read_text())
        handle = ps._OpenProcess(ps.PROCESS_QUERY_LIMITED_INFORMATION, False, child)
        child_created = ps.process_created_at(handle)
        ps._CloseHandle(handle)
        self.addCleanup(reap, child, child_created)
        self.assertTrue(alive(child, child_created))  # The root exited; its child did not.
        task_id = manager._store.agent_task_ids(key)[0]
        row, = manager._store.agent_executions(key)
        self.assertEqual((row['state'], row['uncertainty']), ('uncertain', 'unsupervised'))

        def held():
            self.assertNotEqual(manager._store.get_task(task_id)['status'], 'completed')
            self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
            self.assertIn('process_ownership_unknown',
                          {b['reason_code'] for b in manager._store.get_task(task_id)['active_blockers']})
            with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
                 self.assertRaises(ValueError):
                manager.start_task_agent(task_id)
        held()
        test_persistence.PersistenceTests.recover(self)  # restart after the observed root exit
        held()
        test_persistence.PersistenceTests.recover(self)  # and again, still without evidence
        held()
        with rebooted():  # only a verified restart (or P3 attestation) settles it
            test_persistence.PersistenceTests.recover(self)
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_by_reboot')
        self.assertIsNone(manager._store.get_active_assignment_for_agent(key))

    # -- P2: this controller reconciles its own unverified ends -------------------
    def guarded(self, task_id):
        return [e for e in manager._ownership_guard.values() if e['task_id'] == task_id]

    def test_this_controller_settles_its_own_unverified_abort_from_kernel_evidence(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Own abort', 'Own abort work')
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        row, = self.all_executions()
        self.assertEqual(row['uncertainty'], 'launch_abort_unverified')
        self.refused(manager.start_agent, '', 'codex', task_id=task['task_id'])
        decided = manager.reconcile_executions(task_ids=[task['task_id']])
        self.assertEqual(decided, [(row['execution_id'], 'ended_kernel_guaranteed', None)])
        self.assertEqual(self.guarded(task['task_id']), [])
        key = manager.start_agent('', 'codex', task_id=task['task_id'])
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')

    def test_this_controller_settles_a_guarded_end_its_journal_never_recorded(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Guard only', 'Guard only work')
        resume, terminate = self.partially_resumed_unverified()
        with resume, terminate, self.journal_down(), self.assertRaises(ps.TerminationUnverified):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        row, = self.all_executions()
        self.assertEqual(row['state'], 'contained')  # the uncertainty was never persisted
        self.assertEqual(len(self.guarded(task['task_id'])), 1)
        manager.reconcile_executions(task_ids=[task['task_id']])
        self.assertEqual(self.all_executions()[0]['state'], 'ended_kernel_guaranteed')
        self.assertEqual(self.guarded(task['task_id']), [])
        manager.start_agent('', 'codex', task_id=task['task_id'])

    def test_this_controller_ends_its_own_orphaned_root_by_verified_identity(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Orphan', 'Orphan work')
        with patch.object(ps, '_AssignProcessToJobObject', lambda *_: 0), \
             patch.object(ps, '_TerminateProcess', lambda *_: 0), \
             self.assertRaises(ps.TerminationUnverified) as raised:
            manager.start_agent('', 'codex', task_id=task['task_id'])
        evidence = raised.exception.evidence
        self.addCleanup(reap, evidence['root_pid'], evidence['root_created_at'])
        self.assertTrue(alive(evidence['root_pid'], evidence['root_created_at']))  # suspended outside any Job
        manager.reconcile_executions(task_ids=[task['task_id']])
        row, = self.all_executions()
        self.assertEqual(row['state'], 'launch_aborted')
        self.assertFalse(alive(evidence['root_pid'], evidence['root_created_at']))
        self.assertEqual(self.guarded(task['task_id']), [])

    def concurrent(self, task_id, first, second):
        """A launch is held in resume while a second launch tries to slip in."""
        in_resume, release, outcome = threading.Event(), threading.Event(), {}
        real_spawn, spawned = ps.spawn, []

        def slow_resume(launch):
            in_resume.set()
            release.wait(10)
            launch.resumed = True  # A thread may have run before the failure.
            raise ps.SupervisionError('resume failed')

        def spawn(*args, **kwargs):
            spawned.append(threading.current_thread().name)
            return real_spawn(*args, **kwargs)

        def run():
            try:
                manager._spawn_with_resources(task_id, first, [PYTHON, '-c', 'import time; time.sleep(30)'], 'mock',
                                              self.cwd())
            except BaseException as exc:
                outcome['error'] = exc
        with patch.object(ps.PendingLaunch, 'resume', slow_resume), patch.object(ps, 'spawn', spawn), \
             patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')):
            worker = threading.Thread(target=run, name='first')
            worker.start()
            self.assertTrue(in_resume.wait(10))
            with self.assertRaisesRegex(ValueError, self.REFUSED):
                manager._spawn_with_resources(task_id, second, [PYTHON, '-c', 'pass'], 'mock', self.cwd())
            release.set()
            worker.join(15)
        self.assertIsInstance(outcome['error'], ps.TerminationUnverified)
        self.assertEqual(spawned, ['first'])  # The second attempt never reached Popen.
        with self.assertRaisesRegex(ValueError, self.REFUSED):
            manager._spawn_with_resources(task_id, second, [PYTHON, '-c', 'pass'], 'mock', self.cwd())

    def test_concurrent_launch_on_the_same_task_cannot_slip_in(self):
        task = manager.create_task('Concurrent', 'Concurrent work')
        self.concurrent(task['task_id'], str(uuid4()), str(uuid4()))

    def test_concurrent_launch_of_the_same_agent_cannot_slip_in(self):
        key = str(uuid4())
        self.concurrent(None, key, key)

    def test_only_verified_termination_releases_an_unconfirmed_end(self):
        key, process, grandchild, task_id = self.unverified_completion_with_journal_down()
        row, = manager._store.agent_executions(key)
        self.assertEqual(row['state'], 'running')  # The watcher's uncertainty write failed.
        self.refused(manager._spawn_with_resources, task_id, str(uuid4()), [PYTHON, '-c', 'pass'], 'mock',
                     self.cwd())
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')  # verified this time
        self.assertTrue(until(lambda: not alive_pid(grandchild)))
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'ended_verified')
        replacement = manager.start_agent('', 'codex', task_id=task_id)
        self.assertEqual(manager._store.agent_executions(replacement)[0]['state'], 'running')

    def unverified_completion_with_journal_down(self):
        self.launcher(self.DESCENDANT)
        real_terminate, real_transition = ps.Job.terminate, executions.transition
        watcher = lambda: threading.current_thread() is not threading.main_thread()

        def terminate(job, timeout=5.0):
            if watcher():
                raise ps.SupervisionError('not confirmed')
            return real_terminate(job, timeout)

        def transition(*args, **kwargs):
            if watcher():
                raise sqlite3.OperationalError('disk I/O error')
            return real_transition(*args, **kwargs)
        self.enterContext(patch.object(ps.Job, 'terminate', terminate))
        self.enterContext(patch.object(executions, 'transition', transition))
        key = manager.start_agent('Unverified end', 'codex')
        process = manager.agents[key]
        self.addCleanup(lambda: process.supervision_job.handle and process.supervision_job.close())
        grandchild = int(self.lines(key, 2)[1])
        process._control_center_watcher.join(15)
        return key, process, grandchild, manager._store.agent_task_ids(key)[0]

    def test_a_verified_launch_abort_does_not_block_a_retry(self):
        self.launcher('import time\ntime.sleep(60)\n')
        task = manager.create_task('Retry', 'Retry work')
        with patch.object(ps, '_ResumeThread', return_value=0xFFFFFFFF), self.assertRaises(OSError):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        aborted, = self.all_executions()
        self.assertEqual(aborted['state'], 'launch_aborted')
        key = manager.start_agent('', 'codex', task_id=task['task_id'])
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')

    def test_failure_before_the_admission_commit_never_runs_the_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran.txt'
            self.launcher(marker_script(marker) + '\n')
            launches, real_spawn, real_record = [], ps.spawn, executions.record_launch

            def spawn(*args, **kwargs):
                launches.append(real_spawn(*args, **kwargs))
                return launches[-1]

            def record_then_fail(*args, **kwargs):
                real_record(*args, **kwargs)
                raise sqlite3.OperationalError('disk I/O error')  # inside the admission transaction
            with patch.object(ps, 'spawn', side_effect=spawn), \
                 patch.object(executions, 'record_launch', side_effect=record_then_fail), \
                 self.assertRaises(sqlite3.OperationalError):
                manager.start_agent('Commit fails', 'codex')
            self.assertEqual(len(launches), 1)
            self.assertIsNotNone(launches[0].process.poll())
            # The admission rolled back; the reservation records the verified abort and the root.
            row, = self.all_executions()
            self.assertEqual((row['state'], row['containment'], row['root_pid'], row['root_created_at']),
                             ('launch_aborted', 'job_kill_on_close', launches[0].process.pid,
                              launches[0].root_created_at))
            self.assertTrue(json.loads(row['evidence'])['end']['termination_verified'])
            self.assertFalse(launches[0].resumed)
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_resume_failure_after_commit_is_journaled_as_aborted(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran.txt'
            self.launcher(marker_script(marker) + '\n')
            with patch.object(ps, '_ResumeThread', return_value=0xFFFFFFFF), self.assertRaises(OSError):
                manager.start_agent('Resume fails', 'codex')
            row, = self.all_executions()
            self.assertEqual(row['state'], 'launch_aborted')
            end = json.loads(row['evidence'])['end']
            self.assertTrue(end['termination_verified'])
            self.assertEqual(end['job']['active_processes'], 0)
            self.assertEqual([k for k, p in manager.agents.items() if p is not None and p.poll() is None], [])
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_incomplete_controller_identity_refuses_before_any_process_exists(self):
        self.launcher('print("never", flush=True)\n')
        self.addCleanup(setattr, manager, '_controller', manager._controller)
        manager._controller = {**manager._controller, 'session_id': None}
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ps.SupervisionError, 'controller identity is incomplete'):
            manager.start_agent('No identity', 'codex')
        self.assertEqual(self.all_executions(), [])

    def test_a_job_name_collision_refuses_the_launch_and_leaves_the_other_job_alone(self):
        self.launcher('print("never", flush=True)\n')
        name = ps.new_job_name()
        other = ps.popen([PYTHON, '-c', 'import time; time.sleep(30)'], job_name=name,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        identity = ps.process_created_at(int(other._handle))
        self.addCleanup(reap, other.pid, identity)
        self.addCleanup(lambda: (other.supervision_job.close(), other.wait(5)))
        with patch.object(ps, 'new_job_name', return_value=name), \
             self.assertRaisesRegex(ps.SupervisionError, 'name collision'):
            manager.start_agent('Collides', 'codex')
        row, = self.all_executions()  # The reservation closes as aborted: no process was created.
        self.assertEqual((row['state'], row['containment'], row['job_name'], row['root_pid']),
                         ('launch_aborted', 'none', None, None))
        self.assertFalse(json.loads(row['evidence'])['end']['process_created'])
        self.assertTrue(alive(other.pid, identity))
        self.assertEqual(other.supervision_job.active_processes(), 1)

    # -- Phase 0: durable launch reservation --------------------------------------
    def test_a_reservation_that_cannot_be_persisted_creates_no_process(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('No reservation', 'No reservation work')
        with patch.object(executions, 'reserve', side_effect=sqlite3.OperationalError('disk I/O error')), \
             patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaises(sqlite3.OperationalError):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        self.assertEqual(self.all_executions(), [])
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))
        manager.start_agent('', 'codex', task_id=task['task_id'])  # nothing was left behind

    def held_after_reservation(self, launch, *args, **kwargs):
        """Run `launch` in a thread that stops right after its reservation committed."""
        reserved, release, outcome = threading.Event(), threading.Event(), {}
        real = manager._reserve_launch
        owner = []

        def reserve(*a, **k):
            result = real(*a, **k)
            if threading.current_thread() is owner[0]:
                reserved.set()
                release.wait(15)
            return result

        def run():
            try:
                outcome['result'] = launch(*args, **kwargs)
            except BaseException as exc:
                outcome['error'] = exc
        self.enterContext(patch.object(manager, '_reserve_launch', reserve))
        worker = threading.Thread(target=run)
        owner.append(worker)
        worker.start()
        self.assertTrue(reserved.wait(15))
        return release, worker, outcome

    def test_an_open_reservation_refuses_a_concurrent_launch_of_the_same_task(self):
        self.launcher('import time\ntime.sleep(60)\n')
        task, other = manager.create_task('Reserved', 'Reserved work'), manager.create_task('Free', 'Free work')
        release, worker, outcome = self.held_after_reservation(
            manager._spawn_with_resources, task['task_id'], str(uuid4()), [PYTHON, '-c', 'import time; time.sleep(30)'],
            'mock', self.cwd())
        count = len(self.all_executions())
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, 'already reserved'):
            manager._spawn_with_resources(task['task_id'], str(uuid4()), [PYTHON, '-c', 'pass'], 'mock', self.cwd())
        self.assertEqual(len(self.all_executions()), count)
        # An unrelated Task launches meanwhile.
        key = manager.start_agent('', 'codex', task_id=other['task_id'])
        self.assertEqual(manager._store.agent_executions(key)[0]['state'], 'running')
        release.set()
        worker.join(15)
        process = outcome['result']
        self.addCleanup(lambda: (process.supervision_job.handle and process.supervision_job.terminate(),
                                 process.supervision_job.close(), process.wait(5)))
        first, = rows(self.path, "SELECT state FROM executions WHERE task_id=?", task['task_id'])
        self.assertEqual(first['state'], 'running')

    def test_an_open_reservation_refuses_a_concurrent_launch_of_the_same_agent(self):
        key = str(uuid4())
        release, worker, outcome = self.held_after_reservation(
            manager._spawn_with_resources, None, key, [PYTHON, '-c', 'import time; time.sleep(30)'], 'mock', self.cwd())
        with patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaisesRegex(ValueError, 'already reserved'):
            manager._spawn_with_resources(None, key, [PYTHON, '-c', 'pass'], 'mock', self.cwd())
        release.set()
        worker.join(15)
        process = outcome['result']
        self.addCleanup(lambda: (process.supervision_job.handle and process.supervision_job.terminate(),
                                 process.supervision_job.close(), process.wait(5)))
        self.assertEqual([r['state'] for r in manager._store.agent_executions(key)], ['running'])

    def test_cancel_racing_an_open_reservation_refuses_before_popen(self):
        self.launcher('print("never", flush=True)\n')
        task = manager.create_task('Canceled', 'Canceled work')
        real = manager._reserve_launch

        def reserve_then_cancel(*args, **kwargs):
            result = real(*args, **kwargs)
            manager._store.request_work_control(task['task_id'], 'canceled')
            return result
        with patch.object(manager, '_reserve_launch', reserve_then_cancel), \
             patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaises(ValueError):
            manager.start_agent('', 'codex', task_id=task['task_id'])
        row, = self.all_executions()
        self.assertEqual((row['state'], row['containment']), ('launch_aborted', 'none'))
        self.assertFalse(json.loads(row['evidence'])['end']['process_created'])
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))

    def test_remove_racing_an_open_reservation_refuses_the_reply_before_popen(self):
        key, task_id = self.waiting_agent()
        count = len(self.all_executions())
        real = manager._reserve_launch

        def reserve_then_remove(*args, **kwargs):
            result = real(*args, **kwargs)
            token = manager.removal_preview(key)['plan_token']
            manager.remove_agent(key, request_key=str(uuid4()), plan_token=token)
            return result
        with patch.object(manager, '_reserve_launch', reserve_then_remove), \
             patch.object(ps, 'spawn', side_effect=AssertionError('no process may be created')), \
             self.assertRaises(ValueError):
            manager.reply_agent(key, 'File A')
        row = self.all_executions()[count]
        self.assertEqual((row['state'], row['agent_id']), ('launch_aborted', key))
        self.assertEqual(len(self.all_executions()), count + 1)
        self.assertEqual([r['phase'] for r in manager.list_agent_removals(key)], ['removed'])

    def test_an_admission_refused_after_reservation_releases_nothing_and_allows_a_retry(self):
        key, task_id = self.waiting_agent()
        before, count = self.coordination(task_id), len(self.all_executions())
        refuse = patch.object(manager.delegation_safety, 'worker_boundary',
                              side_effect=ValueError('worker boundary refused'))
        # The reservation cannot even be closed: it stays open and blocks until closed safely.
        with refuse, self.journal_down(), patch.object(ps, 'spawn', side_effect=AssertionError('no process')), \
             self.assertRaisesRegex(ValueError, 'worker boundary refused'):
            manager.reply_agent(key, 'File A')
        self.assertEqual(self.all_executions()[count]['state'], 'launching')
        self.assertEqual(self.coordination(task_id), before)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        # With the journal back, the next reservation closes the known no-process one first.
        self.assertEqual(manager.reply_agent(key, 'File B')['status'], 'running')
        states = [r['state'] for r in self.all_executions()[count:]]
        self.assertEqual(states, ['launch_aborted', 'running'])

    def test_each_resumed_process_is_a_new_execution(self):
        session = str(uuid4())
        self.launcher(f'import time\nprint("session id: {session}", flush=True)\ntime.sleep(60)\n')
        key = manager.start_agent('Redirected', 'codex')
        self.lines(key, 1)
        manager.redirect_agent(key, 'Change course')
        first, second = manager._store.agent_executions(key)
        self.assertEqual((first['state'], first['evidence']['end']['via']), ('ended_verified', 'stop'))
        self.assertEqual(second['state'], 'running')
        self.assertNotEqual(first['job_name'], second['job_name'])
        self.assertEqual(first['assignment_id'], second['assignment_id'])
        self.assertEqual(second['root_pid'], manager.agents[key].pid)


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class CrashBoundaryTests(unittest.TestCase):
    """A disposable helper "backend" is killed (TerminateProcess) at a launch boundary."""
    HELPER = textwrap.dedent('''
        import json, sys, time
        sys.path.insert(0, {backend!r})
        from app import agent_manager as m, executions, process_supervision as ps
        from app.persistence import AgentStore
        m._store = AgentStore(sys.argv[1])
        worker = [sys.executable, "-c", "open(%r, 'w').write('ran')" % sys.argv[3]]

        def report_and_hang(launch_or_row):
            print(json.dumps(launch_or_row), flush=True)
            time.sleep(60)
        if sys.argv[2] == 'before_popen':
            # Reservation committed, admission open, no process yet.
            ps.spawn = lambda *args, **kwargs: report_and_hang(dict(
                pid=None, created_at=None, job_name=kwargs['job_name'], execution_id=m._launch.journal.execution_id))
        elif sys.argv[2] == 'before_contained':
            # Suspended root assigned and verified in its Job; the containment update not yet written.
            def record(db, execution_id, **values):
                report_and_hang(dict(pid=values['root_pid'], created_at=values['root_created_at'],
                                     job_name=values['job_name'], execution_id=execution_id))
            executions.record_launch = record
        else:
            ps.PendingLaunch.resume = lambda launch: report_and_hang(dict(
                pid=launch.process.pid, created_at=launch.root_created_at,
                job_name=launch.job.name, execution_id=launch.execution_id))
        m._spawn_with_resources(None, 'crash-agent', worker, 'mock', sys.argv[4])
    ''').format(backend=BACKEND)

    def crash_at(self, boundary):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path, marker = Path(directory.name) / 'crash.sqlite3', Path(directory.name) / 'ran.txt'
        AgentStore(path)
        backend = subprocess.Popen([PYTHON, '-I', '-B', '-c', self.HELPER, str(path), boundary, str(marker),
                                    directory.name], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (backend.kill(), backend.wait(5), backend.stdout.close()))
        launch = json.loads(backend.stdout.readline())
        if launch['pid'] is not None:
            self.addCleanup(reap, launch['pid'], launch['created_at'])
            self.assertTrue(alive(launch['pid'], launch['created_at']))  # suspended inside its Job
        backend.kill()  # No cleanup code runs in the "backend".
        backend.wait(5)
        if launch['pid'] is not None:
            self.assertTrue(until(lambda: not alive(launch['pid'], launch['created_at'])))
        self.assertFalse(job_exists(launch['job_name']))
        time.sleep(0.3)
        self.assertFalse(marker.exists())  # Not one line of the worker ran.
        return path, launch

    def assert_abandoned_reservation(self, path, launch):
        # The admission rolled back; the reservation committed before it survives,
        # open and never treated as completed (resolving it is P2).
        row, = rows(path, 'SELECT * FROM executions')
        self.assertEqual((row['execution_id'], row['state'], row['job_name'], row['root_pid'], row['ended_at']),
                         (launch['execution_id'], 'launching', launch['job_name'], None, None))
        controller, = rows(path, 'SELECT * FROM controller_instances')
        self.assertEqual(row['controller_instance_id'], controller['instance_id'])
        self.assertIsNone(controller['stopped_at'])

    def test_crash_before_popen_leaves_an_open_reservation(self):
        path, launch = self.crash_at('before_popen')
        self.assert_abandoned_reservation(path, launch)

    def test_crash_after_suspended_popen_before_containment_leaves_an_open_reservation(self):
        path, launch = self.crash_at('before_contained')
        self.assertIsNotNone(launch['pid'])  # A real suspended root existed, and died with its Job.
        self.assert_abandoned_reservation(path, launch)

    def test_crash_after_containment_commit_before_resume_keeps_the_evidence(self):
        path, launch = self.crash_at('before_resume')
        row, = rows(path, 'SELECT * FROM executions')
        # P1 records; it does not resolve. The open row is left for restart resolution (P2).
        self.assertEqual((row['execution_id'], row['state'], row['job_name'], row['root_pid'], row['root_created_at']),
                         (launch['execution_id'], 'contained', launch['job_name'], launch['pid'], launch['created_at']))
        controller, = rows(path, 'SELECT * FROM controller_instances')
        self.assertEqual(row['controller_instance_id'], controller['instance_id'])
        self.assertIsNone(controller['stopped_at'])
        self.assertNotEqual(controller['pid'], os.getpid())


if __name__ == '__main__':
    unittest.main()
