"""Explicit responsibility transfer after an owner Agent was removed (Stage 2D.2).

A Task's operational owner is the Agent of its open TaskAssignment. Removing that
Agent ends the assignment as `removed` and leaves the Task with its identity,
workspace, results and history, but without an owner. Nothing assigns a new
owner automatically. A transfer is the explicit, validated decision that a new
Agent (created by the existing start path, never promoted from a Child) takes
over the Task. It is journaled here as a durable event; earlier assignments are
never rewritten.

Phases: `admitted` (committed before the start path runs), then `completed`
(linked to the new Agent's assignment) or `failed` (nothing took over; the Task
keeps waiting). Startup reconciles an interrupted `admitted` row from durable
assignments alone and never starts anything. SQL helpers run inside the
caller's transaction.
"""
import re
from uuid import uuid4

from . import dependencies

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
REASON_LIMIT = 2000
_KEY = re.compile(r'[\x00-\x1f\x7f]')
_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
AGENT_TYPES = ('mock', 'codex', 'claude', 'gemini')
SANDBOXES = ('read-only', 'workspace-write')
# Why a Task cannot receive a new owner right now (each a hard refusal).
REFUSALS = {
    'task_terminal': 'The Task is completed or canceled; it needs no new owner.',
    'owner_present': 'The Task already has an active owner.',
    'not_awaiting_transfer': 'The Task\'s last owner was not removed; start work through the ordinary controls.',
    'removal_in_progress': 'Removal of the current owner has not settled yet.',
    'transfer_in_progress': 'Another responsibility transfer for this Task is in progress.',
    'execution_unresolved': 'A process of this Task or its removed owner may still be running.',
    'hybrid_orchestration_bound': ('Its Hybrid continuation was bound to the removed Agent\'s session and receipt. It was '
                                   'disabled and cannot be moved to another Agent.'),
    'delegation_handoff_unsettled': ('It coordinates ordinary (non-Hybrid) Delegations. Their result review and '
                                     'acknowledgement protocol is not implemented, so this coordination cannot be '
                                     'transferred to another Agent.'),
    'responsibility_transition_in_progress': 'A replanning transition targets this Task.',
    'unknown_launch_ownership': 'A delegated launch for this Task has an unknown outcome.',
    'task_control': 'The Task is paused or must stop; resume it first.',
    'task_blocked': 'The Task has hard blockers (for example dependencies or resources).',
    'ancestor_control': 'A parent Task is paused, canceled or must stop.',
    'parent_owner_removal_in_progress': 'The parent Task\'s owner is being removed.',
}


class TransferError(ValueError):
    """A refused or failed transfer; `code` is stable and machine-readable."""
    status = 409

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='responsibility_transfers'").fetchone() is not None


def migrate(db):
    """v29 -> v30. Additive and idempotent: one journal table and two indexes."""
    db.execute(f'''CREATE TABLE IF NOT EXISTS responsibility_transfers (
        transfer_id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE CHECK(length(request_key) BETWEEN 1 AND 128),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        from_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        from_assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        removal_id TEXT REFERENCES agent_removals(removal_id),
        to_agent_id TEXT,
        to_assignment_id TEXT REFERENCES task_assignments(assignment_id),
        agent_type TEXT NOT NULL CHECK(agent_type IN ('mock','codex','claude','gemini')),
        sandbox TEXT CHECK(sandbox IS NULL OR sandbox IN ('read-only','workspace-write')),
        source TEXT NOT NULL CHECK(source IN ('user','start_agent')),
        authorization_basis TEXT NOT NULL,
        reason TEXT CHECK(reason IS NULL OR length(reason) BETWEEN 1 AND {REASON_LIMIT}),
        phase TEXT NOT NULL CHECK(phase IN ('admitted','completed','failed')),
        failure TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW}), updated_at TEXT NOT NULL DEFAULT ({NOW}),
        completed_at TEXT,
        CHECK((phase = 'completed') = (to_assignment_id IS NOT NULL AND completed_at IS NOT NULL)),
        CHECK((phase = 'failed') = (failure IS NOT NULL)))''')
    # One live decision per Task; history is kept in insertion order.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS responsibility_transfer_live ON responsibility_transfers(task_id) WHERE phase='admitted'")
    db.execute('CREATE INDEX IF NOT EXISTS responsibility_transfer_task ON responsibility_transfers(task_id)')


def _row(row):
    return dict(row) if row is not None else None


