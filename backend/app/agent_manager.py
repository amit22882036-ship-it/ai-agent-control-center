from .work_control import require_active
import os
import logging
import re
import subprocess
import sys
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from functools import wraps
from contextlib import nullcontext, contextmanager, ExitStack
from threading import RLock, Thread
from typing import Literal
from uuid import UUID, uuid4

from .agent_names import default_display_name, validate_display_name, validate_display_color
from .system_notifications import notifications
from .persistence import AgentStore
from .task_workspaces import provision_task_workspace, task_lock
from .workspace_freshness import evaluate_workspace_freshness, ensure_workspace_current, reject_divergence, session_context
from .project_domain import canonical_path
from .realtime import changes
from .output_history import OutputCache, RECENT_OUTPUT_LIMIT, OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT

logger = logging.getLogger(__name__)

AgentType = Literal["mock", "codex"]
CodexSandbox = Literal["read-only", "workspace-write"]

agents: dict[str, subprocess.Popen | None] = {}
agent_parents: dict[str, str | None] = {}
agent_statuses: dict[str, str] = {}
agent_outputs: dict[str, list[str]] = {}
agent_names: dict[str, str] = {}
agent_colors: dict[str, str] = {}
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


@dataclass
class AlwaysDecisions:
    enabled: bool = False
    configured: bool = False
    automatic_attempt: bool = False
    handled: bool = False


agent_always_decisions: dict[str, AlwaysDecisions] = {}
_always_handled_marker = "CONTROL_CENTER_ALWAYS_HANDLED"
agent_similar_decisions: dict[str, SimilarDecisions] = {}
_similar_handled_marker = "CONTROL_CENTER_SIMILAR_HANDLED"
_state_lock = RLock()
_data_lock = RLock()
_store: AgentStore | None = None
_shutting_down = False
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


def _finalize_process(agent_id: str, process: subprocess.Popen | None, allow_auto: bool = True) -> None:
    # The reader must finish parsing buffered output before deciding the exit state.
    with _state_lock:
        if (process is None or agents.get(agent_id) is not process or agent_statuses[agent_id] != "running"
                or process.poll() is None or not process.stdout.closed):
            return
        task_id = _agent_task_id(agent_id)
        controlled = _store.get_task(task_id) if task_id else None
        if controlled and (controlled['control_intent'] != 'active' or controlled['stop_required'] or any(b['hard'] for b in controlled['active_blockers'])):
            agent_statuses[agent_id] = 'stopped'
            notifications.cancel(agent_id)
            _save_agent(agent_id)
            return
        allow_auto = allow_auto and not _shutting_down
        question = agent_waiting_questions[agent_id]
        policy = agent_similar_decisions[agent_id]
        always = agent_always_decisions[agent_id]
        if (allow_auto and question and always.enabled and agent_sessions[agent_id]
                and agent_types[agent_id] == "codex"
                and (not always.automatic_attempt or always.handled)):
            # An unresolved automatic attempt ends this episode, even if reworded.
            always.automatic_attempt = True
            always.handled = False
            try:
                command = _codex_command(agent_sandboxes[agent_id], agent_sessions[agent_id])
                _resume_agent(agent_id, command, _always_prompt(question, automatic=True),
                              "--- Automatic Always Decision ---", history_text=question,
                              always_decision=True, automatic_always=True)
                return
            except (OSError, RuntimeError, ValueError, LookupError, sqlite3.Error):
                logger.exception("Automatic Always decision could not resume")
                current = agents[agent_id]
                if current is not process and (current.poll() is None or not current.stdout.closed):
                    return
                agent_waiting_questions[agent_id] = question
        if not always.enabled and question and policy.automatic_question and not policy.handled:
            # A declined check is final, even if Codex rephrases the question.
            policy.attempted.add(question.strip())
        if (allow_auto and question and not always.enabled and policy.enabled and policy.examples
                and agent_sessions[agent_id] and agent_types[agent_id] == "codex"
                and question.strip() not in policy.attempted):
            policy.attempted.add(question.strip())
            try:
                command = _codex_command(agent_sandboxes[agent_id], agent_sessions[agent_id])
                _resume_agent(agent_id, command, _similar_prompt(policy, question),
                              "--- Automatic Similar Decision ---", history_text=question,
                              automatic_question=question)
                return
            except (OSError, RuntimeError, ValueError, LookupError, sqlite3.Error):
                logger.exception("Automatic similar decision could not resume")
                current = agents[agent_id]
                if current is not process and (current.poll() is None or not current.stdout.closed):
                    return
                agent_waiting_questions[agent_id] = question
        status = "waiting" if question else "finished"
        agent_statuses[agent_id] = status
        _save_agent(agent_id)
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


def _emit_agent_change(agent_id: str) -> None:
    try:
        changes.publish(agent_id)
    except Exception:
        logger.exception("Could not publish agent invalidation")


