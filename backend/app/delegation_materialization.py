"""Durable, provider-independent child preparation and bounded execution admission.

The journal is evidence of external-operation progress, not Worker ownership.
TaskAssignments remain authoritative. No process/filesystem work occurs in the
transaction helpers; the manager supplies the existing launch implementation.
"""
from dataclasses import dataclass
import json
from uuid import uuid4

from . import delegations, dependencies, work_control, work_intents
from .task_domain import task_title

NOW = delegations.NOW


@dataclass(frozen=True)
class Limits:
    children_per_parent: int = 16
    concurrent_children: int = 4
    depth: int = 1
    preparation_attempts: int = 3
    retry_seconds: tuple = (2, 10)

    def __post_init__(self):
        if min(self.children_per_parent, self.concurrent_children, self.depth, self.preparation_attempts) < 1:
            raise ValueError('Materialization limits must be positive')
        if any(delay <= 0 for delay in self.retry_seconds):
            raise ValueError('Retry delays must be positive')


DEFAULT_LIMITS = Limits()


def migrate(db):
    if 'child_materialization_enabled' not in {r['name'] for r in db.execute('PRAGMA table_info(agents)')}:
        db.execute('ALTER TABLE agents ADD COLUMN child_materialization_enabled INTEGER NOT NULL DEFAULT 0 CHECK(child_materialization_enabled IN (0,1))')
    db.execute(f'''CREATE TABLE IF NOT EXISTS delegation_materializations (
        delegation_id TEXT PRIMARY KEY REFERENCES delegations(delegation_id),
        phase TEXT NOT NULL CHECK(phase IN ('attached','workspace_ready','launching','started','recovery_required')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
        launch_agent_id TEXT UNIQUE,
        last_error TEXT,
        updated_at TEXT NOT NULL DEFAULT ({NOW}))''')


def inspect(db, identifier):
    row = db.execute('SELECT * FROM delegation_materializations WHERE delegation_id=?', (identifier,)).fetchone()
    return dict(row) if row else None


def authorize(db, identifier, limits=DEFAULT_LIMITS):
    record = delegations.get(db, identifier)
    parent = dependencies.task(db, record['parent_task_id'])
    if record['status'] not in ('requested', 'materialized') or parent['project_id'] != record['project_id']:
        raise ValueError('Delegation is canceled, closed or belongs to a different Project')
    receipt = json.loads(parent['orchestration_handoff']) if parent['orchestration_handoff'] else {}
    if (receipt.get('reason') != 'delegation_requested' or identifier not in receipt.get('delegation_ids', ())
            or {'request_key': record['request_key'], 'instruction': record['instruction']} not in receipt.get('requests', ())):
        raise ValueError('Delegation has no matching accepted Parent handoff')
    # The receipt may acknowledge an idempotent request with older provenance.
    # Authority belongs to the receipt's current assignment, not that old Worker.
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (receipt.get('agent_id'),)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (receipt.get('assignment_id'),)).fetchone()
    if (not agent or not agent['child_materialization_enabled'] or not agent['delegation_protocol_enabled']
            or agent['execution_generation'] != receipt.get('generation')
            or agent['session_id'] != receipt.get('session_id') or agent['status'] != 'stopped'
            or not assignment or assignment['ended_at'] is not None
            or assignment['task_id'] != parent['task_id'] or assignment['agent_id'] != agent['agent_id']):
        raise ValueError('Materialization activation or Parent handoff ownership is no longer current')
    if parent['control_intent'] != 'active' or parent['status'] != 'blocked' or parent['stop_required']:
        raise ValueError('Parent control state prevents materialization')
    if db.execute("""SELECT 1 FROM task_blockers WHERE task_id=? AND active=1 AND hard=1
        AND NOT(blocker_type='orchestration' AND reason_code='delegation_requested')""", (parent['task_id'],)).fetchone():
        raise ValueError('Parent has unrelated execution blockers')
    # Orchestration intentionally suspends dormant Parent claims. Suspension
    # alone is not an unrelated blocker; actual conflicts remain checked above.
    if db.execute("SELECT 1 FROM resource_claims WHERE task_id=? AND mode<>'advisory' AND status='waiting'", (parent['task_id'],)).fetchone():
        raise ValueError('Parent resource bundle is not granted')
    if agent['agent_type'] not in ('mock', 'codex') or (agent['agent_type'] == 'codex' and agent['sandbox'] not in ('read-only', 'workspace-write')):
        raise ValueError('Unsupported Parent provider or sandbox')
    return record, parent, dict(agent)


