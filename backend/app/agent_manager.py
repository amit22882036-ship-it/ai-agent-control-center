from . import runtime_resources, dependencies
from .codex_events import StructuredCommand, Execution, CodexEvents
from . import delegation_protocol
from . import managed_orchestration
from . import planning, promotion, replanning
from .work_control import require_active, require_ancestors
import os
import logging
import re
import subprocess
import sys
import json
import sqlite3
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from functools import wraps
from contextlib import nullcontext, contextmanager, ExitStack
from threading import Lock, RLock, Thread, Timer, local
from threading import Thread as _RealThread  # identity check for watcher ownership
_RealPopen = subprocess.Popen  # tests replace Popen with doubles that cannot be supervised
from typing import Literal
from uuid import UUID, uuid4

from .agent_names import default_display_name, validate_display_name, validate_display_color
from .system_notifications import notifications
from .persistence import AgentStore
from .task_workspaces import TaskBusy, provision_task_workspace, task_lock
from . import orchestration_waker
from .workspace_freshness import evaluate_workspace_freshness, ensure_workspace_current, reject_divergence, session_context
from .project_domain import canonical_path
from .realtime import changes
from .output_history import OutputCache, RECENT_OUTPUT_LIMIT, OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT
from . import delegation_safety
from . import delegation_materialization
from . import delegation_continuation, delegation_orchestration
from .delegation_delivery import DeliveryService
from . import process_supervision
from . import recovery_actions
from . import agent_removal
from . import responsibility
from . import executions
from . import execution_recovery
from . import execution_diagnostics, execution_attestation
from . import providers, provider_events, model_catalog, allocation, delegation_v3, provider_policy, provider_onboarding
from . import delegations as delegations_module

logger = logging.getLogger(__name__)

AgentType = Literal["mock", "codex", "claude", "gemini"]
CodexSandbox = Literal["read-only", "workspace-write"]  # Every real provider's access policy

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
# Stage 2F: the selected model (None: the provider's default) and a waiting
# Agent's provider condition (closed-enum JSON, persisted with the Agent).
agent_models: dict[str, str | None] = {}
agent_conditions: dict[str, str | None] = {}
# Per-Agent default reasoning effort (None: the provider's default) and the
# (model, effort) its current provider session was last run with.
agent_efforts: dict[str, str | None] = {}
agent_session_configs: dict[str, tuple | None] = {}
# Automatic transient retries in flight (memory only: startup never launches work).
_provider_retry_attempts: dict[str, int] = {}
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
# Executions whose watcher ended without finalizing them (Phase 2.2A): visible,
# in-memory recovery condition; only an explicit operator action proceeds.
agent_watcher_lost: dict[str, str] = {}
WATCHER_LOST_MARKER = '--- Process watcher lost; explicit recovery required ---'
WATCHER_LOST_REASON = 'Process watcher ended without finalizing the exited execution; explicit recovery required'
UNVERIFIED_END_MARKER = ('--- Process tree end not verified: its Task and resources stay held. '
                         'See Process ownership in the inspector. ---')
SWEEP_INTERVAL_SECONDS = 30
_wake_suppression = local()
_always_handled_marker = "CONTROL_CENTER_ALWAYS_HANDLED"
agent_similar_decisions: dict[str, SimilarDecisions] = {}
_similar_handled_marker = "CONTROL_CENTER_SIMILAR_HANDLED"
_state_lock = RLock()
_data_lock = RLock()
_store: AgentStore | None = None
_shutting_down = False
# This backend instance's identity row (Stage 2D.1b): every journaled launch names it.
_controller: dict | None = None
# Set by _spawn_with_resources around Popen inside its admission transaction.
_launch = local()
# Event-driven diagnostics (Stage 2D.1b P3): lifecycle events queue execution
# ids; only the backend lifespan's worker observes them (never tests, never
# startup itself). Observations never settle anything.
_diagnostic_requests: dict[str, str] = {}
_diagnostic_wakeup = None
_DIAGNOSTIC_QUEUE_LIMIT = 256
# One per execution: every diagnostic of an execution and its attestation's fresh
# check plus settlement run under it, so no observation is in flight while an
# attestation decides (never held across a SQLite write while observing).
_execution_locks: dict[str, RLock] = {}
# Current-controller emergency ownership guard (Stage 2D.1b P1). It covers what
# the execution journal may have failed to record, and only while this backend
# runs: it is in memory, not durable. An entry is created inside the launch's
# admission transaction, before any process exists; every admission checks the
# guard inside its own admission transaction, and SQLite's write lock (BEGIN
# IMMEDIATE) serializes those transactions, so no admission can fall between
# discovering an unconfirmed end and blocking it. Entries end only with a
# verified successful resume or a verified termination, never with a journal
# write, a root exit, an output marker or a process scan.
_ownership_guard: dict[str, dict] = {}
_guard_lock = Lock()  # Dict mutation only; never held while waiting on anything else.
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


def _watcher_owns(process) -> bool:
    watcher = getattr(process, '_control_center_watcher', None)
    return isinstance(watcher, _RealThread) and (watcher.ident is None or watcher.is_alive())


def _auto_continuation_due(agent_id: str) -> bool:
    question = agent_waiting_questions[agent_id]
    if not (question and agent_sessions[agent_id] and agent_types[agent_id] == "codex"):
        return False
    always = agent_always_decisions[agent_id]
    if always.enabled:
        return not always.automatic_attempt or always.handled
    policy = agent_similar_decisions[agent_id]
    return bool(policy.enabled and policy.examples and question.strip() not in policy.attempted)


def _end_unverified(process) -> bool:
    """A supervised execution whose Job has not confirmed that no process remains."""
    job = process_supervision.job_of(process)
    return job is not None and job.ended is None


def _uncertainty_unpersisted(process) -> bool:
    """Its unverified end could not be journaled yet: only memory protects it."""
    return getattr(process, '_control_center_uncertainty_unpersisted', False) is True


STOP_UNRECORDED = ('Stop was not confirmed: the end of this execution could not be verified or recorded. '
                   'Its assignment and resources stay held; retry Stop when persistence is available.')


def _require_recorded_end(process):
    """Refuse to report a Stop whose unverified end is not durable (only memory protects it)."""
    if process is not None and _uncertainty_unpersisted(process):
        raise RuntimeError(STOP_UNRECORDED)


def _execution_live(process) -> bool:
    """Live for ownership: running, or exited with its tree's end unconfirmed or unrecorded."""
    return process is not None and (process.poll() is None or _end_unverified(process)
                                    or _uncertainty_unpersisted(process))


def _watcher_lost(process) -> bool:
    watcher = getattr(process, '_control_center_watcher', None)
    return isinstance(watcher, _RealThread) and watcher.ident is not None and not watcher.is_alive()


def _refresh_status_for_read(agent_id: str) -> None:
    """Read paths (GET) observe state and never launch work.

    A live watcher owns finalization, including Always/Similar continuation and
    delegation scheduling. Only an unwatched exit is settled here, and then
    without automatic continuation or scheduling; work that would start an
    execution is left untouched rather than consumed.

    A watcher that ended without finalizing an exited process has failed. No
    owner remains, so the failure is logged once and the visible status is
    settled without automatic continuation; the user can then reply or decide.
    Structured executions are only reported: their durable handoff needs the
    Phase 2 recovery work and must not run from a read.
    """
    process = agents[agent_id]
    if process is None or _watcher_owns(process):
        return
    if _watcher_lost(process):
        exited = process.poll() is not None and agent_statuses[agent_id] == 'running'
        if exited and getattr(process, '_control_center_watcher_failure_logged', False) is not True:
            process._control_center_watcher_failure_logged = True
            logger.error('Process watcher for agent %s ended without finalizing its exited process', agent_id)
        if not _structured(process):
            _finalize_process(agent_id, process, allow_auto=False, schedule=False)
        return
    if _structured(process) or _auto_continuation_due(agent_id):
        return
    _finalize_process(agent_id, process, allow_auto=False, schedule=False)


def _finalize_process(agent_id: str, process: subprocess.Popen | None, allow_auto: bool = True,
                      schedule: bool = True) -> None:
    # The reader must finish parsing buffered output before deciding the exit state.
    with _state_lock:
        if (process is None or agents.get(agent_id) is not process or agent_statuses[agent_id] != "running"
                or process.poll() is None or not process.stdout.closed
                or (_ordinary_stream(process) is not None and not process.stderr.closed)):
            return
        if _end_unverified(process):
            # The root exited, but its tree's end is unconfirmed: no completion,
            # no assignment closure, no release and no continuation. Only a
            # verified Stop (or Pause/Cancel/Remove) settles it.
            return
        _record_end(process, 'root_exit')  # Journal before the outcome is saved (idempotent).
        if _uncertainty_unpersisted(process):
            # The unverified end is not durable yet: no completion, assignment
            # closure, release or continuation. A later finalization retries it.
            return
        # An end verified only after the watcher could not confirm it is never
        # a successful completion: it settles as stopped.
        unverified = getattr(process, '_control_center_end_unverified', False) is True
        if unverified and _structured(process):
            _structured(process).revoked = True
        if _finish_structured(agent_id, process):
            return
        task_id = _agent_task_id(agent_id)
        controlled = _store.get_task(task_id) if task_id else None
        try:
            removing = _store is not None and _store.agent_removal_pending(agent_id)
        except (sqlite3.Error, OSError):
            logger.exception('Could not read removal state for %s; treating execution as gated', agent_id)
            removing = True
        if unverified or removing or (controlled and (controlled['control_intent'] != 'active' or controlled['stop_required'] or any(b['hard'] for b in controlled['active_blockers']))):
            # An admitted removal outranks a late exit (including success): no
            # completion, no automatic Always/Similar continuation.
            agent_statuses[agent_id] = 'stopped'
            if removing:
                # The persistence boundary clears it too; keep the mirror equal.
                agent_waiting_questions[agent_id] = None
            notifications.cancel(agent_id)
            _save_agent(agent_id)
            _settle_coordination()
            return
        failure = _settle_stream(agent_id, process, schedule=schedule) if _ordinary_stream(process) else None
        allow_auto = allow_auto and not _shutting_down and failure is None
        question = agent_waiting_questions[agent_id]
        policy = agent_similar_decisions[agent_id]
        always = agent_always_decisions[agent_id]
        if (allow_auto and question and always.enabled and agent_sessions[agent_id]
                and providers.supports(agent_types[agent_id], 'decisions')
                and (not always.automatic_attempt or always.handled)):
            # An unresolved automatic attempt ends this episode, even if reworded.
            always.automatic_attempt = True
            always.handled = False
            try:
                command = _agent_command(agent_id)
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
                and agent_sessions[agent_id] and providers.supports(agent_types[agent_id], 'decisions')
                and question.strip() not in policy.attempted):
            policy.attempted.add(question.strip())
            try:
                command = _agent_command(agent_id)
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
        if failure is not None:
            status = 'waiting' if failure['waiting'] else 'stopped'
        else:
            status = "waiting" if question else "finished"
        assignment_id = getattr(process, '_control_center_assignment', None)
        if isinstance(assignment_id, str) and status == 'finished':
            context = _structured(process)
            process._control_center_completion = dict(assignment_id=assignment_id,
                returncode=process.poll(), validated=process.poll() == 0,
                generation=context.origin.generation if context and context.origin else None)
            # Delegated failures must not masquerade as successfully completed work.
            if process.poll() != 0 and _store:
                with _store._connection() as db:
                    if db.execute('SELECT 1 FROM delegations WHERE child_task_id=?', (task_id,)).fetchone():
                        status = 'stopped'
        returncode = process.poll()
        if status == 'finished' and agent_types[agent_id] in providers.REAL and returncode != 0:
            # A failed provider run is not completion evidence: keep the Task
            # incomplete (and its dependents blocked) instead of finishing it.
            status = 'stopped'
            agent_outputs[agent_id].append(f'--- {providers.LABELS[agent_types[agent_id]]} exited with code '
                                           f'{returncode}; the Task was not completed ---')
        agent_statuses[agent_id] = status
        _save_agent(agent_id)
        _settle_coordination()
        if schedule:
            _wake_delegation_siblings(agent_id)
            _schedule_hybrid_for_agent(agent_id)
        try:
            context = _waiting_context(agent_id) if agent_statuses[agent_id] == 'waiting' else None
            if agent_statuses[agent_id] != 'waiting' or (context and context['kind'] == 'user'):
                notifications.transition(agent_id, agent_statuses[agent_id], agent_tasks[agent_id])
            else:
                notifications.cancel(agent_id)
        except Exception:
            logger.exception("Could not schedule native notification")


RETRY_DELAYS = (30, 120)  # Bounded automatic retries of a transient failure before any work.


def _iso_after(seconds):
    moment = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + seconds, timezone.utc)
    return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _condition_value(agent_id):
    try:
        value = json.loads(agent_conditions.get(agent_id) or 'null')
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _record_provider_block(provider, condition, detail, agent_id, process, *, reset_at=None, window_id=None):
    """A known limit blocks this controller at once (memory), then durably once written."""
    if condition not in providers.BLOCKING:
        return
    providers.note_block(_store._connection if _store is not None else None, provider, condition, detail,
                         agent_id=agent_id, execution_id=getattr(process, '_control_center_execution_id', None),
                         reset_at=reset_at, window_id=window_id)


def _record_stream_windows(provider, stream):
    """Provider-reported limit observations of a run (documented structured events only)."""
    if _store is None or not getattr(stream, 'rate_limits', None):
        return
    windows = [w for w in (providers.window_from_claude_event(info, info.get('observed_at'))
                           for info in stream.rate_limits) if w]
    if not windows:
        return
    try:
        with _store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, provider, windows[-1:], source='claude_rate_limit_event')
    except (sqlite3.Error, OSError, ValueError):
        logger.exception('Could not record usage observation for %s', provider)


def _settle_stream(agent_id, process, *, schedule=True):
    """Classify an ordinary Claude Code / Gemini CLI execution from its structured stream.

    Success: waiting and decision markers are read from the final assistant
    message only. A provider-reported limit, sign-in or availability failure
    keeps the Agent, Task, assignment, output and provider session and waits;
    other failures stop without completing the Task. Never substitutes another
    provider or model.
    """
    stream = _ordinary_stream(process)
    provider = agent_types[agent_id]
    label = providers.LABELS[provider]
    result = stream.outcome(process.poll())
    if result['success'] and provider == 'gemini':
        requested = (getattr(process, '_control_center_config', {}) or {}).get('model')
        state, note = providers.model_enforcement(provider, requested, getattr(stream, 'models', ()),
                                                  getattr(stream, 'initial_model', None))
        if state not in ('verified', 'routed'):
            # An explicit selection is never completed as model-verified without evidence.
            # Fail closed: work done by a model the user did not select never completes the
            # Task silently. The Agent waits; the user reviews and decides how to continue.
            agent_waiting_questions[agent_id] = None
            agent_conditions[agent_id] = json.dumps(dict(
                kind='model_not_enforced', provider=provider, model=requested, condition='model_not_enforced',
                reset_at=None, resume_available=True, blocking=[], observed_at=providers._now(),
                session_preserved=agent_sessions[agent_id] is not None, detail=note), separators=(',', ':'))
            agent_outputs[agent_id].append(f'--- {note} The Task was not completed; review the output, then resume, '
                                           'or choose another model. ---')
            return {'waiting': True, 'condition': 'model_not_enforced'}
    if result['success']:
        agent_conditions[agent_id] = None
        _record_stream_windows(provider, stream)
        for line in result['message'].splitlines():
            parsed = _ansi.sub('', line).strip()
            if parsed.startswith(_waiting_marker) and parsed[len(_waiting_marker):].strip():
                agent_waiting_questions[agent_id] = parsed[len(_waiting_marker):].strip()
            if parsed == _similar_handled_marker and agent_similar_decisions[agent_id].automatic_question:
                agent_similar_decisions[agent_id].handled = True
            if parsed == _always_handled_marker and agent_always_decisions[agent_id].automatic_attempt:
                agent_always_decisions[agent_id].handled = True
        return None
    condition, detail = result['condition'], (result['detail'] or 'unknown error')[:300]
    _record_stream_windows(provider, stream)
    window = None
    if getattr(stream, 'limit_rejected', False):
        rejected = [w for w in (providers.window_from_claude_event(info) for info in stream.rate_limits)
                    if w and w['status'] == 'exhausted']
        window = rejected[-1]['window_id'] if rejected else 'active_limit'
    _record_provider_block(provider, condition, detail, agent_id, process, reset_at=stream.reset_at, window_id=window)
    providers.invalidate(provider)
    before_work = not stream.tool_activity and stream.message is None
    attempt = _provider_retry_attempts.pop(agent_id, 0)
    # A limit reported before any session existed still waits (finding 2): the user
    # can continue later in a new session with the Task as context.
    waiting = condition in providers.WAITING and providers.supports(provider, 'session_resume')
    if condition == providers.MODEL_UNAVAILABLE:
        requested = getattr(process, '_control_center_config', {}).get('model')
        if requested:
            model_catalog.record_denial(_store._connection if _store else None, provider, requested, detail,
                                        execution_id=getattr(process, '_control_center_execution_id', None))
    if not waiting:
        agent_conditions[agent_id] = None
        agent_outputs[agent_id].append(f'--- {label} execution did not complete ({condition}): {detail}. '
                                       'The Task was not completed; no other provider or model was tried. ---')
        return {'waiting': False, 'condition': condition}
    retry = (condition == providers.PROVIDER_UNAVAILABLE and before_work and attempt < len(RETRY_DELAYS)
             and schedule and not _shutting_down)
    retry_at = _iso_after(RETRY_DELAYS[attempt]) if retry else None
    kind = providers.WAITING[condition]
    with _provider_db() as db:
        blocking = [{k: c.get(k) for k in ('label', 'reset_at', 'window_id')}
                    for c in providers.constraints(db, provider, agent_models.get(agent_id))][:8]
    agent_conditions[agent_id] = json.dumps(dict(
        kind=kind, provider=provider, model=agent_models.get(agent_id), condition=condition,
        reset_at=stream.reset_at, resume_available=True, retry_at=retry_at, blocking=blocking,
        observed_at=providers._now(), session_preserved=agent_sessions[agent_id] is not None),
        separators=(',', ':'))
    if retry:
        _provider_retry_attempts[agent_id] = attempt + 1
    agent_outputs[agent_id].append(f'--- {label}: {providers.WAITING_LABELS[kind]} ({detail}). The Agent, Task, '
                                   'output and provider session are kept; continue once the provider is available. ---')
    if retry:
        agent_outputs[agent_id].append(f'--- Automatic retry {attempt + 1} of {len(RETRY_DELAYS)} scheduled '
                                       f'for {retry_at} (transient failure before any work) ---')
        _start_timer(RETRY_DELAYS[attempt], lambda: _automatic_provider_retry(agent_id, process, attempt + 1))
    return {'waiting': True, 'condition': condition}


def _automatic_provider_retry(agent_id, failed_process, attempt):
    with _state_lock:
        return _provider_retry(agent_id, failed_process, attempt)


def _provider_retry(agent_id, failed_process, attempt):
    """One bounded retry; only while nothing changed since the transient failure was recorded."""
    if _shutting_down or agents.get(agent_id) is not failed_process or agent_statuses.get(agent_id) != 'waiting':
        return
    value = _condition_value(agent_id)
    if value.get('condition') != providers.PROVIDER_UNAVAILABLE or _provider_retry_attempts.get(agent_id) != attempt:
        return
    if _execution_live(failed_process):
        return
    try:
        command = _agent_command(agent_id)  # Rechecks readiness, enablement, billing and usage.
        _resume_agent(agent_id, command, 'The provider was temporarily unavailable before any work was done. '
                      'Continue the assigned work.', '--- Automatic Provider Retry ---',
                      history_text=f'Attempt {attempt} of {len(RETRY_DELAYS)}', automatic_retry=True)
    except (OSError, RuntimeError, ValueError, LookupError, sqlite3.Error) as exc:
        logger.info('Automatic provider retry for %s was not admitted: %s', agent_id, exc)
        agent_outputs[agent_id].append(f'--- Automatic retry {attempt} was not admitted: {exc} ---')
        _save_agent(agent_id)


def _watch_process(agent_id: str, process: subprocess.Popen, reader: Thread) -> None:
    # Separate from the reader: Redirect/Stop can join the reader while holding
    # the state lock without deadlocking this finalizer.
    job = process_supervision.job_of(process)
    if job is not None:
        # The execution ends with its main process: terminating the Job ends
        # any remaining descendants (which also releases inherited output
        # pipes) and confirms that none remain before the handle is closed.
        process.wait()
        try:
            job.terminate()
        except OSError:
            # Unconfirmed: keep the handle so a Stop can still verify, and keep
            # the Agent running so every durable gate and ownership stays held.
            # Closing a named Job proves nothing if another handle exists.
            process._control_center_end_unverified = True
            process._control_center_guard = _guard_unverified(
                None, agent_id, getattr(process, '_control_center_task_id', None), reason='termination_unconfirmed',
                execution_id=getattr(process, '_control_center_execution_id', None), root_pid=process.pid,
                job_name=job.name)
            logger.exception('Could not confirm the end of execution tree of %s', agent_id)
            _record_end(process, 'root_exit')
            with _data_lock:
                agent_outputs[agent_id].append(UNVERIFIED_END_MARKER)  # saved with the next write
        else:
            job.close()
    reader.join()
    process.wait()
    _record_end(process, 'root_exit')
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


