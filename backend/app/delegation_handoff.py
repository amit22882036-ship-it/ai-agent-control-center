"""Transactional, provider-independent authorization and Parent handoff."""
import json
from dataclasses import dataclass

from . import delegation_protocol, delegations, dependencies, work_control, runtime_resources
from . import delegation_continuation, delegation_orchestration, delegation_rounds, delegation_safety, delegation_v3


@dataclass(frozen=True)
class ExecutionOrigin:
    agent_id: str
    assignment_id: str
    parent_task_id: str
    project_id: str
    generation: str


def migrate(db):
    for table, field, definition in (
        ('agents', 'execution_generation', 'TEXT'),
        ('agents', 'delegation_protocol_enabled', 'INTEGER NOT NULL DEFAULT 0 CHECK(delegation_protocol_enabled IN (0,1))'),
        ('tasks', 'orchestration_handoff', 'TEXT'),
    ):
        if field not in {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {field} {definition}')


def accept(db, origin, message, *, session_id, message_id):
    try:
        payload = delegation_protocol.decode(message)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Malformed delegation JSON') from exc
    extension = payload.get('protocol') == delegation_continuation.PROTOCOL if isinstance(payload, dict) else False
    allocating = payload.get('protocol') == delegation_v3.PROTOCOL if isinstance(payload, dict) else False
    if extension:
        payload = delegation_continuation.validate(message)
        if payload['action'] != 'request_delegations':
            raise ValueError('Expected a delegation request')
        requests = payload['requests']
    elif allocating:
        # v3: v1 requests with an optional Child allocation and v2's continuation.
        payload = delegation_v3.validate(message)
        requests = payload['requests']
        extension = payload['continuation'] is not None
    else:
        requests = delegation_protocol.validate(message)
    task = dependencies.task(db, origin.parent_task_id)
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (origin.agent_id,)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (origin.assignment_id,)).fetchone()
    if extension and (not agent or not agent['hybrid_delegation_enabled']):
        raise ValueError('Hybrid continuation protocol is not enabled')
    if (not agent or not agent['delegation_protocol_enabled']
            or agent['execution_generation'] != origin.generation
            or agent['session_id'] != session_id or not session_id
            or not assignment or assignment['ended_at'] is not None
            or assignment['agent_id'] != origin.agent_id or assignment['task_id'] != origin.parent_task_id
            or task['project_id'] != origin.project_id):
        raise ValueError('Stale or mismatched delegation execution ownership')
    work_control.require_ancestors(db, origin.parent_task_id)
    # A committed receipt can be reread after a lost acknowledgement. It cannot
    # revive canceled work, launch a process, or replace another generation.
    receipt = json.loads(task['orchestration_handoff']) if task['orchestration_handoff'] else None
    plan = payload['continuation'] if extension else None
    if allocating and not agent['delegation_protocol_enabled']:
        raise ValueError('Delegation protocol is not enabled')
    history = delegation_rounds.history(db, origin.parent_task_id)
    for previous in history:
        accepted = previous['receipt']
        if accepted['generation'] == origin.generation and accepted['message_id'] == message_id:
            if accepted['requests'] != requests or accepted.get('continuation') != plan:
                raise ValueError('Conflicting replay of accepted delegation message')
            return accepted
    from . import agent_removal
    # Exact replays above are read-only receipts; anything new after removal
    # admission is refused inside this same transaction.
    agent_removal.require_operational(db, origin.agent_id)
    if receipt:
        if not agent['hybrid_delegation_enabled']:
            raise ValueError('Parent already handed control to orchestration')
        current, _, _ = delegation_orchestration.owner(db, origin.parent_task_id, allow_running=True)
        if current['phase'] not in ('independent', 'finishing'):
            raise ValueError('Parent must finish its current continuation or acknowledge delivered results before a new round')
        if db.execute("SELECT 1 FROM delegation_deliveries WHERE task_id=? AND status<>'acknowledged'", (origin.parent_task_id,)).fetchone():
            raise ValueError('Outstanding delivery must be acknowledged before a new round')
    delegation_safety.check_depth(db, origin.parent_task_id, child=True)
    if db.execute('SELECT 1 FROM delegations WHERE child_task_id=?', (origin.parent_task_id,)).fetchone() and not agent['recursive_delegation_enabled']:
        raise ValueError('Recursive delegation is not authorized for this Child')
    if agent['status'] != 'running':
        raise ValueError('Delegation requires a running originating Agent')
    work_control.require_active(task, db=db)
    for previous in history:
        accepted = previous['receipt']
        if (sorted(accepted['requests'], key=lambda r: r['request_key']) == sorted(requests, key=lambda r: r['request_key'])
                and accepted.get('continuation') == plan):
            # A new Worker/turn replay is not fresh work. Preserve the original
            # round and plan consumption, and do not create a paid resume loop.
            delegation_orchestration.revoke(db, task['task_id'], 'Repeated accepted round; explicit inspection required')
            db.execute("UPDATE agents SET status='stopped',waiting_question=NULL WHERE agent_id=?", (origin.agent_id,))
            dependencies.reconcile(db)
            return accepted
    records = [delegations.create(db, origin.parent_task_id, project_id=origin.project_id,
               requested_by_agent_id=origin.agent_id, requested_by_assignment_id=origin.assignment_id,
               **request) for request in requests]  # A v3 allocation is part of each request's identity.
    if receipt and all(db.execute('SELECT 1 FROM delegation_round_requests WHERE delegation_id=?',
                                  (r['delegation_id'],)).fetchone() for r in records):
        raise ValueError('A new round must include a fresh request key; accepted work cannot be reissued as new work')
    receipt = dict(reason='delegation_requested', agent_id=origin.agent_id,
                   assignment_id=origin.assignment_id, generation=origin.generation,
                   session_id=session_id, turn=1, message_id=message_id, requests=requests,
                   delegation_ids=[r['delegation_id'] for r in records])
    if extension:
        receipt['continuation'] = payload['continuation']
    if allocating:
        receipt['protocol'] = delegation_v3.PROTOCOL
    batch_id = delegation_rounds.record(db, task['task_id'], receipt)
    if agent['hybrid_delegation_enabled']:
        delegation_orchestration.initialize(db, task, receipt, plan, batch_id)
    db.execute("UPDATE tasks SET orchestration_handoff=?,status='blocked',updated_at=" + delegations.NOW + ' WHERE task_id=?',
               (json.dumps(receipt), origin.parent_task_id))
    db.execute("UPDATE agents SET status='stopped',waiting_question=NULL WHERE agent_id=?", (origin.agent_id,))
    # Retain TaskAssignment continuity; existing resource reconciliation remains
    # authoritative about claims and physical ownership. Do not invent releases.
    dependencies.reconcile(db)
    runtime_resources.sync(db)
    return receipt