def _check_depth(db, parent, limits):
    depth, current, seen = 1, parent, set()
    while current['parent_task_id']:
        if current['task_id'] in seen:
            raise ValueError('Task hierarchy cycle')
        seen.add(current['task_id'])
        depth += 1
        current = dependencies.task(db, current['parent_task_id'])
    if depth > limits.depth:
        raise ValueError('Delegation depth limit reached')


def attach(db, identifier, limits=DEFAULT_LIMITS):
    record, parent, _ = authorize(db, identifier, limits)
    _check_depth(db, parent, limits)
    if record['child_task_id']:
        child = dependencies.task(db, record['child_task_id'])
        if child['parent_task_id'] != parent['task_id'] or child['project_id'] != parent['project_id']:
            raise ValueError('Child ownership mismatch')
        if inspect(db, identifier) is None:
            raise ValueError('Externally attached Child requires inspection; cannot adopt execution')
        return record
    count = db.execute('SELECT COUNT(*) FROM tasks WHERE parent_task_id=?', (parent['task_id'],)).fetchone()[0]
    if count >= limits.children_per_parent:
        raise ValueError('Parent Child capacity reached; request retained')
    # The public Task API keeps its 20000-character limit. This internal path
    # preserves the entire validated 32768-character protocol instruction.
    delegations.validate_request(record['request_key'], record['instruction'])
    child_id = str(uuid4())
    db.execute("INSERT INTO tasks(task_id,title,description,status,parent_task_id,project_id) VALUES (?,?,?,'pending',?,?)",
               (child_id, task_title(record['instruction']), record['instruction'], parent['task_id'], parent['project_id']))
    record = delegations.attach_child(db, identifier, child_id)
    db.execute("INSERT INTO delegation_materializations(delegation_id,phase) VALUES (?,'attached')", (identifier,))
    return record


def check_worker(db, task_id, agent_id=None):
    row = db.execute('''SELECT m.* FROM delegation_materializations m JOIN delegations d USING(delegation_id)
        WHERE d.child_task_id=?''', (task_id,)).fetchone()
    if row and row['phase'] == 'started':
        return None  # Explicit normal replacement follows existing Task rules.
    if row and (row['phase'] != 'launching' or row['launch_agent_id'] != agent_id):
        raise ValueError('Delegated Child requires controlled materialization; no duplicate Worker allowed')
    return dict(row) if row else None