def _save_agent(agent_id: str, emit=True) -> bool:
    # Readers never take _state_lock: Stop/Redirect join them while holding it.
    # Cache sequence assignment, persistence and publication share _data_lock.
    with _data_lock:
        cache = agent_outputs[agent_id]
        cache.dirty = True
        if cache.hold:
            return True
        if _store is not None:
            similar = agent_similar_decisions[agent_id]
            always = agent_always_decisions[agent_id]
            try:
                assignment_options = {}
                pending_task = getattr(cache, 'assignment_task_id', None)
                if pending_task is not None:
                    assignment_options['assignment_task_id'] = pending_task
                _store.save_agent({
                    "agent_id": agent_id, "parent_id": agent_parents[agent_id],
                    "task": agent_tasks[agent_id],
                    "display_name": agent_names[agent_id], "agent_type": agent_types[agent_id],
                    "display_color": agent_colors.get(agent_id, "neutral"),
                    "sandbox": agent_sandboxes[agent_id], "status": agent_statuses[agent_id],
                    "session_id": agent_sessions[agent_id], "waiting_question": agent_waiting_questions[agent_id],
                    "similar_decisions_enabled": similar.enabled, "similar_examples": similar.examples.copy(),
                    "always_decide_enabled": always.enabled, "always_decide_configured": always.configured,
                }, output_entries=cache.pending, **assignment_options)
                cache.assignment_task_id = None
            except (sqlite3.Error, OSError):
                # Keep uncommitted entries for the next write/read/shutdown retry.
                # This exceptional backlog must not be trimmed or silently lost.
                logger.exception("Could not persist agent %s; output retained for retry", agent_id)
                return False
        cache.pending.clear()
        cache.dirty = False
        if emit:
            _emit_agent_change(agent_id)
        return True


@_synchronized
def initialize_persistence(path=None) -> None:
    """Explicit startup only. Restored logical agents have no attached processes."""
    global _store, _shutting_down
    if any(process is not None for process in agents.values()):
        raise RuntimeError("Cannot reload persistence while managed processes are attached")
    store = AgentStore(path)
    from .integrations import recover_integrations
    recover_integrations(store)
    store.reconcile_task_recovery()
    records = store.load_agents(output_limit=RECENT_OUTPUT_LIMIT)
    with _data_lock:
        for registry in (agents, agent_parents, agent_statuses, agent_outputs, agent_tasks, agent_names, agent_colors, agent_types,
                         agent_sandboxes, agent_sessions, agent_waiting_questions, agent_readers,
                         agent_similar_decisions, agent_always_decisions):
            registry.clear()
        _store = store
        _shutting_down = False
        for record in records:
            key = record['agent_id']
            agents[key] = None
            agent_parents[key] = record['parent_id']
            agent_tasks[key] = record['task']
            agent_names[key] = record['display_name']
            agent_colors[key] = record['display_color']
            agent_types[key] = record['agent_type']
            agent_sandboxes[key] = record['sandbox']
            agent_statuses[key] = record['status']
            agent_sessions[key] = record['session_id']
            agent_waiting_questions[key] = record['waiting_question']
            agent_outputs[key] = OutputCache(record['output'], record['next_sequence'])
            agent_similar_decisions[key] = SimilarDecisions(
                enabled=bool(record['similar_decisions_enabled']), examples=record['similar_examples'])
            agent_always_decisions[key] = AlwaysDecisions(
                enabled=bool(record['always_decide_enabled']), configured=bool(record['always_decide_configured']))


@_synchronized
def shutdown_agents() -> list[dict[str, str]]:
    """Stop only live work, drain output, and leave genuine waits resumable."""
    global _shutting_down
    _shutting_down = True
    failures = []
    for key, process in list(agents.items()):
        try:
            reader = agent_readers.get(key)
            if process is not None and process.poll() is not None and reader is not None:
                reader.join(timeout=3)
            _refresh_status(key, allow_auto=False)
            if agent_statuses[key] == 'running':
                stop_agent(key)
            if reader is not None:
                reader.join(timeout=3)
                if reader.is_alive():
                    raise RuntimeError('Agent output did not close during shutdown')
            if not _save_agent(key):
                raise RuntimeError('Agent persistence is unavailable during shutdown')
        except Exception as exc:
            logger.exception('Could not shut down agent %s', key)
            failures.append({'agent_id': key, 'error': str(exc)})
    return failures


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
    # CLI section state belongs to this reader/process, never to the logical agent.
    section = "other"
    header = True
    with process.stdout as stdout:
        for line in stdout:
            with _data_lock:
                if agents.get(agent_id) is not process:
                    continue
                agent_outputs[agent_id].append(line.rstrip("\r\n"))
                if agent_types[agent_id] == "codex" and agents.get(agent_id) is process:
                    parsed = _ansi.sub("", line).strip()
                    if parsed == "codex":
                        header = False
                        section = "codex"
                    elif parsed == "user":
                        header = False
                        section = "user"
                    elif parsed in {"thinking", "exec", "tool", "system", "developer", "file update", "tokens used"} or parsed.startswith(
                            ("mcp:", "mcp startup:", "warning:", "error:", "OpenAI Codex ")):
                        section = "other"
                        if parsed in {"thinking", "exec", "tool", "system", "developer", "file update", "tokens used"}:
                            header = False
                    policy = agent_similar_decisions[agent_id]
                    if section == "codex" and policy.automatic_question and parsed == _similar_handled_marker:
                        policy.handled = True
                    always = agent_always_decisions[agent_id]
                    if section == "codex" and always.automatic_attempt and parsed == _always_handled_marker:
                        always.handled = True
                    match = _session_line.search(parsed)
                    if header and parsed.lower().startswith('session id:') and match and agent_sessions[agent_id] is None:
                        agent_sessions[agent_id] = str(UUID(match[1]))
                    if section == "codex" and parsed.startswith(_waiting_marker):
                        question = parsed[len(_waiting_marker):].strip()
                        if question:
                            agent_waiting_questions[agent_id] = question
                _save_agent(agent_id)


