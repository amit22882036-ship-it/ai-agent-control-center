import os
import logging
import re
import subprocess
import sys
import json
from dataclasses import dataclass, field
from pathlib import Path
from functools import wraps
from threading import RLock, Thread
from typing import Literal
from uuid import UUID, uuid4

from .system_notifications import notifications

logger = logging.getLogger(__name__)

AgentType = Literal["mock", "codex"]
CodexSandbox = Literal["read-only", "workspace-write"]

agents: dict[str, subprocess.Popen] = {}
agent_statuses: dict[str, str] = {}
agent_outputs: dict[str, list[str]] = {}
agent_tasks: dict[str, str] = {}
agent_types: dict[str, AgentType] = {}
agent_sandboxes: dict[str, CodexSandbox | None] = {}
agent_sessions: dict[str, str | None] = {}
agent_waiting_questions: dict[str, str | None] = {}
agent_readers: dict[str, Thread] = {}


@dataclass
class SimilarDecisions:
    enabled: bool = False
    examples: list[str] = field(default_factory=list)
    attempted: set[str] = field(default_factory=set)
    automatic_question: str | None = None
    handled: bool = False


agent_similar_decisions: dict[str, SimilarDecisions] = {}
_similar_handled_marker = "CONTROL_CENTER_SIMILAR_HANDLED"
_state_lock = RLock()
_project_root = Path(__file__).resolve().parents[2]
_ansi = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)')
_session_line = re.compile(r'\bsession id:\s*([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\b', re.I)
_waiting_marker = "CONTROL_CENTER_WAITING:"
_delegated_decision = """The user has delegated this current decision to you.

Make this specific decision yourself using your best judgment and continue the original task.

Do not ask the user to make this same decision again.

This delegation applies only to the current decision. It does not give you permanent autonomy over future unrelated decisions.

If you later become genuinely blocked by a different decision or missing information that requires the user, you may use the existing CONTROL_CENTER_WAITING protocol again."""
_waiting_protocol = """If you are genuinely blocked and require information, clarification, or a decision from the user before you can continue, stop work and end your response with exactly one line in this form:

CONTROL_CENTER_WAITING: <your question>

Do not use this marker if you can reasonably continue without user input."""


def _codex_prompt(text: str) -> str:
    return f"Internal control-center instruction:\n{_waiting_protocol}\n\nUser request:\n{text}"


def _refresh_status(agent_id: str, allow_auto: bool = True) -> None:
    _finalize_process(agent_id, agents[agent_id], allow_auto)


def _finalize_process(agent_id: str, process: subprocess.Popen, allow_auto: bool = True) -> None:
    # The reader must finish parsing buffered output before deciding the exit state.
    with _state_lock:
        if (agents.get(agent_id) is not process or agent_statuses[agent_id] != "running"
                or process.poll() is None or not process.stdout.closed):
            return
        question = agent_waiting_questions[agent_id]
        policy = agent_similar_decisions[agent_id]
        if question and policy.automatic_question and not policy.handled:
            # A declined check is final, even if Codex rephrases the question.
            policy.attempted.add(question.strip())
        if (allow_auto and question and policy.enabled and policy.examples
                and agent_sessions[agent_id] and agent_types[agent_id] == "codex"
                and question.strip() not in policy.attempted):
            policy.attempted.add(question.strip())
            try:
                command = _codex_command(agent_sandboxes[agent_id], agent_sessions[agent_id])
                _resume_agent(agent_id, command, _similar_prompt(policy, question),
                              "--- Automatic Similar Decision ---", history_text=question,
                              automatic_question=question)
                return
            except (OSError, RuntimeError):
                logger.exception("Automatic similar decision could not resume")
                agent_waiting_questions[agent_id] = question
        status = "waiting" if question else "finished"
        agent_statuses[agent_id] = status
        try:
            notifications.transition(agent_id, status, agent_tasks[agent_id])
        except Exception:
            logger.exception("Could not schedule native notification")


def _watch_process(agent_id: str, process: subprocess.Popen, reader: Thread) -> None:
    # Separate from the reader: Redirect/Stop can join the reader while holding
    # the state lock without deadlocking this finalizer.
    reader.join()
    process.wait()
    _finalize_process(agent_id, process)


