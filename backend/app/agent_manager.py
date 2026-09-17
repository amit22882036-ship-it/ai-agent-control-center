import os
import subprocess
import sys
from pathlib import Path
from threading import Thread
from typing import Literal
from uuid import uuid4

AgentType = Literal["mock", "codex"]
CodexSandbox = Literal["read-only", "workspace-write"]

agents: dict[str, subprocess.Popen] = {}
agent_statuses: dict[str, str] = {}
agent_outputs: dict[str, list[str]] = {}
agent_tasks: dict[str, str] = {}
agent_types: dict[str, AgentType] = {}
agent_sandboxes: dict[str, CodexSandbox | None] = {}


def _codex_command(sandbox: CodexSandbox = "read-only") -> str:
    if sandbox not in ("read-only", "workspace-write"):
        raise ValueError("Unknown Codex sandbox")
    if os.name != "nt":
        raise RuntimeError("Codex agents currently require the Windows npm installation.")
    appdata = os.environ.get("APPDATA")
    launcher = Path(appdata) / "npm" / "codex.cmd" if appdata else None
    if launcher is None or not launcher.is_file():
        raise FileNotFoundError("npm Codex CLI not found at %APPDATA%\\npm\\codex.cmd.")
    cmd = Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe"
    # Only validated CLI options enter cmd.exe; the prompt is sent through stdin.
    command = f'"{launcher}" exec --sandbox {sandbox} --color never --skip-git-repo-check -'
    # cmd /s /c strips the outer quotes, preserving the quoted launcher path.
    return f'"{cmd}" /d /s /v:off /c "{command}"'


def _read_output(agent_id: str, process: subprocess.Popen) -> None:
    with process.stdout as stdout:
        for line in stdout:
            agent_outputs[agent_id].append(line.rstrip("\r\n"))


def start_agent(task: str, agent_type: AgentType = "mock", sandbox: CodexSandbox = "read-only") -> str:
    script_path = Path(__file__).resolve().parent.parent / "mock_agent.py"
    if agent_type not in ("mock", "codex"):
        raise ValueError("Unknown agent type")
    command = _codex_command(sandbox) if agent_type == "codex" else [sys.executable, str(script_path)]
    agent_id = str(uuid4())
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE if agent_type == "codex" else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8" if agent_type == "codex" else None,
        errors="replace",
        cwd=script_path.parent.parent if agent_type == "codex" else None,
        shell=False,
    )
    agent_statuses[agent_id] = "running"
    agent_outputs[agent_id] = []
    agent_tasks[agent_id] = task
    agent_types[agent_id] = agent_type
    agent_sandboxes[agent_id] = sandbox if agent_type == "codex" else None
    agents[agent_id] = process
    Thread(target=_read_output, args=(agent_id, process), daemon=True).start()
    if agent_type == "codex":
        try:
            with process.stdin:
                process.stdin.write(task)
        except OSError:
            stop_agent(agent_id)
            raise
    return agent_id


def get_agents() -> list[dict[str, str | None]]:
    result = []
    for agent_id, process in list(agents.items()):
        if process.poll() is not None and agent_statuses[agent_id] == "running":
            agent_statuses[agent_id] = "finished"
        result.append({
            "agent_id": agent_id,
            "status": agent_statuses[agent_id],
            "task": agent_tasks[agent_id],
            "agent_type": agent_types[agent_id],
            "sandbox": agent_sandboxes[agent_id],
        })
    return result


def get_agent(agent_id: str) -> dict[str, str | list[str] | None] | None:
    process = agents.get(agent_id)
    if process is None:
        return None
    if process.poll() is not None and agent_statuses[agent_id] == "running":
        agent_statuses[agent_id] = "finished"
    return {
        "agent_id": agent_id,
        "status": agent_statuses[agent_id],
        "task": agent_tasks[agent_id],
        "agent_type": agent_types[agent_id],
        "sandbox": agent_sandboxes[agent_id],
        "output": agent_outputs[agent_id].copy(),
    }


def stop_agent(agent_id: str) -> dict[str, str] | None:
    process = agents.get(agent_id)
    if process is None:
        return None
    if process.poll() is not None:
        if agent_statuses[agent_id] == "running":
            agent_statuses[agent_id] = "finished"
        return {"agent_id": agent_id, "status": agent_statuses[agent_id]}

    if agent_types[agent_id] == "codex" and os.name == "nt":
        _stop_windows_tree(process)
    else:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    agent_statuses[agent_id] = "stopped"
    return {"agent_id": agent_id, "status": "stopped"}


def _stop_windows_tree(process: subprocess.Popen) -> None:
    taskkill = str(Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe")
    for force in (False, True):
        command = [taskkill, "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        try:
            result = subprocess.run(command, capture_output=True, timeout=5, shell=False)
            if result.returncode == 0:
                process.wait(timeout=2)
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Killing only cmd.exe can leave Node/Codex children alive. Never report
    # stopped when tree termination failed.
    raise RuntimeError("Could not terminate the Codex process tree; stop was not confirmed.")
