"""Diagnostics history and human recovery attestations (Stage 2D.1b P3).

A human attestation is an explicit acceptance of responsibility, never
technical proof: the execution moves to `attested_ended` with an `attestation`
of kind `user_responsibility` that links to an `execution_attestations` record.
Kernel and reboot outcomes (`ended_verified`, `ended_kernel_guaranteed`,
`ended_by_reboot`) stay distinct. A database trigger refuses `attested_ended`
without such a record; production accepts no other kind. (Test databases may
widen it for their own test fixture; production code never does.)

Every attempt is audited, refused or settled: who acted (as far as the local
API can tell), what was reviewed, the fresh observations behind the decision,
and the result. Observations (diagnostics) are stored separately from
conclusions (eligibility and outcome). SQL helpers run inside the caller's
transaction and never touch a process.
"""
import json
from uuid import uuid4

from . import execution_diagnostics, executions

NOW = executions.NOW
STATEMENT = ('I have independently verified that every process of this execution has ended. I understand that '
             'the system could not prove this, and I accept responsibility for releasing its Task, assignment '
             'and resources.')
REVIEW_WINDOW_SECONDS = 15 * 60
HISTORY_LIMIT = 20
TRIGGERS = (('executions_attested_insert', 'INSERT'), ('executions_attested_update', 'UPDATE OF state, attestation'))


def migrate(db):
    """v28 -> v29. Additive: two tables, one index, two triggers. Idempotent."""
    db.execute(f'''CREATE TABLE IF NOT EXISTS execution_diagnostics (
        diagnostic_id TEXT PRIMARY KEY,
        execution_id TEXT NOT NULL REFERENCES executions(execution_id),
        trigger TEXT NOT NULL,
        controller_instance_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('complete','incomplete','failed')),
        classification TEXT NOT NULL CHECK(classification IN
            ('verified_owned','ownership_uncertain','no_relevant_candidate','unavailable')),
        observations TEXT NOT NULL,
        conclusions TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT ({NOW}))''')
    # An index entry carries the rowid, so (execution_id) also serves ORDER BY rowid.
    db.execute('CREATE INDEX IF NOT EXISTS execution_diagnostics_latest ON execution_diagnostics(execution_id)')
    db.execute(f'''CREATE TABLE IF NOT EXISTS execution_attestations (
        attestation_id TEXT PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE CHECK(length(request_key) BETWEEN 1 AND 128),
        execution_id TEXT NOT NULL REFERENCES executions(execution_id),
        agent_id TEXT NOT NULL,
        task_id TEXT,
        controller_instance_id TEXT NOT NULL,
        prior_state TEXT NOT NULL,
        prior_uncertainty TEXT,
        prior_updated_at TEXT NOT NULL,
        reviewed_diagnostic_id TEXT REFERENCES execution_diagnostics(diagnostic_id),
        fresh_diagnostic_id TEXT REFERENCES execution_diagnostics(diagnostic_id),
        actor TEXT NOT NULL,
        statement TEXT NOT NULL,
        reason TEXT CHECK(reason IS NULL OR length(reason) <= 2000),
        outcome TEXT NOT NULL CHECK(outcome IN ('settled','refused')),
        refusal TEXT,
        created_at TEXT NOT NULL DEFAULT ({NOW}),
        CHECK((outcome = 'refused') = (refusal IS NOT NULL)))''')
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS execution_attestation_settled ON execution_attestations(execution_id)
        WHERE outcome='settled'""")
    for name, event in TRIGGERS:
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS {name} BEFORE {event} ON executions
            WHEN NEW.state = 'attested_ended' AND NOT (
                json_extract(NEW.attestation, '$.kind') = 'user_responsibility' AND EXISTS (
                    SELECT 1 FROM execution_attestations a WHERE a.attestation_id = json_extract(NEW.attestation, '$.attestation_id')
                    AND a.execution_id = NEW.execution_id AND a.outcome = 'settled'))
            BEGIN SELECT RAISE(ABORT, 'attested_ended requires a settled human attestation record'); END""")


def record_diagnostic(db, diagnostic, conclusions, controller_instance_id):
    diagnostic_id = str(uuid4())
    db.execute('''INSERT INTO execution_diagnostics(diagnostic_id,execution_id,trigger,controller_instance_id,status,
        classification,observations,conclusions,observed_at) VALUES (?,?,?,?,?,?,?,?,?)''',
        (diagnostic_id, diagnostic['execution_id'], diagnostic['trigger'], controller_instance_id, diagnostic['status'],
         diagnostic['classification'],
         json.dumps({'sources': diagnostic['sources'], 'candidates': diagnostic['candidates']}, sort_keys=True),
         json.dumps(conclusions, sort_keys=True), diagnostic['observed_at']))
    # Bounded history per execution; attestation-linked records are always kept.
    db.execute('''DELETE FROM execution_diagnostics WHERE execution_id=? AND diagnostic_id NOT IN (
        SELECT diagnostic_id FROM execution_diagnostics WHERE execution_id=? ORDER BY rowid DESC LIMIT ?)
        AND diagnostic_id NOT IN (SELECT reviewed_diagnostic_id FROM execution_attestations WHERE reviewed_diagnostic_id IS NOT NULL)
        AND diagnostic_id NOT IN (SELECT fresh_diagnostic_id FROM execution_attestations WHERE fresh_diagnostic_id IS NOT NULL)''',
        (diagnostic['execution_id'], diagnostic['execution_id'], HISTORY_LIMIT))
    return diagnostic_id