def _start_reader(agent_id: str, process: subprocess.Popen) -> None:
    reader = Thread(target=_read_output, args=(agent_id, process), daemon=True)
    agent_readers[agent_id] = reader
    reader.start()
    Thread(target=_watch_process, args=(agent_id, process, reader), daemon=True).start()


def _spawn_process(command: str | list[str], agent_type: AgentType, *, cwd) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE if agent_type == "codex" else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8" if agent_type == "codex" else None,
        errors="replace",
        cwd=cwd,
        shell=False,
    )


def _task_project_root(task):
    if not task['project_id']:
        raise ValueError('Task has unresolved Project ownership')
    project = _store.get_project(task['project_id'])
    if project is None:
        raise LookupError('Project not found')
    return Path(canonical_path(project['root_path'], require_directory=True)[0])


def _execution_workspace(task_id, origin_kind='legacy_project_snapshot'):
    _task_store().check_task_integrations(task_id)
    workspace = provision_task_workspace(_task_store(), task_id, origin_kind=origin_kind)
    return Path(workspace['workspace_path'])


def _resume_cwd(agent_id):
    related = _store.agent_task_ids(agent_id) if _store is not None else []
    return _execution_workspace(related[0] if related else None)


def _agent_task_id(agent_id):
    related = _store.agent_task_ids(agent_id) if _store is not None else []
    return related[0] if related else None


@contextmanager
def _upstream_guard(task_id):
    if _store is None or task_id is None:
        yield
        return
    task = _store.get_task(task_id)
    if task is None:
        raise LookupError('Task not found')
    keys = {task['parent_task_id'] or 'project:' + str(task['project_id'])}
    if task['parent_task_id'] and _store.get_task_workspace(task['parent_task_id']) is None:
        # Legacy parent bootstrap also snapshots canonical source.
        keys.add('project:' + str(task['project_id']))
    # Use integration destination locks while observing upstream. Nonblocking
    # acquisition avoids child/parent lock-order inversions with integrations.
    with ExitStack() as guards:
        for key in sorted(keys):
            guards.enter_context(task_lock(_store, key, blocking=False))
        yield


def _workspace_session(action):
    @wraps(action)
    def locked(agent_id, *args, **kwargs):
        task_id = _agent_task_id(agent_id)
        if task_id:
            require_active(_store.get_task(task_id))
        # Nonblocking under the process-state lock avoids lock-order inversions
        # with starts, which do slow Git work outside that broad lock.
        guard = task_lock(_store, task_id, blocking=False) if task_id else nullcontext()
        with guard, _upstream_guard(task_id):
            return action(agent_id, *args, **kwargs)
    return locked


def start_agent(task: str, agent_type: AgentType = "mock", sandbox: CodexSandbox = "read-only",
                parent_id: str | None = None, *, task_id: str | None = None) -> str:
    prepared = _prepare_agent_start(task, agent_type, sandbox, parent_id, task_id=task_id)
    # Git/filesystem work runs outside the broad process-state lock. SQLite
    # ownership is rechecked when launching after this potentially slow step.
    task_id = prepared[2]
    guard = task_lock(_store, task_id, blocking=False) if _store is not None else nullcontext()
    with guard, _upstream_guard(task_id):
        if _store is not None:
            current = _store.get_task(task_id)
            require_active(current, pending=True)
            if current['status'] != 'pending' or _store.get_active_assignment_for_task(task_id):
                raise ValueError('Only an unassigned pending Task can start an agent')
        cwd = _execution_workspace(task_id, prepared[5])
        if _store is not None:
            ensure_workspace_current(_store, task_id)
        return _launch_prepared_agent(*prepared[:5], agent_type, sandbox, cwd)


