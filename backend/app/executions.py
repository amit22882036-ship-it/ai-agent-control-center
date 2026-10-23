"""Durable execution journal (Stage 2D.1b P1): one row per worker process launch.

A row records which backend instance (controller) launched the process and the
kernel evidence that controller held: the named Job Object, the limit flags
read back from it and the root identity (PID plus creation time; a PID alone is
reused). Rows are written only from evidence the controller observed itself; no
historical row is ever fabricated. SQL helpers run inside the caller's
transaction and never touch a process.

Every launch first commits a `launching` reservation (Stage 2D.1b phase 0),
before any process exists; the admission transaction then consumes it. A
process therefore never exists without a durable row naming its launch.

Ownership policy (P2): an execution is unresolved while its row is
`uncertain`, for any reason. An observed root exit is not evidence that the
process tree ended, so an unsupervised execution stays unresolved too. Startup
turns every open row of a previous controller into a settled state or into an
unresolved one (see execution_recovery). While unresolved, its Agent and Task
keep their assignment, claims and work-intent scopes, no new execution is
admitted and no removal completes.
"""
import json
import sqlite3
from uuid import uuid4

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
CONTAINMENT = ('job_kill_on_close', 'none', 'legacy_unknown')
STATES = ('launching', 'spawned', 'contained', 'running', 'ended_verified', 'ended_kernel_guaranteed',
          'ended_by_reboot', 'launch_aborted', 'uncertain', 'attested_ended')
OPEN = ('launching', 'spawned', 'contained', 'running', 'uncertain')
# States the live controller itself may end; anything else needs restart resolution (P2).
LIVE = ('contained', 'running')
# Only kernel evidence from a verified Job Object can produce these states.
KERNEL_ONLY = ('spawned', 'contained', 'ended_verified', 'ended_kernel_guaranteed')
# No uncertainty is exempt: every uncertain row is unresolved and blocks (any
# controller). A root exit observed without Job evidence settles nothing.
NON_BLOCKING = ()
LEGACY_CONTROLLER = 'legacy'


def _sql(values):
    return '(' + ','.join(f"'{value}'" for value in values) + ')'


def migrate(db):
    """v27 -> v28. Two new tables; no existing table changes. Idempotent."""
    db.execute(f'''CREATE TABLE IF NOT EXISTS controller_instances (
        instance_id TEXT PRIMARY KEY,
        pid INTEGER NOT NULL,
        created_at TEXT,
        session_id INTEGER,
        user_sid TEXT,
        boot_id INTEGER,
        os_boot_time TEXT,
        identity TEXT NOT NULL DEFAULT '{{}}',
        started_at TEXT NOT NULL DEFAULT ({NOW}),
        stopped_at TEXT)''')
    db.execute(f'''CREATE TABLE IF NOT EXISTS executions (
        execution_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL,
        assignment_id TEXT REFERENCES task_assignments(assignment_id),
        task_id TEXT REFERENCES tasks(task_id),
        generation TEXT,
        provider TEXT NOT NULL,
        controller_instance_id TEXT NOT NULL REFERENCES controller_instances(instance_id),
        containment TEXT NOT NULL CHECK(containment IN {_sql(CONTAINMENT)}),
        job_name TEXT UNIQUE,
        job_config TEXT,
        root_pid INTEGER,
        root_created_at TEXT,
        state TEXT NOT NULL CHECK(state IN {_sql(STATES)}),
        evidence TEXT NOT NULL DEFAULT '{{}}',
        uncertainty TEXT,
        attestation TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW}), updated_at TEXT NOT NULL DEFAULT ({NOW}),
        ended_at TEXT,
        CHECK((ended_at IS NULL) = (state IN {_sql(OPEN)})),
        CHECK(state <> 'uncertain' OR uncertainty IS NOT NULL),
        CHECK(state NOT IN {_sql(KERNEL_ONLY)} OR containment = 'job_kill_on_close'),
        CHECK(containment <> 'job_kill_on_close' OR state = 'launching' OR (job_name IS NOT NULL
              AND job_config IS NOT NULL AND root_pid IS NOT NULL AND root_created_at IS NOT NULL)),
        CHECK(containment = 'job_kill_on_close' OR job_name IS NULL))''')
    db.execute(f'''CREATE INDEX IF NOT EXISTS executions_open ON executions(assignment_id)
        WHERE state IN {_sql(OPEN)}''')
    db.execute('CREATE INDEX IF NOT EXISTS executions_agent ON executions(agent_id)')