def _synchronized(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with _state_lock:
            return function(*args, **kwargs)
    return wrapped


def _codex_command(sandbox: CodexSandbox = "read-only", session_id: str | None = None) -> str:
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
    prompt_args = f"resume {UUID(session_id)} -" if session_id else "-"
    command = f'"{launcher}" exec --sandbox {sandbox} --color never --skip-git-repo-check {prompt_args}'
    # cmd /s /c strips the outer quotes, preserving the quoted launcher path.
    return f'"{cmd}" /d /s /v:off /c "{command}"'


def _read_output(agent_id: str, process: subprocess.Popen) -> None:
    with process.stdout as stdout:
        for line in stdout:
            agent_outputs[agent_id].append(line.rstrip("\r\n"))
            if agent_types[agent_id] == "codex" and agents.get(agent_id) is process:
                parsed = _ansi.sub("", line).strip()
                policy = agent_similar_decisions[agent_id]
                if policy.automatic_question and parsed == _similar_handled_marker:
                    policy.handled = True
                match = _session_line.search(parsed)
                if match:
                    agent_sessions[agent_id] = str(UUID(match[1]))
                if parsed.startswith(_waiting_marker):
                    question = parsed[len(_waiting_marker):].strip()
                    if question:
                        agent_waiting_questions[agent_id] = question


def _start_reader(agent_id: str, process: subprocess.Popen) -> None:
    reader = Thread(target=_read_output, args=(agent_id, process), daemon=True)
    agent_readers[agent_id] = reader
    reader.start()
    Thread(target=_watch_process, args=(agent_id, process, reader), daemon=True).start()


def _spawn_process(command: str | list[str], agent_type: AgentType) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE if agent_type == "codex" else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8" if agent_type == "codex" else None,
        errors="replace",
        cwd=_project_root if agent_type == "codex" else None,
        shell=False,
    )


@_synchronized
def start_agent(task: str, agent_type: AgentType = "mock", sandbox: CodexSandbox = "read-only") -> str:
    script_path = Path(__file__).resolve().parent.parent / "mock_agent.py"
    if agent_type not in ("mock", "codex"):
        raise ValueError("Unknown agent type")
    command = _codex_command(sandbox) if agent_type == "codex" else [sys.executable, str(script_path)]
    agent_id = str(uuid4())
    process = _spawn_process(command, agent_type)
    agent_statuses[agent_id] = "running"
    agent_outputs[agent_id] = []
    agent_tasks[agent_id] = task
    agent_types[agent_id] = agent_type
    agent_sandboxes[agent_id] = sandbox if agent_type == "codex" else None
    agent_sessions[agent_id] = None
    agent_waiting_questions[agent_id] = None
    agent_similar_decisions[agent_id] = SimilarDecisions()
    agents[agent_id] = process
    _start_reader(agent_id, process)
    if agent_type == "codex":
        try:
            with process.stdin:
                process.stdin.write(_codex_prompt(task))
        except OSError:
            stop_agent(agent_id)
            raise
    return agent_id


@_synchronized
def get_agents() -> list[dict[str, str | None]]:
    result = []
    for agent_id in list(agents):
        _refresh_status(agent_id)
        result.append({
            "agent_id": agent_id,
            "status": agent_statuses[agent_id],
            "task": agent_tasks[agent_id],
            "agent_type": agent_types[agent_id],
            "sandbox": agent_sandboxes[agent_id],
        })
    return result


@_synchronized
def get_agent(agent_id: str) -> dict[str, str | bool | list[str] | None] | None:
    process = agents.get(agent_id)
    if process is None:
        return None
    _refresh_status(agent_id)
    return {
        "agent_id": agent_id,
        "status": agent_statuses[agent_id],
        "task": agent_tasks[agent_id],
        "agent_type": agent_types[agent_id],
        "sandbox": agent_sandboxes[agent_id],
        "session_id": agent_sessions[agent_id],
        "waiting_question": agent_waiting_questions[agent_id],
        "similar_decisions_enabled": agent_similar_decisions[agent_id].enabled,
        "output": agent_outputs[agent_id].copy(),
    }


@_synchronized
def stop_agent(agent_id: str) -> dict[str, str] | None:
    process = agents.get(agent_id)
    if process is None:
        return None
    if process.poll() is not None:
        if agent_statuses[agent_id] == "running" and not process.stdout.closed:
            reader = agent_readers[agent_id]
            reader.join(timeout=3)
            if reader.is_alive():
                raise RuntimeError("Final process output is still being read; please retry Stop.")
        _refresh_status(agent_id, allow_auto=False)
        if agent_statuses[agent_id] == "waiting":
            agent_statuses[agent_id] = "stopped"
            notifications.cancel(agent_id)
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
    notifications.cancel(agent_id)
    return {"agent_id": agent_id, "status": "stopped"}