@_synchronized
def _prepare_agent_start(task: str, agent_type: AgentType, sandbox: CodexSandbox,
                         parent_id: str | None, *, task_id: str | None):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; agent start was not performed')
    origin = 'project_snapshot' if task_id is None else 'legacy_project_snapshot'
    if parent_id is not None and parent_id not in agents:
        raise LookupError("Parent agent not found")
    script_path = Path(__file__).resolve().parent.parent / "mock_agent.py"
    if agent_type not in ("mock", "codex"):
        raise ValueError("Unknown agent type")
    command = _codex_command(sandbox) if agent_type == "codex" else [sys.executable, str(script_path)]
    # Resolve logical ownership from the canonical context, then execute only
    # in the Task Workspace regardless of provider.
    cwd = root_path = _project_root
    parent_task = None
    if _store is not None:
        if task_id is None:
            if parent_id is not None:
                # Ended workers retain their actual assignment history too.
                assignment = _store.get_active_assignment_for_agent(parent_id)
                related = _store.agent_task_ids(parent_id)
                parent_task_id = assignment['task_id'] if assignment else (related[0] if related else None)
                if parent_task_id is None:
                    raise ValueError('Parent agent has no associated Task')
                parent_task = _store.get_task(parent_task_id)
                require_active(parent_task)
                _task_project_root(parent_task)  # Reject unresolved legacy ownership.
                origin = 'parent_task_snapshot'
            task_record = _store.create_task(task, parent_task_id=parent_task['task_id'] if parent_task else None,
                                             root_path=root_path,
                                             project_id=(_store.resolve_project_for_path(cwd) or {}).get('project_id')
                                             if parent_task is None else None)
        else:
            task_record = _store.get_task(task_id)
            if task_record is None:
                raise LookupError('Task not found')
            require_active(task_record, pending=True)
            if task_record['status'] != 'pending' or _store.get_active_assignment_for_task(task_id):
                raise ValueError('Only an unassigned pending Task can start an agent')
            parent_task = _store.get_task_parent(task_id)
            if parent_task is not None:
                origin = 'parent_task_snapshot'
            assignment = _store.get_active_assignment_for_task(parent_task['task_id']) if parent_task else None
            parent_id = assignment['agent_id'] if assignment else None
            cwd = _task_project_root(task_record)
        task_id = task_record['task_id']
        task = task_record['description']
    elif task_id is not None:
        raise RuntimeError('Task persistence is unavailable')
    return task, parent_id, task_id, parent_task, command, origin


@_synchronized
def _launch_prepared_agent(task, parent_id, task_id, parent_task, command, agent_type, sandbox, cwd):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; agent start was not performed')
    if _store is not None:
        _store.check_task_integrations(task_id)
        current = _store.get_task(task_id)
        require_active(current, pending=True)
        if current is None or current['status'] != 'pending' or _store.get_active_assignment_for_task(task_id):
            raise ValueError('Only an unassigned pending Task can start an agent')
    provider_task = task
    if parent_task is not None:
        provider_task = (f"Parent work:\n{parent_task['description']}\n\n"
                         f"Your assigned contribution:\n{task}\n\n"
                         "Work specifically on your assigned contribution in support of the parent work.")
    agent_id = str(uuid4())
    process = _spawn_process(command, agent_type, cwd=cwd)
    agent_statuses[agent_id] = "running"
    agent_parents[agent_id] = parent_id
    agent_outputs[agent_id] = OutputCache()
    agent_outputs[agent_id].assignment_task_id = task_id
    agent_tasks[agent_id] = task
    agent_names[agent_id] = default_display_name(task)
    agent_colors[agent_id] = "neutral"
    agent_types[agent_id] = agent_type
    agent_sandboxes[agent_id] = sandbox if agent_type == "codex" else None
    agent_sessions[agent_id] = None
    agent_waiting_questions[agent_id] = None
    agent_similar_decisions[agent_id] = SimilarDecisions()
    agent_always_decisions[agent_id] = AlwaysDecisions()
    agents[agent_id] = process
    try:
        if not _save_agent(agent_id):
            raise RuntimeError('Unable to persist agent start; please retry.')
    except (ValueError, LookupError, RuntimeError) as exc:
        # A concurrent claimant may win after validation but before the commit.
        # Keep the worker tracked and terminate it; never steal its assignment.
        if isinstance(exc, (ValueError, LookupError)):
            agent_outputs[agent_id].assignment_task_id = None
        cache = agent_outputs[agent_id]
        # Drain output without allowing a retry to publish a successful start
        # while compensation is still in progress.
        cache.hold = True
        try:
            _start_reader(agent_id, process)
            if process.poll() is not None:
                agent_statuses[agent_id] = 'stopped'
            stop_agent(agent_id)
            # Even a process that exited before Stop belongs to a failed start.
            agent_statuses[agent_id] = 'stopped'
        finally:
            cache.hold = False
            _save_agent(agent_id)
        raise
    _start_reader(agent_id, process)
    if agent_type == "codex":
        try:
            with process.stdin:
                process.stdin.write(_codex_prompt(provider_task))
        except OSError:
            stop_agent(agent_id)
            raise
    return agent_id


def _task_store():
    if _store is None:
        raise RuntimeError('Task persistence is unavailable')
    return _store


