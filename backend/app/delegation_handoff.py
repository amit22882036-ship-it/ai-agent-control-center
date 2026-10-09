"""Transactional, provider-independent authorization and Parent handoff."""
import json
from dataclasses import dataclass

from . import delegation_protocol, delegations, dependencies, work_control, runtime_resources


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
    requests = delegation_protocol.validate(message)
    task = dependencies.task(db, origin.parent_task_id)
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (origin.agent_id,)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (origin.assignment_id,)).fetchone()
    if (not agent or not agent['delegation_protocol_enabled']
            or agent['execution_generation'] != origin.generation
            or agent['session_id'] != session_id or not session_id
            or not assignment or assignment['ended_at'] is not None
            or assignment['agent_id'] != origin.agent_id or assignment['task_id'] != origin.parent_task_id
            or task['project_id'] != origin.project_id):
        raise ValueError('Stale or mismatched delegation execution ownership')
    # A committed receipt can be reread after a lost acknowledgement. It cannot
    # revive canceled work, launch a process, or replace another generation.
    receipt = json.loads(task['orchestration_handoff']) if task['orchestration_handoff'] else None
    if receipt:
        if (receipt['generation'] == origin.generation and receipt['message_id'] == message_id
                and receipt['requests'] == requests):
            return receipt
        raise ValueError('Parent already handed control to orchestration')
    if agent['status'] != 'running':
        raise ValueError('Delegation requires a running originating Agent')
    work_control.require_active(task, db=db)
    records = [delegations.create(db, origin.parent_task_id, project_id=origin.project_id,
               requested_by_agent_id=origin.agent_id, requested_by_assignment_id=origin.assignment_id,
               **request) for request in requests]
    receipt = dict(reason='delegation_requested', agent_id=origin.agent_id,
                   assignment_id=origin.assignment_id, generation=origin.generation,
                   session_id=session_id, turn=1, message_id=message_id, requests=requests,
                   delegation_ids=[r['delegation_id'] for r in records])
    db.execute("UPDATE tasks SET orchestration_handoff=?,status='blocked',updated_at=" + delegations.NOW + ' WHERE task_id=?',
               (json.dumps(receipt), origin.parent_task_id))
    db.execute("UPDATE agents SET status='stopped',waiting_question=NULL WHERE agent_id=?", (origin.agent_id,))
    # Retain TaskAssignment continuity; existing resource reconciliation remains
    # authoritative about claims and physical ownership. Do not invent releases.
    dependencies.reconcile(db)
    runtime_resources.sync(db)
    return receipt
