"""Windows Job Object supervision for agent executions (one Job per execution).

Each worker is created suspended, assigned to its own Job Object configured with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (no breakaway), verified with IsProcessInJob,
and only then resumed. Descendants inherit the Job, so terminating or closing it
ends the whole execution tree, including orphaned descendants. If the backend
dies, Windows closes the handle and kills every supervised tree. Any failure is
fail-closed: the never-resumed process is terminated and the launch refused.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

MARKER = '--- Process supervision: active (job object) ---'
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
STOP_EXIT_CODE = 1

_status_lock = threading.Lock()
_status = ({'status': 'not_applicable'} if os.name != 'nt'
           else {'status': 'unavailable', 'reason': 'startup self-test has not run'})


class SupervisionError(OSError):
    """Supervision could not be established or confirmed; nothing runs unsupervised."""


if os.name == 'nt':
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

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

    def _signature(name, restype, *argtypes):
        function = getattr(_kernel32, name)
        function.restype, function.argtypes = restype, argtypes
        return function

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


def _fail(what):
    error = ctypes.get_last_error()
    raise SupervisionError(f'Process supervision unavailable: {what} failed ({ctypes.FormatError(error).strip()})')


class Job:
    """One Job Object owned by this backend process; its handle is never inherited."""

    def __init__(self):
        handle = _CreateJobObjectW(None, None)
        if not handle:
            _fail('CreateJobObject')
        self.handle = handle
        self._lock = threading.Lock()
        limits = _ExtendedLimits()
        # Kill on close and no BREAKAWAY_OK/SILENT_BREAKAWAY_OK: descendants cannot leave.
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _SetInformationJobObject(handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                        ctypes.byref(limits), ctypes.sizeof(limits)):
            self.close()
            _fail('SetInformationJobObject')

    def assign(self, process_handle):
        if not _AssignProcessToJobObject(self.handle, process_handle):
            _fail('AssignProcessToJobObject')

    def contains(self, process_handle):
        result = wintypes.BOOL()
        if not _IsProcessInJob(process_handle, self.handle, ctypes.byref(result)):
            _fail('IsProcessInJob')
        return bool(result.value)

    def active_processes(self):
        info = _Accounting()
        with self._lock:
            if self.handle is None:
                return 0
            if not _QueryInformationJobObject(self.handle, JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
                                              ctypes.byref(info), ctypes.sizeof(info), None):
                _fail('QueryInformationJobObject')
        return info.ActiveProcesses

    def terminate(self, timeout=5.0):
        """Terminate every process in the Job and confirm none remain."""
        with self._lock:
            if self.handle is not None and not _TerminateJobObject(self.handle, STOP_EXIT_CODE):
                _fail('TerminateJobObject')
        deadline = time.monotonic() + timeout
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise SupervisionError('Process supervision could not confirm that the execution tree ended')
            time.sleep(0.02)

    def close(self):
        """Close the handle; KILL_ON_JOB_CLOSE ends any remaining descendants."""
        with self._lock:
            handle, self.handle = self.handle, None
        if handle is not None:
            _CloseHandle(handle)


def _resume_threads(pid):
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
                    if _ResumeThread(thread) == 0xFFFFFFFF:
                        _fail('ResumeThread')
                    resumed += 1
                finally:
                    _CloseHandle(thread)
            more = _Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    if not resumed:
        raise SupervisionError('Process supervision unavailable: no suspended thread to resume')


def popen(command, *, creationflags=0, **options):
    """Start `command` under a dedicated Job Object; returns the Popen with `.supervision_job`.

    Windows only. The process is created suspended, assigned, verified and only
    then resumed; on any failure it is terminated before running any code.
    """
    flags = creationflags | CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, creationflags=flags, **options)
    job = None
    try:
        job = Job()
        job.assign(int(process._handle))
        if not job.contains(int(process._handle)):
            raise SupervisionError('Process supervision unavailable: Job assignment could not be verified')
        _resume_threads(process.pid)
    except BaseException:
        _TerminateProcess(int(process._handle), STOP_EXIT_CODE)
        try:
            process.wait(timeout=5)
        finally:
            if job is not None:
                job.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        raise
    process.supervision_job = job
    return process


def job_of(process):
    job = getattr(process, 'supervision_job', None)
    return job if isinstance(job, Job) else None


def self_test():
    """Harmless startup probe: a supervised, immediately exiting interpreter."""
    global _status
    if os.name != 'nt':
        return status()
    try:
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