@_synchronized
def create_task(title: str, description: str, project_id=None):
    task = _task_store().create_task(description, title=title, project_id=project_id, root_path=_project_root)
    # Task-only invalidation uses the existing generic change stream.
    _emit_agent_change(None)
    return {**task, 'current_assignment': None}


@_synchronized
def get_tasks(task_id=None):
    return _task_store().task_summaries(task_id)


@_synchronized
def get_task_assignments(task_id):
    store = _task_store()
    if store.get_task(task_id) is None:
        raise LookupError('Task not found')
    # Oldest first; insertion order breaks equal timestamp ties.
    return store.list_task_assignments(task_id)


def start_task_agent(task_id, agent_type='mock', sandbox='read-only'):
    agent_id = start_agent('', agent_type, sandbox, task_id=task_id)
    return {'task_id': task_id, 'agent_id': agent_id, 'status': 'running'}


@_synchronized
def get_agents() -> list[dict[str, str | None]]:
    result = []
    for agent_id in list(agents):
        _refresh_status(agent_id)
        if agent_outputs[agent_id].dirty:
            _save_agent(agent_id)
        result.append({
            "agent_id": agent_id,
            "parent_id": agent_parents[agent_id],
            "status": agent_statuses[agent_id],
            "task": agent_tasks[agent_id],
            "display_name": agent_names[agent_id],
            "display_color": agent_colors.get(agent_id, "neutral"),
            "agent_type": agent_types[agent_id],
            "sandbox": agent_sandboxes[agent_id],
            "waiting_question": agent_waiting_questions[agent_id],
        })
    return result


@_synchronized
def get_agent(agent_id: str, include_output: bool = True) -> dict[str, str | bool | list[str] | None] | None:
    if agent_id not in agents:
        return None
    _refresh_status(agent_id)
    with _data_lock:
        cache = agent_outputs[agent_id]
        if cache.dirty:
            _save_agent(agent_id)
        output = {}
        if include_output:
            lines = _store.full_output(agent_id) if _store else cache.copy()
            if _store and cache.pending:
                committed_count = len(lines)
                lines.extend(text for seq, text in cache.pending if seq >= committed_count)
            output = {"output": lines}
    return {
        "agent_id": agent_id,
        "status": agent_statuses[agent_id],
        "task": agent_tasks[agent_id],
        "display_name": agent_names[agent_id],
        "display_color": agent_colors.get(agent_id, "neutral"),
        "agent_type": agent_types[agent_id],
        "sandbox": agent_sandboxes[agent_id],
        "parent_id": agent_parents[agent_id],
        "child_ids": [child for child, parent in agent_parents.items() if parent == agent_id],
        "session_id": agent_sessions[agent_id],
        "waiting_question": agent_waiting_questions[agent_id],
        "similar_decisions_enabled": agent_similar_decisions[agent_id].enabled,
        "always_decide_enabled": agent_always_decisions[agent_id].enabled,
        "task_ids": _store.agent_task_ids(agent_id) if _store else [],
        **output,
    }


@_synchronized
def get_agent_output(agent_id: str, limit: int = OUTPUT_PAGE_SIZE,
                     after: int | None = None, before: int | None = None):
    if agent_id not in agents:
        return None
    if not 1 <= limit <= OUTPUT_MAX_LIMIT or (after is not None and before is not None):
        raise ValueError('Use a limit between 1 and 1000 and only one output cursor')
    if (after is not None and after < 0) or (before is not None and before < 0):
        raise ValueError('Output cursors must be non-negative')
    with _data_lock:
        cache = agent_outputs[agent_id]
        if cache.dirty and not _save_agent(agent_id):
            raise RuntimeError('Output persistence is unavailable; please retry.')
        if _store:
            return _store.read_output(agent_id, limit, after, before)
        # Direct manager users/tests without the application lifespan have no DB.
        entries = [{'seq': cache.next_sequence - len(cache) + i, 'text': text}
                   for i, text in enumerate(cache)]
        matches = [item for item in entries if (after is None or item['seq'] > after)
                   and (before is None or item['seq'] < before)]
        items = matches[:limit] if after is not None else matches[-limit:]
        return {'agent_id': agent_id, 'items': items,
                'has_older': bool(items and entries and items[0]['seq'] > entries[0]['seq']),
                'has_newer': bool(items and entries and items[-1]['seq'] < entries[-1]['seq'])}


@_synchronized
def stop_agent(agent_id: str) -> dict[str, str] | None:
    if agent_id not in agents:
        return None
    process = agents[agent_id]
    if process is None or process.poll() is not None:
        if process is not None and agent_statuses[agent_id] == "running" and not process.stdout.closed:
            reader = agent_readers[agent_id]
            reader.join(timeout=3)
            if reader.is_alive():
                raise RuntimeError("Final process output is still being read; please retry Stop.")
        _refresh_status(agent_id, allow_auto=False)
        if agent_statuses[agent_id] == "waiting":
            agent_statuses[agent_id] = "stopped"
            notifications.cancel(agent_id)
            _save_agent(agent_id)
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
    _save_agent(agent_id)
    return {"agent_id": agent_id, "status": "stopped"}