def _save_agent(agent_id: str, emit=True, *, activation=None) -> bool:
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
                context = _structured(agents.get(agent_id))
                if context and context.origin and not _store.execution_can_save(context.origin):
                    return False
                assignment_options = {}
                if activation is not None:
                    assignment_options['activation'] = activation
                if context and context.origin:
                    assignment_options['execution_origin'] = context.origin
                evidence = getattr(agents.get(agent_id), '_control_center_completion', None)
                if isinstance(evidence, dict):
                    assignment_options['completion_evidence'] = evidence
                pending_task = getattr(cache, 'assignment_task_id', None)
                if pending_task is not None:
                    assignment_options['assignment_task_id'] = pending_task
                execution_id = getattr(agents.get(agent_id), '_control_center_execution_id', None)
                if isinstance(execution_id, str):
                    assignment_options['execution_id'] = execution_id
                _store.save_agent({
                    "agent_id": agent_id, "parent_id": agent_parents[agent_id],
                    "task": agent_tasks[agent_id],
                    "display_name": agent_names[agent_id], "agent_type": agent_types[agent_id],
                    "model": agent_models.get(agent_id), "provider_condition": agent_conditions.get(agent_id),
                    "effort": agent_efforts.get(agent_id),
                    "session_config": json.dumps(agent_session_configs[agent_id]) if agent_session_configs.get(agent_id) else None,
                    "display_color": agent_colors.get(agent_id, "neutral"),
                    "sandbox": agent_sandboxes[agent_id], "status": agent_statuses[agent_id],
                    "session_id": agent_sessions[agent_id], "waiting_question": agent_waiting_questions[agent_id],
                    "similar_decisions_enabled": similar.enabled, "similar_examples": similar.examples.copy(),
                    "always_decide_enabled": always.enabled, "always_decide_configured": always.configured,
                }, output_entries=cache.pending, **assignment_options)
                cache.assignment_task_id = None
                with _store._connection() as db:
                    row = db.execute('''SELECT g.status FROM agents g JOIN task_assignments a USING(agent_id)
                        JOIN delegation_continuations c ON c.task_id=a.task_id
                        WHERE g.agent_id=? AND a.ended_at IS NULL''', (agent_id,)).fetchone()
                    if row:
                        agent_statuses[agent_id] = row['status']
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
    global _store, _shutting_down, _waker, _controller
    if any(process is not None for process in agents.values()):
        raise RuntimeError("Cannot reload persistence while managed processes are attached")
    store = AgentStore(path)
    controller = _new_controller()
    # Serialized: a live (or unverifiable) earlier controller refuses this one.
    store.claim_controller(controller)
    # Previous controllers' executions are decided from kernel and reboot
    # evidence before anything reads them; nothing is launched or reattached.
    # Blockers follow at reconciliation, after running Agents are marked stopped.
    execution_recovery.resolve(store, controller, sync_blockers=False)
    execution_recovery.classify_legacy(store, controller)
    _diagnostic_requests.clear()
    with store._connection() as db:
        unresolved = [r['execution_id'] for r in db.execute(f'SELECT execution_id FROM executions WHERE {executions._BLOCKING}')]
    for execution_id in unresolved:
        request_diagnostics(execution_id, 'controller_restart')  # observed later by the lifespan worker
    from .integrations import recover_integrations
    recover_integrations(store)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        provider_onboarding.recover(db)  # A setup operation cut off by a restart has an unknown outcome.
    provider_onboarding.accept_operations()  # This backend runs again (after an earlier shutdown in-process).
    store.reconcile_task_recovery()
    records = store.load_agents(output_limit=RECENT_OUTPUT_LIMIT)
    with _data_lock:
        for registry in (agents, agent_parents, agent_statuses, agent_outputs, agent_tasks, agent_names, agent_colors, agent_types,
                         agent_sandboxes, agent_sessions, agent_waiting_questions, agent_readers,
                         agent_similar_decisions, agent_always_decisions, agent_watcher_lost,
                         agent_models, agent_conditions, _provider_retry_attempts, agent_efforts, agent_session_configs):
            registry.clear()
        providers.invalidate()
        _store = store
        _controller = controller
        _shutting_down = False
        _waker = _new_waker(store)
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
            agent_models[key] = record.get('model')
            agent_conditions[key] = record.get('provider_condition')
            agent_efforts[key] = record.get('effort')
            try:
                agent_session_configs[key] = tuple(json.loads(record['session_config'])) if record.get('session_config') else None
            except (ValueError, TypeError):
                agent_session_configs[key] = None
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
    if not failures and _store is not None and _controller is not None:
        try:
            _store.controller_stopped(_controller['instance_id'])  # Evidence of a clean stop only.
        except (sqlite3.Error, OSError):
            logger.exception('Could not record controller shutdown')
    return failures


def _provider_db():
    return _store._connection() if _store is not None else nullcontext(None)


def _usage_override():
    """The limits an explicit user retry of this thread's resume may override (never hard ones)."""
    return tuple(getattr(_launch, 'usage_override', ()) or ())


def _execution_config():
    return getattr(_launch, 'execution_config', None) or {}


def _require_launchable(provider, *, model=None, sandbox='read-only', managed=False, child=False, effort=None):
    """Readiness, enablement, billing, model, effort, usage and capability: before any reservation or Popen."""
    if _store is not None:
        providers.reconcile(_store._connection)  # Limits whose earlier write failed.
        if provider in providers.REAL and (model is not None or effort is not None):
            # Untrustworthy (stale or other-installation) catalog evidence is refreshed first.
            model_catalog.ensure_fresh(_store._connection, provider)
    config = _execution_config()
    with _provider_db() as db:
        task_id, _launch.policy_task_id = getattr(_launch, 'policy_task_id', None), None  # Used once.
        if db is not None:
            provider_policy.require_allowed(db, provider, task_id)
            provider_onboarding.require_no_maintenance(db, provider)  # Early; rechecked at the final admission.
        state = providers.require_launchable(db, provider, model=model, sandbox=sandbox, managed=managed, child=child,
                                             override=_usage_override(), effort=effort)
        if provider in providers.REAL and db is not None:
            model_catalog.admission(db, provider, model, effort, explicit_attempt=bool(config.get('attempt')),
                                    automated=bool(getattr(_launch, 'automated', False)))
        return state


class ValidatedCommand(str):
    """A Codex command built after readiness: carries the policy the admission compares again
    and the (model, effort) it was validated for."""
    policy = None
    resolved = None


class ValidatedStructuredCommand(StructuredCommand):
    policy = None
    resolved = None


def _note_resolved(model, effort, sources=None):
    """This execution's configuration as actually requested (legacy environment defaults included)."""
    config = dict(_execution_config())
    config['model'], config['effort'] = model, effort
    environment = sorted(key for key, source in (sources or {}).items() if source == 'environment')
    if environment:
        config['environment'] = environment
    _launch.execution_config = config


def _codex_command(sandbox: CodexSandbox = "read-only", session_id: str | None = None, *, structured=False,
                   model=None, effort=None) -> str:
    # Selection, else the legacy CONTROL_CENTER_CODEX_MODEL / _REASONING_EFFORT, else
    # Codex's own default: resolved once, so admission validates exactly what runs.
    model, effort, sources = providers.CODEX.resolve_configuration(model, effort)
    _note_resolved(model, effort, sources)
    _require_launchable('codex', model=model, sandbox=sandbox, managed=structured, effort=effort)
    result = providers.CODEX.command(sandbox, session_id, structured=structured, model=model, effort=effort)
    command = ValidatedStructuredCommand(result) if structured else ValidatedCommand(result)
    command.policy = providers.policy_snapshot('codex')
    command.resolved = (model, effort)
    return command


class ProviderCommand(str):
    """A launch command of a stream-JSON provider (Claude Code, Gemini CLI)."""
    provider: str = ''
    structured: bool = False
    policy = None
    resolved = None


def _provider_command(provider, sandbox='read-only', session_id=None, *, structured=False, model=None,
                      child=False, effort=None):
    """The launch command of any provider, after its readiness and policy checks."""
    if provider == 'codex':
        # The historical call shapes of the Codex builder are kept exactly.
        options = {'structured': True} if structured else {}
        if model is not None:
            options['model'] = model
        if effort is not None:
            options['effort'] = effort
        return _codex_command(*((sandbox,) if session_id is None else (sandbox, session_id)), **options)
    if provider == 'mock':
        providers.adapter('mock').classify_model(model)
        if effort is not None:
            raise ValueError('Mock agents do not accept an effort level.')
        return [sys.executable, str(Path(__file__).resolve().parent.parent / "mock_agent.py")]
    _require_launchable(provider, model=model, sandbox=sandbox, managed=structured and provider != 'gemini',
                        child=child, effort=effort)
    command = ProviderCommand(providers.adapter(provider).command(sandbox, session_id, structured=structured,
                                                                  model=model, effort=effort))
    command.provider, command.structured = provider, structured
    command.policy = providers.policy_snapshot(provider)
    command.resolved = (model, effort)
    return command


@contextmanager
def _prompt_config(config):
    """One logical instruction's configuration (a next-prompt override never changes Agent defaults)."""
    previous = (getattr(_launch, 'prompt_config', None), getattr(_launch, 'continuity', None),
                getattr(_launch, 'execution_config', None), getattr(_launch, 'policy_task_id', None))
    _launch.prompt_config, _launch.continuity, _launch.execution_config, _launch.policy_task_id = config or {}, None, None, None
    try:
        yield
    finally:
        (_launch.prompt_config, _launch.continuity, _launch.execution_config,
         _launch.policy_task_id) = previous


def _effective_config(agent_id):
    """Explicit next-prompt override > Agent default (the adapter adds legacy and provider defaults)."""
    config = getattr(_launch, 'prompt_config', None) or {}
    model = config.get('model') if config.get('model_set') else agent_models.get(agent_id)
    effort = config.get('effort') if config.get('effort_set') else agent_efforts.get(agent_id)
    source = 'override' if config.get('model_set') or config.get('effort_set') else 'agent_default'
    return model, effort, source, bool(config.get('attempt_unverified')), bool(config.get('confirm_new_session'))


def _agent_command(agent_id, *, structured=False, session_id=None, fresh=False):
    """Continue an Agent within its Task's provider permissions (replies, redirects, resumes).

    The Task id is scoped to this one command: whatever path the command takes, it is
    restored afterwards, so it can never apply to a later, unrelated launch.
    """
    previous = getattr(_launch, 'policy_task_id', None)
    _launch.policy_task_id = _agent_task_id(agent_id)
    try:
        return _agent_command_for(agent_id, structured=structured, session_id=session_id, fresh=fresh)
    finally:
        _launch.policy_task_id = previous


def _agent_command_for(agent_id, *, structured=False, session_id=None, fresh=False):
    """Continue an Agent with its own provider, model, effort, access policy and session.

    A model or effort the provider cannot apply to the existing session needs the
    user's explicit confirmation of a new session (with the Task as context); it
    is never switched silently.
    """
    provider = agent_types[agent_id]
    model, effort, source, attempt, confirm = _effective_config(agent_id)
    session = None if fresh else (session_id if session_id is not None else agent_sessions[agent_id])
    if session:
        previous = agent_session_configs.get(agent_id) or (agent_models.get(agent_id), agent_efforts.get(agent_id))
        with _provider_db() as db:
            compatible = model_catalog.session_compatible(db, provider, previous, (model, effort))
        if not compatible:
            if not confirm:
                raise ValueError(f'{providers.LABELS[provider]} cannot continue its current session with this model '
                                 'or effort. Confirm a new session: it starts with the Task and workspace as context, '
                                 'and the previous session memory is not carried over.')
            session = None
            _launch.continuity = 'configuration_change'
    _launch.execution_config = dict(model=model, effort=effort, source=source, attempt=attempt)
    return _provider_command(provider, agent_sandboxes[agent_id] or 'read-only', session,
                             structured=structured, model=model, effort=effort)


def _stream_command(command):
    return isinstance(command, ProviderCommand)


def _continuity_prompt(agent_id, reason, previous_question):
    why = {'configuration_change': 'The model or effort changed in a way this provider cannot apply to the existing '
                                   'session, so this is a new session.',
           'no_session': 'The previous attempt ended before the provider started a session, so this is a new session.'
           }.get(reason, 'This is a new provider session.')
    note = (f'Control Center continuity note: {why} The previous session memory is not available. The Task below is '
            'authoritative, and the current directory is the Task workspace with all work done so far: inspect it '
            f'before continuing.\n\nAssigned Task:\n{agent_tasks[agent_id]}\n')
    if previous_question:
        note += f'\nThe previous run was waiting on this question: {json.dumps(previous_question)}\n'
    return note + '\n'


def _record_effective(agent_id, process, events):
    """What the provider reported it actually ran (Claude init model, Gemini result stats)."""
    execution_id = getattr(process, '_control_center_execution_id', None)
    if _store is None or not isinstance(execution_id, str) or not (
            getattr(events, 'models', None) or getattr(events, 'initial_model', None)):
        return
    reported = sorted(events.models)
    config = getattr(process, '_control_center_config', {}) or {}
    note = None
    try:
        with _store._connection() as db:
            row = db.execute('SELECT model,resolved_model FROM executions WHERE execution_id=?', (execution_id,)).fetchone()
            expected = row['resolved_model'] if row else None
            if getattr(events, 'provider', None) == 'gemini':
                _, note = providers.model_enforcement('gemini', config.get('model'), reported,
                                                      getattr(events, 'initial_model', None))
            elif expected and any(model != expected for model in reported):
                note = ('The provider reported running ' + ', '.join(reported) + f' although {expected} was requested; '
                        'the requested model was not enforced.')
            elif config.get('model') == 'auto':
                note = 'Automatic model routing chose: ' + ', '.join(reported)
            elif len(reported) > 1:
                note = 'The provider reported several models for this run: ' + ', '.join(reported)
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE executions SET effective_model=?,effective_note=? WHERE execution_id=?',
                       (', '.join(reported)[:300] or None, note, execution_id))
    except (sqlite3.Error, OSError):
        logger.exception('Could not record the effective model of %s', execution_id)
    if note and 'not enforced' in note and getattr(events, 'provider', None) != 'gemini':
        with _data_lock:
            agent_outputs[agent_id].append('--- ' + note + ' ---')


def latest_execution_config(agent_id):
    if _store is None:
        return None
    with _store._connection() as db:
        row = db.execute("""SELECT execution_id,provider,model,resolved_model,requested_effort,effective_model,
            effective_effort,effective_note,config_source,entitlement_state,catalog_revision,configured_at,created_at
            FROM executions WHERE agent_id=? ORDER BY rowid DESC LIMIT 1""", (agent_id,)).fetchone()
        return dict(row) if row else None


