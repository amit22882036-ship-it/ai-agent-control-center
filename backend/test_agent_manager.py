import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app import agent_manager as manager
from app.main import StartAgentRequest, start_mock_agent, stop_mock_agent


class AgentTests(unittest.TestCase):
    def tearDown(self):
        for pid, process in list(manager.agents.items()):
            if process.poll() is None:
                manager.stop_agent(pid)
            deadline = time.monotonic() + 5
            while not process.stdout.closed:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        for registry in (manager.agents, manager.agent_outputs, manager.agent_tasks,
                         manager.agent_statuses, manager.agent_types):
            registry.clear()

    def wait_for_output(self, pid):
        deadline = time.monotonic() + 5
        while not manager.agent_outputs[pid]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_mock_default_and_stop(self):
        request = StartAgentRequest(task="Review code")
        result = start_mock_agent(request)
        pid = result["agent_id"]
        self.assertEqual(result["agent_type"], "mock")
        self.wait_for_output(pid)
        self.assertEqual(manager.get_agent(pid)["output"][0], "Agent started")
        self.assertEqual(manager.get_agents()[0]["task"], request.task)
        self.assertEqual(stop_mock_agent(pid)["status"], "stopped")
        self.assertEqual(manager.get_agent(pid)["status"], "stopped")
        self.assertEqual(stop_mock_agent(pid)["status"], "stopped")
        with self.assertRaises(ValidationError):
            StartAgentRequest(task="Review", agent_type="unknown")

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_missing_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"APPDATA": directory}):
                with self.assertRaises(HTTPException) as error:
                    start_mock_agent(StartAgentRequest(task="Review", agent_type="codex"))
                self.assertEqual(error.exception.status_code, 503)
                self.assertIn("npm", error.exception.detail)

    def make_launcher(self, directory, script):
        npm = Path(directory) / "npm"
        npm.mkdir()
        (npm / "echo_args.py").write_text(script, encoding="utf-8")
        # Match the npm shim's second argument parsing pass, without running AI.
        (npm / "codex.cmd").write_text(
            '@echo off\nsetlocal\n'
            f'set "_prog={sys.executable}"\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & '
            '"%_prog%" "%~dp0echo_args.py" %*\n', encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows launcher integration")
    def test_stdin_prompt_and_output(self):
        tasks = [
            "Review the authentication module",
            'quotes " & echo INJECTION & " | < > ^ %PATH% !PATH! (test)',
            'trailing slash \\',
            'backslash quote \\" and Unicode שלום',
            '--dangerously-bypass-approvals-and-sandbox',
            'Review this module.\nThen explain the findings.\n',
        ]
        with tempfile.TemporaryDirectory(prefix="codex shim ") as directory:
            self.make_launcher(directory,
                'import json, sys\nsys.stdin.reconfigure(encoding="utf-8")\n'
                'print(json.dumps(sys.argv[1:]), flush=True)\n'
                'print(json.dumps(sys.stdin.read()), flush=True)\n'
                'print("stderr captured", file=sys.stderr, flush=True)\n')
            with patch.dict(os.environ, {"APPDATA": directory}):
                for task in tasks:
                    pid = manager.start_agent(task, "codex")
                    self.assertTrue(manager.agents[pid].stdin.closed)
                    manager.agents[pid].wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while not manager.agents[pid].stdout.closed:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    detail = manager.get_agent(pid)
                    self.assertEqual(detail["status"], "finished")
                    self.assertEqual(detail["agent_type"], "codex")
                    self.assertEqual(detail["task"], task)
                    self.assertEqual(json.loads(detail["output"][0]), [
                        "exec", "--sandbox", "read-only", "--color", "never",
                        "--skip-git-repo-check", "-"])
                    self.assertEqual(json.loads(detail["output"][1]), task)
                    self.assertEqual(detail["output"][2:], ["stderr captured"])

    @unittest.skipUnless(os.name == "nt", "Windows process tree integration")
    def test_tree_stop(self):
        with tempfile.TemporaryDirectory(prefix="codex tree ") as directory:
            self.make_launcher(directory,
                'import subprocess, sys, time\n'
                'sys.stdin.read()\n'
                'child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                'print(child.pid, flush=True)\n'
                'time.sleep(60)\n')
            with patch.dict(os.environ, {"APPDATA": directory}):
                pid = manager.start_agent("Review", "codex")
                self.wait_for_output(pid)
                child_pid = int(manager.agent_outputs[pid][0])
                self.assertEqual(manager.stop_agent(pid)["status"], "stopped")
                # tasklist is read-only and confirms the grandchild is gone.
                result = subprocess.run([
                    str(Path(os.environ["SystemRoot"]) / "System32" / "tasklist.exe"),
                    "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH",
                ], capture_output=True, text=True, check=True)
                self.assertNotIn(f'"{child_pid}"', result.stdout)
                self.assertEqual(manager.get_agent(pid)["status"], "stopped")

    def test_tree_failure_does_not_claim_stopped(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.dict(manager.agents, {12345: process}), \
             patch.dict(manager.agent_types, {12345: "codex"}), \
             patch.dict(manager.agent_statuses, {12345: "running"}), \
             patch.object(manager, "_stop_windows_tree", side_effect=RuntimeError("failed")):
            if os.name == "nt":
                with self.assertRaises(HTTPException):
                    stop_mock_agent(12345)
                self.assertEqual(manager.agent_statuses[12345], "running")

    @unittest.skipUnless(os.name == "nt", "Windows process tree fallback")
    def test_force_tree_fallback(self):
        process = Mock(pid=12345)
        with patch.object(manager.subprocess, "run", side_effect=[
            subprocess.TimeoutExpired("taskkill", 5), Mock(returncode=0),
        ]) as run:
            manager._stop_windows_tree(process)
        self.assertNotIn("/F", run.call_args_list[0].args[0])
        self.assertIn("/F", run.call_args_list[1].args[0])
        self.assertIn("/T", run.call_args_list[1].args[0])
        process.wait.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