def admit(db, identifier, limits=DEFAULT_LIMITS):
    record, _, provider = authorize(db, identifier, limits)
    progress = inspect(db, identifier)
    if not progress or progress['phase'] != 'workspace_ready':
        raise ValueError('Child is not ready for a new launch')
    task_id = record['child_task_id']
    child = dependencies.task(db, task_id)
    work_control.require_active(child, pending=True, db=db)
    if db.execute('SELECT 1 FROM task_assignments WHERE task_id=?', (task_id,)).fetchone():
        raise ValueError('Child already has Worker history; automatic replacement is not authorized')
    if not db.execute('SELECT 1 FROM task_workspaces WHERE task_id=? AND source_task_id=? AND project_id=?',
                      (task_id, record['parent_task_id'], record['project_id'])).fetchone():
        raise ValueError('Child Workspace must come from its Parent')
    live = [dict(r) for r in db.execute('''SELECT DISTINCT t.* FROM tasks t JOIN task_assignments a USING(task_id)
        JOIN agents g USING(agent_id) WHERE a.ended_at IS NULL AND g.status IN ('running','waiting')''')]
    reserved = [dict(r) for r in db.execute('''SELECT t.* FROM tasks t JOIN delegations d ON d.child_task_id=t.task_id
        JOIN delegation_materializations m USING(delegation_id) WHERE m.phase IN ('launching','recovery_required')''')]
    live = {t['task_id']: t for t in live + reserved}
    delegated_live = db.execute('''SELECT COUNT(*) FROM delegation_materializations m JOIN delegations d USING(delegation_id)
        WHERE m.phase IN ('launching','recovery_required') OR EXISTS
        (SELECT 1 FROM task_assignments a WHERE a.task_id=d.child_task_id AND a.ended_at IS NULL)''').fetchone()[0]
    if delegated_live >= limits.concurrent_children:
        raise ValueError('Child concurrency capacity reached')
    own = [r for r in work_intents.inspect(db, task_id) if r['status'] == 'active' and r['mode'] == 'single_owner' and r['holds_authority']]
    for other in live.values():
        if other['project_id'] != child['project_id']:
            continue  # Machine-global conflicts are still checked by resource gates.
        theirs = [r for r in work_intents.inspect(db, other['task_id']) if r['status'] == 'active' and r['mode'] == 'single_owner' and r['holds_authority']]
        if not own or not theirs or any(work_intents.overlaps(a, b) for a in own for b in theirs):
            raise ValueError('Parallel work requires declared non-overlapping responsibility scopes')
    agent_id = str(uuid4())
    db.execute(f"UPDATE delegation_materializations SET phase='launching',launch_agent_id=?,last_error=NULL,updated_at={NOW} WHERE delegation_id=?",
               (agent_id, identifier))
    return agent_id, provider


def recover(db):
    # Never infer that an unacknowledged Popen did not happen, even if no Agent
    # row exists. Startup restores evidence only and never launches anything.
    db.execute(f"""UPDATE delegation_materializations SET phase='recovery_required',
        last_error='Backend interrupted launch; process ownership requires inspection',updated_at={NOW}
        WHERE phase='launching' OR (phase='started' AND EXISTS
        (SELECT 1 FROM agents g WHERE g.agent_id=launch_agent_id AND g.status='running'))""")


class Materializer:
    """One controlled pass. The caller schedules bounded retries, never startup."""
    def __init__(self, store, launch, publish, limits=DEFAULT_LIMITS):
        self.store, self.launch, self.publish, self.limits = store, launch, publish, limits

    def prepare(self, identifier):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            record = attach(db, identifier, self.limits)
        self.publish(None)
        return record

    def run(self, identifier):
        record = self.prepare(identifier)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            progress = inspect(db, identifier)
            if progress['phase'] in ('launching', 'started', 'recovery_required'):
                return progress
            if progress['attempts'] >= self.limits.preparation_attempts:
                return progress
            db.execute('UPDATE delegation_materializations SET attempts=attempts+1 WHERE delegation_id=?', (identifier,))
        try:
            # The existing start path provisions/reuses and validates the Task
            # Workspace before the manager's final admission callback.
            self.launch(record['child_task_id'], identifier, self.limits)
        except Exception as exc:
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                progress = inspect(db, identifier)
                phase = 'recovery_required' if progress['phase'] == 'launching' else progress['phase']
                db.execute(f'UPDATE delegation_materializations SET phase=?,last_error=?,updated_at={NOW} WHERE delegation_id=?',
                           (phase, type(exc).__name__ + ': ' + str(exc)[:500], identifier))
                if isinstance(exc, (ValueError, LookupError)) and phase != 'recovery_required':
                    # A lifecycle/capacity gate is not a failed external attempt.
                    # Scheduling remains bounded; a later explicit pass or sibling
                    # completion may reconsider changed coordination evidence.
                    db.execute('UPDATE delegation_materializations SET attempts=MAX(0,attempts-1) WHERE delegation_id=?', (identifier,))
            self.publish(None)
            return self.status(identifier)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(f"UPDATE delegation_materializations SET phase='started',last_error=NULL,updated_at={NOW} WHERE delegation_id=? AND phase='launching'", (identifier,))
        self.publish(None)
        return self.status(identifier)

    def status(self, identifier):
        with self.store._connection() as db:
            return inspect(db, identifier)