@_synchronized
def update_agent_defaults(agent_id, *, model=None, effort=None, model_set=False, effort_set=False,
                          attempt_unverified=False):
    """Change one Agent's own default model and/or effort (same provider). Applies from its next instruction.

    In-flight executions are never changed; a default the current session cannot
    apply needs a confirmed new session at the next instruction. History is kept.
    """
    if agent_id not in agents:
        return None
    provider = agent_types[agent_id]
    if provider == 'mock':
        raise ValueError('Mock agents have no model or effort.')
    new_model = providers.validate_model(model) if model_set else agent_models.get(agent_id)
    new_effort = providers.validate_effort(effort) if effort_set else agent_efforts.get(agent_id)
    if (new_model, new_effort) == (agent_models.get(agent_id), agent_efforts.get(agent_id)):
        raise ValueError('The Agent already uses this model and effort by default.')
    store = _task_store()
    if new_model is not None or new_effort is not None:
        model_catalog.ensure_fresh(store._connection, provider, trigger='defaults')
    with store._connection() as db:
        model_catalog.admission(db, provider, new_model, new_effort, explicit_attempt=attempt_unverified)
        db.execute('BEGIN IMMEDIATE')
        assignment = db.execute('SELECT assignment_id FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                                (agent_id,)).fetchone()
        db.execute("""INSERT INTO agent_provider_changes(agent_id,assignment_id,from_provider,from_model,from_sandbox,
            from_session_id,to_provider,to_model,to_sandbox,session_retained,boundary,handoff_pending,from_effort,to_effort)
            VALUES (?,?,?,?,?,?,?,?,?,1,'defaults',0,?,?)""",
            (agent_id, assignment[0] if assignment else None, provider, agent_models.get(agent_id), agent_sandboxes[agent_id],
             agent_sessions[agent_id], provider, new_model, agent_sandboxes[agent_id], agent_efforts.get(agent_id), new_effort))
    agent_models[agent_id], agent_efforts[agent_id] = new_model, new_effort
    agent_outputs[agent_id].append(f'--- Agent defaults changed: model {new_model or "provider default"}, effort '
                                   f'{new_effort or "provider default"} (from the next instruction) ---')
    if not _save_agent(agent_id):
        raise RuntimeError('The defaults change was recorded but the Agent could not be saved; please retry.')
    return get_agent(agent_id, include_output=False)


# ---------------------------------------------------------------- Auto allocation (user policy, read views)

def auto_allocation_view(task_id):
    """The effective Auto policy of a Task (own or inherited) and its Children's decisions."""
    store = _task_store()
    with store._connection() as db:
        dependencies.task(db, task_id)
        policy = allocation.policy_for(db, task_id)
        own = allocation.own_policy(db, task_id)
        owner = dependencies.task(db, policy['task_id']) if policy else None
        children = [dict(proposal_id=r['proposal_id'], child_task_id=r['child_task_id'], disposition=r['disposition'],
                         reason=r['reason'], allocation=r['allocation'], origin='promotion',
                         history=allocation.history(db, r['delegation_id']))
                    for r in promotion.inspect(db, task_id) if r.get('allocation')]
        promoted = {c['child_task_id'] for c in children}
        for record in delegations_module.list_for_task(db, task_id):
            decision = allocation.summary(db, record['delegation_id'])
            if decision and decision.get('origin') == 'delegation':
                children.append(dict(proposal_id=record['request_key'], child_task_id=record['child_task_id'],
                                     disposition='ready' if decision['status'] == 'selected' else 'blocked',
                                     reason=record.get('admission_error') or '', allocation=decision, origin='delegation',
                                     history=allocation.history(db, record['delegation_id'])))
    return {'task_id': task_id, 'policy': policy, 'inherited': bool(policy and policy['task_id'] != task_id),
            'own_revision': own['revision'] if own else None,
            'owner_title': owner['title'] if owner else None,
            'categories': list(allocation.CATEGORIES), 'levels': list(allocation.LEVELS),
            'provider_default': allocation.PROVIDER_DEFAULT, 'children': children}


def set_auto_allocation_policy(task_id, **values):
    """The user's explicit Auto policy for a Task subtree (the only way Auto is authorized)."""
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        dependencies.task(db, task_id)
        allocation.set_policy(db, task_id, **values)
    _emit_agent_change(None)
    _schedule_materialization(task_id)  # Held Auto items are reconsidered by the existing pass.
    return auto_allocation_view(task_id)


def clear_auto_allocation_policy(task_id, **values):
    """Remove the Task's own policy; it then follows an ancestor's policy, if any."""
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        dependencies.task(db, task_id)
        allocation.clear_policy(db, task_id, **values)
    _emit_agent_change(None)
    _schedule_materialization(task_id)
    return auto_allocation_view(task_id)


def _allocation_fields(agent_id):
    if _store is None:
        return {}
    task_id = _agent_task_id(agent_id) or next(iter(_store.agent_task_ids(agent_id) or []), None)
    if task_id is None:
        return {}
    with _store._connection() as db:
        row = db.execute('SELECT delegation_id FROM delegations WHERE child_task_id=?', (task_id,)).fetchone()
        decision = allocation.summary(db, row[0]) if row else None
        policy = allocation.policy_for(db, task_id)
        permission = provider_policy.view(db, agent_types.get(agent_id), task_id)
    return {'allocation': decision, 'allocation_task_id': task_id, 'provider_permission': permission,
            'auto_allocation': {k: policy[k] for k in ('task_id', 'enabled', 'providers')} if policy else None}


# ---------------------------------------------------------------- Stage 2G: provider permissions and onboarding

def provider_policy_view():
    """The global allowlist and new-root defaults, with any problem of a saved default."""
    store = _task_store()
    with store._connection() as db:
        policy = provider_policy.global_policy(db)
        if policy is None:
            raise RuntimeError('Provider permissions are unavailable; the database has not been migrated.')
        policy['default_problem'] = provider_policy.default_problem(
            db, policy, policy['default_provider'], policy['default_model'], policy['default_effort'])
    return policy


def set_provider_policy(**values):
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        provider_policy.set_global(db, **values)
    _emit_agent_change(None)
    return provider_policy_view()


def task_provider_restrictions(task_id):
    store = _task_store()
    with store._connection() as db:
        dependencies.task(db, task_id)
        allowed, sources = provider_policy.effective(db, task_id)
        inherited, _ = provider_policy.inherited(db, task_id)
        own = provider_policy.own_restriction(db, task_id)
    return {'task_id': task_id, 'own': own, 'effective': allowed, 'inherited': inherited, 'sources': sources}


def set_task_provider_restrictions(task_id, providers_list, **version):
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        dependencies.task(db, task_id)
        provider_policy.set_restriction(db, task_id, providers_list, **version)
    _emit_agent_change(None)
    return task_provider_restrictions(task_id)


def clear_task_provider_restrictions(task_id, **version):
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        dependencies.task(db, task_id)
        provider_policy.clear_restriction(db, task_id, **version)
    _emit_agent_change(None)
    return task_provider_restrictions(task_id)


def onboarding_view():
    """Everything the AI Providers view shows. Reads cached state only: nothing is probed here."""
    store = _task_store()
    with store._connection() as db:
        policy = provider_policy.global_policy(db)
        result = []
        for provider in providers.IDS:
            status = providers.status(db, provider, probe_installation=False)
            entry = {'provider': provider, 'status': status}
            if provider in providers.REAL:
                entry.update(discovery=provider_onboarding.discover(provider),
                             operations=provider_onboarding.latest_operations(db, provider),
                             active_work=provider_onboarding.active_work(db, provider),
                             allowed=bool(policy and provider in policy['allowed']),
                             catalog=model_catalog.catalog_view(db, provider, status['state']['state'])['refresh'])
            result.append(entry)
    return {'providers': result, 'updates': provider_onboarding.update_view(store._connection),
            'policy': provider_policy_view() if policy else None}


def plan_provider_operation(provider, action, channel=None):
    with _task_store()._connection() as db:
        return provider_onboarding.propose(db, provider, action, channel)


def start_provider_operation(provider, plan_id, digest):
    # Owned by this backend's controller identity, so a later backend can tell whether it is gone.
    return provider_onboarding.start(_task_store()._connection, provider, plan_id, digest,
                                     publish=lambda: _emit_agent_change(None), controller=_controller_identity())


def resolve_provider_operation(operation_id, confirm_ended):
    result = provider_onboarding.resolve(_task_store()._connection, operation_id, confirm_ended=confirm_ended)
    _emit_agent_change(None)
    return result


def provider_operation(operation_id):
    return provider_onboarding.operation(_task_store()._connection, operation_id)


def cancel_provider_operation(operation_id):
    return provider_onboarding.cancel(_task_store()._connection, operation_id)


def check_provider_updates():
    result = provider_onboarding.check_updates(_task_store()._connection, force=True)
    _emit_agent_change(None)
    return result


def provider_notifications():
    if _store is None:
        return []
    return provider_onboarding.notifications(_store._connection)


def dismiss_provider_notification(provider, version, mode):
    result = provider_onboarding.dismiss(_task_store()._connection, provider, version, mode)
    _emit_agent_change(None)
    return result


def start_update_scheduler():
    if _store is None:
        return None
    return provider_onboarding.UpdateScheduler(_store._connection, lambda: _emit_agent_change(None)).start()


def model_catalog_view(provider, *, refresh=False, trigger='user'):
    providers.adapter(provider)
    store = _task_store()
    if refresh and provider in providers.REAL:
        with store._connection() as db:
            enabled = providers.settings(db, provider)['enabled']
        # A disabled provider is discovered only on the user's explicit request.
        if enabled or trigger == 'user':
            model_catalog.refresh(store._connection, provider, force=trigger == 'user', trigger=trigger)
    with store._connection() as db:
        state = providers.evaluate(db, provider, probe_installation=False).state
        result = model_catalog.catalog_view(db, provider, provider_state=state)
    if refresh:
        _emit_agent_change(None)
    return result


# ---------------------------------------------------------------- Stage 2F provider operations

def provider_statuses(*, check=False):
    """Every provider's support, readiness, settings and usage. Probes run only when `check` is set."""
    if _store is not None:
        providers.reconcile(_store._connection)
    with _provider_db() as db:
        result = [providers.status(db, provider, force=check, probe_installation=check) for provider in providers.IDS]
    if check:
        _reevaluate_updates()
    return result


def _reevaluate_updates(provider_ids=None):
    """Fresh version evidence re-compares recorded release metadata (no registry read)."""
    if _store is None:
        return
    try:
        if provider_onboarding.reevaluate_updates(_store._connection, provider_ids):
            _emit_agent_change(None)
    except Exception:
        logger.exception('Recorded update comparisons could not be refreshed')


def _refresh_catalog_quietly(provider, trigger):
    try:
        model_catalog.refresh(_task_store()._connection, provider, trigger=trigger)
    except providers.ProviderRefused as exc:
        logger.info('Model discovery for %s was not refreshed (%s)', provider, exc)


def check_provider(provider):
    """Explicit Check again: a fresh, non-billable probe. Ready only on positive evidence."""
    providers.adapter(provider)
    found = providers.probe(provider, force=True)
    if found.ready and provider in providers.REAL and _store is not None:
        _refresh_catalog_quietly(provider, 'sign_in_checked')
    with _provider_db() as db:
        if db is not None and found.ready and found.authenticated is True:
            # A verified sign-in is new evidence for an earlier authentication failure only.
            db.execute('BEGIN IMMEDIATE')
            providers.clear_blocks(db, provider, by='provider_signal', conditions=(providers.AUTH_FAILED,))
            db.commit()
        result = providers.status(db, provider, probe_installation=False)
    if provider in providers.REAL:
        _reevaluate_updates([provider])
    _emit_agent_change(None)
    return result


def update_provider_settings(provider, *, enabled=None, billing_confirmed=None, models=None):
    store = _task_store()
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        providers.update_settings(db, provider, enabled=enabled, billing_confirmed=billing_confirmed, models=models)
    if enabled and provider in providers.REAL:
        _refresh_catalog_quietly(provider, 'provider_enabled')
    with store._connection() as db:
        result = providers.status(db, provider, probe_installation=False)
    _emit_agent_change(None)
    return result


def allow_provider_work(provider):
    """Resolve recorded provider limits by the user's decision. Resumes nothing.

    Limits with a provider-reported reset still in the future, limits not yet
    durable and fresh exhausted observations stay: clearing them would bypass
    known exhaustion without new evidence. A waiting Agent resumes only through
    its own explicit Resume.
    """
    store = _task_store()
    if provider not in providers.REAL:
        raise ValueError('Only real providers have usage limits')
    providers.reconcile(store._connection)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        hard = [c for c in providers.constraints(db, provider) if c['hard']]
        if hard:
            raise providers.ProviderRefused(provider, providers.USAGE_LIMITED, providers.describe_constraints(hard),
                                            'This limit cannot be resolved yet.')
        cleared = providers.clear_blocks(db, provider, by='user', reason='user_resolved')
    with store._connection() as db:
        result = providers.status(db, provider, probe_installation=False)
    _emit_agent_change(None)
    return {**result, 'cleared': cleared}


def refresh_provider_usage(provider):
    """A supported, non-billable usage check. A refresh is evidence, never permission to run."""
    store = _task_store()
    providers.adapter(provider)
    if provider != 'claude':
        raise providers.ProviderRefused(provider, 'usage_unavailable', providers.USAGE_SOURCES[provider][1],
                                        'No usage refresh exists for this provider; nothing was requested.')
    found = providers.probe(provider, force=True)
    if not found.ready:
        raise providers.ProviderRefused(provider, found.state, found.reason or found.state, found.guidance)
    if not providers.CLAUDE.usage_command_supported:
        found_version = '.'.join(map(str, providers.CLAUDE.installed_version)) if providers.CLAUDE.installed_version else 'unknown'
        raise providers.ProviderRefused(provider, 'usage_unavailable', 'Usage refresh needs Claude Code 2.1.273 or later '
                                        f'(documented usage_report; installed: {found_version}). Usage is shown as '
                                        'unavailable and nothing was sent.', 'Update Claude Code.')
    report = providers.CLAUDE.usage_report()
    windows = providers.windows_from_claude_report(report)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if windows:
            providers.record_windows(db, provider, windows, source='claude_usage_command')
            # Only a limit the report positively covers can be resolved: same window
            # identity (and scope) now reported available. Omitted windows, blocks
            # without a window identity and the unidentified active limit stay.
            available = {(w['window_id'], w.get('scope_key') or '') for w in windows if w['status'] == 'available'}
            for block in providers.active_blocks(db, provider):
                if block.get('window_id') and (block['window_id'], '') in available:
                    db.execute(f"""UPDATE provider_usage_blocks SET cleared_at={providers.NOW},cleared_by='provider_signal',
                        clear_reason='usage_refresh' WHERE block_id=?""", (block['block_id'],))
    with store._connection() as db:
        result = providers.status(db, provider, probe_installation=False)
    _emit_agent_change(None)
    return {**result, 'reported': windows is not None}


def _provider_history(agent_id):
    if _store is None:
        return []
    with _store._connection() as db:
        if not providers.exists(db):
            return []
        return [dict(r) for r in db.execute('''SELECT change_id,from_provider,from_model,from_sandbox,to_provider,
            to_model,to_sandbox,session_retained,boundary,handoff_pending,changed_at,from_effort,to_effort
            FROM agent_provider_changes WHERE agent_id=? ORDER BY change_id''', (agent_id,))]


def _pending_handoff(agent_id):
    if _store is None:
        return None
    with _store._connection() as db:
        row = db.execute('''SELECT * FROM agent_provider_changes WHERE agent_id=? AND handoff_pending=1
            ORDER BY change_id DESC LIMIT 1''', (agent_id,)).fetchone()
        return dict(row) if row else None


def _handoff_prompt(agent_id, handoff, previous_question):
    note = (f'Control Center provider handoff: this Agent previously ran on {providers.LABELS.get(handoff["from_provider"], handoff["from_provider"])}'
            f' and now runs on {providers.LABELS[handoff["to_provider"]]}. The previous provider\'s internal session '
            'and memory are not available to you. The Task below is authoritative. The current directory is the '
            'Task workspace and contains all work done so far: inspect it before continuing.\n\n'
            f'Assigned Task:\n{agent_tasks[agent_id]}\n')
    if previous_question:
        note += f'\nThe previous provider was waiting on this question: {json.dumps(previous_question)}\n'
    return note + '\n'


def _complete_handoff(handoff, process):
    with _task_store()._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE agent_provider_changes SET handoff_pending=0,first_execution_id=? WHERE change_id=?',
                   (getattr(process, '_control_center_execution_id', None), handoff['change_id']))