def _diagnostic(row):
    if row is None:
        return None
    value = dict(row)
    observations = json.loads(value.pop('observations'))
    value.update(sources=observations['sources'], candidates=observations['candidates'],
                 conclusions=json.loads(value['conclusions']))
    return value


def latest_diagnostic(db, execution_id):
    return _diagnostic(db.execute('SELECT * FROM execution_diagnostics WHERE execution_id=? ORDER BY rowid DESC LIMIT 1',
                                  (execution_id,)).fetchone())


def diagnostic(db, diagnostic_id):
    return _diagnostic(db.execute('SELECT * FROM execution_diagnostics WHERE diagnostic_id=?', (diagnostic_id,)).fetchone())


def _attestation(row):
    if row is None:
        return None
    value = dict(row)
    value['actor'] = json.loads(value['actor'])
    value['refusal'] = json.loads(value['refusal']) if value['refusal'] else None
    return value


def by_request_key(db, request_key):
    return _attestation(db.execute('SELECT * FROM execution_attestations WHERE request_key=?', (request_key,)).fetchone())


def history(db, execution_id):
    return [_attestation(r) for r in db.execute(
        'SELECT * FROM execution_attestations WHERE execution_id=? ORDER BY rowid', (execution_id,))]


def record(db, *, outcome, request_key, row, controller_instance_id, reviewed_diagnostic_id, fresh_diagnostic_id,
           actor, reason, refusal=None):
    attestation_id = str(uuid4())
    db.execute('''INSERT INTO execution_attestations(attestation_id,request_key,execution_id,agent_id,task_id,
        controller_instance_id,prior_state,prior_uncertainty,prior_updated_at,reviewed_diagnostic_id,
        fresh_diagnostic_id,actor,statement,reason,outcome,refusal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (attestation_id, request_key, row['execution_id'], row['agent_id'], row['task_id'], controller_instance_id,
         row['state'], row['uncertainty'], row['updated_at'], reviewed_diagnostic_id, fresh_diagnostic_id,
         json.dumps(actor, sort_keys=True), STATEMENT, reason, outcome,
         json.dumps(refusal, sort_keys=True) if refusal is not None else None))
    return attestation_id


class Changed(ValueError):
    """The execution, its ownership or its evidence changed after the fresh check: nothing was committed."""

    def __init__(self, message, code='execution_changed'):
        super().__init__(message)
        self.code = code


def settle(db, *, request_key, row, controller_instance_id, reviewed_diagnostic_id, fresh_diagnostic_id, actor, reason):
    """Inside one BEGIN IMMEDIATE transaction: revalidate, audit, settle and release only what it covered."""
    from . import dependencies, work_control
    current = executions.get(db, row['execution_id'])
    if current is None or current['state'] != 'uncertain' or current['updated_at'] != row['updated_at']:
        raise Changed('The execution changed while it was being checked; review it again')
    if db.execute('''SELECT 1 FROM executions WHERE state='launching' AND (agent_id=? OR (? IS NOT NULL AND task_id=?))''',
                  (row['agent_id'], row['task_id'], row['task_id'])).fetchone():
        raise Changed('A launch of this Task or Agent is in progress')
    # The evidence settled on must still be the newest: a later diagnostic (owned or
    # uncertain process, live Job, failed or incomplete inspection) always wins.
    fresh, latest = diagnostic(db, fresh_diagnostic_id), latest_diagnostic(db, row['execution_id'])
    if fresh is None or latest is None or latest['diagnostic_id'] != fresh_diagnostic_id \
            or fresh['execution_id'] != row['execution_id']:
        raise Changed('Newer diagnostics exist for this execution; review them again', 'diagnostics_superseded')
    if fresh['trigger'] != 'attestation_recheck' or fresh['controller_instance_id'] != controller_instance_id:
        raise Changed('The fresh check was not taken by this backend for this attestation', 'diagnostics_superseded')
    # Never a stored conclusion alone: eligibility is recomputed from the stored observations.
    if not fresh['conclusions'].get('eligible') or not execution_diagnostics.eligibility(current, fresh)['eligible']:
        raise Changed('The fresh diagnostics do not allow attestation', 'diagnostics_not_eligible')
    attestation_id = record(db, outcome='settled', request_key=request_key, row=row,
                            controller_instance_id=controller_instance_id,
                            reviewed_diagnostic_id=reviewed_diagnostic_id, fresh_diagnostic_id=fresh_diagnostic_id,
                            actor=actor, reason=reason)
    attestation = {'kind': 'user_responsibility', 'attestation_id': attestation_id, 'technically_verified': False}
    if not executions.transition(db, row['execution_id'], 'attested_ended', ('uncertain',), attestation=attestation,
                                 evidence={'attestation': attestation}):
        raise Changed('The execution changed while it was being settled')
    # Release only what no other unresolved execution still holds.
    agent = db.execute('SELECT status FROM agents WHERE agent_id=?', (row['agent_id'],)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                            (row['agent_id'],)).fetchone()
    if agent is not None and assignment is not None and agent['status'] != 'running' \
            and not executions.blocking(db, agent_id=row['agent_id']) \
            and not executions.blocking(db, task_id=assignment['task_id']):
        work_control.sync(db, assignment, agent['status'])
    dependencies.reconcile(db)
    return attestation_id