@_synchronized
def redirect_agent(agent_id: str, instruction: str) -> dict[str, str] | None:
    if not instruction.strip():
        raise ValueError("Redirect instruction must not be blank.")
    process = agents.get(agent_id)
    if process is None:
        return None
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents can be redirected.")
    if process.poll() is not None or agent_statuses[agent_id] != "running":
        raise ValueError("Only running agents can be redirected.")
    session_id = agent_sessions[agent_id]
    if not session_id:
        raise ValueError("Redirect will be available once the Codex session starts.")
    # Resolve and validate before interrupting the current process.
    command = _codex_command(agent_sandboxes[agent_id], session_id)
    _stop_windows_tree(process)
    try:
        return _resume_agent(agent_id, command, instruction, "--- Redirect ---")
    except (OSError, RuntimeError):
        if agents[agent_id] is process:
            agent_statuses[agent_id] = "stopped"
        raise


@_synchronized
def reply_agent(agent_id: str, answer: str) -> dict[str, str] | None:
    if not answer.strip():
        raise ValueError("Reply answer must not be blank.")
    command = _waiting_resume_command(agent_id)
    if command is None:
        return None
    return _resume_agent(agent_id, command, answer, "--- User Reply ---")


@_synchronized
def decide_agent(agent_id: str) -> dict[str, str] | None:
    command = _waiting_resume_command(agent_id)
    if command is None:
        return None
    return _resume_agent(agent_id, command, _delegated_decision,
                         "--- Delegated Decision ---", history_text="Decide for me this time")


@_synchronized
def decide_similar_agent(agent_id: str) -> dict[str, str] | None:
    command = _waiting_resume_command(agent_id)
    if command is None:
        return None
    question = agent_waiting_questions[agent_id]
    if not question:
        raise ValueError("The waiting question is not available.")
    result = _resume_agent(agent_id, command, _delegated_decision,
                           "--- Similar Decisions Enabled ---",
                           history_text="Decide similar questions automatically")
    policy = agent_similar_decisions[agent_id]
    policy.enabled = True
    if question not in policy.examples:
        policy.examples.append(question)
    policy.attempted.discard(question.strip())
    return result


@_synchronized
def disable_similar_agent(agent_id: str) -> dict[str, str | bool] | None:
    if agent_id not in agents:
        return None
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents support similar decisions.")
    agent_similar_decisions[agent_id] = SimilarDecisions()
    return {"agent_id": agent_id, "similar_decisions_enabled": False}


def _similar_prompt(policy: SimilarDecisions, question: str) -> str:
    return f"""The user authorized automatic decisions only for materially similar decision types and scope.
Approved examples (JSON): {json.dumps(policy.examples)}
Current blocked question (JSON): {json.dumps(question)}
Conservatively determine similarity within this existing session. Do not broaden authorization
merely because questions concern the same project. If uncertain or dissimilar, do not decide;
end with CONTROL_CENTER_WAITING: followed by the exact current blocked question.
If materially similar, make this decision yourself and continue the original task.
Only after actually making that decision, emit the standalone line '{_similar_handled_marker}'
before continuing. Never emit that line for a declined check.
For a later unrelated blocker use the existing waiting protocol again.
This instruction authorizes only this check, not permanent or unrestricted autonomy."""


def _waiting_resume_command(agent_id: str) -> str | None:
    if agent_id not in agents:
        return None
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents can receive replies.")
    _refresh_status(agent_id, allow_auto=False)
    if agent_statuses[agent_id] != "waiting":
        raise ValueError("Only waiting agents can receive replies.")
    session_id = agent_sessions[agent_id]
    if not session_id:
        raise ValueError("The Codex session ID is not available.")
    return _codex_command(agent_sandboxes[agent_id], session_id)


def _resume_agent(agent_id: str, command: str, text: str, marker: str,
                  history_text: str | None = None,
                  automatic_question: str | None = None) -> dict[str, str]:
    # Drain the old process before adding the marker or resumed output.
    reader = agent_readers[agent_id]
    reader.join(timeout=3)
    if reader.is_alive():
        raise RuntimeError("Old process output has not closed; resume was not started.")
    replacement = _spawn_process(command, "codex")
    agent_outputs[agent_id].extend([marker, text if history_text is None else history_text])
    agents[agent_id] = replacement
    agent_waiting_questions[agent_id] = None
    agent_statuses[agent_id] = "running"
    policy = agent_similar_decisions[agent_id]
    policy.automatic_question = automatic_question
    policy.handled = False
    notifications.cancel(agent_id)
    _start_reader(agent_id, replacement)
    try:
        with replacement.stdin:
            replacement.stdin.write(_codex_prompt(text))
    except OSError:
        stop_agent(agent_id)
        raise
    return {"agent_id": agent_id, "status": "running"}


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
