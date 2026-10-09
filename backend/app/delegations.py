"""Provider-independent delegation records; no execution or hierarchy side effects.

Mutation helpers require the caller's BEGIN IMMEDIATE transaction. Assignment
ownership remains authoritative in task_assignments, never in this history.
"""
from uuid import uuid4

from . import dependencies, work_control

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def migrate(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS delegations (
        delegation_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
        requested_by_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        requested_by_assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        request_key TEXT NOT NULL CHECK(length(request_key) BETWEEN 1 AND 128),
        instruction TEXT NOT NULL CHECK(length(instruction) BETWEEN 1 AND 32768),
        status TEXT NOT NULL CHECK(status IN ('requested','materialized','result_ready','acknowledged','canceled')),
        child_task_id TEXT UNIQUE REFERENCES tasks(task_id),
        created_at TEXT NOT NULL DEFAULT ({NOW}),
        updated_at TEXT NOT NULL DEFAULT ({NOW}),
        materialized_at TEXT,closed_at TEXT,
        UNIQUE(parent_task_id,request_key),
        CHECK(child_task_id IS NULL OR child_task_id<>parent_task_id),
        CHECK((child_task_id IS NULL AND materialized_at IS NULL) OR
              (child_task_id IS NOT NULL AND materialized_at IS NOT NULL)),
        CHECK((status='requested' AND child_task_id IS NULL) OR
              (status IN ('materialized','result_ready','acknowledged') AND child_task_id IS NOT NULL) OR
              status='canceled'),
        CHECK((status IN ('acknowledged','canceled') AND closed_at IS NOT NULL) OR
              (status NOT IN ('acknowledged','canceled') AND closed_at IS NULL)))''')
    # The unique indexes already cover parent/idempotency and child lookup.
    db.execute('CREATE INDEX IF NOT EXISTS delegation_project_status ON delegations(project_id,status)')
    db.execute('CREATE INDEX IF NOT EXISTS delegation_status ON delegations(status)')
    db.execute('CREATE INDEX IF NOT EXISTS delegation_assignment ON delegations(requested_by_assignment_id)')


def get(db, delegation_id):
    row = db.execute('SELECT * FROM delegations WHERE delegation_id=?', (delegation_id,)).fetchone()
    if row is None:
        raise LookupError('Delegation not found')
    return dict(row)


def list_for_task(db, parent_task_id):
    dependencies.task(db, parent_task_id)
    return [dict(r) for r in db.execute(
        'SELECT * FROM delegations WHERE parent_task_id=? ORDER BY rowid', (parent_task_id,))]


def create(db, parent_task_id, *, project_id, requested_by_agent_id,
           requested_by_assignment_id, request_key, instruction):
    if (not isinstance(request_key, str) or not 1 <= len(request_key) <= 128
            or request_key != request_key.strip()
            or any(ord(c) < 32 or ord(c) == 127 for c in request_key)):
        raise ValueError('Request key must be 1-128 characters without outer whitespace or controls')
    if (not isinstance(instruction, str) or not instruction.strip()
            or len(instruction) > 32768 or '\x00' in instruction):
        raise ValueError('Delegation instruction must be non-blank and at most 32768 characters without NUL')
    parent = dependencies.task(db, parent_task_id)
    if not project_id or parent['project_id'] != project_id:
        raise ValueError('Delegation requires the Parent Task Project')
    origin = db.execute('''SELECT a.*,g.status AS agent_status FROM task_assignments a
        JOIN agents g USING(agent_id) WHERE assignment_id=?''', (requested_by_assignment_id,)).fetchone()
    if (origin is None or origin['task_id'] != parent_task_id
            or origin['agent_id'] != requested_by_agent_id):
        raise ValueError('Delegation origin must match the Parent Task assignment and Agent')
    if origin['ended_at'] is not None or origin['agent_status'] not in ('running', 'waiting'):
        raise ValueError('Delegation origin must be a current active assignment')
    existing = db.execute('SELECT * FROM delegations WHERE parent_task_id=? AND request_key=?',
                          (parent_task_id, request_key)).fetchone()
    if existing:
        if existing['instruction'] != instruction:
            raise ValueError('Request key already belongs to a different delegation instruction')
        # A current replacement may rediscover the request; keep original origin,
        # timestamps, child link and terminal state. Never revive or overwrite it.
        return dict(existing)
    work_control.require_active(parent, db=db)
    identifier = str(uuid4())
    db.execute('''INSERT INTO delegations(delegation_id,project_id,parent_task_id,
        requested_by_agent_id,requested_by_assignment_id,request_key,instruction,status)
        VALUES (?,?,?,?,?,?,?,'requested')''',
        (identifier, project_id, parent_task_id, requested_by_agent_id,
         requested_by_assignment_id, request_key, instruction))
    return get(db, identifier)


def attach_child(db, delegation_id, child_task_id):
    record = get(db, delegation_id)
    parent = dependencies.task(db, record['parent_task_id'])
    child = dependencies.task(db, child_task_id)
    if (child_task_id == parent['task_id'] or child['parent_task_id'] != parent['task_id']
            or child['project_id'] != record['project_id'] or parent['project_id'] != record['project_id']):
        raise ValueError('Delegation child must be a direct Child Task in the same Project')
    if record['status'] == 'materialized' and record['child_task_id'] == child_task_id:
        return record
    if record['status'] != 'requested' or record['child_task_id'] is not None:
        raise ValueError('Only a requested Delegation can attach one Child Task')
    if db.execute('SELECT 1 FROM delegations WHERE child_task_id=?', (child_task_id,)).fetchone():
        raise ValueError('Child Task already belongs to a Delegation')
    db.execute(f'''UPDATE delegations SET child_task_id=?,status='materialized',
        materialized_at={NOW},updated_at={NOW} WHERE delegation_id=?''', (child_task_id, delegation_id))
    return get(db, delegation_id)


def transition(db, delegation_id, status):
    record = get(db, delegation_id)
    if status == 'canceled' and record['status'] == 'canceled':
        return record
    if status != 'canceled' or record['status'] not in ('requested', 'materialized'):
        raise ValueError('Unsupported Delegation transition; result delivery is not implemented')
    db.execute(f"UPDATE delegations SET status='canceled',closed_at={NOW},updated_at={NOW} WHERE delegation_id=?", (delegation_id,))
    return get(db, delegation_id)
