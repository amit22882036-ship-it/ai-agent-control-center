"""Windows Job Object supervision for agent executions (one Job per execution).

Each execution gets its own Job Object with an unpredictable name, configured
with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (no breakaway); the flags are read back
before use. The worker is created suspended, assigned to the Job, verified with
IsProcessInJob and the Job's accounting, and only then resumed. Descendants
inherit the Job, so terminating or closing it ends the whole execution tree,
including orphaned descendants. If the backend dies, Windows closes the handle
and kills every supervised tree. Any failure is fail-closed: the never-resumed
process is terminated and the launch refused.

`spawn` stops at the verified, still-suspended state so that the caller can
persist the containment evidence before any worker code runs.
"""
import ctypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone

MARKER = '--- Process supervision: active (job object) ---'
JOB_NAME_PREFIX = 'Local\\ControlCenter-'
_JOB_NAME = re.compile(r'Local\\ControlCenter-[0-9a-f]{32}')
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
ERROR_ALREADY_EXISTS = 183
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_MORE_DATA = 234
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STOP_EXIT_CODE = 1
BOOT_ID_KEY = r'SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters'

_status_lock = threading.Lock()
_status = ({'status': 'not_applicable'} if os.name != 'nt'
           else {'status': 'unavailable', 'reason': 'startup self-test has not run'})


class SupervisionError(OSError):
    """Supervision could not be established or confirmed; nothing runs unsupervised."""


class TerminationUnverified(SupervisionError):
    """A refused launch whose process could not be confirmed ended; ownership is kept."""