@_synchronized
def change_agent_provider(agent_id, *, provider, model=None, sandbox=None, effort=None, attempt_unverified=False):
    """Change an Agent's provider, model or (narrower) access at a safe execution boundary.

    The Agent and Task identities, assignment, output and history stay. A new
    provider (or a model change its CLI cannot apply to a session) starts a new
    session: the previous provider's session is never handed to another provider.
    Nothing is launched here; the next Continue or Reply runs on the new provider.
    """
    if agent_id not in agents:
        return None
    store = _task_store()
    providers.adapter(provider)
    providers.require_capability(provider, 'session_resume', 'Continuing an existing Agent')
    model = providers.validate_model(model)
    effort = providers.validate_effort(effort)
    current_provider, current_model = agent_types[agent_id], agent_models.get(agent_id)
    current_sandbox = agent_sandboxes[agent_id]
    if current_provider == 'mock':
        raise ValueError('Mock agents only simulate work; start a new agent with a real provider instead.')
    target_sandbox = sandbox or current_sandbox or 'read-only'
    if target_sandbox not in providers.SANDBOXES:
        raise ValueError('Unknown access policy')
    if current_sandbox == 'read-only' and target_sandbox == 'workspace-write':
        raise ValueError('Access cannot be widened by a provider or model change; it may only stay or narrow.')
    if (provider, model, target_sandbox, effort) == (current_provider, current_model, current_sandbox,
                                                      agent_efforts.get(agent_id)):
        raise ValueError('The Agent already uses this provider, model and access policy.')
    process = agents[agent_id]
    if agent_statuses[agent_id] != 'waiting':
        raise ValueError('Provider and model changes are allowed only at a safe boundary: while the Agent '
                         'waits, never during a running execution.')
    if process is not None and (process.poll() is None or _execution_live(process) or not process.stdout.closed):
        raise ValueError('Execution ownership is not settled yet; provider and model cannot change now.')
    assignment = store.get_active_assignment_for_agent(agent_id)
    if assignment is None:
        raise ValueError('The Agent has no active Task assignment to continue.')
    if store.execution_settings(agent_id):
        raise ValueError('Managed orchestration and delegated results are bound to this provider session; '
                         'its provider cannot change in Stage 2F.')
    task_id = assignment['task_id']
    _require_unguarded(agent_id, task_id)
    with store._connection() as db:
        if executions.blocking(db, agent_id=agent_id) or executions.blocking(db, task_id=task_id):
            raise ValueError(executions.REFUSED)
        agent_removal.require_operational(db, agent_id)
    with store._connection() as db:
        provider_policy.require_allowed(db, provider, task_id)  # A change never escapes the Task's permissions.
    # The new provider must be usable now (no fallback is ever chosen for it).
    previous_config = getattr(_launch, 'execution_config', None)
    _launch.execution_config = dict(model=model, effort=effort, source='agent_default', attempt=bool(attempt_unverified))
    try:
        _require_launchable(provider, model=model, sandbox=target_sandbox, effort=effort)
    finally:
        _launch.execution_config = previous_config
    current_effort = agent_efforts.get(agent_id)
    with store._connection() as db:
        compatible = model_catalog.session_compatible(
            db, provider, agent_session_configs.get(agent_id) or (current_model, current_effort), (model, effort))
    keep_session = bool(provider == current_provider and agent_sessions[agent_id] and compatible)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        agent_removal.require_operational(db, agent_id)
        if executions.blocking(db, agent_id=agent_id) or executions.blocking(db, task_id=task_id):
            raise ValueError(executions.REFUSED)
        db.execute('''INSERT INTO agent_provider_changes(agent_id,assignment_id,from_provider,from_model,from_sandbox,
            from_session_id,to_provider,to_model,to_sandbox,session_retained,boundary,handoff_pending,from_effort,to_effort)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (agent_id, assignment['assignment_id'], current_provider, current_model, current_sandbox,
             agent_sessions[agent_id], provider, model, target_sandbox, int(keep_session), 'waiting',
             int(not keep_session), current_effort, effort))
    agent_types[agent_id], agent_models[agent_id], agent_sandboxes[agent_id] = provider, model, target_sandbox
    agent_efforts[agent_id] = effort
    if not keep_session:
        agent_sessions[agent_id] = None
    agent_conditions[agent_id] = json.dumps(dict(kind='resume_ready', provider=provider, condition=None,
                                                 reset_at=None, resume_available=True), separators=(',', ':'))
    _provider_retry_attempts.pop(agent_id, None)
    describe = lambda p, m: providers.LABELS[p] + (f' ({m})' if m else '')
    agent_outputs[agent_id].append(
        f'--- Provider changed from {describe(current_provider, current_model)} to {describe(provider, model)} '
        f'at a safe boundary; access: {target_sandbox}. '
        + ('The provider session is kept. ---' if keep_session else
           'The previous provider session is not reused: the next run starts a new session with the Task '
           'context; internal model memory is not transferred. ---'))
    if not _save_agent(agent_id):
        raise RuntimeError('The provider change was recorded but the Agent could not be saved; please retry.')
    return get_agent(agent_id, include_output=False)


@_synchronized
def continue_agent(agent_id, explicit_retry=False, config=None):
    with _prompt_config(config):
        return _continue_agent(agent_id, explicit_retry)


def _continue_agent(agent_id, explicit_retry=False):
    """The user's explicit Resume of one waiting Agent (never automatic, never at startup).

    Rechecks enablement, installation, sign-in, the billing route and its
    acknowledgment, every known limit, the model and capabilities, that the
    previous execution is settled, and Task, assignment and resource admission,
    then resumes through the existing session path. A limit whose renewal cannot be
    confirmed without a request may be overridden only by an explicit retry; a
    limit known to still apply never. The request is journaled with its outcome.
    """
    if agent_id not in agents:
        return None
    if agent_statuses[agent_id] == 'waiting' and agent_waiting_questions[agent_id]:
        raise ValueError('This Agent is waiting for your answer; reply to it instead.')
    if providers.condition_view(agent_conditions.get(agent_id)) is None and _pending_handoff(agent_id) is None:
        raise ValueError('This Agent is not waiting on a provider; there is nothing to resume.')
    process, provider, model = agents[agent_id], agent_types[agent_id], agent_models.get(agent_id)
    if process is not None and (_execution_live(process) or not process.stdout.closed):
        raise ValueError('The previous execution is not safely settled yet; resume is not available.')
    store = _task_store()
    with store._connection() as db:
        if executions.blocking(db, agent_id=agent_id):
            raise ValueError(executions.REFUSED)
    providers.reconcile(store._connection)
    providers.probe(provider, force=True)  # Fresh installation, sign-in and route check.
    with store._connection() as db:
        assessment = providers.resume_assessment(db, provider, model)
    if not assessment['available']:
        raise providers.ProviderRefused(provider, 'resume_unavailable', assessment['reason'])
    if assessment['mode'] == 'explicit_retry' and not explicit_retry:
        raise providers.ProviderRefused(provider, 'explicit_retry_required', assessment['reason'],
                                        'Choose Resume with an explicit retry to try anyway.')
    overridden = [c['id'] for c in assessment['blocking'] if not c['hard']] if explicit_retry else []
    request_id = str(uuid4())
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""INSERT INTO agent_resume_requests(request_id,agent_id,provider,model,mode,overridden,outcome)
            VALUES (?,?,?,?,?,?,'pending')""", (request_id, agent_id, provider, model,
                                              'explicit_retry' if overridden else 'verified', json.dumps(overridden)))
    _launch.usage_override, _launch.resume_request = tuple(overridden), request_id
    try:
        command = _waiting_resume_command(agent_id)  # Rechecks readiness, enablement, billing and usage.
        if command is None:
            return None
        result = _resume_agent(agent_id, command, 'Continue the assigned work from where it stopped. Inspect the '
                               'current workspace state first.', '--- Continued after provider wait ---',
                               history_text='Resume (explicit retry)' if overridden else 'Resume')
    except Exception as exc:
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(f"""UPDATE agent_resume_requests SET outcome='refused',reason=?,completed_at={providers.NOW}
                WHERE request_id=?""", (str(exc)[:500], request_id))
        raise
    finally:
        _launch.usage_override, _launch.resume_request = (), None
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f"""UPDATE agent_resume_requests SET outcome='admitted',execution_id=?,completed_at={providers.NOW}
            WHERE request_id=?""", (getattr(agents[agent_id], '_control_center_execution_id', None), request_id))
    return result


def _resume_view(agent_id):
    """Why a waiting Agent can or cannot be resumed now (no probe, no launch)."""
    if agent_statuses.get(agent_id) != 'waiting' or agent_waiting_questions.get(agent_id):
        return None
    if providers.condition_view(agent_conditions.get(agent_id)) is None and _pending_handoff(agent_id) is None:
        return None
    process = agents.get(agent_id)
    if process is not None and (_execution_live(process) or not process.stdout.closed):
        return {'available': False, 'mode': None, 'reason': 'Previous execution not safely settled.', 'blocking': []}
    with _provider_db() as db:
        view = providers.resume_assessment(db, agent_types[agent_id], agent_models.get(agent_id))
    view['blocking'] = [{k: c.get(k) for k in ('label', 'reset_at', 'hard', 'kind', 'observed_at', 'fresh')}
                        for c in view['blocking']][:8]
    view['session_resumable'] = bool(agent_sessions.get(agent_id))
    return view


def _structured(process):
    context = getattr(process, '_control_center_execution', None)
    return context if isinstance(context, Execution) else None


def _ordinary_stream(process):
    """The JSON-stream parser of an ordinary (non-managed) Claude Code or Gemini CLI execution."""
    events = getattr(process, '_control_center_stream', None)
    return events if isinstance(events, provider_events.StreamEvents) else None


def _events_of(process):
    context = _structured(process)
    return context.events if context else _ordinary_stream(process)


def _bind_stream(agent_id, process):
    stream = _ordinary_stream(process)
    if stream is not None:
        # A resumed run must report the session it was asked to continue.
        stream.expected_session = agent_sessions[agent_id]


def _bind_execution(agent_id, process):
    context = _structured(process)
    if context:
        context.events.native_collaboration_prohibited = True
        context.events.planning_enabled = _task_store().planning_enabled(agent_id)
        context.events.expected_session = agent_sessions[agent_id]
        context.origin = _task_store().begin_delegation_execution(agent_id, str(uuid4()))


def _finish_structured(agent_id, process):
    """Finalize only after both pipes close, under the existing runtime lock.

    Readers cannot acquire that lock (Stop/Redirect join them). They collect
    evidence; this finalizer alone performs the durable handoff.
    """
    context = _structured(process)
    if not context:
        return False
    if not process.stderr.closed:
        return True
    if context.origin and not _task_store().execution_is_current(context.origin):
        # Ownership may have moved independently of this process. Do not write
        # even a failure status through the newer assignment.
        agent_statuses[agent_id] = 'stopped'
        return True
    if context.processed:
        return False
    context.processed = True
    try:
        if _store is not None and not context.revoked:
            try:
                # The journal, not only the in-memory flag, revokes authority.
                context.revoked = _store.agent_removal_pending(agent_id)
            except (sqlite3.Error, OSError):
                context.revoked = True  # Unknown removal state never authorizes work.
        if context.revoked or _shutting_down or context.origin is None:
            raise ValueError('Delegation execution was interrupted')
        message = context.events.finish(process.poll())
        if not message.strip():
            raise ValueError('Structured execution has no final response to validate')
        control = delegation_protocol.is_envelope(message)
        payload = delegation_protocol.decode(message) if control else None
        replans = replanning.is_envelope(message)
        promoting = promotion.is_envelope(message) or replans
        if control and not promoting:
            if isinstance(payload, dict) and payload.get('protocol') == delegation_continuation.PROTOCOL:
                delegation_continuation.validate(message)
            elif isinstance(payload, dict) and payload.get('protocol') == delegation_v3.PROTOCOL:
                delegation_v3.validate(message)
            else:
                delegation_protocol.validate(message)
        if context.events.plans and not promoting:
            _task_store().accept_planning_messages(context.origin, context.events.plans,
                                                  session_id=context.events.session_id)
            _emit_agent_change(agent_id)
        if control:
            if isinstance(payload, dict) and payload.get('protocol') == delegation_continuation.PROTOCOL and payload.get('action') == 'acknowledge_results':
                payload = delegation_continuation.validate(message)
                with _task_store()._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    delegation_orchestration.acknowledge(db, context.origin, payload)
                agent_statuses[agent_id] = 'stopped'
                agent_waiting_questions[agent_id] = None
                _save_agent(agent_id)
                _schedule_hybrid_for_agent(agent_id)
                if _task_store().list_promotions(context.origin.parent_task_id):
                    _schedule_materialization(context.origin.parent_task_id)
                return True
            if replans:
                _task_store().accept_replanning_message(context.origin, message,
                    session_id=context.events.session_id, message_id=context.events.message_id, plans=context.events.plans)
            elif promoting:
                _task_store().accept_promotion_message(context.origin, message,
                    session_id=context.events.session_id, message_id=context.events.message_id,
                    plans=context.events.plans)
            else:
                _task_store().accept_delegation_message(context.origin, message,
                    session_id=context.events.session_id, message_id=context.events.message_id)
            agent_statuses[agent_id] = 'stopped'
            agent_waiting_questions[agent_id] = None
            notifications.cancel(agent_id)
            agent_outputs[agent_id].append('--- Delegation requests recorded; waiting for orchestration ---')
            _save_agent(agent_id)
            with _task_store()._connection() as db:
                enabled = db.execute('SELECT child_materialization_enabled FROM agents WHERE agent_id=?', (agent_id,)).fetchone()[0]
            if enabled:
                _schedule_materialization(context.origin.parent_task_id)
            return True
        # Waiting/decision control is trusted only from the successful final
        # assistant message, never from tools, stderr or unfinished turns.
        for line in message.splitlines():
            parsed = _ansi.sub('', line).strip()
            if parsed.startswith(_waiting_marker) and parsed[len(_waiting_marker):].strip():
                agent_waiting_questions[agent_id] = parsed[len(_waiting_marker):].strip()
            if parsed == _similar_handled_marker and agent_similar_decisions[agent_id].automatic_question:
                agent_similar_decisions[agent_id].handled = True
            if parsed == _always_handled_marker and agent_always_decisions[agent_id].automatic_attempt:
                agent_always_decisions[agent_id].handled = True
        return False
    except (ValueError, LookupError, OSError, sqlite3.Error, RecursionError):
        # Also safe if commit succeeded but acknowledgement/publication failed:
        # work_control.sync retains the committed orchestration continuation.
        logger.exception('Structured execution did not complete normally for %s', agent_id)
        condition = getattr(context.events, 'condition', None)
        if condition in providers.BLOCKING:
            # A provider-reported limit: no new work is admitted on this provider
            # until it is cleared explicitly; this execution stops as before.
            _record_provider_block(agent_types[agent_id], condition,
                                   context.events.error or condition, agent_id, process)
            providers.invalidate(agent_types[agent_id])
        agent_statuses[agent_id] = 'stopped'
        agent_waiting_questions[agent_id] = None
        notifications.cancel(agent_id)
        agent_outputs[agent_id].append('--- Structured execution stopped; inspect output before retrying ---')
        if context.events.error:
            agent_outputs[agent_id].append(context.events.error)
        _save_agent(agent_id)
        _notify_hybrid_child_end(agent_id)
        return True


def _read_stderr(agent_id, process):
    try:
        with process.stderr as stream:
            for line in stream:
                with _data_lock:
                    if agents.get(agent_id) is process:
                        agent_outputs[agent_id].append(line.rstrip('\r\n'))
                        _save_agent(agent_id)
    except (OSError, ValueError):
        with _data_lock:
            events = _events_of(process)
            if events is not None:
                events.error = events.error or 'Provider stderr stream was interrupted'
        logger.exception('Could not finish reading provider stderr')


def _read_output(agent_id: str, process: subprocess.Popen) -> None:
    context = _structured(process)
    events = context.events if context else _ordinary_stream(process)
    if events is not None:
        stderr_reader = Thread(target=_read_stderr, args=(agent_id, process), daemon=True)
        stderr_reader.start()
        try:
            with process.stdout as stdout:
                for line in stdout:
                    with _data_lock:
                        if agents.get(agent_id) is not process:
                            continue
                        agent_outputs[agent_id].extend(events.feed(line))
                        if events.session_id and not events.error:
                            agent_sessions[agent_id] = events.session_id
                        _save_agent(agent_id)
                    if (context and events.native_collaboration_prohibited
                            and events.native_collaboration_seen
                            and not context.policy_stop_requested):
                        context.policy_stop_requested = True
                        # Stop/Redirect may join this reader under the state lock.
                        Thread(target=_stop_policy_violation, args=(agent_id, process), daemon=True).start()
        except (OSError, ValueError):
            events.error = events.error or 'Provider event stream was interrupted'
            logger.exception('Could not finish reading provider events')
        finally:
            stderr_reader.join()
        _record_effective(agent_id, process, events)
        return
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
    watcher = Thread(target=_watch_process, args=(agent_id, process, reader), daemon=True)
    process._control_center_watcher = watcher
    watcher.start()


def _new_controller():
    return {**process_supervision.controller_identity(), 'instance_id': str(uuid4())}


def _controller_identity():
    global _controller
    if _controller is None:
        _controller = _new_controller()
    return _controller


@dataclass
class _LaunchJournal:
    db: sqlite3.Connection
    task_id: str | None
    agent_id: str
    provider: str
    execution_id: str  # The committed `launching` reservation this attempt consumes
    job_name: str | None
    process: object = None  # Any process created for it, once created
    consumed: bool = False
    discard_evidence: dict | None = None  # A never-resumed root that could not be confirmed ended


@contextmanager
def _journal(db, task_id, agent_id, provider, reservation):
    previous = getattr(_launch, 'journal', None)
    _launch.journal = journal = _LaunchJournal(db, task_id, agent_id, provider, *reservation)
    try:
        yield journal
    finally:
        _launch.journal = previous


# Reservations this controller knows produced no process but could not yet
# close (the database was unavailable): closed by the next reservation
# transaction. Only "no process was created" is remembered here.
_unspawned_reservations: dict[str, dict] = {}


def _supervised_launch():
    return os.name == 'nt' and subprocess.Popen is _RealPopen


def _reserve_launch(task_id, agent_id, provider, model=None):
    """Phase 0: commit a `launching` row before any process exists; refuse on conflict.

    Returns (execution_id, job_name). A failure here creates no process.
    """
    controller = _controller_identity()
    supervised = _supervised_launch()
    if supervised:
        process_supervision.require_identity(controller)  # Before any row or process.
    job_name = process_supervision.new_job_name() if supervised else None
    with _store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        with _guard_lock:
            unspawned = dict(_unspawned_reservations)
        for execution_id, (evidence, identity) in list(unspawned.items()):
            try:
                executions.transition(db, execution_id, 'launch_aborted', ('launching',), evidence=evidence,
                                      identity=identity)
            except sqlite3.Error:
                unspawned.pop(execution_id)  # Still unclosed: kept for the next attempt.
                logger.exception('Could not close launch reservation %s', execution_id)
        executions.require_admissible(db, controller['instance_id'], agent_id, task_id)
        _require_unguarded(agent_id, task_id)
        execution_id = executions.reserve(db, controller, agent_id=agent_id, task_id=task_id, provider=provider,
                                          containment='job_kill_on_close' if supervised else 'none',
                                          job_name=job_name)
        if model is not None:
            # Historical provenance: the model this execution was launched with.
            db.execute('UPDATE executions SET model=? WHERE execution_id=?', (model, execution_id))
    with _guard_lock:
        for key in unspawned:
            _unspawned_reservations.pop(key, None)
    return execution_id, job_name


NOT_CONTAINED = dict(containment='none', job_name=None)  # No process was ever inside its Job.


def _close_reservation(execution_id, state, evidence, uncertainty=None, identity=None):
    """Close a reservation whose attempt failed; only "no process" may be retried later."""
    if _record_execution(execution_id, state, ('launching',), evidence=evidence, uncertainty=uncertainty,
                         identity=identity):
        return
    if state == 'launch_aborted' and identity == NOT_CONTAINED:
        with _guard_lock:
            _unspawned_reservations[execution_id] = (evidence, identity)
    # Anything else stays `launching`: an open reservation keeps blocking this
    # Task and Agent, and is never treated as completed.


def _guard_begin(agent_id, task_id, **evidence):
    token = str(uuid4())
    with _guard_lock:
        _ownership_guard[token] = dict(agent_id=agent_id, task_id=task_id, state='launching',
                                       since=process_supervision._now(), **evidence)
    return token


def _guard_unverified(token, agent_id, task_id, **evidence):
    """Block, infallibly and before any journal write, a process whose end is unconfirmed."""
    token = token or str(uuid4())
    with _guard_lock:
        entry = _ownership_guard.setdefault(token, dict(agent_id=agent_id, task_id=task_id,
                                                        since=process_supervision._now()))
        entry.update(evidence, state='unverified')
    return token


def _guard_update(token, **evidence):
    with _guard_lock:
        if token in _ownership_guard:
            _ownership_guard[token].update(evidence)


def _guard_end(token):
    if token is not None:
        with _guard_lock:
            _ownership_guard.pop(token, None)


def _require_unguarded(agent_id, task_id):
    with _guard_lock:
        states = {e['state'] for e in _ownership_guard.values()
                  if e['agent_id'] == agent_id or (task_id is not None and e['task_id'] == task_id)}
    if 'unverified' in states:
        raise ValueError(executions.REFUSED)
    if states:
        raise ValueError('Execution refused: another launch of this Task or Agent is in progress and '
                         'could not be confirmed ended yet')


def _spawn_supervised(journal, command, options):
    """Create suspended in a verified Job and journal it; the caller resumes after commit."""
    process_supervision.require_identity(_controller_identity())
    if journal.job_name is None:
        # The reservation was made for an unsupervised launch: never improvise one.
        raise process_supervision.SupervisionError('Process supervision unavailable: launch reservation has no Job')
    # Guarded before a process exists, inside this admission transaction.
    token = _guard_begin(journal.agent_id, journal.task_id, execution_id=journal.execution_id,
                         job_name=journal.job_name)
    try:
        pending = process_supervision.spawn(command, job_name=journal.job_name, **options)
    except process_supervision.TerminationUnverified as exc:
        journal.discard_evidence = dict(getattr(exc, 'evidence', None) or {}, aborted_at=process_supervision._now())
        _guard_unverified(token, journal.agent_id, journal.task_id, reason='spawn_discard_unverified',
                          **(getattr(exc, 'evidence', None) or {}))
        raise
    except BaseException:
        _guard_end(token)  # No process was created, or its exit was observed before it ever ran.
        raise
    pending.guard = token
    _guard_update(token, root_pid=pending.process.pid, root_created_at=pending.root_created_at,
                  job_name=pending.job.name)
    process = journal.process = pending.process
    process._control_center_pending_launch = pending
    process._control_center_task_id = journal.task_id
    process._control_center_agent_id = journal.agent_id
    pending.journal = dict(agent_id=journal.agent_id, task_id=journal.task_id, provider=journal.provider)
    pending.execution_id = journal.execution_id
    # A failure here is aborted by _admitted_spawn once this transaction has
    # rolled back; the committed reservation then records how it ended.
    executions.record_launch(
        journal.db, journal.execution_id, containment='job_kill_on_close', state='contained',
        evidence={'launch': pending.evidence}, job_name=pending.job.name, job_config=pending.job.config,
        root_pid=pending.process.pid, root_created_at=pending.root_created_at)
    journal.consumed = True
    process._control_center_execution_id = pending.execution_id
    return process


def _journal_unsupervised(journal, process):
    # No Job: nothing proves the end of this tree. Recorded so that later
    # recovery never mistakes it for a contained execution.
    pid = process.pid if isinstance(process.pid, int) else None
    try:
        executions.record_launch(journal.db, journal.execution_id, containment='none', state='running',
                                 root_pid=pid, evidence={'launch': {'supervised': False}})
        journal.consumed = True
        process._control_center_execution_id = journal.execution_id
        process._control_center_task_id = journal.task_id
        process._control_center_agent_id = journal.agent_id
    except BaseException:
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            logger.exception('Could not end an unjournaled unsupervised launch')
        raise


def _spawn_process(command: str | list[str], agent_type: AgentType, *, cwd) -> subprocess.Popen:
    stream = _stream_command(command)
    structured = isinstance(command, StructuredCommand) or (stream and command.structured)
    real = agent_type in providers.REAL
    # Windows: every real worker runs under its own verified Job Object; a
    # supervision failure refuses the launch (SupervisionError is an OSError).
    supervised = os.name == 'nt' and subprocess.Popen is _RealPopen
    options = dict(
        stdin=subprocess.PIPE if real else None,
        stdout=subprocess.PIPE,
        # JSON streams keep stderr apart so it can never be parsed as events.
        stderr=subprocess.PIPE if structured or stream else subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8" if real else None,
        errors="replace",
        cwd=cwd,
        shell=False,
    )
    if real:
        # Variables that would silently switch billing to a paid API route are
        # removed; the provider uses the sign-in that readiness verified.
        options['env'] = providers.adapter(agent_type).environment()
    if agent_type == 'gemini' and getattr(command, 'policy', None) is not None:
        # The effective configuration of this execution's own workspace, never a
        # readiness result observed for another one.
        refusal = providers.GEMINI.route_refusal(cwd)
        if refusal is not None:
            raise providers.ProviderRefused('gemini', refusal[0], refusal[1] + ' (execution workspace)',
                                            providers.GEMINI.billing_guidance)
    if agent_type == 'codex' and getattr(command, 'policy', None) is not None:
        problems = providers.CODEX.route_problems(cwd)
        if problems:
            raise providers.ProviderRefused('codex', providers.BILLING_BLOCKED, 'The Codex configuration for this '
                                            'workspace changes the model route: ' + '; '.join(problems) + '.',
                                            providers.CODEX.billing_guidance)
    journal = getattr(_launch, 'journal', None)
    if not supervised:
        process = subprocess.Popen(command, **options)
        if journal is not None:
            journal.process = process
            _journal_unsupervised(journal, process)
    elif journal is None:
        process = process_supervision.popen(command, **options)  # No store: nothing to journal.
    else:
        process = _spawn_supervised(journal, command, options)
    if structured:
        events = provider_events.parser(agent_type) if stream else CodexEvents()
        process._control_center_execution = Execution(events=events)
        process._control_center_execution.events.native_collaboration_prohibited = True
    elif stream:
        process._control_center_stream = provider_events.parser(agent_type)
    if stream or real:
        process._control_center_provider = agent_type
    # What this execution requested (model, effort, override or default).
    process._control_center_config = dict(_execution_config())
    return process


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


def _parent_policy_task(parent_id):
    """The Task whose provider permissions a new Child of `parent_id` inherits (early check only;
    the final admission checks the Child's own Task)."""
    if parent_id is None or _store is None:
        return None
    assignment = _store.get_active_assignment_for_agent(parent_id)
    if assignment:
        return assignment['task_id']
    return _agent_task_id(parent_id)


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


def _execution_preflight(task_id, *, pending=False):
    with _store._connection() as db:
        require_ancestors(db, task_id)
    current = _store.get_task(task_id)
    if current and any(c['mode'] != 'advisory' and c['status'] != 'released'
                       for c in _store.resource_claims(task_id)):
        current = _store.preflight_task(task_id)
        _emit_agent_change(None)
    require_active(current, pending=pending)
    return current


def _workspace_session(action):
    @wraps(action)
    def locked(agent_id, *args, **kwargs):
        task_id = _agent_task_id(agent_id)
        if task_id:
            _execution_preflight(task_id)
        # Nonblocking under the process-state lock avoids lock-order inversions
        # with starts, which do slow Git work outside that broad lock.
        guard = task_lock(_store, task_id, blocking=False) if task_id else nullcontext()
        with guard, _upstream_guard(task_id):
            return action(agent_id, *args, **kwargs)
    return locked


def start_agent(task: str, agent_type: AgentType = "mock", sandbox: CodexSandbox = "read-only",
                parent_id: str | None = None, *, task_id: str | None = None, project_id: str | None = None,
                delegation_protocol_enabled: bool = False,
                child_materialization_enabled: bool = False, hybrid_delegation_enabled: bool = False,
                recursive_delegation_enabled: bool = False, planning_enabled: bool = False, _materialization=None,
                model: str | None = None, effort: str | None = None, attempt_unverified: bool = False) -> str:
    """A new Agent with its own default provider, model and effort (never shared with another Agent)."""
    providers.validate_effort(effort)
    with _prompt_config({}):
        _launch.execution_config = dict(model=model, effort=effort, source='agent_default',
                                        attempt=bool(attempt_unverified))
        _launch.new_agent_effort = effort
        return _start_agent(task, agent_type, sandbox, parent_id, task_id=task_id, project_id=project_id,
                            delegation_protocol_enabled=delegation_protocol_enabled,
                            child_materialization_enabled=child_materialization_enabled,
                            hybrid_delegation_enabled=hybrid_delegation_enabled,
                            recursive_delegation_enabled=recursive_delegation_enabled,
                            planning_enabled=planning_enabled, _materialization=_materialization, model=model,
                            effort=effort)


def _start_agent(task, agent_type, sandbox, parent_id, *, task_id, project_id, delegation_protocol_enabled,
                 child_materialization_enabled, hybrid_delegation_enabled, recursive_delegation_enabled,
                 planning_enabled, _materialization, model, effort):
    providers.adapter(agent_type)  # Unknown providers are refused before anything else.
    if project_id is not None and (parent_id is not None or task_id is not None):
        raise ValueError('Project selection is only available for new root Tasks')
    if project_id is not None and _store is None:
        raise RuntimeError('Project persistence is unavailable')
    if planning_enabled and _store is None:
        raise ValueError('Planning requires a persistent agent')
    if planning_enabled:
        providers.require_capability(agent_type, 'autonomous', 'Planning and autonomous orchestration')
    if recursive_delegation_enabled and not hybrid_delegation_enabled:
        raise ValueError('Recursive delegation requires explicit Hybrid activation')
    if hybrid_delegation_enabled and not child_materialization_enabled:
        raise ValueError('Hybrid execution requires explicit Child materialization activation')
    if child_materialization_enabled and not delegation_protocol_enabled:
        raise ValueError('Child materialization requires explicit delegation protocol activation')
    if delegation_protocol_enabled and _store is None:
        raise ValueError('Delegation protocol requires a persistent agent')
    if delegation_protocol_enabled:
        providers.require_capability(agent_type, 'delegation', 'The delegation protocol')
    prepared = _prepare_agent_start(task, agent_type, sandbox, parent_id, task_id=task_id, project_id=project_id,
                                    model=model, effort=effort)
    if delegation_protocol_enabled or planning_enabled:
        prepared = (*prepared[:4], _provider_command(agent_type, sandbox, structured=True, model=model, effort=effort),
                    prepared[5])
    elif _store and agent_type in providers.REAL:
        with _store._connection() as db:
            # Explicit replacement on an existing Child Task needs the same
            # successful structured completion proof as its original materialization.
            record = db.execute('SELECT parent_task_id FROM delegations WHERE child_task_id=?', (prepared[2],)).fetchone()
            if record and delegation_orchestration.state(db, record[0]):
                prepared = (*prepared[:4], _provider_command(agent_type, sandbox, structured=True, model=model,
                                                             child=True, effort=effort), prepared[5])
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
        return _launch_prepared_agent(*prepared[:5], agent_type, sandbox, cwd,
                                      materialization=_materialization, model=model, effort=effort,
                                      child_materialization_enabled=child_materialization_enabled,
                                      hybrid_delegation_enabled=hybrid_delegation_enabled,
                                      recursive_delegation_enabled=recursive_delegation_enabled,
                                      planning_enabled=planning_enabled, delegation_protocol_enabled=delegation_protocol_enabled)


@_synchronized
def _prepare_agent_start(task: str, agent_type: AgentType, sandbox: CodexSandbox,
                         parent_id: str | None, *, task_id: str | None, project_id: str | None = None,
                         model: str | None = None, effort: str | None = None):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; agent start was not performed')
    origin = 'project_snapshot' if task_id is None else 'legacy_project_snapshot'
    if parent_id is not None and parent_id not in agents:
        raise LookupError("Parent agent not found")
    if parent_id is not None and _store is not None:
        _store.require_agent_operational(parent_id)
    providers.adapter(agent_type)
    # Readiness, enablement, billing, usage and access policy are checked here,
    # before any Task, workspace, reservation or process is created. A Child (or a
    # start on an existing Task) is checked against the provider permissions it will
    # inherit, so an inherited restriction is reported as such before readiness or usage.
    previous_policy_task = getattr(_launch, 'policy_task_id', None)
    _launch.policy_task_id = task_id if task_id is not None else _parent_policy_task(parent_id)
    try:
        command = _provider_command(agent_type, sandbox, model=model, effort=effort)
    finally:
        _launch.policy_task_id = previous_policy_task
    # Resolve logical ownership from the canonical context, then execute only
    # in the Task Workspace regardless of provider.
    cwd = root_path = _project_root
    parent_task = None
    if _store is not None:
        if task_id is None:
            if project_id is not None:
                project = _store.get_project(project_id)
                if project is None:
                    raise LookupError('Selected Project not found. Refresh the Project list.')
                root_path = Path(canonical_path(project['root_path'], require_directory=True)[0])
                resolved = _store.resolve_project_for_path(root_path)
                if resolved is None or resolved['project_id'] != project_id:
                    raise ValueError('Selected Project root has changed; register the correct repository.')
                from .workspace_git import repository
                repository(root_path)
            if parent_id is not None:
                # Ended workers retain their actual assignment history too.
                assignment = _store.get_active_assignment_for_agent(parent_id)
                related = _store.agent_task_ids(parent_id)
                parent_task_id = assignment['task_id'] if assignment else (related[0] if related else None)
                if parent_task_id is None:
                    raise ValueError('Parent agent has no associated Task')
                parent_task = _store.get_task(parent_task_id)
                parent_task = _execution_preflight(parent_task_id)
                _task_project_root(parent_task)  # Reject unresolved legacy ownership.
                origin = 'parent_task_snapshot'
            task_record = _store.create_task(task, parent_task_id=parent_task['task_id'] if parent_task else None,
                                             root_path=root_path,
                                             project_id=project_id if project_id is not None else (_store.resolve_project_for_path(cwd) or {}).get('project_id')
                                             if parent_task is None else None)
        else:
            task_record = _store.get_task(task_id)
            if task_record is None:
                raise LookupError('Task not found')
            task_record = _execution_preflight(task_id, pending=True)
            if task_record['status'] != 'pending' or _store.get_active_assignment_for_task(task_id):
                raise ValueError('Only an unassigned pending Task can start an agent')
            parent_task = _store.get_task_parent(task_id)
            if parent_task is not None:
                origin = 'parent_task_snapshot'
            assignment = _store.get_active_assignment_for_task(parent_task['task_id']) if parent_task else None
            parent_id = assignment['agent_id'] if assignment else None
            if parent_id is not None:
                _store.require_agent_operational(parent_id)
            cwd = _task_project_root(task_record)
        task_id = task_record['task_id']
        task = task_record['description']
    elif task_id is not None:
        raise RuntimeError('Task persistence is unavailable')
    return task, parent_id, task_id, parent_task, command, origin


@_synchronized
def _launch_prepared_agent(task, parent_id, task_id, parent_task, command, agent_type, sandbox, cwd,
                           *, materialization=None, child_materialization_enabled=False, hybrid_delegation_enabled=False, recursive_delegation_enabled=False,
                           planning_enabled=False, delegation_protocol_enabled=False, model=None, effort=None):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; agent start was not performed')
    if _store is not None:
        _store.check_task_integrations(task_id)
        current = _execution_preflight(task_id, pending=True)
        if current is None or current['status'] != 'pending' or _store.get_active_assignment_for_task(task_id):
            raise ValueError('Only an unassigned pending Task can start an agent')
    provider_task = task
    if parent_task is not None:
        provider_task = (f"Parent work:\n{parent_task['description']}\n\n"
                         f"Your assigned contribution:\n{task}\n\n"
                         "Work specifically on your assigned contribution in support of the parent work.")
    agent_id = str(uuid4())
    if _store is not None:
        with _store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if materialization:
                identifier, limits = materialization
                record, _, provider = delegation_materialization.authorize(db, identifier, limits)
                expected = record.get('child_provider') or provider['agent_type']
                if (record['child_task_id'] != task_id or expected != agent_type
                        or (agent_type in providers.REAL and provider['sandbox'] != sandbox)
                        or (record.get('child_provider') and record.get('child_model') != model)
                        or (record.get('child_provider') and record.get('child_effort') != effort)):
                    raise ValueError('Child launch provider, permissions or ownership changed')
                db.execute("UPDATE delegation_materializations SET phase='workspace_ready' WHERE delegation_id=? AND phase='attached'", (identifier,))
                agent_id, _ = delegation_materialization.admit(db, identifier, limits)
            else:
                delegation_materialization.check_worker(db, task_id)
    if recursive_delegation_enabled:
        with _store._connection() as db:
            delegation_safety.activation(db, task_id, agent_type, sandbox)
    activation = dict(child_materialization_enabled=child_materialization_enabled,
                      hybrid_delegation_enabled=hybrid_delegation_enabled,
                      recursive_delegation_enabled=recursive_delegation_enabled,
                      planning_enabled=planning_enabled, delegation_protocol_enabled=delegation_protocol_enabled)
    process = _spawn_with_resources(task_id, agent_id, command, agent_type, cwd,
                                   materialization=materialization, capabilities=activation, sandbox=sandbox,
                                   parent_id=parent_id, model=model)
    agent_statuses[agent_id] = "running"
    agent_parents[agent_id] = parent_id
    agent_outputs[agent_id] = OutputCache()
    agent_outputs[agent_id].assignment_task_id = task_id
    if process_supervision.job_of(process):
        agent_outputs[agent_id].append(process_supervision.MARKER)
    agent_tasks[agent_id] = task
    agent_names[agent_id] = default_display_name(task)
    agent_colors[agent_id] = "neutral"
    agent_types[agent_id] = agent_type
    agent_sandboxes[agent_id] = sandbox if agent_type != "mock" else None
    agent_models[agent_id] = model
    agent_efforts[agent_id] = getattr(_launch, 'new_agent_effort', None)
    agent_session_configs[agent_id] = (model, agent_efforts[agent_id])
    agent_conditions[agent_id] = None
    agent_sessions[agent_id] = None
    agent_waiting_questions[agent_id] = None
    agent_similar_decisions[agent_id] = SimilarDecisions()
    agent_always_decisions[agent_id] = AlwaysDecisions()
    agents[agent_id] = process
    try:
        if not _save_agent(agent_id, activation=activation):
            raise RuntimeError('Unable to persist agent start; please retry.')
        _bind_execution(agent_id, process)
        _bind_stream(agent_id, process)
        if _store:
            process._control_center_assignment = _store.get_active_assignment_for_agent(agent_id)['assignment_id']
    except (ValueError, LookupError, RuntimeError, OSError, sqlite3.Error) as exc:
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
    if agent_type in providers.REAL:
        try:
            with process.stdin:
                process.stdin.write(_execution_prompt(agent_id, provider_task, process))
        except (OSError, ValueError, LookupError, sqlite3.Error):
            stop_agent(agent_id)
            raise
    return agent_id


def _execution_prompt(agent_id, text, process):
    policy = ''
    if _structured(process):
        with _task_store()._connection() as db:
            settings = db.execute('SELECT * FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
        policy = managed_orchestration.instruction(managed_orchestration.can_promote(settings))
        if settings['delegation_protocol_enabled']:
            with _task_store()._connection() as db:
                policy += delegation_v3.instruction(db, _agent_task_id(agent_id))
    return policy + _planning_prompt(agent_id) + _codex_prompt(text)


def _planning_prompt(agent_id):
    if _store and _store.planning_enabled(agent_id):
        current = _store.current_plan(_agent_task_id(agent_id))
        revision = current['revision'] if current else 0
        context = ('\nCurrent accepted proposal (descriptive data, not execution authority):\n'
                   + json.dumps(current['content'], ensure_ascii=False) + '\n') if current else ''
        lead = planning.OPTIONAL_LEAD
        with _store._connection() as db:
            settings = db.execute('SELECT * FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
            if all(settings[k] for k in ('delegation_protocol_enabled', 'child_materialization_enabled', 'hybrid_delegation_enabled')):
                context += promotion.instruction(_agent_task_id(agent_id))
                context += managed_orchestration.example(_agent_task_id(agent_id), revision)
                context += replanning.instruction() + '\nReplanning context (data only):\n' + replanning.context(db, _agent_task_id(agent_id))
                context += '\nPromotion dispositions (data only):\n' + promotion.context(db, _agent_task_id(agent_id))
                context += allocation.instruction(db, _agent_task_id(agent_id))
                if managed_orchestration.decision_requested(db, _agent_task_id(agent_id)):
                    lead = managed_orchestration.DECISION_LEAD
        return planning.instruction(revision, lead) + context
    return ''


def _spawn_with_resources(task_id, agent_id, command, agent_type, cwd, *, materialization=None, capabilities=None, sandbox=None,
                          parent_id=None, model=None):
    try:
        with runtime_resources.launch(_store, task_id, agent_id):
            process = _admitted_spawn(task_id, agent_id, command, agent_type, cwd, materialization=materialization,
                                      capabilities=capabilities, sandbox=sandbox, parent_id=parent_id, model=model)
            return _resume_launch(process)
    except runtime_resources.ResourceUnavailable:
        # The launch was prevented, but its durable blocker still needs an SSE
        # invalidation. Never publish it from inside the resource transaction.
        _emit_agent_change(None)
        raise


def _admitted_spawn(task_id, agent_id, command, agent_type, cwd, *, materialization, capabilities, sandbox, parent_id=None,
                    model=None):
    if not process_supervision.platform_supported():
        # Fail closed before any reservation or process: an unsupervised tree's
        # end could never be established, so its work could never settle.
        raise process_supervision.SupervisionError(process_supervision.UNSUPPORTED)
    if _store is None:
        providers.admission_check(None, agent_type, model=model, policy=getattr(command, 'policy', None),
                                  override=_usage_override())
        return _spawn_process(command, agent_type, cwd=cwd)
    # Phase 0: a durable reservation, committed before any process exists. If
    # it cannot be committed, or conflicts, nothing is created.
    resolved = getattr(command, 'resolved', None)
    if resolved is not None:
        # The model and effort this command was validated and built for (never re-derived).
        model, effort = resolved
    else:
        effort = _execution_config().get('effort')
    reservation = _reserve_launch(task_id, agent_id, agent_type, model)
    process = journal = None
    try:
        # Serialize the last control check with Popen. This short transaction
        # excludes concurrent cancellation at the boundary and commits the
        # execution's journal row before the worker may run; slow Git
        # preparation never holds a SQLite write transaction.
        with _store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # Stage 2F: provider enablement, billing acknowledgment, configuration,
            # every known limit (durable or not) and newer negative readiness,
            # revalidated at this boundary. An explicit user retry may override
            # only limits whose renewal cannot be confirmed; it commits with the launch.
            # Stage 2G: the hard provider permission of this Task subtree, in the same
            # transaction as the launch (a revocation after preparation refuses it here).
            provider_policy.require_allowed(db, agent_type, task_id)
            # Maintenance and execution exclude each other per provider: a setup operation that
            # is running or has an unverified end refuses the launch here, and an operation
            # starts only when no execution of the provider is open (provider_onboarding.start).
            provider_onboarding.require_no_maintenance(db, agent_type)
            overridden = providers.admission_check(db, agent_type, model=model,
                                                   policy=getattr(command, 'policy', None), override=_usage_override())
            if overridden:
                providers.clear_overridden(db, overridden, getattr(_launch, 'resume_request', None) or 'unrecorded')
            config = _execution_config()
            if getattr(command, 'policy', None) is not None and agent_type in providers.REAL:
                # The model and effort are rechecked against the catalog in this
                # transaction; automated launches never attempt unverified models.
                snapshot = model_catalog.admission(
                    db, agent_type, model, effort, explicit_attempt=bool(config.get('attempt')),
                    automated=materialization is not None or bool(getattr(_launch, 'automated', False)))
                source = config.get('source', 'agent_default')
                if config.get('environment'):
                    source += '+environment'  # A legacy environment default supplied part of it.
                db.execute(f'''UPDATE executions SET requested_effort=?,config_source=?,entitlement_state=?,catalog_scope=?,
                    catalog_revision=?,resolved_model=?,configured_at={providers.NOW} WHERE execution_id=?''',
                    (effort, source, snapshot['entitlement_state'],
                     snapshot['catalog_scope'], snapshot['catalog_revision'], snapshot['resolved_model'], reservation[0]))
            if materialization:
                identifier, limits = materialization
                delegation, _, provider = delegation_materialization.authorize(db, identifier, limits)
                delegation_safety.require_inherited(provider, agent_type, sandbox, capabilities or {},
                                                    child_provider=delegation.get('child_provider'))
                promotion.require_ready(db, identifier)
                allocation.require_delegation_authorized(db, delegation)  # Ordinary Auto, at the final boundary.
                delegation_materialization.check_worker(db, task_id, agent_id)
                require_active(dependencies.task(db, task_id), pending=True, db=db)
            elif task_id:
                agent_removal.require_operational(db, agent_id)
                # A new Agent's lineage: a retiring or retired Parent gains no Child
                # (serialized with removal admission by this write transaction).
                agent_removal.require_parent_operational(db, parent_id)
                delegation_safety.worker_boundary(db, task_id)
                require_ancestors(db, task_id)
                current = delegation_orchestration.state(db, task_id)
                if current:
                    _, _, owner = delegation_orchestration.owner(db, task_id, allow_running=True)
                    if current['phase'] not in delegation_orchestration.EXECUTING or owner['agent_id'] != agent_id:
                        raise ValueError('Parent launch is not authorized')
                    plan = json.loads(current['plan']) if current['plan'] else None
                    delegation_orchestration.parallel_admission(db, task_id,
                        plan['intent_ids'] if plan and current['mode'] == 'independent' else ())
                    require_active(dependencies.task(db, task_id), db=db)
                else:
                    # Task control (Cancel/Pause/blockers) is rechecked here, after
                    # the reservation and immediately before Popen.
                    require_active(dependencies.task(db, task_id), db=db)
            # Durable, in this transaction: a worker that may still be alive
            # blocks every new execution of its Task or Agent, claims or not.
            executions.require_admissible(db, _controller_identity()['instance_id'], agent_id, task_id)
            # What the journal may have failed to record (fail closed, this controller only).
            _require_unguarded(agent_id, task_id)
            # Authorization above was revalidated here; the reservation is still ours alone.
            executions.require_reserved(db, reservation[0], _controller_identity()['instance_id'], agent_id, task_id)
            with _journal(db, task_id, agent_id, agent_type, reservation) as journal:
                process = _spawn_process(command, agent_type, cwd=cwd)
                if not journal.consumed:
                    # A process this supervisor did not journal itself: record
                    # it as unsupervised, never as contained.
                    journal.process = process
                    _journal_unsupervised(journal, process)
        return process
    except BaseException:
        # Includes a failed journal write or commit: the suspended worker never runs.
        candidate = process if process is not None else getattr(journal, 'process', None)
        if _pending_launch(candidate) is not None:
            _abort_launch(candidate)
        elif journal is not None and journal.discard_evidence is not None:
            # A never-resumed root outside any verified Job that may survive.
            _close_reservation(reservation[0], 'uncertain', {'end': {**journal.discard_evidence,
                               'job_name': reservation[1]}}, uncertainty='launch_abort_unverified',
                               identity={**NOT_CONTAINED, 'root_pid': journal.discard_evidence.get('root_pid'),
                                         'root_created_at': journal.discard_evidence.get('root_created_at')})
        elif candidate is not None:
            # An unsupervised process was created: nothing proves its tree ended.
            pid = getattr(candidate, 'pid', None)
            _close_reservation(reservation[0], 'uncertain', {'end': {'supervised': False,
                               'observed_at': process_supervision._now()}}, uncertainty='unsupervised',
                               identity={**NOT_CONTAINED, 'root_pid': pid if isinstance(pid, int) else None})
        else:
            _close_reservation(reservation[0], 'launch_aborted', {'end': {
                'process_created': False, 'job_name': reservation[1], 'aborted_at': process_supervision._now()}},
                identity=NOT_CONTAINED)
        raise


def _pending_launch(process):
    pending = getattr(process, '_control_center_pending_launch', None)
    return pending if isinstance(pending, process_supervision.PendingLaunch) else None


def _resume_launch(process):
    """Let a journaled worker run only after its containment evidence committed."""
    pending = _pending_launch(process)
    if pending is None:
        return process
    pending.committed = True
    try:
        evidence = pending.resume()
    except BaseException:
        _abort_launch(process)
        raise
    process._control_center_pending_launch = None
    # A failure here keeps the committed 'contained' row: still conservative.
    _record_execution(pending.execution_id, 'running', ('contained',), evidence={'resume': evidence})
    _guard_end(pending.guard)  # Verified resume: ordinary running-state gates own it from here.
    return process


def _abort_launch(process):
    pending = _pending_launch(process)
    if pending is None:
        return
    process._control_center_pending_launch = None
    state, evidence = pending.abort()
    uncertainty = 'launch_abort_unverified' if state == 'uncertain' else None
    if state == 'uncertain':
        # First and infallibly: nothing may overlap a process that may survive.
        # The journal writes below are best effort and can never clear this.
        journal = pending.journal or {}
        _guard_unverified(pending.guard, journal.get('agent_id'), journal.get('task_id'),
                          reason=uncertainty, execution_id=pending.execution_id, root_pid=pending.process.pid,
                          root_created_at=pending.root_created_at, job_name=pending.job.name,
                          job_config=pending.job.config, launch=pending.evidence,
                          worker_code_may_have_run=pending.resumed)
    else:
        _guard_end(pending.guard)  # Verified: the Job confirmed that no process remains.
    if state == 'uncertain':
        request_diagnostics(pending.execution_id, 'launch_abort_unverified')
    if pending.committed:
        _record_execution(pending.execution_id, state, executions.LIVE, evidence={'end': evidence},
                          uncertainty=uncertainty)
    else:
        # The admission rolled back: the committed reservation records the end.
        # Its root identity is in the evidence (the containment update was lost).
        _close_reservation(pending.execution_id, state, {'launch': pending.evidence, 'end': evidence},
                           uncertainty=uncertainty, identity=dict(
                               containment='job_kill_on_close', job_name=pending.job.name,
                               job_config=pending.job.config, root_pid=pending.process.pid,
                               root_created_at=pending.root_created_at))
    if state == 'uncertain':
        raise process_supervision.TerminationUnverified(
            'Launch refused; its process could not be confirmed ended, so its ownership is kept')


def _record_execution(execution_id, state, from_states, **changes):
    if _store is None or not isinstance(execution_id, str):
        return False
    try:
        return _store.transition_execution(execution_id, state, from_states, **changes)
    except Exception:
        # A failed write leaves the row open (conservative) and must never break
        # the Stop, watcher or launch path that called it. None (not False):
        # callers that depend on the write can tell it did not happen.
        logger.exception('Could not record execution %s as %s', execution_id, state)
        return None


def _record_end(process, via):
    """Persist how a journaled execution ended; never claims more than was observed."""
    execution_id = getattr(process, '_control_center_execution_id', None)
    if not isinstance(execution_id, str):
        if process_supervision.job_of(process) is not None and process_supervision.job_of(process).ended is not None:
            _guard_end(getattr(process, '_control_center_guard', None))
        return
    reason = getattr(process, '_control_center_end', None)
    via = reason if isinstance(reason, str) else via
    job = process_supervision.job_of(process)
    from_states = executions.LIVE
    if job is not None and job.ended is not None:
        state, uncertainty, evidence = 'ended_verified', None, {**job.ended, 'via': via}
        from_states = executions.LIVE + ('uncertain',)  # Only a Job's unconfirmed end can be upgraded.
    else:
        # No Job (unsupervised), or the Job's end could not be confirmed.
        state = 'uncertain'
        uncertainty = 'unsupervised' if job is None else 'termination_unconfirmed'
        code = process.poll()
        evidence = {'via': via, 'root_exit_code': code if isinstance(code, int) else None,
                    'observed_at': process_supervision._now()}
        # First and infallibly: descendants may be alive, so nothing may overlap
        # them, whatever happens to the journal write below.
        process._control_center_guard = _guard_unverified(
            getattr(process, '_control_center_guard', None), getattr(process, '_control_center_agent_id', None),
            getattr(process, '_control_center_task_id', None), reason=uncertainty, execution_id=execution_id,
            root_pid=process.pid if isinstance(process.pid, int) else None)
    recorded = _record_execution(execution_id, state, from_states, evidence={'end': evidence}, uncertainty=uncertainty)
    if state == 'uncertain':
        request_diagnostics(execution_id, 'termination_unverified' if job is not None else 'unsupervised_end')
        # None: not written. Finalization waits for a successful (idempotent)
        # retry; a later unrelated successful write proves nothing about it.
        process._control_center_uncertainty_unpersisted = recorded is None
    if state == 'ended_verified':
        # Verified termination (not the journal write) releases the emergency
        # guard; an earlier unrecorded uncertainty no longer matters.
        process._control_center_uncertainty_unpersisted = False
        _guard_end(getattr(process, '_control_center_guard', None))


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


def start_task_agent(task_id, agent_type='mock', sandbox='read-only', model=None, effort=None, attempt_unverified=False):
    if _store is not None:
        current = _store.responsibility(task_id)
        if current['awaiting_transfer']:
            # The last owner was removed: a new owner is an explicit, journaled
            # transfer with every responsibility check, never a silent restart.
            transfer = transfer_responsibility(task_id, request_key=f'start-agent:{uuid4()}',
                                               expected_owner_assignment_id=None, agent_type=agent_type,
                                               sandbox=sandbox, reason=None, source='start_agent')
            return {'task_id': task_id, 'agent_id': transfer['to_agent_id'], 'status': 'running'}
    options = {'model': model} if model is not None else {}
    if effort is not None:
        options['effort'] = effort
    if attempt_unverified:
        options['attempt_unverified'] = True
    agent_id = start_agent('', agent_type, sandbox, task_id=task_id, **options)
    return {'task_id': task_id, 'agent_id': agent_id, 'status': 'running'}


# Stage 2D.2: explicit responsibility transfer. The journal (responsibility.py)
# commits the decision before the existing start path runs; that path rechecks
# every execution, resource, work-intent and lifecycle gate in its own
# admission transaction. No Agent is created or promoted automatically.

def responsibility_state(task_id):
    return _task_store().responsibility(task_id)


def transfer_responsibility(task_id, *, request_key, expected_owner_assignment_id=None, agent_type='mock',
                            sandbox='read-only', reason=None, source='user'):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; the transfer was not admitted')
    store = _task_store()
    # The Task lock excludes concurrent removal retries, integrations and starts on this Task.
    with task_lock(store, task_id, blocking=False):
        transfer, created = store.admit_transfer(task_id, request_key=request_key,
                                                 expected_from_assignment_id=expected_owner_assignment_id,
                                                 agent_type=agent_type, sandbox=sandbox, reason=reason, source=source)
        if not created:
            return transfer  # Exact replay: the durable decision, never a second start.
        _emit_agent_change(None)
        try:
            agent_id = start_agent('', agent_type, sandbox or 'read-only', task_id=task_id)
        except (ValueError, LookupError, RuntimeError, OSError, sqlite3.Error) as exc:
            failed = store.fail_transfer(transfer['transfer_id'], 'start_refused', str(exc))
            _emit_agent_change(None)
            raise responsibility.TransferError('transfer_failed', f'The new owner could not start: {exc}',
                                               transfer=failed) from exc
        completed = store.complete_transfer(transfer['transfer_id'], agent_id)
        _emit_agent_change(agent_id)
        return completed


def _launch_delegated_child(task_id, identifier, limits):
    with _task_store()._connection() as db:
        record, _, provider = delegation_materialization.authorize(db, identifier, limits)
    # Stage 2F: the Child's provider is the one the Parent requested (never a
    # substitute) or, without a request, the Parent's own; access never widens.
    child_provider = record.get('child_provider') or provider['agent_type']
    # An automated launch: never an unverified model, never a self-granted attempt.
    previous, _launch.automated = getattr(_launch, 'automated', False), True
    try:
        # An Auto allocation is recorded in the same fields as an explicit request.
        return start_agent('', child_provider, provider['sandbox'] or 'read-only',
                           task_id=task_id, _materialization=(identifier, limits),
                           model=record.get('child_model') if record.get('child_provider') else None,
                           effort=record.get('child_effort') if record.get('child_provider') else None,
                           **delegation_safety.inherited(provider, child_provider))
    finally:
        _launch.automated = previous


def materialize_delegations(parent_task_id, *, limits=None, retry=False):
    """Backend-only controlled recovery; never called by GET or startup.

    Each pass is bounded. Started/uncertain executions cannot be relaunched by
    retrying, and the existing Task locks serialize concurrent preparation.
    """
    store = _task_store()
    limits = limits or delegation_materialization.configured_limits()
    service = delegation_materialization.Materializer(store, _launch_delegated_child, _emit_agent_change, limits)
    results = []
    for record in store.list_delegations(parent_task_id):
        if record['status'] not in ('requested', 'materialized'):
            continue
        try:
            with task_lock(store, 'delegation:' + record['delegation_id'], blocking=False):
                if retry:
                    with store._connection() as db:
                        db.execute('BEGIN IMMEDIATE')
                        delegation_materialization.authorize(db, record['delegation_id'], limits)
                        db.execute("UPDATE delegation_materializations SET attempts=0 WHERE delegation_id=? AND phase IN ('attached','workspace_ready')", (record['delegation_id'],))
                results.append(service.run(record['delegation_id']))
        except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
            logger.info('Delegation preparation deferred: %s', exc)
            results.append({'delegation_id': record['delegation_id'], 'last_error': str(exc),
                            'busy': isinstance(exc, TaskBusy)})
    return results


def _orchestration_pass(task_id, steps):
    """One bounded pass; each step is isolated so one failure cannot skip the others."""
    outcome = orchestration_waker.PassOutcome()
    if 'replanning' in steps:
        try:
            advance_replanning(task_id, report_busy=True)
        except TaskBusy:
            outcome.busy = True
        except Exception:
            logger.exception('Replanning pass requires inspection')
    if 'materialize' in steps:
        try:
            results = materialize_delegations(task_id)
            outcome.busy |= any(r.get('busy') for r in results)
            outcome.pending |= any(r.get('phase') in ('attached', 'workspace_ready') for r in results)
        except TaskBusy:
            outcome.busy = True
        except Exception:
            logger.exception('Delegation materialization requires inspection')
    if 'advance' in steps:
        try:
            advance_delegations(task_id)
        except TaskBusy:
            outcome.busy = True
        except Exception:
            logger.exception('Hybrid continuation requires inspection')  # recorded in last_error
    return outcome


def _record_wake_exhausted(task_id, reason):
    if _store is None:
        return
    with _store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        if delegation_orchestration.state(db, task_id):
            db.execute("UPDATE delegation_continuations SET last_error=? WHERE task_id=? AND (phase='waiting' OR last_error IS NULL)",
                       (reason[:500], task_id))
    _emit_agent_change(None)


def _start_timer(delay, function):
    timer = Timer(delay, function)
    timer.daemon = True
    timer.start()


def _new_waker(store):
    # Thread/Timer are resolved at call time, as the previous per-event threads were.
    return orchestration_waker.OrchestrationWaker(
        _orchestration_pass, thread_factory=lambda target: Thread(target=target, daemon=True).start(),
        timer_factory=_start_timer, on_exhausted=_record_wake_exhausted,
        is_active=lambda: store is not None and store is _store and not _shutting_down)


_waker = _new_waker(None)


def wake_orchestration(task_id, steps=orchestration_waker.STEPS, reason='event'):
    if _store is None or _shutting_down:
        return {'state': 'idle'}
    if getattr(_wake_suppression, 'active', False) and reason != 'operator':
        logger.info('Wake for Task %s suppressed during explicit recovery; use the orchestration wake action', task_id)
        return {'state': 'suppressed'}
    return _waker.wake(task_id, steps, reason)


def orchestration_status(task_id):
    """Read-only: durable orchestration evidence plus this process's wake state."""
    store = _task_store()
    if store.get_task(task_id) is None:
        raise LookupError('Task not found')
    with store._connection() as db:
        current = delegation_orchestration.state(db, task_id)
        materializations = [dict(r) for r in db.execute('''SELECT m.delegation_id, m.phase, m.attempts, m.last_error
            FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE d.parent_task_id=? ORDER BY d.rowid''', (task_id,))]
        decision = managed_orchestration.decision(db, task_id)
    continuation = ({key: current[key] for key in ('phase', 'mode', 'last_error', 'plan_consumed')}
                    if current else None)
    return {'task_id': task_id, 'continuation': continuation, 'materializations': materializations,
            'decision': decision, 'wake': _waker.status(task_id)}


def sweep_lost_watchers():
    """Detect and mark only: never finalizes, hands off, launches or wakes anything."""
    marked = []
    for agent_id, process in list(agents.items()):
        if (process is None or agent_id in agent_watcher_lost or not _watcher_lost(process)
                or agent_statuses.get(agent_id) != 'running' or process.poll() is None):
            continue
        if _end_unverified(process) or _uncertainty_unpersisted(process):
            continue  # Not a lost watcher: an unverified end, held by the execution policy.
        with _data_lock:
            agent_watcher_lost[agent_id] = WATCHER_LOST_REASON
            agent_outputs[agent_id].append(WATCHER_LOST_MARKER)
            _save_agent(agent_id)
        logger.error('Agent %s: %s', agent_id, WATCHER_LOST_REASON)
        task_id = _agent_task_id(agent_id)
        if _store is not None and task_id:
            with _store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("""UPDATE delegation_continuations SET phase='recovery_required',last_error=?
                    WHERE task_id=? AND phase NOT IN ('completed','stopped','recovery_required')""",
                           (WATCHER_LOST_REASON, task_id))
            _emit_agent_change(agent_id)
        marked.append(agent_id)
    return marked


def start_recovery_sweep(interval=SWEEP_INTERVAL_SECONDS):
    """Backend lifespan only (never reads or tests): periodic detect-and-mark sweep."""
    store = _store

    def tick():
        if store is None or store is not _store or _shutting_down:
            return
        try:
            sweep_lost_watchers()
        except Exception:
            logger.exception('Lost-watcher sweep failed; it will run again')
        _start_timer(interval, tick)
    _start_timer(interval, tick)


@_synchronized
def recover_lost_watcher(agent_id):
    """Explicit operator action: run a lost watcher's finalization (incl. structured handoff) once.

    No automatic Always/Similar continuation and no wakes: the Parent continues
    only through an explicit orchestration wake.
    """
    if agent_id not in agents:
        return None
    process = agents[agent_id]
    if (process is None or not _watcher_lost(process) or agent_statuses[agent_id] != 'running'
            or process.poll() is None):
        raise ValueError('Recovery applies only to an exited execution whose watcher was lost')
    if _end_unverified(process) or _uncertainty_unpersisted(process):
        # Its "failed recovery" path would stop the Agent and release ownership.
        raise ValueError('The end of this execution tree is unconfirmed; Stop it to verify termination first')
    if not process.stdout.closed or (_structured(process) and not process.stderr.closed):
        raise ValueError('Execution output is still being read; retry shortly')
    if getattr(process, '_control_center_recovery_attempted', False) is True:
        raise ValueError('Recovery was already attempted for this execution; inspect it before acting')
    process._control_center_recovery_attempted = True
    _wake_suppression.active = True
    try:
        _finalize_process(agent_id, process, allow_auto=False, schedule=False)
        failure = None if agent_statuses[agent_id] != 'running' else 'Recovery did not settle the execution'
    except Exception as exc:
        logger.exception('Explicit recovery of agent %s failed', agent_id)
        failure = f'Recovery failed ({type(exc).__name__}): {exc}'
    finally:
        _wake_suppression.active = False
    if failure:
        # Fail closed and inspectable: never leave it appearing to run.
        agent_statuses[agent_id] = 'stopped'
        agent_waiting_questions[agent_id] = None
        notifications.cancel(agent_id)
        agent_outputs[agent_id].append('--- Explicit recovery failed; execution stopped for inspection ---')
        _save_agent(agent_id)
        agent_watcher_lost[agent_id] = failure[:500]
        raise RuntimeError(failure[:500])
    agent_watcher_lost.pop(agent_id, None)
    return {'agent_id': agent_id, 'status': agent_statuses[agent_id]}


def _guard_row(entry, durable):
    """A decision row for a guarded execution whose journal update may have been lost."""
    base = dict(execution_id=entry.get('execution_id'), agent_id=entry.get('agent_id'), task_id=entry.get('task_id'),
                durable_state=durable['state'] if durable else None, uncertainty=None)
    if entry.get('job_config') and entry.get('job_name') and entry.get('root_pid') and entry.get('root_created_at'):
        return dict(base, containment='job_kill_on_close', state='contained', job_name=entry['job_name'],
                    job_config=entry['job_config'], root_pid=entry['root_pid'],
                    root_created_at=entry['root_created_at'], evidence={'launch': entry.get('launch') or {}})
    if entry.get('reason') == 'spawn_discard_unverified' and entry.get('root_pid') and entry.get('root_created_at'):
        return dict(base, containment='none', state='uncertain', uncertainty='launch_abort_unverified',
                    root_pid=entry['root_pid'], root_created_at=entry['root_created_at'],
                    evidence={'end': {'worker_code_may_have_run': False}})
    return dict(base, containment='legacy_unknown', state='uncertain', uncertainty='guard_without_evidence')


def reconcile_executions(*, task_ids=None, agent_ids=None):
    """Re-decide unresolved executions from kernel and reboot evidence; never launches.

    Tracked executions of this controller are only observed (Stop owns their
    termination); every other unresolved one, of this or a previous
    controller, is decided by execution_recovery. An emergency-guard entry ends
    only with a verified settlement. Returns the decisions.
    """
    store = _task_store()
    current = _controller_identity()
    wanted_agents, wanted_tasks = set(agent_ids or ()), set(task_ids or ())

    def selected(agent_id, task_id):
        return (agent_ids is None and task_ids is None) or agent_id in wanted_agents or (task_id and task_id in wanted_tasks)
    tracked = set()
    for key, process in list(agents.items()):
        execution_id = getattr(process, '_control_center_execution_id', None) if process is not None else None
        if isinstance(execution_id, str):
            tracked.add(execution_id)
        job = process_supervision.job_of(process)
        if job is None or job.ended is not None or job.handle is None or process.poll() is None \
                or not selected(key, getattr(process, '_control_center_task_id', None)):
            continue
        try:
            info = job.accounting()  # Observe only: never terminate a tracked execution here.
        except OSError:
            continue
        if info['active_processes'] == 0:
            job.ended = {**info, 'verified_at': process_supervision._now()}
            job.close()
            _record_end(process, 'reconcile')  # Upgrades the unconfirmed end and ends its guard.
    with store._connection() as db:
        rows = [r for r in execution_recovery.candidates(db, current['instance_id'], agent_ids=agent_ids,
                                                          task_ids=task_ids) if r['execution_id'] not in tracked]
        with _guard_lock:
            entries = {t: dict(e) for t, e in _ownership_guard.items()
                       if e['state'] == 'unverified' and selected(e['agent_id'], e['task_id'])}
        decided = {r['execution_id'] for r in rows}
        synthetic = [(token, _guard_row(entry, executions.get(db, entry['execution_id']) if entry.get('execution_id') else None))
                     for token, entry in entries.items()
                     if entry.get('execution_id') not in tracked and entry.get('execution_id') not in decided]
    results = execution_recovery.resolve(store, current, rows=rows)
    settled = {execution_id for execution_id, state, _ in results if state not in executions.OPEN}
    with _guard_lock:
        for token in [t for t, e in _ownership_guard.items() if e.get('execution_id') in settled]:
            _ownership_guard.pop(token, None)
    for token, row in synthetic:
        state, uncertainty, details = execution_recovery.decide(row, current, current)
        if state in executions.OPEN:
            continue
        if row['execution_id'] and row['durable_state'] in executions.OPEN:
            identity = {k: row.get(k) for k in executions.IDENTITY}
            _record_execution(row['execution_id'], state, (row['durable_state'],), identity=identity,
                              evidence={'recovery': {'outcome': state, 'from_guard': True, **details}})
        _guard_end(token)  # Verified settlement, whether or not the journal write succeeded.
        results.append((row['execution_id'], state, uncertainty))
    return results


def request_diagnostics(execution_id, trigger):
    """Queue an observation (bounded); the lifespan worker runs it. Never blocks the caller."""
    if not isinstance(execution_id, str):
        return
    with _guard_lock:
        if execution_id in _diagnostic_requests or len(_diagnostic_requests) < _DIAGNOSTIC_QUEUE_LIMIT:
            _diagnostic_requests[execution_id] = trigger
    wakeup = _diagnostic_wakeup
    if wakeup is not None:
        wakeup.set()


def process_diagnostic_requests():
    """Observe every queued execution once (worker body; callable directly in tests)."""
    while True:
        with _guard_lock:
            if not _diagnostic_requests:
                return
            execution_id = next(iter(_diagnostic_requests))
            trigger = _diagnostic_requests.pop(execution_id)
        try:
            run_diagnostics(execution_id, trigger)
        except Exception:
            logger.exception('Diagnostics for execution %s failed', execution_id)


def start_diagnostics_worker():
    """Backend lifespan only: one daemon thread observing queued executions."""
    global _diagnostic_wakeup
    from threading import Event
    wakeup = _diagnostic_wakeup = Event()
    store = _store

    def run():
        while store is _store and not _shutting_down:
            wakeup.wait(SWEEP_INTERVAL_SECONDS)
            wakeup.clear()
            if store is not _store or _shutting_down:
                return
            process_diagnostic_requests()
    worker = Thread(target=run, daemon=True, name='execution-diagnostics')
    worker.start()
    wakeup.set()  # observe what startup queued


def _tracked_roots():
    return [p.pid for p in list(agents.values()) if p is not None and _execution_live(p) and isinstance(p.pid, int)]


def _tracked(execution_id):
    if any(getattr(p, '_control_center_execution_id', None) == execution_id and _execution_live(p)
           for p in list(agents.values()) if p is not None):
        return True
    with _guard_lock:
        return any(e.get('execution_id') == execution_id and e['state'] == 'launching' for e in _ownership_guard.values())


def _execution_lock(execution_id):
    with _guard_lock:
        return _execution_locks.setdefault(execution_id, RLock())


def run_diagnostics(execution_id, trigger, *, probe=execution_diagnostics.DiagnosticProbe):
    """Fresh observations for one unresolved execution, stored with their conclusions.

    Serialized per execution with every other diagnostic and with attestation
    (explicit refresh and the event-driven worker wait; an attestation in
    progress finishes first). No database transaction is held while processes
    are observed. Returns the stored diagnostic, or None when not unresolved.
    """
    with _execution_lock(execution_id):
        return _run_diagnostics(execution_id, trigger, probe)


def _run_diagnostics(execution_id, trigger, probe):
    store = _task_store()
    current = _controller_identity()
    with store._connection() as db:
        row = executions.get(db, execution_id)
        controller = executions.controller(db, row['controller_instance_id']) if row else None
    if row is None:
        raise LookupError('Execution not found')
    if row['state'] != 'uncertain':
        return None
    observation = execution_diagnostics.diagnose(row, controller, current, trigger=trigger,
                                                 tracked_roots=_tracked_roots(), probe=probe)
    conclusions = execution_diagnostics.eligibility(row, observation, tracked=_tracked(execution_id))
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        latest = db.execute('SELECT state,updated_at FROM executions WHERE execution_id=?', (execution_id,)).fetchone()
        if latest is None or latest['state'] != 'uncertain' or latest['updated_at'] != row['updated_at']:
            conclusions = {'eligible': False, 'reasons': [{'code': 'execution_changed',
                           'message': 'The execution changed while it was being observed; refresh again.'}]}
        diagnostic_id = execution_attestation.record_diagnostic(db, observation, conclusions, current['instance_id'])
        stored = execution_attestation.diagnostic(db, diagnostic_id)
    _emit_agent_change(row['agent_id'])
    return stored


def _execution_view(db, row):
    from .execution_diagnostics import REFUSALS
    controller = executions.controller(db, row['controller_instance_id'])
    view = {key: row.get(key) for key in ('execution_id', 'agent_id', 'task_id', 'provider', 'containment', 'state',
                                           'uncertainty', 'job_name', 'root_pid', 'root_created_at', 'created_at',
                                           'updated_at', 'ended_at')}
    view['attestation'] = row.get('attestation')
    view['controller'] = None if controller is None else {
        'legacy': str(controller['instance_id']).startswith('legacy-'), 'pid': controller['pid'],
        'started_at': controller['started_at'], 'stopped_at': controller['stopped_at'],
        'current': controller['instance_id'] == (_controller or {}).get('instance_id')}
    view['recovery'] = (row.get('evidence') or {}).get('recovery')
    latest = execution_attestation.latest_diagnostic(db, row['execution_id'])
    view['diagnostic'] = latest
    view['history'] = execution_attestation.history(db, row['execution_id'])
    # Advisory only: the server re-checks freshly before any settlement.
    conclusions = (latest or {}).get('conclusions') or execution_diagnostics.eligibility(row, None)
    if row['state'] != 'uncertain':
        conclusions = {'eligible': False, 'reasons': [{'code': 'not_unresolved', 'message': REFUSALS['not_unresolved']}]}
    view['attestation_offer'] = {**conclusions, 'statement': execution_attestation.STATEMENT,
                                 'review_window_seconds': execution_attestation.REVIEW_WINDOW_SECONDS,
                                 'reviewed_diagnostic_id': (latest or {}).get('diagnostic_id')}
    return view


def unresolved_executions(*, agent_id=None, task_id=None):
    """Read-only: unresolved executions of an Agent (and its Tasks) or a Task."""
    store = _task_store()
    tasks = set([task_id] if task_id else [])
    if agent_id:
        tasks.update(store.agent_task_ids(agent_id))
    with store._connection() as db:
        rows = {r['execution_id']: r for r in executions.blocking(db, agent_id=agent_id)} if agent_id else {}
        for key in tasks:
            rows.update({r['execution_id']: r for r in executions.blocking(db, task_id=key)})
        return {'executions': [_execution_view(db, r) for r in sorted(rows.values(), key=lambda r: r['created_at'])]}


def execution_detail(execution_id):
    store = _task_store()
    with store._connection() as db:
        row = executions.get(db, execution_id)
        if row is None:
            raise LookupError('Execution not found')
        return _execution_view(db, row)


def refresh_execution_diagnostics(execution_id):
    """Explicit refresh: authoritative reconciliation first, then fresh observations."""
    store = _task_store()
    with store._connection() as db:
        row = executions.get(db, execution_id)
    if row is None:
        raise LookupError('Execution not found')
    if row['state'] == 'uncertain':
        reconcile_executions(agent_ids=[row['agent_id']], task_ids=[row['task_id']] if row['task_id'] else [])
        run_diagnostics(execution_id, 'explicit_refresh')
    return execution_detail(execution_id)


class AttestationRefused(ValueError):
    """A refused human attestation: `reasons` explains why; nothing changed but the audit."""

    def __init__(self, message, reasons, detail=None):
        super().__init__(message)
        self.reasons, self.detail = reasons, detail


def attest_execution(execution_id, *, request_key, reviewed_diagnostic_id, statement, reason=None, actor):
    """Human responsibility attestation for one eligible unresolved execution (never technical proof).

    Order: replay check, Task lock, statement and review freshness, authoritative
    reconciliation, fresh diagnostics without a write transaction, eligibility,
    then one atomic transaction that revalidates, audits, settles and releases
    only what this execution held. Refusals are audited and change nothing else.
    """
    store = _task_store()
    if not isinstance(request_key, str) or not 1 <= len(request_key) <= 128 or not request_key.isprintable():
        raise ValueError('request_key must be 1-128 printable characters')
    if reason is not None and (not isinstance(reason, str) or len(reason) > 2000):
        raise ValueError('reason must be at most 2000 characters')
    with store._connection() as db:
        replay = execution_attestation.by_request_key(db, request_key)
        row = executions.get(db, execution_id)
    if replay is not None:
        # Idempotent: the same request returns its recorded outcome, never a second decision.
        # A refusal recorded before its review was known (None) matches any review of the same execution.
        if replay['execution_id'] != execution_id or replay['reviewed_diagnostic_id'] not in (None, reviewed_diagnostic_id):
            raise ValueError('request_key was already used for a different attestation')
        if replay['outcome'] == 'refused':
            raise AttestationRefused('This attestation request was already refused', replay['refusal'],
                                     execution_detail(execution_id))
        return {'attestation': replay, 'outcome': 'settled_by_attestation', 'replayed': True,
                'execution': execution_detail(execution_id)}
    if row is None:
        raise LookupError('Execution not found')
    if statement != execution_attestation.STATEMENT:
        raise ValueError('The attestation statement must be acknowledged exactly as presented')
    def busy():
        # Not audited: nothing was evaluated, and the request may be retried with its key.
        return AttestationRefused('Another recovery decision for this execution or its Task is in progress',
                                  [{'code': 'concurrent_decision', 'message': 'Another recovery decision is in progress.'}],
                                  execution_detail(execution_id))
    lock = _execution_lock(execution_id)
    if not lock.acquire(blocking=False):  # another attestation, or a diagnostic in flight
        raise busy()
    try:
        with (task_lock(store, row['task_id'], blocking=False) if row['task_id'] else nullcontext()):
            return _attest(store, row, request_key, reviewed_diagnostic_id, reason, actor)
    except TaskBusy:
        raise busy() from None
    finally:
        lock.release()


def _attest(store, row, request_key, reviewed_diagnostic_id, reason, actor):
    current = _controller_identity()

    def refuse(reasons, message, fresh_id=None, audited_row=None):
        try:
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                execution_attestation.record(db, outcome='refused', request_key=request_key, row=audited_row or row,
                                             controller_instance_id=current['instance_id'],
                                             reviewed_diagnostic_id=reviewed_diagnostic_id if reviewed else None,
                                             fresh_diagnostic_id=fresh_id, actor=actor, reason=reason, refusal=reasons)
        except Exception:
            logger.exception('Could not audit a refused attestation for %s', row['execution_id'])
        raise AttestationRefused(message, reasons, execution_detail(row['execution_id']))
    with store._connection() as db:
        latest = execution_attestation.latest_diagnostic(db, row['execution_id'])
        reviewed = execution_attestation.diagnostic(db, reviewed_diagnostic_id) if reviewed_diagnostic_id else None
    if reviewed is not None and reviewed['execution_id'] != row['execution_id']:
        reviewed = None  # Another execution's diagnostic is never recorded as this one's review.
    if row['state'] != 'uncertain':
        from .execution_diagnostics import REFUSALS
        refuse([{'code': 'not_unresolved', 'message': REFUSALS['not_unresolved']}], REFUSALS['not_unresolved'])
    if reviewed is None:
        refuse([{'code': 'unknown_review', 'message': 'The reviewed diagnostic does not belong to this execution.'}],
               'Review the latest diagnostics before attesting')
    if latest is None or latest['diagnostic_id'] != reviewed_diagnostic_id:
        refuse([{'code': 'stale_review', 'message': 'Newer diagnostics exist; review them first.'}],
               'Newer diagnostics exist; review them before attesting')
    age = (execution_recovery._time(process_supervision._now()) - execution_recovery._time(reviewed['observed_at']))
    if age.total_seconds() > execution_attestation.REVIEW_WINDOW_SECONDS or not reviewed['conclusions'].get('eligible'):
        refuse([{'code': 'review_expired' if reviewed['conclusions'].get('eligible') else 'review_not_eligible',
                 'message': 'The reviewed diagnostics are too old or did not allow attestation; refresh them.'}],
               'Refresh diagnostics before attesting')
    # Authoritative evidence first: if it settles the execution, no attestation is needed.
    reconcile_executions(agent_ids=[row['agent_id']], task_ids=[row['task_id']] if row['task_id'] else [])
    with store._connection() as db:
        fresh_row = executions.get(db, row['execution_id'])
    if fresh_row['state'] != 'uncertain':
        return {'attestation': None, 'outcome': 'settled_by_evidence', 'execution': execution_detail(row['execution_id'])}
    fresh = run_diagnostics(row['execution_id'], 'attestation_recheck')
    if fresh is not None and _tracked(row['execution_id']):
        fresh = {**fresh, 'conclusions': {'eligible': False, 'reasons': [
            {'code': 'tracked_live', 'message': execution_diagnostics.REFUSALS['tracked_live']}]}}
    if fresh is None or not fresh['conclusions'].get('eligible'):
        refuse((fresh or {}).get('conclusions', {}).get('reasons') or
               [{'code': 'execution_changed', 'message': 'The execution changed; review it again.'}],
               'Attestation is not allowed: the fresh check found a reason to refuse',
               fresh_id=(fresh or {}).get('diagnostic_id'), audited_row=fresh_row)
    try:
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # The fresh check above read this exact row version; anything newer refuses.
            attestation_id = execution_attestation.settle(
                db, request_key=request_key, row=fresh_row, controller_instance_id=current['instance_id'],
                reviewed_diagnostic_id=reviewed_diagnostic_id, fresh_diagnostic_id=fresh['diagnostic_id'],
                actor=actor, reason=reason)
    except execution_attestation.Changed as exc:
        refuse([{'code': exc.code, 'message': str(exc)}], str(exc), fresh_id=fresh['diagnostic_id'],
               audited_row=fresh_row)
    with _guard_lock:
        for token in [t for t, e in _ownership_guard.items() if e.get('execution_id') == row['execution_id']]:
            _ownership_guard.pop(token, None)
    for process in agents.values():
        if process is not None and getattr(process, '_control_center_execution_id', None) == row['execution_id']:
            process._control_center_uncertainty_unpersisted = False
    _emit_agent_change(row['agent_id'])
    with store._connection() as db:
        settled = [a for a in execution_attestation.history(db, row['execution_id']) if a['attestation_id'] == attestation_id][0]
    return {'attestation': settled, 'outcome': 'settled_by_attestation', 'execution': execution_detail(row['execution_id'])}


def _require_settled(task_ids, confirm_ended, agent_ids=()):
    """The shared precondition of every explicit recovery action (Stage 2D.1b policy).

    Settlement comes only from kernel or reboot evidence (reconciled here).
    The operator confirmation and the clean process scan remain required, but
    neither is ever evidence that an execution ended.
    """
    if confirm_ended is not True:
        raise recovery_actions.RecoveryRefused('Explicit confirmation {"confirm_ended": true} is required')
    task_ids, agent_ids = [t for t in task_ids if t], [a for a in agent_ids if a]
    _refuse_live(task_ids)
    reconcile_executions(task_ids=task_ids, agent_ids=agent_ids)
    with _task_store()._connection() as db:
        unresolved = {r['execution_id']: r for t in task_ids for r in executions.blocking(db, task_id=t)}
        unresolved.update({r['execution_id']: r for a in agent_ids for r in executions.blocking(db, agent_id=a)})
    with _guard_lock:
        guarded = [e for e in _ownership_guard.values()
                   if e['agent_id'] in agent_ids or (e['task_id'] and e['task_id'] in task_ids)]
    if unresolved or guarded:
        detail = executions.describe(list(unresolved.values())) or 'an in-memory unverified end'
        raise recovery_actions.RecoveryRefused(
            f'Process termination is not established ({detail}). A confirmation or a clean process scan is not '
            'proof; it settles only from kernel evidence (its Job verified or gone) or a verified Windows restart')
    _clean_host(confirm_ended)


def _supervised_roots():
    return [p.pid for p in agents.values()
            if p is not None and process_supervision.job_of(p) is not None and _execution_live(p)]


def _refuse_live(task_ids):
    store = _task_store()
    for task_id in task_ids:
        for assignment in store.list_task_assignments(task_id):
            process = agents.get(assignment['agent_id'])
            if _execution_live(process):
                raise recovery_actions.RecoveryRefused('A live execution handle exists; stop it before recovery')


def _clean_host(confirm_ended):
    recovery_actions.require_clean_host(confirm_ended, _supervised_roots(), scan=recovery_actions.scan_processes)


def confirm_process_ownership_stopped(task_id, confirm_ended):
    """Explicit: clear process_ownership_unknown after confirmation and a clean scan."""
    store = _task_store()
    if store.get_task(task_id) is None:
        raise LookupError('Task not found')
    blocker_type, reason = recovery_actions.OWNERSHIP_BLOCKER
    with store._connection() as db:
        if not db.execute('SELECT 1 FROM task_blockers WHERE task_id=? AND blocker_type=? AND reason_code=? AND active=1',
                          (task_id, blocker_type, reason)).fetchone():
            raise recovery_actions.RecoveryRefused('This Task has no process-ownership block')
    _require_settled([task_id], confirm_ended)  # May itself settle and lift the block from evidence.
    with _state_lock, store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f"""UPDATE task_blockers SET active=0,resolved_at={delegation_materialization.NOW}
            WHERE task_id=? AND blocker_type=? AND reason_code=? AND active=1""", (task_id, blocker_type, reason))
        dependencies.reconcile(db)
    _emit_agent_change(None)
    return {'task_id': task_id, 'process_ownership': 'confirmed_stopped'}


def resolve_launch(delegation_id, confirm_ended):
    """Explicit: settle an uncertain Child launch; never launches anything."""
    store = _task_store()
    with store._connection() as db:
        row = db.execute('''SELECT m.*, d.child_task_id FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE m.delegation_id=?''', (delegation_id,)).fetchone()
    if row is None:
        raise LookupError('Delegation launch not found')
    if row['phase'] != 'recovery_required':
        raise recovery_actions.RecoveryRefused('Only a launch in recovery_required can be resolved')
    process = agents.get(row['launch_agent_id']) if row['launch_agent_id'] else None
    if _execution_live(process):
        raise recovery_actions.RecoveryRefused('A live execution handle exists for this launch')
    _require_settled([row['child_task_id']], confirm_ended, agent_ids=[row['launch_agent_id']])
    parked = delegation_materialization.configured_limits().preparation_attempts
    with _state_lock, store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM delegation_materializations WHERE delegation_id=?', (delegation_id,)).fetchone()
        if current['phase'] != 'recovery_required':
            raise recovery_actions.RecoveryRefused('Launch state changed; inspect it again')
        launched = current['launch_agent_id'] and db.execute(
            'SELECT 1 FROM task_assignments WHERE agent_id=? AND task_id=?', (current['launch_agent_id'], row['child_task_id'])).fetchone()
        if launched:
            phase, reason = 'started', 'Resolved by operator: the launch occurred and its process ended'
            db.execute(f"UPDATE delegation_materializations SET phase='started',last_error=?,updated_at={delegation_materialization.NOW} WHERE delegation_id=?",
                       (reason, delegation_id))
        else:
            # Parked with existing phases: attempts exhausted, so neither a wake pass
            # nor the follow-up can launch it; relaunch needs an explicit operator action.
            phase, reason = 'workspace_ready', ('Resolved by operator: no launch evidence. Parked; relaunch requires an '
                                                'explicit operator action')
            db.execute(f"""UPDATE delegation_materializations SET phase='workspace_ready',attempts=?,last_error=?,
                launch_agent_id=NULL,updated_at={delegation_materialization.NOW} WHERE delegation_id=?""", (parked, reason, delegation_id))
    _emit_agent_change(None)
    return {'delegation_id': delegation_id, 'phase': phase, 'last_error': reason}


def rearm_continuation(task_id, confirm_ended):
    """Explicit: return a recovery_required/stopped continuation to waiting; never resumes it."""
    store = _task_store()
    task = store.get_task(task_id)
    if task is None:
        raise LookupError('Task not found')
    with store._connection() as db:
        current = delegation_orchestration.state(db, task_id)
        launching = db.execute("""SELECT 1 FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE d.parent_task_id=? AND m.phase='launching'""", (task_id,)).fetchone()
    if current is None or current['phase'] not in ('recovery_required', 'stopped'):
        raise recovery_actions.RecoveryRefused('Only a recovery_required or stopped continuation can be rearmed')
    if task['control_intent'] != 'active':
        raise recovery_actions.RecoveryRefused('Task work control is not active; resume it first')
    if launching:
        raise recovery_actions.RecoveryRefused('A Child launch is in progress for this Parent')
    _require_settled([task_id], confirm_ended)
    with _state_lock, store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = delegation_orchestration.state(db, task_id)
        if current['phase'] not in ('recovery_required', 'stopped'):
            raise recovery_actions.RecoveryRefused('Continuation state changed; inspect it again')
        deliveries = db.execute("SELECT delivery_id,result_ids FROM delegation_deliveries WHERE task_id=? AND status='recovery_required'",
                                (task_id,)).fetchall()
        for delivery in deliveries:
            ids = json.loads(delivery['result_ids'])
            if ids and db.execute(f"SELECT 1 FROM delegation_results WHERE phase='delivered' AND result_id IN ({','.join('?' * len(ids))})",
                                  ids).fetchone():
                raise recovery_actions.RecoveryRefused('A delivery in recovery_required already marked results delivered; inspect it')
        db.execute("UPDATE delegation_deliveries SET status='prepared' WHERE task_id=? AND status='recovery_required'", (task_id,))
        db.execute(f"""UPDATE delegation_continuations SET phase='waiting',mode=NULL,launch_id=NULL,delivery_id=NULL,
            last_error=NULL,updated_at={delegation_materialization.NOW} WHERE task_id=?""", (task_id,))
        dependencies.reconcile(db)
    _emit_agent_change(None)
    return {'task_id': task_id, 'continuation': 'waiting', 'next': 'POST /tasks/{task_id}/orchestration/wake'}


def reverify_transitions(task_id, confirm_ended):
    """Explicit: re-check recovery_required replanning transitions; never stops or launches."""
    store = _task_store()
    if store.get_task(task_id) is None:
        raise LookupError('Task not found')
    with store._connection() as db:
        rows = [dict(r) for r in db.execute('''SELECT t.* FROM replan_transitions t JOIN replan_decisions d USING(decision_id)
            WHERE d.task_id=? AND t.phase='recovery_required' ORDER BY t.rowid''', (task_id,))] if replanning.exists(db) else []
    if not rows:
        raise recovery_actions.RecoveryRefused('No replanning transition requires recovery')
    targets = [r['task_id'] for r in rows if r['task_id']]
    _require_settled(targets, confirm_ended)
    results = []
    with _state_lock, store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            child = dependencies.task(db, row['task_id']) if row['task_id'] else None
            open_assignment = child is not None and db.execute(
                'SELECT 1 FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (child['task_id'],)).fetchone()
            if child is None or (child['status'] == 'canceled' and not child['stop_required'] and not open_assignment):
                phase, reason = 'stopping', 'Operator reverified termination; the next replanning pass completes it'
            else:
                phase, reason = 'recovery_required', 'Reverify: termination not confirmed (Child not canceled, unsettled or assigned)'
            db.execute('UPDATE replan_transitions SET phase=?,reason=? WHERE transition_id=? AND phase=?',
                       (phase, reason, row['transition_id'], 'recovery_required'))
            results.append({'transition_id': row['transition_id'], 'phase': phase, 'reason': reason})
    _emit_agent_change(None)
    return {'task_id': task_id, 'transitions': results}


def _schedule_materialization(parent_task_id):
    # Coalesced, bounded pass outside the reader/finalizer and lifecycle lock.
    # No automatic scheduling during initialization or recovery.
    wake_orchestration(parent_task_id, reason='materialization')


def _wake_delegation_siblings(agent_id):
    if _store is None or _shutting_down:
        return
    with _store._connection() as db:
        row = db.execute('''SELECT d.parent_task_id FROM delegations d JOIN task_assignments a
            ON a.task_id=d.child_task_id WHERE a.agent_id=?''', (agent_id,)).fetchone()
        if not row:
            return
        parent = _store.get_task(row['parent_task_id'])
        receipt = parent['orchestration_handoff']
        # Only live-run activation may schedule successors. Restored parents
        # have no process; recovery always requires an explicit backend call.
        if not receipt or agents.get(receipt['agent_id']) is None:
            return
    _schedule_materialization(row['parent_task_id'])


def _schedule_hybrid_for_agent(agent_id):
    if _store is None or _shutting_down:
        return
    task_id = _agent_task_id(agent_id)
    if not task_id:
        return
    with _store._connection() as db:
        current = delegation_orchestration.state(db, task_id)
    if current:
        wake_orchestration(task_id, ('advance',), reason='hybrid')


def _notify_hybrid_child_end(agent_id):
    if _store is None or _shutting_down:
        return
    with _store._connection() as db:
        row = db.execute('''SELECT 1 FROM task_assignments a JOIN delegations d ON d.child_task_id=a.task_id
            JOIN delegation_continuations c ON c.task_id=d.parent_task_id WHERE a.agent_id=?''', (agent_id,)).fetchone()
    if row:
        _wake_delegation_siblings(agent_id)


def advance_replanning(task_id, *, report_busy=False):
    """Explicit/live-event deterministic pass. Never called by startup or GET."""
    store = _task_store()
    with task_lock(store, task_id, blocking=False), _state_lock:
        def stop(identifier):
            assignment = store.get_active_assignment_for_task(identifier)
            if assignment and agents.get(assignment['agent_id']) is None:
                raise RuntimeError('Unknown Worker handle; termination cannot be confirmed')
            _settle_tasks([identifier], cascade=False)
        def quiet(identifier):
            for assignment in store.list_task_assignments(identifier):
                process = agents.get(assignment['agent_id'])
                if process is not None and (process.poll() is None or not process.stdout.closed):
                    raise ValueError('Child process/output remains active')
        service = replanning.TransitionService(store, stop, quiet, _emit_agent_change)
        rows = service.run(task_id)
    if report_busy and service.busy:
        raise TaskBusy('Replanning transition target busy; retry shortly')
    return rows


def advance_delegations(task_id):
    """Backend-only bounded pass; never called by startup or polling."""
    store = _task_store()
    # Acquire the Task lock nonblocking before runtime serialization. Starts use
    # this same order; existing resume paths use nonblocking Task acquisition.
    with task_lock(store, task_id, blocking=False), _state_lock:
        if _shutting_down:
            return None
        with store._connection() as db:
            current = delegation_orchestration.state(db, task_id)
        if not current:
            return None

        def quiet(identifier):
            for assignment in store.list_task_assignments(identifier):
                process = agents.get(assignment['agent_id'])
                if process is not None and (process.poll() is None or not process.stdout.closed):
                    raise ValueError('Result synchronization requires a quiet Task Workspace')
            with store._connection() as db:
                if db.execute('''SELECT 1 FROM task_assignments a JOIN agents g USING(agent_id)
                    WHERE a.task_id=? AND a.ended_at IS NULL AND g.status IN ('running','waiting')''', (identifier,)).fetchone():
                    raise ValueError('Result synchronization cannot interrupt an active Parent or Child')

        def resume(identifier, launch):
            key = launch['agent_id']
            quiet(identifier)
            providers.require_capability(agent_types[key], 'hybrid', 'Hybrid Parent continuation')
            command = _agent_command(key, structured=True)
            _resume_agent(key, command, launch['prompt'], '--- Parent continuation: ' + launch['mode'] + ' ---')
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                delegation_orchestration.launched(db, identifier, launch['launch_id'])
            _emit_agent_change(key)

        service = DeliveryService(store, quiet, integrate_task, resume, _emit_agent_change)
        try:
            return service.run(task_id)
        except (ValueError, LookupError) as exc:
            if isinstance(exc, TaskBusy):
                raise  # Not a verdict: the waker retries a busy pass.
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE delegation_continuations SET last_error=? WHERE task_id=? AND phase='waiting'", (str(exc)[:500], task_id))
            _emit_agent_change(None)
            return None
        except Exception as exc:
            # A defect, not a domain refusal: make it visible without hiding it.
            # Never retried here; an existing inspection reason is not replaced.
            reason = f'Unexpected continuation error ({type(exc).__name__}): {exc}'[:500]
            try:
                with store._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE delegation_continuations SET last_error=? WHERE task_id=? AND (phase='waiting' OR last_error IS NULL)",
                               (reason, task_id))
                _emit_agent_change(None)
            except (sqlite3.Error, OSError):
                logger.exception('Could not record continuation error for Task %s', task_id)
            raise


@_synchronized
def get_agents() -> list[dict[str, str | None]]:
    result = []
    removals = {}
    if _store is not None:
        with _store._connection() as db:
            removals = agent_removal.agent_states(db)
    for agent_id in list(agents):
        _refresh_status_for_read(agent_id)
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
            "model": agent_models.get(agent_id),
            "effort": agent_efforts.get(agent_id),
            "sandbox": agent_sandboxes[agent_id],
            "waiting_question": agent_waiting_questions[agent_id],
            "provider_condition": providers.condition_view(agent_conditions.get(agent_id)),
            "removed": bool(removals.get(agent_id) and removals[agent_id]['state'] == 'removed'),
            "removal": removals.get(agent_id),
        })
    return result


def _removal_fields(agent_id):
    """Read-only Stage 2D.2 removal state for an Agent read model."""
    if _store is None:
        return {'removed': False, 'removal': None}
    with _store._connection() as db:
        removal = agent_removal.agent_states(db, [agent_id]).get(agent_id)
    return {'removed': bool(removal and removal['state'] == 'removed'), 'removal': removal}


@_synchronized
def get_agent(agent_id: str, include_output: bool = True) -> dict[str, str | bool | list[str] | None] | None:
    if agent_id not in agents:
        return None
    _refresh_status_for_read(agent_id)
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
        "model": agent_models.get(agent_id),
        "sandbox": agent_sandboxes[agent_id],
        "provider_condition": providers.condition_view(agent_conditions.get(agent_id)),
        "provider_history": _provider_history(agent_id),
        "managed": bool(_store and _store.execution_settings(agent_id)),
        "effort": agent_efforts.get(agent_id),
        "session_config": list(agent_session_configs[agent_id]) if agent_session_configs.get(agent_id) else None,
        "latest_execution": latest_execution_config(agent_id),
        "resume": _resume_view(agent_id),
        "parent_id": agent_parents[agent_id],
        "child_ids": [child for child, parent in agent_parents.items() if parent == agent_id],
        "session_id": agent_sessions[agent_id],
        "waiting_question": agent_waiting_questions[agent_id],
        "waiting_context": _waiting_context(agent_id),
        "similar_decisions_enabled": agent_similar_decisions[agent_id].enabled,
        "always_decide_enabled": agent_always_decisions[agent_id].enabled,
        "watcher_lost": agent_watcher_lost.get(agent_id),
        "task_ids": _store.agent_task_ids(agent_id) if _store else [],
        **_allocation_fields(agent_id),
        **_removal_fields(agent_id),
        **output,
    }


def _waiting_context(agent_id):
    if _store:
        from .presentation import agent_context
        context = agent_context(_store, agent_id)
        return context['waiting_context'] if context and context['status'] == agent_statuses[agent_id] else None
    if agent_statuses[agent_id] == 'waiting' and agent_waiting_questions[agent_id]:
        return {'kind': 'user', 'label': 'Waiting for you'}
    view = providers.condition_view(agent_conditions.get(agent_id))
    if agent_statuses[agent_id] == 'waiting' and view:
        return {'kind': 'provider', 'label': view['label'], 'condition': view['kind']}
    return None


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
def _stop_policy_violation(agent_id, process):
    """Stop only the offending current execution, never its replacement."""
    context = _structured(process)
    if (agents.get(agent_id) is not process or not context
            or not context.events.native_collaboration_prohibited
            or not context.events.native_collaboration_seen
            or (context.origin and not _task_store().execution_is_current(context.origin))):
        return
    agent_outputs[agent_id].append(managed_orchestration.VIOLATION)
    _save_agent(agent_id)
    stop_agent(agent_id)


def _require_managed_resume(agent_id):
    if not _store or not _store.execution_settings(agent_id):
        return
    context = _structured(agents.get(agent_id))
    seen = bool(context and context.events.native_collaboration_seen)
    # Historical provider activity is not adopted as managed work. Query only
    # candidate wire records, then parse their structure (not prose keywords).
    with _store._connection() as db:
        lines = db.execute("SELECT line FROM output WHERE agent_id=? AND (instr(line, 'collab_tool_call')>0 "
                           "OR instr(line, '\"tool_use\"')>0)", (agent_id,))
        seen = seen or any(managed_orchestration.collaboration_event(r[0]) or provider_events.collaboration_event(r[0])
                           for r in lines)
    if seen:
        raise ValueError(f'Cannot resume this managed session: prior native {providers.LABELS[agent_types[agent_id]]} '
                         'collaboration requires explicit inspection; no provider Children are adopted or resumed.')


@_synchronized
def stop_agent(agent_id: str) -> dict[str, str] | None:
    if agent_id not in agents:
        return None
    process = agents[agent_id]
    task_id = _agent_task_id(agent_id)
    if _store and task_id:
        with _store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if delegation_orchestration.state(db, task_id):
                delegation_orchestration.revoke(db, task_id)
                dependencies.reconcile(db)
    if _structured(process):
        _structured(process).revoked = True
    if process is not None and process.poll() is not None and _end_unverified(process):
        # The root exited but its tree's end was never confirmed: verify it now
        # (raises "stop was not confirmed" and keeps every gate on failure).
        _stop_windows_tree(process)
    if process is None or process.poll() is not None:
        if process is not None and agent_statuses[agent_id] == "running" and not process.stdout.closed:
            reader = agent_readers[agent_id]
            reader.join(timeout=3)
            if reader.is_alive():
                raise RuntimeError("Final process output is still being read; please retry Stop.")
        _refresh_status(agent_id, allow_auto=False)  # Retries an unrecorded unverified end (idempotent).
        _require_recorded_end(process)
        if agent_statuses[agent_id] == "waiting":
            agent_statuses[agent_id] = "stopped"
            notifications.cancel(agent_id)
            _save_agent(agent_id)
        _notify_hybrid_child_end(agent_id)
        return {"agent_id": agent_id, "status": agent_statuses[agent_id]}

    if (agent_types[agent_id] in providers.REAL and os.name == "nt") or process_supervision.job_of(process):
        _stop_windows_tree(process)
    else:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    _record_end(process, 'stop')  # Journal the observed end before the stopped status is saved.
    _require_recorded_end(process)

    agent_statuses[agent_id] = "stopped"
    assignment_id = getattr(process, '_control_center_assignment', None)
    if isinstance(assignment_id, str):
        process._control_center_completion = dict(assignment_id=assignment_id, returncode=process.poll(), validated=False)
    notifications.cancel(agent_id)
    _save_agent(agent_id)
    _notify_hybrid_child_end(agent_id)
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
def redirect_agent(agent_id: str, instruction: str, config=None) -> dict[str, str] | None:
    with _prompt_config(config):
        return _redirect_agent(agent_id, instruction)


@_workspace_session
def _redirect_agent(agent_id: str, instruction: str) -> dict[str, str] | None:
    if not instruction.strip():
        raise ValueError("Redirect instruction must not be blank.")
    if agent_id not in agents:
        return None
    process = agents[agent_id]
    providers.require_capability(agent_types[agent_id], 'redirect', 'Redirect')
    if process is None or process.poll() is not None or agent_statuses[agent_id] != "running":
        raise ValueError("Only running agents can be redirected.")
    session_id = agent_sessions[agent_id]
    if not session_id:
        raise ValueError("Redirect will be available once the provider session starts.")
    # Resolve and validate before interrupting the current process.
    _require_managed_resume(agent_id)
    command = _agent_command(agent_id, session_id=session_id)
    cwd = _resume_cwd(agent_id)
    task_id = _agent_task_id(agent_id)
    if task_id:
        reject_divergence(evaluate_workspace_freshness(_store, task_id))
    if _structured(process):
        _structured(process).revoked = True
    _stop_windows_tree(process)
    try:
        return _resume_agent(agent_id, command, instruction, "--- Redirect ---", cwd=cwd)
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        if agents[agent_id] is process:
            agent_statuses[agent_id] = "stopped"
            _save_agent(agent_id)
        raise


@_synchronized
def reply_agent(agent_id: str, answer: str, config=None) -> dict[str, str] | None:
    if not answer.strip():
        raise ValueError("Reply answer must not be blank.")
    with _prompt_config(config):
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
    providers.require_capability(agent_types[agent_id], 'decisions', 'Similar decisions')
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
    providers.require_capability(agent_types[agent_id], 'decisions', 'Always Decide')
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
    _launch.continuity = None
    if agent_id in agents:
        # A provider that cannot continue a session is refused with its precise reason first.
        providers.require_capability(agent_types[agent_id], 'reply', 'Replying')
    task_id = _agent_task_id(agent_id)
    if task_id:
        _execution_preflight(task_id)
    if agent_id not in agents:
        return None
    _refresh_status(agent_id, allow_auto=False)
    if agent_statuses[agent_id] != "waiting":
        raise ValueError("Only waiting agents can receive replies.")
    session_id = agent_sessions[agent_id]
    handoff = _pending_handoff(agent_id)
    no_session_wait = not session_id and providers.condition_view(agent_conditions.get(agent_id)) is not None
    if not session_id and handoff is None and not no_session_wait:
        raise ValueError("The provider session ID is not available.")
    _require_managed_resume(agent_id)
    # After a provider change the new provider starts a new session (never the
    # previous provider's); the handoff prompt carries the Task context. A limit
    # reached before any session existed continues in a new session the same way.
    if no_session_wait and handoff is None:
        _launch.continuity = 'no_session'
    return _agent_command(agent_id, fresh=(handoff is not None or no_session_wait) and not session_id)


@_workspace_session
def _resume_agent(agent_id: str, command: str, text: str, marker: str,
                  history_text: str | None = None,
                  automatic_question: str | None = None,
                  always_decision: bool = False, automatic_always: bool = False,
                  cwd=None, automatic_retry: bool = False) -> dict[str, str]:
    execution = _structured(agents[agent_id])
    previous_hybrid = None
    if always_decision and _store:
        with _store._connection() as db:
            previous_hybrid = delegation_orchestration.state(db, _agent_task_id(agent_id))
    if execution and execution.origin and not _task_store().execution_is_current(execution.origin):
        raise ValueError('Cannot resume a stale execution assignment')
    # Drain the old process before adding the marker or resumed output.
    reader = agent_readers.get(agent_id)
    if reader is not None:
        reader.join(timeout=3)
        if reader.is_alive():
            raise RuntimeError("Old process output has not closed; resume was not started.")
    _require_managed_resume(agent_id)
    previous = (agents[agent_id], agent_statuses[agent_id], agent_waiting_questions[agent_id],
                agent_sessions[agent_id], agent_outputs[agent_id])
    cwd = cwd if cwd is not None else _resume_cwd(agent_id)
    task_id = _agent_task_id(agent_id)
    state = ensure_workspace_current(_store, task_id) if task_id else None
    if state:
        state['integration_order'] = _store.integration_cursor(task_id)
    invalidation = session_context(_store, agent_id, state) if state else ''
    if _store and _store.execution_settings(agent_id):
        command = _agent_command(agent_id, structured=True)
    handoff = _pending_handoff(agent_id)
    continuity = getattr(_launch, 'continuity', None)
    config = _execution_config() or {'model': agent_models.get(agent_id), 'effort': agent_efforts.get(agent_id)}
    replacement = _spawn_with_resources(task_id, agent_id, command, agent_types[agent_id], cwd,
                                        model=config.get('model'))
    previous_session_config = agent_session_configs.get(agent_id)
    if continuity:
        agent_sessions[agent_id] = None  # The new session's id arrives from the provider.
    agent_session_configs[agent_id] = (config.get('model'), config.get('effort'))
    history = [marker, text if history_text is None else history_text]
    if process_supervision.job_of(replacement):
        history.append(process_supervision.MARKER)
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
    previous_question = agent_waiting_questions[agent_id]
    agent_waiting_questions[agent_id] = None
    agent_statuses[agent_id] = "running"
    agent_conditions[agent_id] = None
    if not automatic_retry:
        _provider_retry_attempts.pop(agent_id, None)
    policy = agent_similar_decisions[agent_id]
    policy.automatic_question = automatic_question
    policy.handled = False
    always = agent_always_decisions[agent_id]
    always.automatic_attempt = automatic_always
    always.handled = False
    notifications.cancel(agent_id)
    saved = _save_agent(agent_id)
    try:
        try:
            if not saved:
                raise OSError('Could not persist resumed execution before prompt delivery')
            _bind_execution(agent_id, replacement)
            _bind_stream(agent_id, replacement)
            if _store:
                replacement._control_center_assignment = _store.get_active_assignment_for_agent(agent_id)['assignment_id']
        finally:
            _start_reader(agent_id, replacement)
        with replacement.stdin:
            prompt = invalidation + _execution_prompt(agent_id, text, replacement)
            if handoff is not None:
                prompt = _handoff_prompt(agent_id, handoff, previous_question) + prompt
                _complete_handoff(handoff, replacement)
            elif continuity:
                prompt = _continuity_prompt(agent_id, continuity, previous_question) + prompt
            if not always_decision and always.configured:
                setting = "enabled" if always.enabled else "disabled"
                prompt += (f"\n\nCurrent control-center setting: Always Decide is {setting}. "
                           "This supersedes earlier Always Decide settings in this session. "
                           "When disabled, honor the current request and any explicit current decision "
                           "delegation, but do not apply earlier Always Decide authorization.")
            replacement.stdin.write(prompt)
    except (OSError, ValueError, LookupError, sqlite3.Error):
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
                old_execution, failed_execution = _structured(old_process), _structured(replacement)
                if old_execution and old_execution.origin and failed_execution and failed_execution.origin:
                    _store.restore_delegation_execution(old_execution.origin, failed_execution.origin, previous_hybrid=previous_hybrid)
                agents[agent_id] = old_process
                agent_statuses[agent_id] = old_status
                agent_waiting_questions[agent_id] = old_question
                agent_sessions[agent_id] = old_session
                agent_session_configs[agent_id] = previous_session_config
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
    job = process_supervision.job_of(process)
    if job is not None:
        # The Job contains every descendant, including orphans taskkill /T misses.
        process._control_center_end = 'stop'  # The watcher may record the end first.
        try:
            job.terminate()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process._control_center_end = None
            raise RuntimeError("Could not terminate the Codex process tree; stop was not confirmed.") from None
        job.close()
        _record_end(process, 'stop')
        return
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
            with store._connection() as db:
                delegation_safety.require_known_ownership(db, current_task_id)
                blocker_type, reason = recovery_actions.OWNERSHIP_BLOCKER
                if db.execute('SELECT 1 FROM task_blockers WHERE task_id=? AND blocker_type=? AND reason_code=? AND active=1',
                              (current_task_id, blocker_type, reason)).fetchone():
                    raise ValueError('Integration refuses a Task whose process ownership is unknown; confirm it stopped first')
            for assignment in store.list_task_assignments(current_task_id):
                key = assignment['agent_id']
                process = agents.get(key)
                if _execution_live(process):
                    raise ValueError('Integration refuses a Task Workspace with a live Agent process')
                if key not in agents or process is None:
                    record = next((r for r in store.load_agents(output_limit=0) if r['agent_id'] == key), None)
                    if record and record['status'] == 'running':
                        raise ValueError('Integration refuses a Task with an unowned running Agent')
    return integrate(store, task_id, assert_quiet)


def _settle_coordination():
    # Called after durable lifecycle mutation, never from the output reader or
    # inside SQL. The state lock protects runtime identity while draining.
    if _store is not None:
        pending = _store.unsettled_tasks()
        if pending:
            _settle_tasks(pending)


def _settle_tasks(task_ids, *, cascade=True):
    """Phase 2: durable gates already exist; never wait on a process in SQL."""
    failures, first_error = [], None
    pending = set(task_ids) | (set(_store.unsettled_tasks()) if cascade else set())
    visited = set()
    while pending:
        task_id = min(pending)
        pending.remove(task_id)
        visited.add(task_id)
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
                    if _execution_live(process):
                        if (agent_types[key] in providers.REAL and os.name == 'nt') or process_supervision.job_of(process):
                            _stop_windows_tree(process)
                        else:
                            process.terminate()
                            try:
                                process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=2)
                        _record_end(process, 'stop')
                    if process is not None:
                        if _execution_live(process):
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
        # Ending a worker can release claims and change other participants.
        if cascade:
            pending.update(set(_store.unsettled_tasks()) - visited)
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
        _settle_coordination()
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


@_synchronized
def create_work_intent(task_id, **options):
    store = _task_store()
    with task_lock(store, task_id, blocking=False):
        identifier, changed = store.create_work_intent(task_id, **options)
        if changed:
            _settle_coordination()
        _emit_agent_change(None)
    return next(i for i in store.work_intents(task_id) if i['intent_id'] == identifier)


@_synchronized
def release_work_intent(task_id, intent_id):
    store = _task_store()
    with task_lock(store, task_id, blocking=False):
        changed = store.release_work_intent(task_id, intent_id)
        if changed:
            _settle_coordination()
        _emit_agent_change(None)
    return next(i for i in store.work_intents(task_id) if i['intent_id'] == intent_id)


# Stage 2D.1: durable single-Agent removal. The journal (agent_removal.py)
# commits intent before any process work; Removed is recorded only after the
# execution is verified settled. Startup never resumes or settles live work.

def removal_preview(agent_id, mode='single', task_disposition='preserve'):
    """Read-only: impact and the authoritative plan_token. Never stops anything."""
    preview = _task_store().removal_preview(agent_id, mode, task_disposition)
    # In-memory runtime evidence is reported but excluded from the plan token.
    preview['watcher_lost'] = agent_watcher_lost.get(agent_id)
    return preview


def get_removal(removal_id):
    return _task_store().removal(removal_id)


def list_agent_removals(agent_id):
    return _task_store().agent_removals(agent_id)


@_synchronized
def remove_agent(agent_id, *, request_key, plan_token, mode='single', task_disposition='preserve', reason=None):
    if _shutting_down:
        raise RuntimeError('Backend is shutting down; removal was not admitted')
    store = _task_store()
    task_id = _agent_task_id(agent_id)
    tasks = {task_id} if task_id else set()
    if mode == 'retire_branch':
        # Every member's Task: a changed branch is refused as stale at admission anyway.
        tasks |= {m['task_id'] for m in store.removal_preview(agent_id, mode, task_disposition)['members'] if m['task_id']}
    # The existing Task locks exclude integrations and worker starts on these Tasks.
    with ExitStack() as locks:
        for key in sorted(tasks):
            locks.enter_context(task_lock(store, key, blocking=False))
        operation, created = store.admit_removal(agent_id, request_key=request_key, plan_token=plan_token,
                                                 mode=mode, disposition=task_disposition, reason=reason)
        if not created:
            return operation  # Exact replay: report the durable operation, never re-execute.
        _emit_agent_change(agent_id)
        return _advance_removal(operation['removal_id'])


@_synchronized
def retry_removal(removal_id, confirm_ended=False):
    """Explicit retry of an unfinished removal; never launches anything."""
    store = _task_store()
    operation = store.removal(removal_id)
    if operation['phase'] == 'removed':
        return operation
    if operation['phase'] not in ('admitted', 'stopping', 'recovery_required'):
        raise agent_removal.RemovalError('not_retryable', 'This removal cannot be retried')
    pending = [m for m in operation['members'] if m['phase'] != 'removed']
    tasks = sorted({m['task_id'] for m in pending if m['task_id']})
    # Kernel and reboot evidence decide; `confirm_ended` is accepted for
    # compatibility but never settles an unresolved execution by itself.
    reconcile_executions(agent_ids=[m['agent_id'] for m in pending], task_ids=tasks)
    with ExitStack() as locks:
        for key in tasks:
            locks.enter_context(task_lock(store, key, blocking=False))
        return _advance_removal(removal_id)


def _advance_removal(removal_id, *, confirmed=False):
    store = _task_store()
    for member in store.removal(removal_id)['members']:
        if member['phase'] != 'removed':
            _settle_removal_member(removal_id, member['agent_id'], confirmed)
    operation = store.removal(removal_id)
    with store._connection() as db:
        for member in operation['members']:
            if member['phase'] == 'recovery_required':
                for row in executions.blocking(db, agent_id=member['agent_id']):
                    request_diagnostics(row['execution_id'], 'removal_awaiting_confirmation')
    _emit_agent_change(operation['target_agent_id'])
    return operation


def _settle_removal_member(removal_id, key, confirmed):
    store = _task_store()
    process = agents.get(key)
    if _structured(process):
        _structured(process).revoked = True  # A late structured exit accepts nothing.
    if key in agent_watcher_lost:
        store.fail_removal(removal_id, key, 'watcher_lost', agent_watcher_lost[key])
        return
    status = agent_statuses.get(key)
    live = _execution_live(process)
    outcome = 'no_active_execution'
    if live or status in ('running', 'waiting'):
        if status == 'running' and process is None:
            store.fail_removal(removal_id, key, 'no_process_handle')
            return
        outcome = 'stop_confirmed' if live or status == 'running' else 'waiting_session_ended'
        store.mark_removal_stopping(removal_id, key)
        _emit_agent_change(key)
        try:
            stop_agent(key)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # Termination is unconfirmed: keep the assignment, claims and gates.
            store.fail_removal(removal_id, key, 'stop_not_confirmed', str(exc))
            return
    if _execution_live(process):
        store.fail_removal(removal_id, key, 'stop_not_confirmed', 'process is still running')
        return
    reader = agent_readers.get(key)
    if reader is not None and reader.is_alive():
        reader.join(timeout=3)
        if reader.is_alive():
            store.fail_removal(removal_id, key, 'output_draining', 'final output is still being read')
            return
    if agent_statuses.get(key) == 'running':
        store.fail_removal(removal_id, key, 'worker_not_settled')
        return
    if key in agent_outputs and not _save_agent(key):
        store.fail_removal(removal_id, key, 'output_not_persisted', 'final output or status could not be saved')
        return
    try:
        store.settle_removal(removal_id, key, outcome, confirmed=confirmed)
    except agent_removal.RemovalError as exc:
        store.fail_removal(removal_id, key, exc.code, str(exc))
        return
    with store._connection() as db:
        row = db.execute('SELECT status,waiting_question FROM agents WHERE agent_id=?', (key,)).fetchone()
    if key in agent_statuses:
        # Keep the runtime mirror identical to the durable row; later saves
        # rewrite these columns from memory.
        agent_statuses[key] = row['status']
        agent_waiting_questions[key] = row['waiting_question']
    notifications.cancel(key)