@_synchronized
def stop_branch(agent_id: str) -> dict | None:
    if agent_id not in agents:
        return None
    children: dict[str | None, list[str]] = {}
    for child_id, parent_id in agent_parents.items():
        if child_id in agents:
            children.setdefault(parent_id, []).append(child_id)
    # Iterative postorder avoids recursion limits and visits corrupt cycles once.
    visited = set()
    order = []
    pending = [(agent_id, False)]
    while pending:
        current, expanded = pending.pop()
        if expanded:
            order.append(current)
        elif current not in visited:
            visited.add(current)
            pending.append((current, True))
            pending.extend((child, False) for child in reversed(children.get(current, [])))
    results = []
    failures = []
    # Keep the manager lock for discovery and every stop: creation/resumes cannot
    # interleave with this branch snapshot. stop_agent uses the same reentrant lock.
    for current in order:
        try:
            results.append(stop_agent(current))
        except Exception as exc:
            error = str(exc) if isinstance(exc, (OSError, RuntimeError)) else "Unable to stop agent."
            failures.append({"agent_id": current, "error": error})
    return {"root_agent_id": agent_id, "ok": not failures, "results": results, "failures": failures}


@_synchronized
@_workspace_session
def redirect_agent(agent_id: str, instruction: str) -> dict[str, str] | None:
    if not instruction.strip():
        raise ValueError("Redirect instruction must not be blank.")
    if agent_id not in agents:
        return None
    process = agents[agent_id]
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents can be redirected.")
    if process is None or process.poll() is not None or agent_statuses[agent_id] != "running":
        raise ValueError("Only running agents can be redirected.")
    session_id = agent_sessions[agent_id]
    if not session_id:
        raise ValueError("Redirect will be available once the Codex session starts.")
    # Resolve and validate before interrupting the current process.
    command = _codex_command(agent_sandboxes[agent_id], session_id)
    cwd = _resume_cwd(agent_id)
    task_id = _agent_task_id(agent_id)
    if task_id:
        reject_divergence(evaluate_workspace_freshness(_store, task_id))
    _stop_windows_tree(process)
    try:
        return _resume_agent(agent_id, command, instruction, "--- Redirect ---", cwd=cwd)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        if agents[agent_id] is process:
            agent_statuses[agent_id] = "stopped"
            _save_agent(agent_id)
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
    _save_agent(agent_id)
    return result


@_synchronized
def disable_similar_agent(agent_id: str) -> dict[str, str | bool] | None:
    if agent_id not in agents:
        return None
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents support similar decisions.")
    agent_similar_decisions[agent_id] = SimilarDecisions()
    _save_agent(agent_id)
    return {"agent_id": agent_id, "similar_decisions_enabled": False}


@_synchronized
def decide_always_agent(agent_id: str) -> dict[str, str] | None:
    command = _waiting_resume_command(agent_id)
    if command is None:
        return None
    if agent_always_decisions[agent_id].enabled:
        raise ValueError("Always Decide is already enabled for this agent.")
    question = agent_waiting_questions[agent_id]
    if not question:
        raise ValueError("The waiting question is not available.")
    result = _resume_agent(agent_id, command, _always_prompt(question),
                           "--- Always Decide Enabled ---", history_text="Always decide for this agent",
                           always_decision=True)
    agent_always_decisions[agent_id].enabled = True
    agent_always_decisions[agent_id].configured = True
    _save_agent(agent_id)
    return result


@_synchronized
def disable_always_agent(agent_id: str) -> dict[str, str | bool] | None:
    if agent_id not in agents:
        return None
    if agent_types[agent_id] != "codex":
        raise ValueError("Only Codex agents support Always Decide.")
    agent_always_decisions[agent_id].enabled = False
    agent_always_decisions[agent_id].configured = True
    _save_agent(agent_id)
    return {"agent_id": agent_id, "always_decide_enabled": False}


def _always_prompt(question: str, automatic: bool = False) -> str:
    text = f"""The user has enabled Always Decide for this agent.
Current blocked question (JSON): {json.dumps(question)}
Resolve this decision yourself using your best judgment and continue the original task.
For future decisions, preferences, implementation choices, clarifications, or tradeoffs
that you can reasonably resolve yourself, decide and continue without asking the user.
This delegates decisions only. Do not expand the original task, permissions, or sandbox;
do not bypass approval/security rules or authorize unrelated external actions.
Do not fabricate unavailable facts, credentials, secrets, or user-specific information.
If genuinely blocked by required unavailable information that cannot reasonably be inferred,
or an explicit approval that is actually required, do not invent it or bypass the requirement.
Return control using CONTROL_CENTER_WAITING: followed by the required question.
Later control-center instructions may disable this delegation."""
    if automatic:
        text += f"""\nOnly after actually resolving the current delegated decision, emit the standalone
line '{_always_handled_marker}' before continuing. Never emit it for an unresolved blocker."""
    return text


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
    task_id = _agent_task_id(agent_id)
    if task_id:
        require_active(_store.get_task(task_id))
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


