"""Durable Agent removal journal (Stage 2D.1 foundation, Stage 2D.2 hierarchy-aware).

Removal retires an Agent identity; it never deletes or completes its Task. SQL
helpers run inside the caller's transaction. No process or filesystem work
happens here: the manager stops workers between short transactions.

Modes (Stage 2D.2):

- `single` removes only the selected Agent. Its descendants keep their own
  identities, lineage and execution authority; they continue where their work
  is independent. Work that needs the removed Agent (Hybrid delivery and
  acknowledgement, new Delegations, Child creation) waits for an explicit
  responsibility transfer (see responsibility.py).
- `retire_branch` removes the Agent and every descendant not yet removed in one
  operation, with one member row per Agent. The member set is fixed at
  admission: an Agent created later is never added silently.
- `continue_children` is superseded by `single` and stays unsupported.
"""
import json
import re
import sqlite3
from hashlib import sha256
from uuid import uuid4

from . import dependencies

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
MODES = ('single', 'retire_branch', 'continue_children')
SUPPORTED_MODES = ('single', 'retire_branch')
DISPOSITIONS = ('preserve', 'cancel')
SUPPORTED_DISPOSITIONS = ('preserve',)
LIVE_OPERATION = ('proposed', 'admitted', 'stopping', 'recovery_required')
LIVE_MEMBER = ('admitted', 'stopping', 'recovery_required')
# Failures that only an explicit operator confirmation with a clean process
# scan may settle. A missing handle or an output marker is never proof.
CONFIRMATION_REQUIRED = ('restart_interrupted_execution', 'process_ownership_unknown', 'no_process_handle')
REASON_LIMIT = 2000
_KEY = re.compile(r'[\x00-\x1f\x7f]')
_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
OWNERSHIP_BLOCKER = ('process_ownership', 'process_ownership_unknown')


class RemovalError(ValueError):
    """A refused removal request; `code` is a stable machine-readable reason."""
    status = 409

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


class Unsupported(RemovalError):
    status = 422


class StalePreview(RemovalError):
    def __init__(self, preview):
        super().__init__('stale_preview', 'Removal impact changed since the preview; review the new preview',
                         preview=preview)


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_removals'").fetchone() is not None