if os.name == 'nt':
    import winreg

    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
            'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                    ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                    ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                    ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                    ('SchedulingClass', wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimits), ('IoInfo', _IoCounters),
                    ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                    ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [('TotalUserTime', ctypes.c_longlong), ('TotalKernelTime', ctypes.c_longlong),
                    ('ThisPeriodTotalUserTime', ctypes.c_longlong), ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
                    ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                    ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                    ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG),
                    ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]

    def _signature(name, restype, *argtypes, library=None):
        function = getattr(library or _kernel32, name)
        function.restype, function.argtypes = restype, argtypes
        return function

    _FILETIME = ctypes.POINTER(wintypes.FILETIME)
    _CreateJobObjectW = _signature('CreateJobObjectW', wintypes.HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
    _SetInformationJobObject = _signature('SetInformationJobObject', wintypes.BOOL, wintypes.HANDLE,
                                          ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _QueryInformationJobObject = _signature('QueryInformationJobObject', wintypes.BOOL, wintypes.HANDLE,
                                            ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    _AssignProcessToJobObject = _signature('AssignProcessToJobObject', wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE)
    _IsProcessInJob = _signature('IsProcessInJob', wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE,
                                 ctypes.POINTER(wintypes.BOOL))
    _TerminateJobObject = _signature('TerminateJobObject', wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _TerminateProcess = _signature('TerminateProcess', wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _CloseHandle = _signature('CloseHandle', wintypes.BOOL, wintypes.HANDLE)
    _CreateToolhelp32Snapshot = _signature('CreateToolhelp32Snapshot', wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD)
    _Thread32First = _signature('Thread32First', wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    _Thread32Next = _signature('Thread32Next', wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_ThreadEntry))
    _OpenThread = _signature('OpenThread', wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _ResumeThread = _signature('ResumeThread', wintypes.DWORD, wintypes.HANDLE)
    _GetProcessTimes = _signature('GetProcessTimes', wintypes.BOOL, wintypes.HANDLE,
                                  _FILETIME, _FILETIME, _FILETIME, _FILETIME)
    _GetCurrentProcess = _signature('GetCurrentProcess', wintypes.HANDLE)
    _ProcessIdToSessionId = _signature('ProcessIdToSessionId', wintypes.BOOL, wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD))
    _GetTickCount64 = _signature('GetTickCount64', ctypes.c_ulonglong)
    _LocalFree = _signature('LocalFree', ctypes.c_void_p, ctypes.c_void_p)
    _OpenJobObjectW = _signature('OpenJobObjectW', wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _OpenProcess = _signature('OpenProcess', wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _WaitForSingleObject = _signature('WaitForSingleObject', wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _OpenProcessToken = _signature('OpenProcessToken', wintypes.BOOL, wintypes.HANDLE, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.HANDLE), library=_advapi32)
    _GetTokenInformation = _signature('GetTokenInformation', wintypes.BOOL, wintypes.HANDLE, ctypes.c_int,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                      library=_advapi32)
    _ConvertSidToStringSidW = _signature('ConvertSidToStringSidW', wintypes.BOOL, ctypes.c_void_p,
                                         ctypes.POINTER(ctypes.c_void_p), library=_advapi32)


def _error(what, error):
    return SupervisionError(f'Process supervision unavailable: {what} failed ({ctypes.FormatError(error).strip()})')


def _fail(what):
    raise _error(what, ctypes.get_last_error())


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def new_job_name():
    """128 random bits: a name another process cannot predict or pre-create."""
    return JOB_NAME_PREFIX + secrets.token_hex(16)


class Job:
    """One Job Object owned by this backend process; its handle is never inherited.

    A named Job must be new: if the name already exists, the returned handle to
    that other object is closed unused and creation fails.
    """

    def __init__(self, name=None):
        if name is not None and not _JOB_NAME.fullmatch(name):
            raise SupervisionError('Process supervision unavailable: invalid Job Object name')
        ctypes.set_last_error(0)
        handle = _CreateJobObjectW(None, name)
        error = ctypes.get_last_error()
        if not handle:
            raise _error('CreateJobObject', error)
        if name is not None and error == ERROR_ALREADY_EXISTS:
            _CloseHandle(handle)  # Never configure, assign to or terminate another owner's Job.
            raise SupervisionError('Process supervision unavailable: Job Object name collision')
        self.handle, self.name = handle, name
        self.ended = None  # Verified-termination evidence; survives close().
        self._lock = threading.Lock()
        limits = _ExtendedLimits()
        # Kill on close and no BREAKAWAY_OK/SILENT_BREAKAWAY_OK: descendants cannot leave.
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise _error('SetInformationJobObject', error)
        try:
            self.config = self._read_config()
        except BaseException:
            self.close()
            raise

    @classmethod
    def open_existing(cls, name, terminate=True):
        """Reopen one of our named Jobs; None when no object has that name in this session.

        Inspection and any termination use this single handle, so the object
        that was verified is the object that is terminated (no substitution
        between check and use). Nothing is configured or assigned.
        """
        if not _JOB_NAME.fullmatch(name or ''):
            raise SupervisionError('Process supervision: invalid Job Object name')
        access = JOB_OBJECT_QUERY | (JOB_OBJECT_TERMINATE if terminate else 0)  # diagnostics: query only
        handle = _OpenJobObjectW(access, False, name)
        if not handle:
            error = ctypes.get_last_error()
            if error == ERROR_FILE_NOT_FOUND:
                return None
            raise _error('OpenJobObject', error)
        job = cls.__new__(cls)
        job.handle, job.name, job.ended, job._lock = handle, name, None, threading.Lock()
        try:
            job.config = job.limits()
        except BaseException:
            job.close()
            raise
        return job

    def limits(self):
        """The limits as Windows reports them, not as requested."""
        limits = _ExtendedLimits()
        if not _QueryInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                          ctypes.byref(limits), ctypes.sizeof(limits), None):
            _fail('QueryInformationJobObject')
        flags = limits.BasicLimitInformation.LimitFlags
        return {'limit_flags': flags,
                'kill_on_job_close': bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
                'breakaway_ok': bool(flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK),
                'silent_breakaway_ok': bool(flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK)}

    def _read_config(self):
        config = self.limits()
        if not config['kill_on_job_close'] or config['breakaway_ok'] or config['silent_breakaway_ok']:
            raise SupervisionError('Process supervision unavailable: Job Object limits could not be verified')
        return config

    def members(self):
        """PIDs currently in the Job (JobObjectBasicProcessIdList)."""
        capacity = 16
        while True:
            size = 8 + ctypes.sizeof(ctypes.c_size_t) * capacity
            buffer = ctypes.create_string_buffer(size)
            with self._lock:
                if self.handle is None:
                    raise SupervisionError('Process supervision: Job handle is closed')
                ok = _QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_PROCESS_ID_LIST, buffer, size, None)
                error = ctypes.get_last_error()
            assigned, listed = (wintypes.DWORD.from_buffer(buffer, offset).value for offset in (0, 4))
            if not ok and error != ERROR_MORE_DATA:
                raise _error('QueryInformationJobObject', error)
            if ok and listed >= assigned:
                ids = (ctypes.c_size_t * listed).from_buffer(buffer, 8)
                return [int(pid) for pid in ids]
            if capacity > 65536:
                raise SupervisionError('Process supervision: Job member list could not be read')
            capacity = max(capacity * 2, assigned + 8)

    def assign(self, process_handle):
        if not _AssignProcessToJobObject(self.handle, process_handle):
            _fail('AssignProcessToJobObject')

    def contains(self, process_handle):
        if self.handle is None:
            # IsProcessInJob(process, NULL) would test membership in any Job.
            raise SupervisionError('Process supervision: Job handle is closed')
        result = wintypes.BOOL()
        if not _IsProcessInJob(process_handle, self.handle, ctypes.byref(result)):
            _fail('IsProcessInJob')
        return bool(result.value)

    def accounting(self):
        info = _Accounting()
        with self._lock:
            if self.handle is None:
                # A closed handle is not evidence that no process remains.
                raise SupervisionError('Process supervision: Job handle is closed')
            if not _QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
                _fail('QueryInformationJobObject')
        return {'active_processes': info.ActiveProcesses, 'total_processes': info.TotalProcesses,
                'terminated_processes': info.TotalTerminatedProcesses}

    def active_processes(self):
        return self.accounting()['active_processes']

    def terminate(self, timeout=5.0):
        """Terminate every process in the Job, confirm none remain and return that evidence."""
        with self._lock:
            if self.handle is None:
                if self.ended is not None:
                    return self.ended  # Verified before the handle was closed.
                raise SupervisionError('Process supervision could not confirm termination: Job handle is closed')
            if not _TerminateJobObject(self.handle, STOP_EXIT_CODE):
                _fail('TerminateJobObject')
        deadline = time.monotonic() + timeout
        while True:
            try:
                info = self.accounting()
            except SupervisionError:
                if self.ended is not None:
                    return self.ended  # A concurrent caller verified, then closed.
                raise
            if not info['active_processes']:
                break
            if time.monotonic() >= deadline:
                raise SupervisionError('Process supervision could not confirm that the execution tree ended')
            time.sleep(0.02)
        self.ended = {**info, 'verified_at': _now()}
        return self.ended

    def release(self):
        """Clear kill on close, then close: the processes still in the Job keep running.

        Only for programs an interactive command started for the user and that outlive it
        (a provider sign-in opening a browser). Raises if the limit cannot be cleared; the
        caller then still owns the handle (and closing it ends those processes).
        """
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0
        with self._lock:
            if self.handle is None:
                raise SupervisionError('Process supervision: Job handle is closed')
            if not _SetInformationJobObject(self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
                _fail('SetInformationJobObject')
        self.close()

    def close(self):
        """Close the handle; KILL_ON_JOB_CLOSE ends any remaining descendants."""
        with self._lock:
            handle, self.handle = self.handle, None
        if handle is not None:
            _CloseHandle(handle)


def process_created_at(handle):
    """Creation time (FILETIME, 100 ns since 1601 UTC) as a decimal string."""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        _fail('GetProcessTimes')
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def _resume_threads(pid, launch=None):
    snapshot = _CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        _fail('CreateToolhelp32Snapshot')
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = _Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = _OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    _fail('OpenThread')
                try:
                    previous = _ResumeThread(thread)
                    if previous == 0xFFFFFFFF:
                        _fail('ResumeThread')
                    if launch is not None:
                        launch.resumed = True  # From here on worker code may have run.
                    if previous == 0:
                        raise SupervisionError('Process supervision unavailable: the process was not suspended')
                    resumed += 1
                finally:
                    _CloseHandle(thread)
            more = _Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    if not resumed:
        raise SupervisionError('Process supervision unavailable: no suspended thread to resume')


def _close_streams(process):
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _discard(process, job):
    """End a never-resumed root that was not (verifiably) contained; True once its exit is observed.

    It never ran, so it has no descendants: its own exit ends the whole tree.
    """
    _TerminateProcess(int(process._handle), STOP_EXIT_CODE)
    try:
        process.wait(timeout=5)
        return True
    except subprocess.TimeoutExpired:
        return False  # Still suspended, outside any verified Job.
    finally:
        job.close()
        _close_streams(process)


class PendingLaunch:
    """A root created suspended, assigned to its own Job and verified. Nothing has run.

    `evidence` describes the containment the controller observed. `resume` lets
    the worker run; `abort` ends it and reports whether worker code could have
    run. The caller persists `evidence` before calling `resume`.
    """

    def __init__(self, process, job, root_created_at, evidence):
        self.process, self.job = process, job
        self.root_created_at, self.evidence = root_created_at, evidence
        self.resumed = False
        self.execution_id = None
        self.committed = False  # Set by the caller once the journal row committed.
        self.guard = None  # The caller's in-memory ownership guard for this launch.
        self.journal = None  # What the caller journals it for (agent, task, provider).

    def resume(self):
        _resume_threads(self.process.pid, self)
        return {'resumed_at': _now()}

    def abort(self):
        """Terminate the execution; returns (journal state, evidence). Never raises OSError.

        Two separate facts are reported: whether worker code may have run
        (`resumed`) and whether the Job confirmed that no process remains. Only
        the second is evidence of termination; a root exit, a closed handle or
        a never-resumed root is not.
        """
        evidence, failures = {'aborted_at': _now(), 'worker_code_may_have_run': self.resumed}, []
        try:
            evidence['job'] = self.job.terminate()
        except OSError as exc:
            failures.append(f'job: {str(exc)[:300]}')
            if not _TerminateProcess(int(self.process._handle), STOP_EXIT_CODE):
                failures.append(f'root: TerminateProcess failed ({ctypes.FormatError(ctypes.get_last_error()).strip()})')
        try:
            self.process.wait(timeout=5)
            evidence['root_exited'] = True
        except subprocess.TimeoutExpired:
            evidence['root_exited'] = False
            failures.append('root: did not exit within 5 seconds')
        finally:
            self.job.close()  # Best effort (kill on close); never treated as evidence.
            _close_streams(self.process)
        verified = 'job' in evidence
        evidence['termination_verified'] = verified
        if failures:
            evidence['failures'] = failures
        if not verified:
            return 'uncertain', evidence
        return ('ended_verified' if self.resumed else 'launch_aborted'), evidence


def spawn(command, *, job_name=None, creationflags=0, **options):
    """Create `command` suspended inside its own new Job and verify containment."""
    job = Job(job_name)  # Before Popen: without a verified Job no process is created.
    flags = creationflags | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(command, creationflags=flags, **options)
    except BaseException:
        job.close()
        raise
    created_at = None
    try:
        handle = int(process._handle)
        created_at = process_created_at(handle)  # We hold the handle: this identity is certain.
        job.assign(handle)
        if not job.contains(handle):
            raise SupervisionError('Process supervision unavailable: Job assignment could not be verified')
        members = job.accounting()
        if members['active_processes'] != 1 or members['total_processes'] != 1:
            raise SupervisionError('Process supervision unavailable: Job membership is ambiguous')
    except BaseException as exc:
        if not _discard(process, job):
            error = TerminationUnverified('Launch refused; its suspended process could not be confirmed ended')
            error.evidence = {'root_pid': process.pid, 'root_created_at': created_at, 'worker_code_may_have_run': False,
                              'termination_verified': False}
            raise error from exc
        raise
    process.supervision_job = job
    return PendingLaunch(process, job, created_at, {
        'is_process_in_job': True, 'job_members': members, 'contained_at': _now()})


def popen(command, *, creationflags=0, job_name=None, **options):
    """Start `command` under a dedicated Job Object; returns the Popen with `.supervision_job`.

    Windows only. The process is created suspended, assigned, verified and only
    then resumed; on any failure it is terminated before running any code.
    """
    launch = spawn(command, job_name=job_name or new_job_name(), creationflags=creationflags, **options)
    try:
        launch.resume()
    except BaseException as exc:
        if launch.abort()[0] == 'uncertain':
            raise TerminationUnverified('Launch refused; its process could not be confirmed ended') from exc
        raise
    return launch.process


def platform_supported():
    """Whether trustworthy process-tree supervision exists on this platform.

    Windows only for now (Job Objects). This is the single seam for adding
    Linux or macOS supervision later; Task and Agent ownership do not change.
    """
    return os.name == 'nt'


UNSUPPORTED = ('Process supervision unavailable: trustworthy process-tree supervision is not implemented on this '
               'platform yet, so worker launches are refused')


def process_status(pid, created_at):
    """'alive', 'gone' or 'unknown' for the exact process (PID and creation time).

    A PID now used by another process means ours is gone. Anything that cannot
    be established (access denied, no creation time, non-Windows) is 'unknown'.
    """
    if pid is None or created_at is None:
        return 'unknown'
    if os.name != 'nt':
        return 'unknown'
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
    if not handle:
        return 'gone' if ctypes.get_last_error() == ERROR_INVALID_PARAMETER else 'unknown'
    try:
        if process_created_at(handle) != str(created_at):
            return 'gone'  # The PID was reused; ours no longer exists.
        return 'gone' if _WaitForSingleObject(handle, 0) == WAIT_OBJECT_0 else 'alive'
    except OSError:
        return 'unknown'
    finally:
        _CloseHandle(handle)


def created_at_of(pid):
    """Creation time of the process currently using `pid`, or None if it cannot be read."""
    if os.name != 'nt' or not pid:
        return None
    handle = _OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return process_created_at(handle)
    except OSError:
        return None
    finally:
        _CloseHandle(handle)


def session_of(pid):
    """Windows session of a process, or None if it cannot be read."""
    if os.name != 'nt' or not pid:
        return None
    session = wintypes.DWORD()
    return session.value if _ProcessIdToSessionId(int(pid), ctypes.byref(session)) else None


def terminate_exact(pid, created_at, timeout=5.0):
    """End one exact process (verified PID and creation time on the same handle); True once gone.

    Only for a never-resumed root outside any Job, whose identity the
    controller recorded itself. Never used on a name or a scan result.
    """
    if os.name != 'nt' or pid is None or created_at is None:
        return False
    handle = _OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
    if not handle:
        return ctypes.get_last_error() == ERROR_INVALID_PARAMETER  # No such process.
    try:
        if process_created_at(handle) != str(created_at):
            return True  # Ours is gone; this is someone else's process: untouched.
        if _WaitForSingleObject(handle, 0) != WAIT_OBJECT_0:
            _TerminateProcess(handle, STOP_EXIT_CODE)
        return _WaitForSingleObject(handle, int(timeout * 1000)) == WAIT_OBJECT_0
    except OSError:
        return False
    finally:
        _CloseHandle(handle)


def job_of(process):
    job = getattr(process, 'supervision_job', None)
    return job if isinstance(job, Job) else None


def _session_id():
    session = wintypes.DWORD()
    if not _ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        _fail('ProcessIdToSessionId')
    return session.value


def _user_sid():
    token = wintypes.HANDLE()
    if not _OpenProcessToken(_GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        _fail('OpenProcessToken')
    try:
        size = wintypes.DWORD()
        _GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            _fail('GetTokenInformation')
        buffer = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
            _fail('GetTokenInformation')
        sid, text = ctypes.c_void_p.from_buffer(buffer).value, ctypes.c_void_p()  # TOKEN_USER.User.Sid
        if not _ConvertSidToStringSidW(sid, ctypes.byref(text)):
            _fail('ConvertSidToStringSid')
        try:
            return ctypes.wstring_at(text.value)
        finally:
            _LocalFree(text)
    finally:
        _CloseHandle(token)


def _boot_id():
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, BOOT_ID_KEY) as key:
        value, kind = winreg.QueryValueEx(key, 'BootId')
    if kind != winreg.REG_DWORD:
        raise SupervisionError('Boot counter has an unexpected type')
    return int(value)


def controller_identity():
    """This backend process and its boot session. Unavailable values stay None, never guessed.

    Boot values are recorded evidence only; nothing in P1 decides on them.
    """
    observed = datetime.now(timezone.utc)
    identity = {'pid': os.getpid(), 'created_at': None, 'session_id': None, 'user_sid': None,
                'boot_id': None, 'os_boot_time': None, 'uptime_ms': None,
                'observed_at': observed.isoformat(timespec='milliseconds').replace('+00:00', 'Z'), 'errors': []}
    if os.name != 'nt':
        identity['errors'].append('not_windows')
        return identity
    probes = (('created_at', lambda: process_created_at(_GetCurrentProcess())), ('session_id', _session_id),
              ('user_sid', _user_sid), ('boot_id', _boot_id), ('uptime_ms', lambda: int(_GetTickCount64())))
    for key, probe in probes:
        try:
            identity[key] = probe()
        except OSError as exc:
            identity['errors'].append(f'{key}: {str(exc)[:200]}')
    if identity['uptime_ms'] is not None:
        boot = observed - timedelta(milliseconds=identity['uptime_ms'])
        identity['os_boot_time'] = boot.isoformat(timespec='seconds').replace('+00:00', 'Z')
    return identity


def require_identity(identity):
    """Supervised executions are journaled only under a fully identified controller."""
    missing = [key for key in ('created_at', 'session_id', 'user_sid') if identity.get(key) is None]
    if missing:
        raise SupervisionError('Process supervision unavailable: controller identity is incomplete ('
                               + ', '.join(missing) + ')')
    return identity


def self_test():
    """Harmless startup probe: a supervised, immediately exiting interpreter."""
    global _status
    if os.name != 'nt':
        return status()
    try:
        require_identity(controller_identity())
        process = popen([sys.executable, '-I', '-S', '-c', 'pass'], stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            if process.wait(timeout=10) != 0:
                raise SupervisionError('Process supervision self-test process failed')
        finally:
            process.supervision_job.terminate()
            process.supervision_job.close()
        result = {'status': 'supervised'}
    except (OSError, subprocess.SubprocessError) as exc:
        result = {'status': 'unavailable', 'reason': str(exc)[:300]}
    with _status_lock:
        _status = result
    return status()


def status():
    with _status_lock:
        return dict(_status)