@_workspace_session
def _resume_agent(agent_id: str, command: str, text: str, marker: str,
                  history_text: str | None = None,
                  automatic_question: str | None = None,
                  always_decision: bool = False, automatic_always: bool = False,
                  cwd=None) -> dict[str, str]:
    # Drain the old process before adding the marker or resumed output.
    reader = agent_readers.get(agent_id)
    if reader is not None:
        reader.join(timeout=3)
        if reader.is_alive():
            raise RuntimeError("Old process output has not closed; resume was not started.")
    previous = (agents[agent_id], agent_statuses[agent_id], agent_waiting_questions[agent_id],
                agent_sessions[agent_id], agent_outputs[agent_id])
    cwd = cwd if cwd is not None else _resume_cwd(agent_id)
    task_id = _agent_task_id(agent_id)
    state = ensure_workspace_current(_store, task_id) if task_id else None
    if state:
        state['integration_order'] = _store.integration_cursor(task_id)
    invalidation = session_context(_store, agent_id, state) if state else ''
    replacement = _spawn_process(command, "codex", cwd=cwd)
    history = [marker, text if history_text is None else history_text]
    with _data_lock:
        if always_decision:
            # Stage tentative output while a reader drains the replacement pipe.
            # Failed prompt delivery restores the old cache without deleting SQL
            # rows or reusing any published sequence numbers.
            old_cache = agent_outputs[agent_id]
            staged = OutputCache(old_cache, old_cache.next_sequence)
            staged.pending = old_cache.pending.copy()
            staged.hold = True
            agent_outputs[agent_id] = staged
        agent_outputs[agent_id].extend(history)
        agents[agent_id] = replacement
    agent_waiting_questions[agent_id] = None
    agent_statuses[agent_id] = "running"
    policy = agent_similar_decisions[agent_id]
    policy.automatic_question = automatic_question
    policy.handled = False
    always = agent_always_decisions[agent_id]
    always.automatic_attempt = automatic_always
    always.handled = False
    notifications.cancel(agent_id)
    _save_agent(agent_id)
    _start_reader(agent_id, replacement)
    try:
        with replacement.stdin:
            prompt = invalidation + _codex_prompt(text)
            if not always_decision and always.configured:
                setting = "enabled" if always.enabled else "disabled"
                prompt += (f"\n\nCurrent control-center setting: Always Decide is {setting}. "
                           "This supersedes earlier Always Decide settings in this session. "
                           "When disabled, honor the current request and any explicit current decision "
                           "delegation, but do not apply earlier Always Decide authorization.")
            replacement.stdin.write(prompt)
    except OSError:
        try:
            stop_agent(agent_id)
        except (OSError, RuntimeError):
            if always_decision:
                agent_outputs[agent_id].hold = False
                _save_agent(agent_id)
            raise
        if always_decision:
            agent_readers[agent_id].join(timeout=3)
            if agent_readers[agent_id].is_alive():
                # Keep failed work tracked if its output cannot be safely drained.
                agent_outputs[agent_id].hold = False
                _save_agent(agent_id)
                raise RuntimeError("Failed resume output is still closing; please retry.")
            with _data_lock:
                old_process, old_status, old_question, old_session, old_cache = previous
                agents[agent_id] = old_process
                agent_statuses[agent_id] = old_status
                agent_waiting_questions[agent_id] = old_question
                agent_sessions[agent_id] = old_session
                agent_outputs[agent_id] = old_cache
                if reader is None:
                    agent_readers.pop(agent_id, None)
                else:
                    agent_readers[agent_id] = reader
                _save_agent(agent_id, emit=False)
        raise
    if state:
        try:
            _store.acknowledge_agent_source_context(agent_id, state['base_snapshot'], state['integration_order'])
        except (sqlite3.Error, OSError):
            # Repeating a warning next time is safe; forgetting it is not.
            logger.exception('Could not acknowledge session source context')
    if always_decision:
        with _data_lock:
            agent_outputs[agent_id].hold = False
            _save_agent(agent_id)
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


@_synchronized
def rename_agent(agent_id: str, display_name: str):
    if agent_id not in agents:
        return None
    name = validate_display_name(display_name)
    with _data_lock:
        previous = agent_names[agent_id]
        agent_names[agent_id] = name
        if not _save_agent(agent_id):
            agent_names[agent_id] = previous
            raise RuntimeError("Unable to save agent name. Please retry.")
    return {"agent_id": agent_id, "display_name": name}


@_synchronized
def set_agent_color(agent_id: str, display_color: str):
    if agent_id not in agents:
        return None
    color = validate_display_color(display_color)
    with _data_lock:
        previous = agent_colors.get(agent_id, "neutral")
        agent_colors[agent_id] = color
        if not _save_agent(agent_id):
            agent_colors[agent_id] = previous
            raise RuntimeError("Unable to save agent color. Please retry.")
    return {"agent_id": agent_id, "display_color": color}