def migrate(db):
    """v26 -> v27. Idempotent: older downgrade tests re-run every later step."""
    db.execute(f'''CREATE TABLE IF NOT EXISTS agent_removals (
        removal_id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE CHECK(length(request_key) BETWEEN 1 AND 128),
        target_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        project_id TEXT REFERENCES projects(project_id),
        mode TEXT NOT NULL CHECK(mode IN ('single','retire_branch','continue_children')),
        task_disposition TEXT NOT NULL CHECK(task_disposition IN ('preserve','cancel')),
        source TEXT NOT NULL CHECK(source IN ('user','parent_agent','orchestrator')),
        authorization_basis TEXT NOT NULL,
        requested_by_agent_id TEXT REFERENCES agents(agent_id),
        requested_by_assignment_id TEXT REFERENCES task_assignments(assignment_id),
        requested_generation TEXT,
        reason TEXT CHECK(reason IS NULL OR length(reason) BETWEEN 1 AND {REASON_LIMIT}),
        plan TEXT NOT NULL, plan_token TEXT NOT NULL,
        control_operation_id TEXT REFERENCES task_control_operations(operation_id),
        phase TEXT NOT NULL CHECK(phase IN ('proposed','admitted','stopping','removed','recovery_required','rejected','withdrawn')),
        last_error TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW}), updated_at TEXT NOT NULL DEFAULT ({NOW}),
        removed_at TEXT,
        CHECK((phase='removed') = (removed_at IS NOT NULL)))''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS agent_removal_live ON agent_removals(target_agent_id)
        WHERE phase IN ('proposed','admitted','stopping','recovery_required')''')
    db.execute(f'''CREATE TABLE IF NOT EXISTS agent_removal_members (
        removal_id TEXT NOT NULL REFERENCES agent_removals(removal_id),
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        role TEXT NOT NULL CHECK(role IN ('target','descendant')),
        task_id TEXT REFERENCES tasks(task_id),
        assignment_id TEXT REFERENCES task_assignments(assignment_id),
        generation TEXT,
        stop_order INTEGER NOT NULL DEFAULT 0,
        phase TEXT NOT NULL CHECK(phase IN ('admitted','stopping','removed','recovery_required')),
        outcome TEXT, failure TEXT,
        updated_at TEXT NOT NULL DEFAULT ({NOW}),
        PRIMARY KEY(removal_id, agent_id))''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_removal_member_live ON agent_removal_members(agent_id) WHERE phase <> 'removed'")
    columns = {r['name'] for r in db.execute('PRAGMA table_info(agents)')}
    if 'removed_at' not in columns:
        db.execute('ALTER TABLE agents ADD COLUMN removed_at TEXT')
    if 'removal_id' not in columns:
        db.execute('ALTER TABLE agents ADD COLUMN removal_id TEXT REFERENCES agent_removals(removal_id)')
    # Widen the assignment end reason without changing identities or row order.
    # The caller disables foreign-key enforcement on the migration connection.
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_assignments'").fetchone()[0]
    if "'removed'" not in sql:
        old = "'external_resource_unavailable','canceled'"
        if old not in sql:
            raise RuntimeError('Unexpected task_assignments schema; cannot add removal end reason')
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_assignments' AND sql IS NOT NULL")]
        sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE task_assignments_v27', sql, count=1)
        db.execute(sql.replace(old, "'external_resource_unavailable','removed','canceled'"))
        names = ','.join('"' + r['name'] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
        db.execute(f'INSERT INTO task_assignments_v27(rowid,{names}) SELECT rowid,{names} FROM task_assignments')
        db.execute('DROP TABLE task_assignments')
        db.execute('ALTER TABLE task_assignments_v27 RENAME TO task_assignments')
        for index in indexes:
            db.execute(index)


def live_member(db, agent_id):
    if not exists(db):
        return None
    row = db.execute("SELECT * FROM agent_removal_members WHERE agent_id=? AND phase IN ('admitted','stopping','recovery_required')",
                     (agent_id,)).fetchone()
    return dict(row) if row else None


def removal_pending(db, agent_id):
    return live_member(db, agent_id) is not None


def agent_states(db, agent_ids=None):
    """Read-only removal state per Agent: removing, blocked (recovery_required) or removed."""
    if not exists(db):
        return {}
    operations = {r['removal_id']: dict(r) for r in db.execute('SELECT removal_id,mode,target_agent_id FROM agent_removals')}
    result = {}
    for row in db.execute("SELECT agent_id,removal_id,phase,failure FROM agent_removal_members WHERE phase<>'removed'"):
        operation = operations.get(row['removal_id'], {})
        result[row['agent_id']] = dict(state='blocked' if row['phase'] == 'recovery_required' else 'removing',
                                       removal_id=row['removal_id'], mode=operation.get('mode'),
                                       target_agent_id=operation.get('target_agent_id'),
                                       failure=failure_code(dict(row)), removed_at=None)
    for row in db.execute('SELECT agent_id,removal_id,removed_at FROM agents WHERE removed_at IS NOT NULL'):
        if row['agent_id'] in result:
            continue
        operation = operations.get(row['removal_id'], {})
        result[row['agent_id']] = dict(state='removed', removal_id=row['removal_id'], mode=operation.get('mode'),
                                       target_agent_id=operation.get('target_agent_id'), failure=None,
                                       removed_at=row['removed_at'])
    if agent_ids is not None:
        wanted = set(agent_ids)
        result = {key: value for key, value in result.items() if key in wanted}
    return result


def requires_confirmation(member):
    """Pre-P2 failure codes (kept for reading old journals); the execution policy now decides."""
    return member is not None and member['phase'] == 'recovery_required' and failure_code(member) in CONFIRMATION_REQUIRED


def held_tasks(db):
    """Tasks whose worker is being removed. Like replanning's uncertain stops,
    their claims and scopes are neither suspended nor released until settlement."""
    if not exists(db):
        return set()
    return {r[0] for r in db.execute('''SELECT a.task_id FROM task_assignments a JOIN agent_removal_members m USING(agent_id)
        WHERE a.ended_at IS NULL AND m.phase IN ('admitted','stopping','recovery_required')''')}


def protected_assignments(db, was_running=None):
    """Open assignments startup reconciliation must leave open: an execution of
    their Agent or Task is unresolved (removal or not)."""
    from . import executions
    return {r['assignment_id'] for r in db.execute('SELECT assignment_id,agent_id,task_id FROM task_assignments WHERE ended_at IS NULL')
            if executions.blocking(db, agent_id=r['agent_id']) or executions.blocking(db, task_id=r['task_id'])}


def require_operational(db, agent_id):
    """Authoritative gate: a removal-pending or removed Agent never (re)starts work."""
    if agent_id is None or not exists(db):
        return
    row = db.execute('SELECT removed_at FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
    if row is not None and row['removed_at'] is not None:
        raise ValueError('Agent has been removed and cannot start or resume work')
    if removal_pending(db, agent_id):
        raise ValueError('Agent removal is in progress; the Agent cannot start or resume work')


def require_parent_operational(db, parent_id):
    """In a new Agent's admission transaction: a retired or retiring Agent never gains a Child.

    Existing Children keep their own authority; only new lineage is refused, so a
    branch removal admitted first cannot be escaped by a concurrent Child start.
    """
    if parent_id is None or not exists(db):
        return
    row = db.execute('SELECT removed_at FROM agents WHERE agent_id=?', (parent_id,)).fetchone()
    if row is not None and row['removed_at'] is not None:
        raise ValueError('Parent Agent has been removed and cannot create Children')
    if removal_pending(db, parent_id):
        raise ValueError('Parent Agent removal is in progress; it cannot create Children')


def _latest_assignment(db, agent_id):
    row = db.execute('SELECT * FROM task_assignments WHERE agent_id=? ORDER BY rowid DESC LIMIT 1', (agent_id,)).fetchone()
    return dict(row) if row else None


def _descendants(db, agent_id):
    """Every descendant by historical Agent lineage, breadth first with depth (parents before children)."""
    children = {}
    for row in db.execute('SELECT agent_id,parent_id FROM agents ORDER BY creation_order'):
        children.setdefault(row['parent_id'], []).append(row['agent_id'])
    seen, level, result, depth = {agent_id}, [agent_id], [], 0
    while level:
        depth += 1
        following = []
        for parent in level:
            for child in children.get(parent, ()):
                if child not in seen:
                    seen.add(child)
                    result.append((child, parent, depth))
                    following.append(child)
        level = following
    return result


def _task_reasons(db, task_id, open_assignment):
    """Responsibility a removal must not interrupt (shared by every mode and member)."""
    reasons = []
    if task_id and db.execute("SELECT 1 FROM sqlite_master WHERE name='replan_transitions'").fetchone() and db.execute(
            "SELECT 1 FROM replan_transitions WHERE task_id=? AND phase NOT IN ('applied','rejected','canceled')", (task_id,)).fetchone():
        reasons.append('responsibility_transition_in_progress')
    if open_assignment and db.execute('''SELECT 1 FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE d.child_task_id=? AND m.phase IN ('launching','recovery_required')''', (task_id,)).fetchone():
        reasons.append('unknown_launch_ownership')
    return reasons


def _launches_under(db, task_ids):
    """A worker launch for a Child Task of these Tasks whose Agent row is not saved yet.

    Such an Agent could not be listed in a branch; admission waits for it.
    """
    if not task_ids or not db.execute("SELECT 1 FROM sqlite_master WHERE name='executions'").fetchone():
        return False
    marks = ','.join('?' for _ in task_ids)
    return db.execute(f'''SELECT 1 FROM executions e JOIN tasks t ON t.task_id=e.task_id
        WHERE e.state IN ('launching','spawned','contained','running') AND t.parent_task_id IN ({marks})
        AND NOT EXISTS (SELECT 1 FROM agents g WHERE g.agent_id=e.agent_id) LIMIT 1''', list(task_ids)).fetchone() is not None \
        or db.execute(f'''SELECT 1 FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE d.parent_task_id IN ({marks}) AND m.phase='launching' LIMIT 1''', list(task_ids)).fetchone() is not None


def _delegated_child(db, child_task_id, parent_task_id):
    return bool(child_task_id and parent_task_id and db.execute(
        'SELECT 1 FROM delegations WHERE child_task_id=? AND parent_task_id=?', (child_task_id, parent_task_id)).fetchone())


def _orchestrating(db, task_id):
    """The Agent coordinates delegated work it alone may review (Hybrid owner)."""
    if task_id is None:
        return False
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_continuations'").fetchone() and db.execute(
            "SELECT 1 FROM delegation_continuations WHERE task_id=? AND phase<>'completed'", (task_id,)).fetchone():
        return True
    return db.execute("""SELECT 1 FROM delegations WHERE parent_task_id=? AND status IN ('requested','materialized','result_ready')""",
                      (task_id,)).fetchone() is not None


def orchestration_kind(db, task_id):
    """Which coordination binds a Task to its owner, from authoritative durable state.

    - `hybrid`: a Hybrid continuation that is not completed. Its delegated work is
      managed by that continuation, so this kind takes precedence (deterministic).
    - `delegation_handoff`: otherwise, open Delegations, or an accepted handoff
      receipt with any Delegation not acknowledged. Ordinary (non-Hybrid)
      Delegations have no result review or acknowledgement protocol yet.
    - None: no coordination.
    """
    if task_id is None:
        return None
    if _hybrid(db, task_id):
        return 'hybrid'
    if _orchestrating(db, task_id):
        return 'delegation_handoff'
    row = db.execute('SELECT orchestration_handoff FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is not None and row[0] and db.execute(
            "SELECT 1 FROM delegations WHERE parent_task_id=? AND status<>'acknowledged'", (task_id,)).fetchone():
        return 'delegation_handoff'
    return None


def _hybrid(db, task_id):
    if task_id is None or not db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_continuations'").fetchone():
        return False
    return db.execute("SELECT 1 FROM delegation_continuations WHERE task_id=? AND phase<>'completed'", (task_id,)).fetchone() is not None


def plan(db, agent_id, mode='single', disposition='preserve'):
    """Deterministic, read-only impact plan derived only from durable state."""
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
    if agent is None:
        raise LookupError('Agent not found')
    if mode not in MODES:
        raise Unsupported('invalid_mode', 'Unknown removal mode')
    if disposition not in DISPOSITIONS:
        raise Unsupported('invalid_task_disposition', 'Unknown Task disposition')
    from . import executions
    assignment = _latest_assignment(db, agent_id)
    task = dependencies.task(db, assignment['task_id']) if assignment else None
    task_id = task['task_id'] if task else None
    open_assignment = assignment if assignment and assignment['ended_at'] is None else None
    descendants = []
    for key, parent_key, depth in _descendants(db, agent_id):
        row = db.execute('SELECT agent_id,status,removed_at,execution_generation FROM agents WHERE agent_id=?', (key,)).fetchone()
        latest = _latest_assignment(db, key)
        current = latest if latest and latest['ended_at'] is None else None
        active = row['removed_at'] is None and (current is not None or row['status'] in ('running', 'waiting'))
        child_task = latest['task_id'] if latest else None
        live = live_member(db, key)
        # Single removal: an active descendant keeps its own execution authority.
        # A delegated Child of the removed Agent's Task still runs; only delivery
        # and acknowledgement of its result need the (future) responsible owner.
        continuation = ('idle' if not active else 'continues_result_waits'
                        if parent_key == agent_id and _delegated_child(db, child_task, task_id) else 'continues')
        descendants.append(dict(agent_id=key, parent_id=parent_key, depth=depth, status=row['status'],
                                removed=row['removed_at'] is not None, active=active, task_id=child_task,
                                assignment_id=current['assignment_id'] if current else None,
                                execution_generation=row['execution_generation'],
                                live_removal_id=live['removal_id'] if live else None,
                                unresolved_executions=len(executions.blocking(db, agent_id=key)),
                                continuation=continuation))
    blockers = sorted({r[0] for r in db.execute('SELECT reason_code FROM task_blockers WHERE task_id=? AND active=1', (task_id,))}) if task_id else []
    dependents = [dict(task_id=r[0], status=r[1]) for r in db.execute(
        '''SELECT t.task_id,t.status FROM task_dependencies d JOIN tasks t ON t.task_id=d.task_id
           WHERE d.depends_on_task_id=? ORDER BY t.task_id''', (task_id,))] if task_id else []
    integrations = [dict(integration_id=r[0], status=r[1], role=r[2]) for r in db.execute(
        '''SELECT integration_id,status,CASE WHEN source_task_id=? THEN 'source' ELSE 'destination' END FROM integrations
           WHERE source_task_id=? OR destination_key=? ORDER BY integration_order''',
        (task_id, task_id, 'task:' + str(task_id)))] if task_id else []
    results = [dict(result_id=r[0], phase=r[1], outcome=r[2]) for r in db.execute(
        'SELECT result_id,phase,outcome FROM delegation_results WHERE child_task_id=? ORDER BY rowid', (task_id,))] if task_id else []
    claims = {r[0]: r[1] for r in db.execute(
        "SELECT lifetime,COUNT(*) FROM resource_claims WHERE task_id=? AND status<>'released' GROUP BY lifetime", (task_id,))} if task_id else {}
    workspace = db.execute('SELECT 1 FROM task_workspaces WHERE task_id=?', (task_id,)).fetchone() is not None if task_id else False
    live = live_member(db, agent_id)
    common = []
    if agent['removed_at'] is not None:
        common.append('already_removed')
    if live:
        common.append('removal_in_progress')
    orchestration = orchestration_kind(db, task_id)
    orchestrating = orchestration is not None
    single = common + _task_reasons(db, task_id, open_assignment)
    # The branch: the target and every descendant not removed yet, parents first.
    members = [dict(agent_id=agent_id, role='target', depth=0, task_id=task_id,
                    assignment_id=open_assignment['assignment_id'] if open_assignment else None,
                    execution_generation=agent['execution_generation'], status=agent['status'],
                    unresolved_executions=len(executions.blocking(db, agent_id=agent_id)))]
    members += [dict(agent_id=d['agent_id'], role='descendant', depth=d['depth'], task_id=d['task_id'],
                     assignment_id=d['assignment_id'], execution_generation=d['execution_generation'], status=d['status'],
                     unresolved_executions=d['unresolved_executions']) for d in descendants if not d['removed']]
    branch = list(common)
    for item in members[1:]:
        for reason in _task_reasons(db, item['task_id'], item['assignment_id']):
            if reason not in branch:
                branch.append(reason)
    if any(d['live_removal_id'] and not d['removed'] for d in descendants):
        branch.append('descendant_removal_in_progress')
    if _launches_under(db, {m['task_id'] for m in members if m['task_id']}):
        branch.append('launch_in_progress')
    for reason in _task_reasons(db, task_id, open_assignment):
        if reason not in branch:
            branch.append(reason)
    mode_reasons = {'single': single, 'retire_branch': branch}
    options = {}
    for name in MODES:
        if name not in SUPPORTED_MODES:
            options[name] = dict(available=False, reasons=['not_supported'])
        else:
            options[name] = dict(available=not mode_reasons[name], reasons=list(mode_reasons[name]))
    dispositions = {name: dict(available=name in SUPPORTED_DISPOSITIONS, reasons=[] if name in SUPPORTED_DISPOSITIONS else ['not_supported'])
                    for name in DISPOSITIONS}
    consequences = []
    if open_assignment and agent['status'] == 'running':
        consequences.append('stop_running_worker')
    if open_assignment and agent['status'] == 'waiting':
        consequences.append('discard_waiting_session')
    if open_assignment:
        consequences.append('task_awaits_new_assignment' if task['status'] not in dependencies.TERMINAL else 'task_unchanged')
    elif task:
        consequences.append('task_unchanged')
    consequences.append('identity_retired_permanently')
    if mode == 'single' and any(d['active'] for d in descendants):
        consequences.append('children_continue')
    if mode == 'retire_branch' and len(members) > 1:
        consequences.append('branch_members_removed')
    if orchestration == 'hybrid':
        # Hybrid exception: the continuation is bound to this Agent's session and
        # receipt; it is disabled, never silently moved to another model.
        consequences.append('orchestration_disabled')
    if open_assignment and task['status'] not in dependencies.TERMINAL:
        # The Task keeps its identity and waits for an explicit new owner, which
        # is impossible while its orchestration is bound to the removed Agent.
        consequences.append({'hybrid': 'task_waits_hybrid_orchestration_bound',
                             'delegation_handoff': 'task_waits_delegation_handoff_unsettled'}.get(
                                 orchestration, 'task_awaits_responsibility_transfer'))
    pending_delegations = db.execute(
        "SELECT COUNT(*) FROM delegations WHERE parent_task_id=? AND status='requested'", (task_id,)).fetchone()[0] if task_id else 0
    body = dict(
        agent=dict(agent_id=agent_id, status=agent['status'], execution_generation=agent['execution_generation'],
                   removed_at=agent['removed_at'], removal_id=agent['removal_id'], parent_id=agent['parent_id'],
                   session_present=agent['session_id'] is not None, waiting_question_present=agent['waiting_question'] is not None),
        mode=mode, task_disposition=disposition,
        task=None if task is None else {k: task[k] for k in ('task_id', 'project_id', 'parent_task_id', 'status', 'control_intent', 'stop_required')},
        assignment=None if assignment is None else {k: assignment[k] for k in ('assignment_id', 'ended_at', 'ended_reason')},
        descendants=descendants, blockers=blockers, external_dependents=dependents, integrations=integrations,
        delegated_results=results, resource_claims=dict(task=claims.get('task', 0), worker=claims.get('worker', 0)),
        workspace_preserved=workspace, live_removal_id=live['removal_id'] if live else None,
        options=options, task_dispositions=dispositions, consequences=consequences,
        members=members if mode == 'retire_branch' else members[:1],
        orchestration=dict(coordinates_delegated_work=orchestrating, kind=orchestration, hybrid=orchestration == 'hybrid',
                           waiting_delegations=pending_delegations))
    body['plan_token'] = sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return body


def _validate_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip() or _KEY.search(value):
        raise Unsupported('invalid_request_key', 'request_key must be 1-128 visible characters')
    return value


def _validate_reason(value):
    if value is None:
        return None
    if not isinstance(value, str) or _TEXT.search(value) or len(value) > REASON_LIMIT:
        raise Unsupported('invalid_reason', f'Reason must be plain text of at most {REASON_LIMIT} characters')
    return value if value.strip() else None


def get(db, removal_id):
    row = db.execute('SELECT * FROM agent_removals WHERE removal_id=?', (removal_id,)).fetchone()
    if row is None:
        raise LookupError('Agent removal not found')
    result = dict(row)
    result['plan'] = json.loads(result['plan'])
    result['members'] = [dict(r) for r in db.execute(
        'SELECT * FROM agent_removal_members WHERE removal_id=? ORDER BY stop_order,agent_id', (removal_id,))]
    return result


def for_agent(db, agent_id):
    if db.execute('SELECT 1 FROM agents WHERE agent_id=?', (agent_id,)).fetchone() is None:
        raise LookupError('Agent not found')
    return [get(db, r[0]) for r in db.execute(
        '''SELECT DISTINCT r.removal_id,r.rowid FROM agent_removals r LEFT JOIN agent_removal_members m USING(removal_id)
           WHERE r.target_agent_id=? OR m.agent_id=? ORDER BY r.rowid''', (agent_id, agent_id))]


def admit(db, *, agent_id, request_key, plan_token, mode, disposition, reason):
    """T1. Returns (operation, created). Replays never re-execute anything."""
    request_key, reason = _validate_key(request_key), _validate_reason(reason)
    if not isinstance(plan_token, str) or not plan_token:
        raise Unsupported('invalid_plan_token', 'plan_token from a removal preview is required')
    existing = db.execute('SELECT * FROM agent_removals WHERE request_key=?', (request_key,)).fetchone()
    if existing is not None:
        same = (existing['target_agent_id'], existing['mode'], existing['task_disposition'], existing['reason'], existing['plan_token']) \
            == (agent_id, mode, disposition, reason, plan_token)
        if not same:
            raise RemovalError('request_key_conflict', 'This request_key was already used for a different removal request',
                               removal_id=existing['removal_id'])
        return get(db, existing['removal_id']), False
    if mode not in MODES:
        raise Unsupported('invalid_mode', 'Unknown removal mode')
    if mode not in SUPPORTED_MODES:
        raise Unsupported('unsupported_mode', f'Removal mode {mode} is not supported; use single (Children continue) '
                                              'or retire_branch (the whole branch)')
    if disposition not in DISPOSITIONS:
        raise Unsupported('invalid_task_disposition', 'Unknown Task disposition')
    if disposition not in SUPPORTED_DISPOSITIONS:
        raise Unsupported('unsupported_task_disposition', 'Canceling the Task during removal is not supported yet; cancel the Task separately')
    current = plan(db, agent_id, mode, disposition)
    if current['live_removal_id']:
        raise RemovalError('removal_in_progress', 'Another removal of this Agent is already in progress',
                           removal_id=current['live_removal_id'])
    if current['agent']['removed_at'] is not None:
        raise RemovalError('already_removed', 'This Agent has already been removed', removal_id=current['agent']['removal_id'])
    if current['plan_token'] != plan_token:
        raise StalePreview(current)
    option = current['options'][mode]
    if not option['available']:
        raise RemovalError('not_available', 'Removal is not available for this Agent: ' + ', '.join(option['reasons']),
                           reasons=option['reasons'])
    identifier = str(uuid4())
    task = current['task']
    assignment = current['assignment']
    db.execute('''INSERT INTO agent_removals(removal_id,request_key,target_agent_id,project_id,mode,task_disposition,source,
        authorization_basis,reason,plan,plan_token,phase) VALUES (?,?,?,?,?,?,'user','local_api',?,?,?,'admitted')''',
               (identifier, request_key, agent_id, task['project_id'] if task else None, mode, disposition, reason,
                json.dumps(current, sort_keys=True), plan_token))
    # One member per Agent, fixed now: parents stop before their descendants. The
    # partial unique index refuses an Agent already in another live removal.
    try:
        for item in current['members']:
            db.execute('''INSERT INTO agent_removal_members(removal_id,agent_id,role,task_id,assignment_id,generation,stop_order,phase)
                VALUES (?,?,?,?,?,?,?,'admitted')''',
                       (identifier, item['agent_id'], item['role'], item['task_id'], item['assignment_id'],
                        item['execution_generation'], item['depth']))
    except sqlite3.IntegrityError as exc:
        raise RemovalError('descendant_removal_in_progress',
                           'An Agent of this branch is already part of another removal') from exc
    return get(db, identifier), True


def member(db, removal_id, agent_id):
    row = db.execute('SELECT * FROM agent_removal_members WHERE removal_id=? AND agent_id=?', (removal_id, agent_id)).fetchone()
    if row is None:
        raise LookupError('Agent removal member not found')
    return dict(row)


def mark_stopping(db, removal_id, agent_id):
    # Journal BEFORE touching the process, like replanning's `stopping` phase.
    db.execute(f"""UPDATE agent_removal_members SET phase='stopping',failure=NULL,updated_at={NOW}
        WHERE removal_id=? AND agent_id=? AND phase IN ('admitted','stopping','recovery_required')""", (removal_id, agent_id))
    db.execute(f"UPDATE agent_removals SET phase='stopping',last_error=NULL,updated_at={NOW} WHERE removal_id=? AND phase IN ('admitted','recovery_required')",
               (removal_id,))


def fail(db, removal_id, agent_id, code, detail=None):
    text = code if not detail else f'{code}: {detail}'
    db.execute(f"""UPDATE agent_removal_members SET phase='recovery_required',failure=?,updated_at={NOW}
        WHERE removal_id=? AND agent_id=? AND phase<>'removed'""", (text[:1000], removal_id, agent_id))
    finish(db, removal_id)


def failure_code(row):
    return (row.get('failure') or '').split(':', 1)[0] or None


def ownership_unknown(db, agent_id, task_id):
    """The Task's unknown-process blocker applies to this Agent's last execution."""
    if task_id is None:
        return False
    if not db.execute('SELECT 1 FROM task_blockers WHERE task_id=? AND blocker_type=? AND reason_code=? AND active=1',
                      (task_id, *OWNERSHIP_BLOCKER)).fetchone():
        return False
    latest = db.execute('SELECT agent_id FROM task_assignments WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone()
    return latest is not None and latest[0] == agent_id


def end_open_assignment(db, agent_id):
    """End an Agent's open assignment as `removed` (task-level intent rules kept)."""
    from . import work_control
    assignment = db.execute('SELECT * FROM task_assignments WHERE agent_id=? AND ended_at IS NULL', (agent_id,)).fetchone()
    if assignment is not None:
        work_control.sync(db, assignment, 'stopped')


def settle(db, removal_id, agent_id, outcome, *, confirmed=False):
    """T2: finalize one member only after its execution is verified settled."""
    row = member(db, removal_id, agent_id)
    if row['phase'] == 'removed':
        return False
    agent = db.execute('SELECT status FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
    if agent['status'] == 'running':
        raise RemovalError('worker_not_settled', 'The worker is still recorded as running')
    from . import executions
    unresolved = executions.blocking(db, agent_id=agent_id)
    if unresolved:
        # Only kernel or reboot evidence settles an execution; a confirmation
        # or a clean process scan is never proof that it ended.
        raise RemovalError('execution_unresolved', 'Process termination is not established for this Agent ('
                           + executions.describe(unresolved) + ')')
    # No execution of this Agent is unresolved: lift the hold so the
    # assignment ends and ownership follows normal coordination below.
    db.execute(f"UPDATE agent_removal_members SET phase='stopping',failure=NULL,updated_at={NOW} WHERE removal_id=? AND agent_id=?",
               (removal_id, agent_id))
    end_open_assignment(db, agent_id)
    if db.execute('SELECT 1 FROM task_assignments WHERE agent_id=? AND ended_at IS NULL', (agent_id,)).fetchone():
        raise RemovalError('assignment_not_settled', 'The Agent assignment could not be settled')
    ended = db.execute('SELECT ended_reason FROM task_assignments WHERE assignment_id=?', (row['assignment_id'],)).fetchone() \
        if row['assignment_id'] else None
    if ended is not None and ended[0] != 'removed':
        outcome = f'{outcome}; assignment_ended_{ended[0]}'
    db.execute(f"UPDATE agents SET status=CASE WHEN status='waiting' THEN 'stopped' ELSE status END,waiting_question=NULL,removed_at={NOW},removal_id=? WHERE agent_id=?",
               (removal_id, agent_id))
    db.execute(f"""UPDATE agent_removal_members SET phase='removed',outcome=?,failure=NULL,updated_at={NOW}
        WHERE removal_id=? AND agent_id=?""", (outcome[:500], removal_id, agent_id))
    finish(db, removal_id)
    dependencies.reconcile(db)
    return True


def finish(db, removal_id):
    """T3: aggregate operation phase from its members."""
    phases = [r[0] for r in db.execute('SELECT phase FROM agent_removal_members WHERE removal_id=?', (removal_id,))]
    if phases and all(p == 'removed' for p in phases):
        db.execute(f"UPDATE agent_removals SET phase='removed',last_error=NULL,removed_at=COALESCE(removed_at,{NOW}),updated_at={NOW} WHERE removal_id=?",
                   (removal_id,))
    elif 'recovery_required' in phases:
        failure = db.execute("SELECT failure FROM agent_removal_members WHERE removal_id=? AND phase='recovery_required' ORDER BY stop_order LIMIT 1",
                             (removal_id,)).fetchone()[0]
        db.execute(f"UPDATE agent_removals SET phase='recovery_required',last_error=?,updated_at={NOW} WHERE removal_id=?",
                   (failure, removal_id))


def recover(db, was_running):
    """Startup only, after running Agents were marked stopped. Never launches.

    The execution journal decides (already resolved from kernel and reboot
    evidence): a member whose Agent has an unresolved execution is
    `recovery_required` (`execution_unresolved`) and its assignment, claims,
    scopes and the Task's slot stay held; every other admitted or stopping
    member settles, including one whose Agent was executing when the backend
    stopped. Output markers and process scans are never evidence.
    """
    from . import executions
    if not exists(db):
        return []
    settled = []
    for row in [dict(r) for r in db.execute("SELECT * FROM agent_removal_members WHERE phase IN ('admitted','stopping','recovery_required')")]:
        unresolved = executions.blocking(db, agent_id=row['agent_id'])
        if unresolved:
            if row['phase'] != 'recovery_required' or failure_code(row) != 'execution_unresolved':
                fail(db, row['removal_id'], row['agent_id'], 'execution_unresolved',
                     'process termination is not established (' + executions.describe(unresolved) + ')')
            continue
        if row['phase'] == 'recovery_required' and failure_code(row) not in ('execution_unresolved',)                 + CONFIRMATION_REQUIRED:
            continue  # Another failure (for example stop_not_confirmed): an explicit retry decides.
        try:
            settle(db, row['removal_id'], row['agent_id'], 'settled_by_restart_recovery')
            settled.append(row['removal_id'])
        except RemovalError as exc:
            fail(db, row['removal_id'], row['agent_id'], exc.code, str(exc))
    for row in db.execute("SELECT removal_id FROM agent_removals WHERE phase IN ('admitted','stopping','recovery_required')").fetchall():
        finish(db, row[0])
    return settled
