"""PR-1: Windows Job Object supervision, verified with harmless local processes only."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, process_supervision as ps
from app.main import process_supervision_status
import test_agent_manager
from workspace_test_support import worker_output

PYTHON = getattr(sys, '_base_executable', sys.executable)  # real interpreter, no venv launcher job
BACKEND = str(Path(__file__).resolve().parent)
SYNCHRONIZE, PROCESS_TERMINATE = 0x00100000, 0x0001
DETACHED_PROCESS, CREATE_NEW_CONSOLE = 0x00000008, 0x00000010


def alive(pid):
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def reap(*pids):
    """Test hygiene: end any test-created process left by a failing assertion."""
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    for pid in pids:
        handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if handle:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)


def until(condition, timeout=8.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


TREE = textwrap.dedent('''
    import subprocess, sys, time
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x00000008)
    print(grandchild.pid, flush=True)
    try:
        escaped = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x01000000)
        print("breakaway", escaped.pid, flush=True)
    except OSError as error:
        print("refused", error.winerror, flush=True)
    time.sleep(60)
''')


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class JobObjectTests(unittest.TestCase):
    def start_tree(self):
        process = ps.popen([PYTHON, '-c', TREE], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (process.supervision_job.close(), process.stdout.close()))
        grandchild = int(process.stdout.readline())
        breakaway = process.stdout.readline().split()
        self.addCleanup(reap, process.pid, grandchild, *breakaway[1:2])
        return process, grandchild, breakaway

    def test_whole_tree_is_contained_and_terminated(self):
        process, grandchild, breakaway = self.start_tree()
        job = process.supervision_job
        self.assertTrue(job.contains(int(process._handle)))
        self.assertEqual(breakaway[0], 'refused')  # No BREAKAWAY_OK on the Job.
        self.assertEqual(int(breakaway[1]), 5)  # ERROR_ACCESS_DENIED
        self.assertGreaterEqual(job.active_processes(), 2)
        job.terminate()
        self.assertEqual(job.active_processes(), 0)
        self.assertEqual(process.wait(5), ps.STOP_EXIT_CODE)
        self.assertFalse(alive(grandchild))  # Detached grandchild: taskkill /T can miss it; the Job cannot.

    def test_closing_the_job_ends_remaining_descendants(self):
        process, grandchild, _ = self.start_tree()
        process.supervision_job.close()  # KILL_ON_JOB_CLOSE
        self.assertTrue(until(lambda: not alive(grandchild) and process.poll() is not None))

    def test_backend_crash_kills_every_supervised_tree(self):
        helper = textwrap.dedent(f'''
            import subprocess, sys, time
            sys.path.insert(0, {BACKEND!r})
            from app import process_supervision as ps
            child = ps.popen([sys.executable, "-c", {TREE!r}], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)
            print(child.pid, child.stdout.readline().strip(), flush=True)
            time.sleep(60)
        ''')
        backend = subprocess.Popen([PYTHON, '-I', '-B', '-c', helper], stdout=subprocess.PIPE, text=True)
        self.addCleanup(reap, backend.pid)
        child, grandchild = map(int, backend.stdout.readline().split())
        self.addCleanup(reap, child, grandchild)
        self.assertTrue(alive(child) and alive(grandchild))
        backend.kill()  # TerminateProcess: no cleanup code runs in the "backend"
        backend.wait(5)
        backend.stdout.close()
        self.assertTrue(until(lambda: not alive(child) and not alive(grandchild)))

    def run_failed_launch(self, **failure):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran.txt'
            script = f'open({str(marker)!r}, "w").write("ran")'
            created = []
            real_popen = subprocess.Popen

            def record(*args, **kwargs):
                created.append(real_popen(*args, **kwargs))
                return created[-1]
            with patch.object(ps.subprocess, 'Popen', side_effect=record), patch.multiple(ps, **failure):
                with self.assertRaises(ps.SupervisionError):
                    ps.popen([PYTHON, '-c', script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            self.assertEqual(len(created), 1)
            self.assertIsNotNone(created[0].poll())  # Terminated while still suspended.
            time.sleep(0.3)
            self.assertFalse(marker.exists())  # Not one line of the child ran.

    def test_assignment_failure_never_runs_the_process(self):
        self.run_failed_launch(_AssignProcessToJobObject=lambda *_: 0)

    def test_verification_failure_never_runs_the_process(self):
        self.run_failed_launch(_IsProcessInJob=lambda handle, job, result: (setattr(result._obj, 'value', 0) or 1))

    def test_resume_failure_never_runs_the_process(self):
        self.run_failed_launch(_ResumeThread=lambda *_: 0xFFFFFFFF)

    def test_job_creation_failure_never_runs_the_process(self):
        self.run_failed_launch(_CreateJobObjectW=lambda *_: None)

    def test_ctrl_c_in_backend_console_does_not_reach_supervised_workers(self):
        helper = textwrap.dedent(f'''
            import ctypes, json, subprocess, sys, time
            sys.path.insert(0, {BACKEND!r})
            from app import process_supervision as ps
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.SetConsoleCtrlHandler(None, False)  # undo any inherited "ignore Ctrl+C"
            worker = "import time\\ntry:\\n    time.sleep(30)\\nexcept KeyboardInterrupt:\\n    raise SystemExit(130)"
            supervised = ps.popen([sys.executable, "-c", worker])
            control = subprocess.Popen([sys.executable, "-c", worker])  # same console group
            time.sleep(1.0)
            # Only now (children inherit the flag): this "backend" ignores Ctrl+C itself.
            kernel32.SetConsoleCtrlHandler(None, True)
            kernel32.GenerateConsoleCtrlEvent(0, 0)  # CTRL_C_EVENT to the whole console
            time.sleep(1.5)
            result = dict(supervised=supervised.poll(), control=control.poll())
            supervised.supervision_job.terminate(); control.kill()
            open(sys.argv[1], "w").write(json.dumps(result))
        ''')
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0  # SW_HIDE: isolated console, never this test runner's
            backend = subprocess.Popen([PYTHON, '-I', '-B', '-c', helper, str(report)],
                                       creationflags=CREATE_NEW_CONSOLE, startupinfo=info)
            self.addCleanup(reap, backend.pid)
            backend.wait(30)
            result = json.loads(report.read_text())
        self.assertIsNotNone(result['control'])  # Ctrl+C really was delivered to the console.
        self.assertIsNone(result['supervised'])  # CREATE_NEW_PROCESS_GROUP isolated the worker.

    def test_supervision_status_self_test_and_read_only_endpoint(self):
        self.assertEqual(ps.self_test(), {'status': 'supervised'})
        with patch.object(ps, 'popen', side_effect=AssertionError('GET must not probe')):
            self.assertEqual(process_supervision_status(), {'status': 'supervised'})
        with patch.object(ps, '_CreateJobObjectW', lambda *_: None):
            result = ps.self_test()
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('CreateJobObject', result['reason'])
        self.assertEqual(process_supervision_status(), result)
        self.assertEqual(ps.self_test(), {'status': 'supervised'})


@unittest.skipUnless(os.name == 'nt', 'Windows Job Object supervision')
class SupervisedAgentTests(unittest.TestCase):
    """Agent lifecycle through the real launcher path with a fake codex.cmd (no AI)."""
    from workspace_test_support import process_test_setup as setUp
    tearDown = test_agent_manager.AgentTests.tearDown
    make_launcher = test_agent_manager.AgentTests.make_launcher

    def launcher(self, script):
        directory = tempfile.TemporaryDirectory(prefix='codex supervised ')
        self.addCleanup(directory.cleanup)
        self.make_launcher(directory.name, 'import sys\nsys.stdin.read()\n' + script)
        environment = patch.dict(os.environ, {'APPDATA': directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def lines(self, agent_id, count):
        self.assertTrue(until(lambda: len(worker_output(manager.agent_outputs[agent_id])) >= count))
        return worker_output(manager.agent_outputs[agent_id])

    def test_launch_is_supervised_and_marked(self):
        self.launcher('import time\nprint("work", flush=True)\ntime.sleep(60)\n')
        agent_id = manager.start_agent('Supervised', 'codex')
        process = manager.agents[agent_id]
        self.assertTrue(process.supervision_job.contains(int(process._handle)))
        self.assertEqual(manager.agent_outputs[agent_id][0], ps.MARKER)
        self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
        self.assertIsNone(process.supervision_job.handle)  # Closed after confirmed termination.

    def test_stop_ends_detached_descendants(self):
        self.launcher(textwrap.dedent('''
            import subprocess, sys, time
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x00000008)
            print(g.pid, flush=True)
            time.sleep(60)
        '''))
        agent_id = manager.start_agent('Tree', 'codex')
        grandchild = int(self.lines(agent_id, 1)[0])
        self.addCleanup(reap, grandchild)
        self.assertTrue(alive(grandchild))
        self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')
        self.assertFalse(alive(grandchild))

    def test_normal_completion_ends_remaining_descendants(self):
        self.launcher(textwrap.dedent('''
            import subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=0x00000008, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("codex", flush=True)
            print(g.pid, flush=True)
        '''))
        agent_id = manager.start_agent('Leaves a descendant', 'codex')
        grandchild = int(self.lines(agent_id, 2)[1])
        self.addCleanup(reap, grandchild)
        process = manager.agents[agent_id]
        process._control_center_watcher.join(10)
        self.assertEqual(manager.get_agent(agent_id)['status'], 'finished')
        self.assertTrue(until(lambda: not alive(grandchild)))
        self.assertIsNone(process.supervision_job.handle)

    def test_descendant_holding_output_pipe_cannot_keep_the_agent_running(self):
        # The descendant inherits stdout; before supervision the reader waited for it.
        self.launcher(textwrap.dedent('''
            import subprocess, sys
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x00000008)
            print(g.pid, flush=True)
        '''))
        agent_id = manager.start_agent('Pipe holder', 'codex')
        grandchild = int(self.lines(agent_id, 1)[0])
        self.addCleanup(reap, grandchild)
        manager.agents[agent_id]._control_center_watcher.join(10)
        self.assertEqual(manager.get_agent(agent_id)['status'], 'finished')
        self.assertTrue(until(lambda: not alive(grandchild)))

    def test_supervision_failure_refuses_the_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ran.txt'
            self.launcher(f'open({str(marker)!r}, "w").write("ran")\n')
            with patch.object(ps, '_AssignProcessToJobObject', lambda *_: 0):
                with self.assertRaises(OSError):
                    manager.start_agent('Refused', 'codex')
            self.assertEqual([k for k, p in manager.agents.items() if p is not None and p.poll() is None], [])
            time.sleep(0.3)
            self.assertFalse(marker.exists())

    def test_unconfirmed_termination_keeps_the_agent_running(self):
        self.launcher('import time\ntime.sleep(60)\n')
        agent_id = manager.start_agent('Stuck', 'codex')
        process = manager.agents[agent_id]
        with patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('not confirmed')):
            with self.assertRaisesRegex(RuntimeError, 'stop was not confirmed'):
                manager.stop_agent(agent_id)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')
        self.assertIsNone(process.poll())
        self.assertEqual(manager.stop_agent(agent_id)['status'], 'stopped')

    def test_redirect_uses_a_new_job_and_closes_the_old_one(self):
        session = str(uuid4())
        self.launcher(f'import time\nprint("session id: {session}", flush=True)\ntime.sleep(60)\n')
        agent_id = manager.start_agent('Redirected', 'codex')
        self.lines(agent_id, 1)
        old = manager.agents[agent_id]
        manager.redirect_agent(agent_id, 'Change course')
        new = manager.agents[agent_id]
        self.assertIsNot(new.supervision_job, old.supervision_job)
        self.assertIsNone(old.supervision_job.handle)
        self.assertTrue(new.supervision_job.contains(int(new._handle)))
        self.assertEqual(manager.agent_outputs[agent_id].count(ps.MARKER), 2)

    def test_shutdown_stops_supervised_work_without_automatic_continuation(self):
        session = str(uuid4())
        self.launcher(textwrap.dedent(f'''
            import time
            print("session id: {session}", flush=True)
            print("codex", flush=True)
            print("CONTROL_CENTER_WAITING: Which file next?", flush=True)
            time.sleep(60)
        '''))
        agent_id = manager.start_agent('Always enabled', 'codex')
        self.lines(agent_id, 3)
        manager.agent_always_decisions[agent_id].enabled = True
        process = manager.agents[agent_id]
        with patch.object(manager, '_shutting_down', False), \
             patch.object(manager, '_spawn_process', side_effect=AssertionError('no continuation')) as spawn:
            self.assertEqual(manager.shutdown_agents(), [])
            spawn.assert_not_called()
        self.assertEqual(manager.agent_statuses[agent_id], 'stopped')
        self.assertIsNotNone(process.poll())
        self.assertIsNone(process.supervision_job.handle)


if __name__ == '__main__':
    unittest.main()
