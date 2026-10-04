import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError

from app import agent_manager as manager
from app.main import RedirectAgentRequest, redirect_codex_agent
import test_agent_manager


class RedirectTests(unittest.TestCase):
    from workspace_test_support import process_test_setup as setUp
    make_launcher = test_agent_manager.AgentTests.make_launcher

    def tearDown(self):
        for process in manager.agents.values():
            if isinstance(process, Mock):
                process.poll.return_value = 0
        test_agent_manager.AgentTests.tearDown(self)

    def fake_agent(self, agent_type="codex", session=True):
        process = Mock(pid=12345, stdout=io.StringIO(), stdin=io.StringIO())
        process.poll.return_value = None
        with patch.object(manager, "_spawn_process", return_value=process), \
             patch.object(manager, "_codex_command", return_value="fixed command"), \
             patch.object(manager, "Thread"):
            agent_id = manager.start_agent("Original task", agent_type)
        process.stdout.close()
        manager.agent_readers[agent_id].is_alive.return_value = False
        manager.agent_sessions[agent_id] = str(uuid4()) if session else None
        self.addCleanup(setattr, process.poll, "return_value", 0)
        return agent_id, process

    def test_session_detection_preserves_ansi_output(self):
        agent_id, process = self.fake_agent(session=False)
        session_id = str(uuid4())
        line = f"\x1b[32msession id:\x1b[0m \x1b[1m{session_id}\x1b[0m"
        process.stdout = io.StringIO(f"session id: invalid\n{line}\n")
        manager._read_output(agent_id, process)
        detail = manager.get_agent(agent_id)
        self.assertEqual(detail["session_id"], session_id)
        self.assertEqual(detail["output"], ["session id: invalid", line])
        mock_id, mock_process = self.fake_agent("mock", session=False)
        mock_process.stdout = io.StringIO(f"session id: {session_id}\n")
        manager._read_output(mock_id, mock_process)
        self.assertIsNone(manager.get_agent(mock_id)["session_id"])

    def test_redirect_preconditions(self):
        for blank in ("", " ", "\n\t"):
            with self.assertRaises(ValidationError):
                RedirectAgentRequest(instruction=blank)
            with self.assertRaises(ValueError):
                manager.redirect_agent("unknown", blank)
        with self.assertRaises(HTTPException) as missing:
            redirect_codex_agent(str(uuid4()), RedirectAgentRequest(instruction="Correct"))
        self.assertEqual(missing.exception.status_code, 404)
        for agent_type, session, exited in (("mock", False, False), ("codex", False, False),
                                          ("codex", True, True)):
            agent_id, process = self.fake_agent(agent_type, session)
            process.poll.return_value = 0 if exited else None
            with patch.object(manager, "_stop_windows_tree") as stop:
                with self.assertRaises(HTTPException) as error:
                    redirect_codex_agent(agent_id, RedirectAgentRequest(instruction="Correct"))
                self.assertEqual(error.exception.status_code, 409)
                stop.assert_not_called()

    def test_failed_stop_does_not_launch_replacement(self):
        agent_id, process = self.fake_agent()
        with patch.object(manager, "_codex_command", return_value="fixed command"), \
             patch.object(manager, "_stop_windows_tree", side_effect=RuntimeError("Cannot stop tree")), \
             patch.object(manager, "_spawn_process") as spawn:
            with self.assertRaises(HTTPException) as error:
                redirect_codex_agent(agent_id, RedirectAgentRequest(instruction="Correct"))
            self.assertEqual(error.exception.status_code, 503)
            self.assertIn("Cannot stop tree", error.exception.detail)
            spawn.assert_not_called()
        self.assertIs(manager.agents[agent_id], process)
        self.assertEqual(manager.agent_statuses[agent_id], "running")

    def test_failed_spawn_keeps_history_and_reports_stopped(self):
        agent_id, process = self.fake_agent()
        manager.agent_outputs[agent_id].append("Original output")
        with patch.object(manager, "_codex_command", return_value="fixed command"), \
             patch.object(manager, "_stop_windows_tree", side_effect=lambda proc: setattr(proc.poll, "return_value", 0)), \
             patch.object(manager, "_spawn_process", side_effect=OSError("Cannot launch")):
            with self.assertRaises(HTTPException) as error:
                redirect_codex_agent(agent_id, RedirectAgentRequest(instruction="Correct"))
            self.assertEqual(error.exception.status_code, 503)
        self.assertIs(manager.agents[agent_id], process)
        self.assertEqual(manager.get_agent(agent_id)["status"], "stopped")
        self.assertEqual(manager.get_agent(agent_id)["output"], ["Original output"])

    @unittest.skipUnless(os.name == "nt", "Windows resume shim")
    def test_redirect_resumes_same_session_and_stops_replacement(self):
        session_id = str(uuid4())
        instruction = 'Correct "this" & echo not-a-command\nKeep context.'
        with tempfile.TemporaryDirectory(prefix="codex redirect ") as directory:
            self.make_launcher(directory,
                'import json, os, sys, time\n'
                'sys.stdin.reconfigure(encoding="utf-8")\n'
                'prompt = sys.stdin.read()\n'
                f'print("session id: {session_id}", flush=True)\n'
                'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
                'time.sleep(60)\n')
            with patch.dict(os.environ, {"APPDATA": directory}):
                for sandbox in ("read-only", "workspace-write"):
                    agent_id = manager.start_agent("Original task", "codex", sandbox)
                    deadline = time.monotonic() + 5
                    while len(manager.agent_outputs[agent_id]) < 2:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    old = manager.agents[agent_id]
                    history = manager.agent_outputs[agent_id].copy()
                    result = redirect_codex_agent(agent_id, RedirectAgentRequest(instruction=instruction))
                    self.assertEqual(result, {"agent_id": agent_id, "status": "running"})
                    replacement = manager.agents[agent_id]
                    self.assertIsNot(old, replacement)
                    self.assertIsNotNone(old.poll())
                    self.assertIsNone(replacement.poll())
                    self.assertTrue(replacement.stdin.closed)
                    deadline = time.monotonic() + 5
                    while len(manager.agent_outputs[agent_id]) < 6:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.02)
                    detail = manager.get_agent(agent_id)
                    self.assertEqual(detail["task"], "Original task")
                    self.assertEqual(detail["sandbox"], sandbox)
                    self.assertEqual(detail["session_id"], session_id)
                    self.assertEqual(detail["output"][:4], history + ["--- Redirect ---", instruction])
                    resumed = json.loads(detail["output"][5])
                    self.assertEqual(resumed["args"], ["exec", "--sandbox", sandbox, "--color", "never",
                                                     "--skip-git-repo-check", "resume", session_id, "-"])
                    self.assertEqual(resumed["prompt"], manager._codex_prompt(instruction))
                    self.assertEqual(resumed["cwd"], json.loads(history[1])["cwd"])
                    self.assertEqual(Path(resumed["cwd"]), self.workspace_path)
                    self.assertEqual(sum(item["agent_id"] == agent_id for item in manager.get_agents()), 1)
                    self.assertEqual(manager.stop_agent(agent_id)["status"], "stopped")
                    self.assertIsNotNone(replacement.poll())


if __name__ == "__main__":
    unittest.main()