@_synchronized
def get_name_history(agent_id: str):
    if agent_id not in agents:
        return None
    if _store is None:
        raise RuntimeError("Name history is temporarily unavailable.")
    with _data_lock:
        return {"agent_id": agent_id, "history": _store.name_history(agent_id)}


def integrate_task(task_id):
    from .integrations import integrate
    store = _task_store()

    def assert_quiet(current_task_id):
        # Do not refresh statuses here: that could automatically resume a policy.
        with _state_lock:
            for assignment in store.list_task_assignments(current_task_id):
                key = assignment['agent_id']
                process = agents.get(key)
                if process is not None and process.poll() is None:
                    raise ValueError('Integration refuses a Task Workspace with a live Agent process')
                if key not in agents or process is None:
                    record = next((r for r in store.load_agents(output_limit=0) if r['agent_id'] == key), None)
                    if record and record['status'] == 'running':
                        raise ValueError('Integration refuses a Task with an unowned running Agent')
    return integrate(store, task_id, assert_quiet)


def _settle_tasks(task_ids):
    """Phase 2: durable gates already exist; never wait on a process in SQL."""
    failures, first_error = [], None
    for task_id in sorted(set(task_ids)):
        try:
            task = _store.get_task(task_id)
            assignment = _store.get_active_assignment_for_task(task_id)
            gated = task['control_intent'] != 'active' or task['stop_required'] or any(b['hard'] for b in task['active_blockers'])
            if gated and assignment:
                key = assignment['agent_id']
                if key not in agents:
                    raise RuntimeError('Worker ownership unavailable; restart recovery required')
                process = agents[key]
                preserve_wait = task['control_intent'] != 'canceled' and agent_statuses[key] == 'waiting' and not task['stop_required']
                if preserve_wait and process is not None and process.poll() is None:
                    preserve_wait = False
                if not preserve_wait:
                    if process is not None and process.poll() is None:
                        if agent_types[key] == 'codex' and os.name == 'nt':
                            _stop_windows_tree(process)
                        else:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                    if process is not None:
                        if process.poll() is None:
                            raise RuntimeError('Worker has not stopped; durable execution gate retained')
                        reader = agent_readers.get(key)
                        if reader is not None:
                            reader.join(timeout=3)
                            if reader.is_alive():
                                raise RuntimeError('Final output is still being read; retry work control')
                    agent_statuses[key] = 'stopped'
                    notifications.cancel(key)
                    if not _save_agent(key):
                        raise RuntimeError('Could not persist stopped worker; retry work control')
            _store.finalize_work_control(task_id)
        except (OSError, RuntimeError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
            failures.append({'task_id': task_id, 'reason': 'worker_reconciliation_required'})
            first_error = first_error or (RuntimeError('Worker termination did not complete')
                                          if isinstance(exc, subprocess.SubprocessError) else exc)
    _store.finish_control_operations(failures)
    if not failures:
        _emit_agent_change(None)
    if first_error is not None:
        raise first_error


@_synchronized
def control_task(task_id, intent):
    store = _task_store()
    if intent == 'active':
        store.request_work_control(task_id, intent)
        store.finish_control_operations()
        _emit_agent_change(None)
        return store.get_task(task_id)
    action = 'pause' if intent == 'paused' else 'cancel'
    preview = store.control_impact(task_id, action)
    keys = set(preview['affected_task_ids']) | set(preview['external_dependent_task_ids']) | {task_id}
    with ExitStack() as locks:
        for key in sorted(keys):
            locks.enter_context(task_lock(store, key, blocking=False))
        # This call recomputes impact inside the intent transaction. The preview
        # above is only used to respect existing workspace operation locks.
        store.request_work_control(task_id, intent)
        operation = next(o for o in reversed(store.control_operations(task_id)) if o['action'] == action and not o['released'])
        plan = operation['impact']
        _settle_tasks(set(plan['affected_task_ids']) | set(plan['external_dependent_task_ids']) | {task_id})
    return store.get_task(task_id)


@_synchronized
def change_dependency(task_id, source_id, remove=False):
    store = _task_store()
    with task_lock(store, task_id, blocking=False):
        store.change_dependency(task_id, source_id, remove)
        _settle_tasks([task_id])
    return store.get_task(task_id)


@_synchronized
def create_resource_claim(task_id, **options):
    store = _task_store()
    with task_lock(store, task_id, blocking=False):
        identifier = store.create_resource_claim(task_id, **options)
        _settle_tasks([task_id])
    return next(c for c in store.resource_claims(task_id) if c['claim_id'] == identifier)


@_synchronized
def release_resource_claim(task_id, claim_id):
    store = _task_store()
    with task_lock(store, task_id, blocking=False):
        store.release_resource_claim(task_id, claim_id)
        _settle_tasks([task_id])
    return next(c for c in store.resource_claims(task_id) if c['claim_id'] == claim_id)