def register_controller(db, identity):
    """Insert this controller's row once; repeated calls are no-ops."""
    db.execute('''INSERT OR IGNORE INTO controller_instances
        (instance_id,pid,created_at,session_id,user_sid,boot_id,os_boot_time,identity)
        VALUES (?,?,?,?,?,?,?,?)''',
        (identity['instance_id'], identity['pid'], identity['created_at'], identity['session_id'],
         identity['user_sid'], identity['boot_id'], identity['os_boot_time'],
         json.dumps(identity, sort_keys=True)))


def controller_stopped(db, instance_id):
    db.execute(f'UPDATE controller_instances SET stopped_at={NOW} WHERE instance_id=? AND stopped_at IS NULL',
               (instance_id,))


RESERVED = 'Execution refused: another launch of this Task or Agent is already reserved'
_OPEN_RESERVATION = '''SELECT execution_id FROM executions WHERE state='launching' AND controller_instance_id=?
    AND (agent_id=? OR (? IS NOT NULL AND task_id=?)) AND execution_id<>? LIMIT 1'''


def reserve(db, controller, *, agent_id, task_id, provider, containment, job_name=None):
    """Commit a `launching` row before any process exists; returns its execution_id.

    An open reservation of this controller for the same Task or Agent refuses
    the new one. The caller's BEGIN IMMEDIATE serializes check and insert.
    """
    register_controller(db, controller)
    if db.execute(_OPEN_RESERVATION, (controller['instance_id'], agent_id, task_id, task_id, '')).fetchone():
        raise ValueError(RESERVED)
    assignment = db.execute('SELECT assignment_id FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                            (agent_id,)).fetchone()
    execution_id = str(uuid4())
    db.execute('''INSERT INTO executions(execution_id,agent_id,assignment_id,task_id,provider,controller_instance_id,
        containment,job_name,state) VALUES (?,?,?,?,?,?,?,?,'launching')''',
        (execution_id, agent_id, assignment['assignment_id'] if assignment else None, task_id, provider,
         controller['instance_id'], containment, job_name))
    return execution_id


def require_reserved(db, execution_id, controller_instance_id, agent_id, task_id):
    """In the admission transaction: the reservation is still open and the only one."""
    row = db.execute('SELECT state FROM executions WHERE execution_id=?', (execution_id,)).fetchone()
    if row is None or row['state'] != 'launching':
        raise ValueError('Execution refused: its launch reservation is no longer open')
    if db.execute(_OPEN_RESERVATION, (controller_instance_id, agent_id, task_id, task_id, execution_id)).fetchone():
        raise ValueError(RESERVED)


def record_launch(db, execution_id, *, containment, state, evidence,
                  job_name=None, job_config=None, root_pid=None, root_created_at=None):
    """Consume the reservation inside the admission transaction (launching -> contained or running)."""
    row = db.execute("SELECT evidence,agent_id FROM executions WHERE execution_id=? AND state='launching'",
                     (execution_id,)).fetchone()
    if row is None:
        raise sqlite3.IntegrityError('Launch reservation is no longer open')
    assignment = db.execute('SELECT assignment_id FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                            (row['agent_id'],)).fetchone()
    db.execute(f'''UPDATE executions SET containment=?,job_name=?,job_config=?,root_pid=?,root_created_at=?,state=?,
        evidence=?,assignment_id=COALESCE(assignment_id,?),updated_at={NOW}
        WHERE execution_id=? AND state=?''',
        (containment, job_name, json.dumps(job_config, sort_keys=True) if job_config is not None else None,
         root_pid, root_created_at, state, json.dumps({**json.loads(row['evidence']), **evidence}, sort_keys=True),
         assignment['assignment_id'] if assignment else None, execution_id, 'launching'))
    return execution_id


REFUSED = ('Execution refused: an earlier process of this Task or Agent could not be confirmed ended, '
           'and that is never assumed')


_BLOCKING = "state='uncertain'" + (f' AND uncertainty NOT IN {_sql(NON_BLOCKING)}' if NON_BLOCKING else '')


def blocking(db, *, agent_id=None, task_id=None):
    """Unresolved executions of an Agent or a Task (any controller)."""
    if agent_id is None and task_id is None:
        return []
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='executions'").fetchone():
        return []  # Earlier schema steps run shared lifecycle code before v28 exists.
    return [_row(r) for r in db.execute(f'''SELECT * FROM executions WHERE {_BLOCKING}
        AND ((? IS NOT NULL AND agent_id=?) OR (? IS NOT NULL AND task_id=?)) ORDER BY rowid''',
        (agent_id, agent_id, task_id, task_id))]


def held_tasks(db):
    """Tasks whose claims and work-intent scopes stay held: an execution may still own them."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='executions'").fetchone():
        return set()
    return {r[0] for r in db.execute(f'SELECT DISTINCT task_id FROM executions WHERE {_BLOCKING} AND task_id IS NOT NULL')}


def describe(rows):
    return ', '.join(f"{r['execution_id']} ({r['uncertainty']})" for r in rows)


def require_admissible(db, controller_instance_id, agent_id, task_id):
    """Refuse a launch, inside its admission transaction, over any unresolved earlier execution."""
    if blocking(db, agent_id=agent_id, task_id=task_id):
        raise ValueError(REFUSED)


def legacy_controller(db, current):
    """A controller row standing for every backend before the journal, with today's boot evidence."""
    instance = f'{LEGACY_CONTROLLER}-{uuid4()}'
    register_controller(db, {'instance_id': instance, 'pid': 0, 'created_at': None, 'session_id': None,
                             'user_sid': None, 'boot_id': current.get('boot_id'),
                             'os_boot_time': current.get('os_boot_time'),
                             'legacy': True, 'observed_by': current.get('instance_id')})
    return instance


def record_legacy(db, legacy_instance, agent_id, basis):
    """One `legacy_unknown` uncertainty row: no containment evidence is claimed."""
    agent = db.execute('SELECT agent_type FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
    latest = db.execute('SELECT assignment_id,task_id FROM task_assignments WHERE agent_id=? ORDER BY rowid DESC LIMIT 1',
                        (agent_id,)).fetchone()
    execution_id = str(uuid4())
    db.execute('''INSERT INTO executions(execution_id,agent_id,assignment_id,task_id,provider,controller_instance_id,
        containment,state,uncertainty,evidence) VALUES (?,?,?,?,?,?,'legacy_unknown','uncertain','legacy',?)''',
        (execution_id, agent_id, latest['assignment_id'] if latest else None, latest['task_id'] if latest else None,
         agent['agent_type'] if agent else 'unknown', legacy_instance, json.dumps({'legacy': {'basis': basis}})))
    return execution_id


IDENTITY = ('containment', 'job_name', 'job_config', 'root_pid', 'root_created_at')


def transition(db, execution_id, state, from_states, *, evidence=None, uncertainty=None, identity=None,
               replace_uncertainty=False, sync_blocker=True, attestation=None):
    """Conditional transition; returns False when the row is not in `from_states`.

    `identity` sets observed containment columns, used when a reservation is
    closed by its failed launch (the admission's containment update was lost).
    """
    row = db.execute('SELECT evidence,state,task_id FROM executions WHERE execution_id=?', (execution_id,)).fetchone()
    if row is None or row['state'] not in from_states:
        return False
    merged = {**json.loads(row['evidence']), **(evidence or {})}
    ended = state not in OPEN
    identity = {k: (json.dumps(v, sort_keys=True) if k == 'job_config' and v is not None else v)
                for k, v in (identity or {}).items() if k in IDENTITY}
    if attestation is not None:
        identity['attestation'] = json.dumps(attestation, sort_keys=True)
    columns = ''.join(f'{key}=?, ' for key in identity)
    keep = 'uncertainty' if not (replace_uncertainty and uncertainty is not None) else 'NULL'
    db.execute(f'''UPDATE executions SET {columns}state=?, evidence=?, uncertainty=COALESCE(?,{keep}),
        updated_at={NOW}, ended_at=CASE WHEN ? THEN {NOW} ELSE NULL END WHERE execution_id=? AND state=?''',
        (*identity.values(), state, json.dumps(merged, sort_keys=True), uncertainty, int(ended), execution_id,
         row['state']))
    if sync_blocker and row['task_id'] and 'uncertain' in (state, row['state']):
        sync_ownership_blocker(db, row['task_id'])
    return True


def sync_ownership_blocker(db, task_id):
    """The Task's visible hard blocker follows its unresolved executions, in the same transaction.

    Added the moment an execution becomes uncertain (before its outcome is
    saved), so no completion, release or replacement can run first; lifted
    only when authoritative evidence settled every execution of the Task.
    """
    from . import dependencies
    from .recovery_actions import OWNERSHIP_BLOCKER
    agents = [r[0] for r in db.execute('SELECT DISTINCT agent_id FROM task_assignments WHERE task_id=?', (task_id,))]
    if blocking(db, task_id=task_id) or any(blocking(db, agent_id=agent) for agent in agents):
        db.execute('''INSERT OR IGNORE INTO task_blockers(id,task_id,blocker_type,reason_code,active,hard)
            VALUES (?,?,?,?,1,1)''', (str(uuid4()), task_id, *OWNERSHIP_BLOCKER))
    else:
        db.execute(f'''UPDATE task_blockers SET active=0,resolved_at={NOW}
            WHERE task_id=? AND blocker_type=? AND reason_code=? AND active=1''', (task_id, *OWNERSHIP_BLOCKER))
    dependencies.reconcile(db)


def link(db, execution_id, agent_id, generation=None):
    """Attach the Agent's open assignment (and generation) once, in the caller's save."""
    assignment = db.execute('SELECT assignment_id FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                            (agent_id,)).fetchone()
    db.execute(f'''UPDATE executions SET assignment_id=COALESCE(assignment_id,?), generation=COALESCE(generation,?),
        updated_at={NOW} WHERE execution_id=? AND agent_id=?
        AND ((assignment_id IS NULL AND ? IS NOT NULL) OR (generation IS NULL AND ? IS NOT NULL))''',
        (assignment['assignment_id'] if assignment else None, generation, execution_id, agent_id,
         assignment['assignment_id'] if assignment else None, generation))


def _row(row):
    if row is None:
        return None
    value = dict(row)
    for key in ('evidence', 'job_config', 'attestation', 'identity'):
        if value.get(key) is not None:
            value[key] = json.loads(value[key])
    return value


def get(db, execution_id):
    return _row(db.execute('SELECT * FROM executions WHERE execution_id=?', (execution_id,)).fetchone())


def for_agent(db, agent_id):
    return [_row(r) for r in db.execute('SELECT * FROM executions WHERE agent_id=? ORDER BY rowid', (agent_id,))]


def controller(db, instance_id):
    return _row(db.execute('SELECT * FROM controller_instances WHERE instance_id=?', (instance_id,)).fetchone())
