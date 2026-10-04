import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app import agent_manager as manager
from app.main import app, StartAgentRequest, start_mock_agent, stop_mock_agent


class AgentTests(unittest.TestCase):
    from workspace_test_support import process_test_setup as setUp
    def test_same_pid_keeps_separate_agents(self):
        first = Mock(pid=12345, stdout=io.StringIO("First output\n"))
        second = Mock(pid=12345, stdout=io.StringIO("Second output\n"))
        first.poll.return_value = 0
        second.poll.return_value = None
        with patch.object(manager.subprocess, "Popen", side_effect=[first, second]), \
             patch.object(manager, "Thread"):
            first_id = manager.start_agent("First task")
            second_id = manager.start_agent("Second task")
        self.assertNotEqual(first_id, second_id)
        for agent_id in (first_id, second_id):
            self.assertEqual(str(UUID(agent_id)), agent_id)
            self.assertNotEqual(agent_id, str(first.pid))
        self.assertIs(manager.agents[first_id], first)
        self.assertIs(manager.agents[second_id], second)
        # Read after both launches: an old reader must still target its own ID.
        manager._read_output(first_id, first)
        manager._read_output(second_id, second)
        self.assertEqual(manager.get_agent(first_id)["output"], ["First output"])
        self.assertEqual(manager.get_agent(second_id)["output"], ["Second output"])
        self.assertEqual(manager.get_agent(first_id)["task"], "First task")
        self.assertEqual(manager.get_agent(second_id)["task"], "Second task")
        self.assertEqual(len(manager.get_agents()), 2)
        self.assertEqual(manager.stop_agent(second_id)["status"], "stopped")
        second.terminate.assert_called_once()
        first.terminate.assert_not_called()
        self.assertEqual(manager.get_agent(first_id)["status"], "finished")
        second.poll.return_value = 0

    def test_uuid_api_routes(self):
        async def request(method, path, body=None, status=200):
            messages = []
            async def receive():
                return {"type": "http.request", "body": json.dumps(body).encode() if body else b"",
                        "more_body": False}
            async def send(message):
                messages.append(message)
            await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                       "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
                       "query_string": b"", "headers": [(b"content-type", b"application/json")],
                       "client": ("127.0.0.1", 1234), "server": ("localhost", 8000), "root_path": ""},
                      receive, send)
            self.assertEqual(messages[0]["status"], status)
            return json.loads(b"".join(message.get("body", b"") for message in messages))

        async def check():
            started = await request("POST", "/agents/start", {"task": "Review code"})
            agent_id = started["agent_id"]
            self.assertIsNone(started['parent_id'])
            self.assertEqual(str(UUID(agent_id)), agent_id)
            self.assertNotEqual(agent_id, str(manager.agents[agent_id].pid))
            self.assertEqual((await request("GET", "/agents"))["agents"][0]["agent_id"], agent_id)
            self.assertEqual((await request("GET", f"/agents/{agent_id}"))["agent_id"], agent_id)
            self.assertIsNone((await request("GET", f"/agents/{agent_id}"))["session_id"])
            await request("POST", f"/agents/{agent_id}/redirect", {"instruction": "Correct this"}, 409)
            await request("POST", f"/agents/{agent_id}/redirect", {"instruction": " \n "}, 422)
            await request("POST", f"/agents/{agent_id}/reply", {"answer": "Continue"}, 409)
            await request("POST", f"/agents/{agent_id}/decide", status=409)
            await request("POST", f"/agents/{agent_id}/reply", {"answer": " \n "}, 422)
            self.assertEqual(await request("POST", f"/agents/{agent_id}/stop"),
                             {"agent_id": agent_id, "status": "stopped"})
            self.assertEqual((await request("GET", f"/agents/{agent_id}"))["status"], "stopped")
            child = await request('POST', f'/agents/{agent_id}/children/start', {'task': 'Child task'})
            child_id = child['agent_id']
            self.assertEqual(child['parent_id'], agent_id)
            self.assertNotEqual(child_id, agent_id)
            self.assertEqual((await request('GET', f'/agents/{child_id}'))['parent_id'], agent_id)
            self.assertEqual((await request('GET', f'/agents/{agent_id}'))['child_ids'], [child_id])
            await request('POST', '/agents/missing/children/start', {'task': 'Child task'}, 404)
            missing = str(uuid4())
            await request("GET", f"/agents/{missing}", status=404)
            await request("POST", f"/agents/{missing}/stop", status=404)
            await request("POST", f"/agents/{missing}/redirect", {"instruction": "Correct this"}, 404)
            await request("POST", f"/agents/{missing}/reply", {"answer": "Continue"}, 404)
            await request("POST", f"/agents/{missing}/decide", status=404)
        asyncio.run(check())

    def tearDown(self):
        for agent_id, process in list(manager.agents.items()):
            if process.poll() is None:
                manager.stop_agent(agent_id)
            deadline = time.monotonic() + 5
            while not process.stdout.closed:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        for registry in (manager.agents, manager.agent_outputs, manager.agent_tasks,
                         manager.agent_statuses, manager.agent_types, manager.agent_sandboxes,
                         manager.agent_sessions, manager.agent_readers, manager.agent_waiting_questions,
                         manager.agent_similar_decisions, manager.agent_always_decisions, manager.agent_parents):
            registry.clear()

    def wait_for_output(self, agent_id):
        deadline = time.monotonic() + 5
        while not manager.agent_outputs[agent_id]:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def test_mock_default_and_stop(self):
        request = StartAgentRequest(task="Review code")
        result = start_mock_agent(request)
        agent_id = result["agent_id"]
        self.assertEqual(result["agent_type"], "mock")
        self.assertIsNone(result["sandbox"])
        self.wait_for_output(agent_id)
        self.assertEqual(manager.get_agent(agent_id)["output"][0], "Agent started")
        self.assertEqual(manager.get_agents()[0]["task"], request.task)
        self.assertEqual(stop_mock_agent(agent_id)["status"], "stopped")
        self.assertEqual(manager.get_agent(agent_id)["status"], "stopped")
        self.assertEqual(stop_mock_agent(agent_id)["status"], "stopped")
        with self.assertRaises(ValidationError):
            StartAgentRequest(task="Review", agent_type="unknown")

    def test_sandbox_validation_and_api_forwarding(self):
        self.assertEqual(StartAgentRequest(task="Review").sandbox, "read-only")
        for sandbox in ("read-only", "workspace-write"):
            with patch("app.main.start_agent", return_value=str(uuid4())) as start:
                result = start_mock_agent(StartAgentRequest(
                    task="Review", agent_type="codex", sandbox=sandbox))
                start.assert_called_once_with("Review", "codex", sandbox)
                self.assertEqual(result["sandbox"], sandbox)
        for invalid in ("danger-full-access", "read-only & echo injected", "", None):
            with self.assertRaises(ValidationError):
                StartAgentRequest(task="Review", agent_type="codex", sandbox=invalid)
            with patch.object(manager.subprocess, "Popen") as popen:
                with self.assertRaises(ValueError):
                    manager.start_agent("Review", "codex", invalid)
                popen.assert_not_called()

    def test_mock_ignores_sandbox(self):
        result = start_mock_agent(StartAgentRequest(task="Review", sandbox="workspace-write"))
        agent_id = result["agent_id"]
        self.wait_for_output(agent_id)
        self.assertIsNone(result["sandbox"])
        self.assertIsNone(manager.get_agent(agent_id)["sandbox"])
        self.assertIsNone(manager.get_agents()[0]["sandbox"])
        self.assertEqual(manager.get_agent(agent_id)["output"][0], "Agent started")

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
                for task, sandbox in [(task, mode) for task in tasks
                                      for mode in ("read-only", "workspace-write")]:
                    agent_id = manager.start_agent(task, "codex", sandbox)
                    self.assertTrue(manager.agents[agent_id].stdin.closed)
                    manager.agents[agent_id].wait(timeout=5)
                    deadline = time.monotonic() + 5
                    while not manager.agents[agent_id].stdout.closed:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    detail = manager.get_agent(agent_id)
                    self.assertEqual(detail["status"], "finished")
                    self.assertEqual(detail["agent_type"], "codex")
                    self.assertEqual(detail["task"], task)
                    self.assertEqual(detail["sandbox"], sandbox)
                    listed = next(item for item in manager.get_agents() if item["agent_id"] == agent_id)
                    self.assertEqual(listed["sandbox"], sandbox)
                    self.assertEqual(json.loads(detail["output"][0]), [
                        "exec", "--sandbox", sandbox, "--color", "never",
                        "--skip-git-repo-check", "-"])
                    self.assertEqual(json.loads(detail["output"][1]), manager._codex_prompt(task))
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
                agent_id = manager.start_agent("Review", "codex")
                self.assertEqual(manager.get_agent(agent_id)["sandbox"], "read-only")
                self.wait_for_output(agent_id)
                child_pid = int(manager.agent_outputs[agent_id][0])
                self.assertEqual(manager.stop_agent(agent_id)["status"], "stopped")
                # tasklist is read-only and confirms the grandchild is gone.
                result = subprocess.run([
                    str(Path(os.environ["SystemRoot"]) / "System32" / "tasklist.exe"),
                    "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH",
                ], capture_output=True, text=True, check=True)
                self.assertNotIn(f'"{child_pid}"', result.stdout)
                self.assertEqual(manager.get_agent(agent_id)["status"], "stopped")

    def test_tree_failure_does_not_claim_stopped(self):
        agent_id = str(uuid4())
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.dict(manager.agents, {agent_id: process}), \
             patch.dict(manager.agent_types, {agent_id: "codex"}), \
             patch.dict(manager.agent_statuses, {agent_id: "running"}), \
             patch.object(manager, "_stop_windows_tree", side_effect=RuntimeError("failed")):
            if os.name == "nt":
                with self.assertRaises(HTTPException):
                    stop_mock_agent(agent_id)
                self.assertEqual(manager.agent_statuses[agent_id], "running")

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
        self.assertIn("12345", run.call_args_list[1].args[0])
        process.wait.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()