def history(db, task_id):
    if not exists(db):
        return []
    return [dict(r) for r in db.execute('SELECT * FROM responsibility_transfers WHERE task_id=? ORDER BY rowid', (task_id,))]


def get(db, transfer_id):
    row = db.execute('SELECT * FROM responsibility_transfers WHERE transfer_id=?', (transfer_id,)).fetchone()
    if row is None:
        raise LookupError('Responsibility transfer not found')
    return dict(row)


# Why a completed delegated Child's workspace cannot be integrated now. These mirror
# the durable preconditions of the existing explicit integration (integrate_task):
# both workspaces exist, ownership is known and no process is live. Git state is
# re-checked by the integration itself. Integration never acknowledges a Delegation.
INTEGRATION_BLOCKERS = {
    'workspace_missing': 'The Child has no Task workspace.',
    'parent_workspace_missing': 'This Task has no workspace to integrate into.',
    'launch_ownership_unknown': 'A delegated launch has an unknown outcome.',
    'process_ownership_unknown': 'A process end is not established.',
    'live_process': 'An Agent of this Task or Child is still running.',
    'integration_in_progress': 'Another integration is in progress or needs recovery.',
}
_ACTIVE_INTEGRATION = "('preparing','ready','applying','recovery_required')"


def _ownership_issues(db, task_id):
    from . import executions
    issues = []
    if db.execute('''SELECT 1 FROM delegation_materializations m JOIN delegations d USING(delegation_id)
            WHERE d.child_task_id=? AND m.phase IN ('launching','recovery_required')''', (task_id,)).fetchone() or db.execute(
            "SELECT 1 FROM delegation_continuations WHERE task_id=? AND phase IN ('launching','recovery_required')",
            (task_id,)).fetchone():
        issues.append('launch_ownership_unknown')
    if executions.blocking(db, task_id=task_id) or db.execute(
            "SELECT 1 FROM task_blockers WHERE task_id=? AND reason_code='process_ownership_unknown' AND active=1",
            (task_id,)).fetchone():
        issues.append('process_ownership_unknown')
    if db.execute('''SELECT 1 FROM task_assignments a JOIN agents g USING(agent_id)
            WHERE a.task_id=? AND g.status='running' ''', (task_id,)).fetchone():
        issues.append('live_process')
    return issues


def integration_candidates(db, task_id):
    """Read-only guidance: completed delegated Children whose workspaces the existing
    explicit integration could bring into this Task. Never a Delegation acknowledgement."""
    parent = []
    if not db.execute('SELECT 1 FROM task_workspaces WHERE task_id=?', (task_id,)).fetchone():
        parent.append('parent_workspace_missing')
    parent += _ownership_issues(db, task_id)
    if db.execute(f'SELECT 1 FROM integrations WHERE destination_key=? AND status IN {_ACTIVE_INTEGRATION}',
                  ('task:' + task_id,)).fetchone():
        parent.append('integration_in_progress')
    result = []
    for row in db.execute('''SELECT d.delegation_id,d.status AS delegation_status,t.task_id,t.title FROM delegations d
            JOIN tasks t ON t.task_id=d.child_task_id WHERE d.parent_task_id=? AND t.status='completed'
            AND t.parent_task_id=d.parent_task_id ORDER BY d.rowid''', (task_id,)).fetchall():
        issues = list(parent)
        if not db.execute('SELECT 1 FROM task_workspaces WHERE task_id=?', (row['task_id'],)).fetchone():
            issues.append('workspace_missing')
        issues += [code for code in _ownership_issues(db, row['task_id']) if code not in issues]
        if 'integration_in_progress' not in issues and db.execute(
                f'SELECT 1 FROM integrations WHERE source_task_id=? AND status IN {_ACTIVE_INTEGRATION}', (row['task_id'],)).fetchone():
            issues.append('integration_in_progress')
        integrated = db.execute("""SELECT 1 FROM integrations WHERE source_task_id=? AND destination_key=?
            AND status IN ('applied','noop')""", (row['task_id'], 'task:' + task_id)).fetchone() is not None
        result.append({'child_task_id': row['task_id'], 'title': row['title'], 'delegation_id': row['delegation_id'],
                       'delegation_status': row['delegation_status'], 'integration_available': not issues,
                       'integration_blockers': [{'code': code, 'message': INTEGRATION_BLOCKERS[code]} for code in issues],
                       'previously_integrated': integrated})
    return result


