"""Immutable acceptance history, separate from the single current Parent cursor.

All mutations run inside the caller's immediate transaction. Rounds confer no
Worker ownership: current assignments, generations and control gates still do.
"""
import json
from dataclasses import dataclass
from uuid import uuid4


@dataclass(frozen=True)
class Policy:
    burst: int = 4
    window_seconds: int = 60

    def __post_init__(self):
        if self.burst < 1 or self.window_seconds < 1:
            raise ValueError('Round admission limits must be positive')


DEFAULT_POLICY = Policy()


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS delegation_rounds (
        round_id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),
        assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),generation TEXT NOT NULL,
        message_id TEXT NOT NULL,receipt TEXT NOT NULL,
        accepted_at REAL,not_before REAL NOT NULL,
        UNIQUE(task_id,generation,message_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS delegation_round_requests (
        round_id TEXT NOT NULL REFERENCES delegation_rounds(round_id),
        delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
        PRIMARY KEY(round_id,delegation_id))''')
    # Preserve the exact legacy receipt and the v21 cursor's batch identity.
    for task in db.execute('SELECT task_id,orchestration_handoff FROM tasks WHERE orchestration_handoff IS NOT NULL').fetchall():
        if db.execute('SELECT 1 FROM delegation_rounds WHERE task_id=?', (task['task_id'],)).fetchone():
            continue
        receipt = json.loads(task['orchestration_handoff'])
        cursor = db.execute('SELECT batch_id FROM delegation_continuations WHERE task_id=?', (task['task_id'],)).fetchone()
        record(db, task['task_id'], receipt, round_id=cursor[0] if cursor else str(uuid4()), legacy=True)


def now(db):
    return db.execute("SELECT (julianday('now') - 2440587.5)*86400.0").fetchone()[0]


def history(db, task_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_rounds'").fetchone():
        return []
    return [{**dict(row), 'receipt': json.loads(row['receipt'])} for row in db.execute(
        'SELECT * FROM delegation_rounds WHERE task_id=? ORDER BY rowid', (task_id,))]


def record(db, task_id, receipt, *, round_id=None, legacy=False, policy=None):
    policy = policy or DEFAULT_POLICY
    timestamp = now(db)
    # Reserve durable slots. Four fresh rounds per rolling minute by default;
    # excess valid work is retained, with no timer or model retry generated here.
    prior = db.execute('SELECT not_before FROM delegation_rounds WHERE task_id=? ORDER BY rowid DESC LIMIT 1 OFFSET ?',
                       (task_id, policy.burst - 1)).fetchone()
    eligible = 0 if legacy else timestamp if not prior else max(timestamp, prior[0] + policy.window_seconds)
    identifier = round_id or str(uuid4())
    db.execute('''INSERT INTO delegation_rounds(round_id,task_id,assignment_id,agent_id,
        generation,message_id,receipt,accepted_at,not_before) VALUES (?,?,?,?,?,?,?,?,?)''',
        (identifier, task_id, receipt['assignment_id'], receipt['agent_id'], receipt['generation'],
         receipt['message_id'], json.dumps(receipt), None if legacy else timestamp, eligible))
    db.executemany('INSERT INTO delegation_round_requests VALUES (?,?)',
                   [(identifier, key) for key in receipt['delegation_ids']])
    return identifier


def require_admitted(db, round_id):
    row = db.execute('SELECT not_before FROM delegation_rounds WHERE round_id=?', (round_id,)).fetchone()
    if row is None:
        raise ValueError('Delegation round acceptance evidence is missing')
    if row[0] > now(db):
        raise ValueError('Delegation round rate capacity reached; durable work retained pending admission')


def receipt_for(db, task_id, delegation_id):
    row = db.execute('''SELECT r.* FROM delegation_rounds r JOIN delegation_round_requests q USING(round_id)
        WHERE r.task_id=? AND q.delegation_id=? ORDER BY r.rowid LIMIT 1''', (task_id, delegation_id)).fetchone()
    if not row:
        raise ValueError('Delegation has no matching accepted Parent handoff')
    require_admitted(db, row['round_id'])
    return json.loads(row['receipt'])