def state(db, task_id):
    """Read-only: who owns the Task now, whether it awaits a new owner and why a transfer is refused."""
    from . import agent_removal, executions, work_control
    row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise LookupError('Task not found')
    task = dependencies.describe(db, row)
    current = _row(db.execute('SELECT * FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone())
    latest = _row(db.execute('SELECT * FROM task_assignments WHERE task_id=? ORDER BY rowid DESC LIMIT 1', (task_id,)).fetchone())
    removing = current is not None and agent_removal.removal_pending(db, current['agent_id'])
    removed = latest if latest and latest['ended_at'] is not None and latest['ended_reason'] == 'removed' else None
    removed_agent = _row(db.execute('SELECT agent_id,removed_at,removal_id FROM agents WHERE agent_id=?',
                                    (removed['agent_id'],)).fetchone()) if removed else None
    terminal = task['status'] in dependencies.TERMINAL
    awaiting = not terminal and (removing or (current is None and removed is not None))
    reasons = []
    if terminal:
        reasons.append('task_terminal')
    if current is not None and not removing:
        reasons.append('owner_present')
    if removing:
        reasons.append('removal_in_progress')
    if current is None and removed is None and not terminal:
        reasons.append('not_awaiting_transfer')
    if exists(db) and db.execute("SELECT 1 FROM responsibility_transfers WHERE task_id=? AND phase='admitted'", (task_id,)).fetchone():
        reasons.append('transfer_in_progress')
    previous = removed['agent_id'] if removed else current['agent_id'] if current else None
    if executions.blocking(db, task_id=task_id) or (previous and executions.blocking(db, agent_id=previous)):
        reasons.append('execution_unresolved')
    orchestration = agent_removal.orchestration_kind(db, task_id)
    if orchestration == 'hybrid':
        reasons.append('hybrid_orchestration_bound')
    elif orchestration == 'delegation_handoff':
        reasons.append('delegation_handoff_unsettled')
    for reason in agent_removal._task_reasons(db, task_id, current or removed):
        reasons.append(reason)
    if not terminal and (task['control_intent'] != 'active' or task['stop_required']):
        reasons.append('task_control')
    # Orchestration and unresolved-ownership blockers are reported by their own reasons.
    own = {'process_ownership_unknown', 'delegation_requested'}
    if not terminal and any(b['hard'] and b['reason_code'] not in own for b in task['active_blockers']):
        reasons.append('task_blocked')
    try:
        work_control.require_ancestors(db, task_id)
    except ValueError:
        reasons.append('ancestor_control')
    parent = task.get('parent_task_id')
    owner_of_parent = db.execute('SELECT agent_id FROM task_assignments WHERE task_id=? AND ended_at IS NULL',
                                 (parent,)).fetchone() if parent else None
    if owner_of_parent and agent_removal.removal_pending(db, owner_of_parent[0]):
        reasons.append('parent_owner_removal_in_progress')
    reasons = list(dict.fromkeys(reasons))
    return {
        'task_id': task_id, 'task_status': task['status'], 'awaiting_transfer': awaiting,
        'transferable': awaiting and not reasons,
        'owner': None if current is None or removing else {'agent_id': current['agent_id'], 'assignment_id': current['assignment_id']},
        'removed_owner': None if removed is None else {
            'agent_id': removed['agent_id'], 'assignment_id': removed['assignment_id'], 'ended_at': removed['ended_at'],
            'removal_id': removed_agent['removal_id'] if removed_agent else None},
        'removing_owner': {'agent_id': current['agent_id'], 'assignment_id': current['assignment_id']} if removing else None,
        'orchestration_bound': orchestration is not None,
        'orchestration_kind': orchestration,
        'reasons': [{'code': code, 'message': REFUSALS[code]} for code in reasons],
        # Guidance only for ordinary Delegations; it never settles delegation_requested.
        'integration_candidates': integration_candidates(db, task_id) if orchestration == 'delegation_handoff' else [],
        'transfers': history(db, task_id),
    }


def _validate_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or value != value.strip() or _KEY.search(value):
        raise TransferError('invalid_request_key', 'request_key must be 1-128 visible characters')
    return value


def _validate_reason(value):
    if value is None:
        return None
    if not isinstance(value, str) or _TEXT.search(value) or len(value) > REASON_LIMIT:
        raise TransferError('invalid_reason', f'Reason must be plain text of at most {REASON_LIMIT} characters')
    return value if value.strip() else None


def admit(db, task_id, *, request_key, expected_from_assignment_id, agent_type, sandbox, reason, source='user'):
    """T1, inside BEGIN IMMEDIATE. Returns (transfer, created); an exact replay never acts again."""
    request_key, reason = _validate_key(request_key), _validate_reason(reason)
    if agent_type not in AGENT_TYPES:
        raise TransferError('invalid_agent_type', 'Unknown agent type')
    sandbox = sandbox if agent_type != 'mock' else None
    if agent_type != 'mock' and sandbox not in SANDBOXES:
        raise TransferError('invalid_sandbox', 'A real provider needs a supported access policy')
    existing = db.execute('SELECT * FROM responsibility_transfers WHERE request_key=?', (request_key,)).fetchone()
    if existing is not None:
        same = (existing['task_id'], existing['agent_type'], existing['sandbox'], existing['reason']) == (task_id, agent_type, sandbox, reason)
        if not same or (expected_from_assignment_id is not None and existing['from_assignment_id'] != expected_from_assignment_id):
            raise TransferError('request_key_conflict', 'This request_key was already used for a different transfer',
                                transfer_id=existing['transfer_id'])
        return dict(existing), False
    current = state(db, task_id)
    removed = current['removed_owner']
    if expected_from_assignment_id is not None and (removed is None or removed['assignment_id'] != expected_from_assignment_id):
        raise TransferError('stale_responsibility', 'The Task\'s responsibility changed since it was reviewed',
                            responsibility=current)
    if not current['transferable']:
        raise TransferError('not_transferable', 'Responsibility cannot be transferred: '
                            + ', '.join(r['code'] for r in current['reasons']), responsibility=current,
                            reasons=current['reasons'])
    transfer_id = str(uuid4())
    db.execute('''INSERT INTO responsibility_transfers(transfer_id,request_key,task_id,from_agent_id,from_assignment_id,
        removal_id,agent_type,sandbox,source,authorization_basis,reason,phase) VALUES (?,?,?,?,?,?,?,?,?,'local_api',?,'admitted')''',
               (transfer_id, request_key, task_id, removed['agent_id'], removed['assignment_id'], removed['removal_id'],
                agent_type, sandbox, source, reason))
    return get(db, transfer_id), True


def complete(db, transfer_id, agent_id):
    """T2: link the new owner's assignment; only an assignment created for this Task counts."""
    transfer = get(db, transfer_id)
    if transfer['phase'] != 'admitted':
        return transfer
    assignment = db.execute('''SELECT assignment_id FROM task_assignments WHERE task_id=? AND agent_id=?
        AND rowid > (SELECT rowid FROM task_assignments WHERE assignment_id=?) ORDER BY rowid LIMIT 1''',
                            (transfer['task_id'], agent_id, transfer['from_assignment_id'])).fetchone()
    if assignment is None:
        raise TransferError('assignment_missing', 'The new owner has no assignment for this Task')
    db.execute(f'''UPDATE responsibility_transfers SET phase='completed',to_agent_id=?,to_assignment_id=?,completed_at={NOW},
        updated_at={NOW} WHERE transfer_id=? AND phase='admitted' ''', (agent_id, assignment[0], transfer_id))
    return get(db, transfer_id)


def fail(db, transfer_id, code, detail=None):
    text = code if not detail else f'{code}: {detail}'
    db.execute(f"""UPDATE responsibility_transfers SET phase='failed',failure=?,updated_at={NOW}
        WHERE transfer_id=? AND phase='admitted'""", (text[:1000], transfer_id))
    return get(db, transfer_id)


def recover(db):
    """Startup only: settle interrupted transfers from durable assignments. Never starts anything."""
    if not exists(db):
        return []
    settled = []
    for row in db.execute("SELECT * FROM responsibility_transfers WHERE phase='admitted'").fetchall():
        assignment = db.execute('''SELECT assignment_id,agent_id FROM task_assignments WHERE task_id=?
            AND rowid > (SELECT rowid FROM task_assignments WHERE assignment_id=?) ORDER BY rowid LIMIT 1''',
                                (row['task_id'], row['from_assignment_id'])).fetchone()
        if assignment is not None:
            complete(db, row['transfer_id'], assignment['agent_id'])
        else:
            # Any process a launch may have created is governed by the execution journal.
            fail(db, row['transfer_id'], 'interrupted', 'the backend stopped before a new owner was recorded')
        settled.append(row['transfer_id'])
    return settled
