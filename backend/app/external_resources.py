"""External observations/provenance within the existing resource transaction."""
import re
from uuid import uuid4

from . import resource_probes
from .resource_coordination import DEADLOCK, NOW

BLOCKER = "(reason_code='external_resource_unavailable' AND source_type='resource_probe' AND waiting_claim_id IS NOT NULL)"


def migrate(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(resource_claims)')}
    if 'probe_status' not in columns:
        db.execute("ALTER TABLE resource_claims ADD COLUMN probe_status TEXT CHECK(probe_status IN ('available','unavailable','unknown','not_supported'))")
        db.execute('ALTER TABLE resource_claims ADD COLUMN probe_checked_at TEXT')
        db.execute('ALTER TABLE resource_claims ADD COLUMN probe_reason TEXT')
    db.execute('DROP INDEX IF EXISTS task_blockers_active')
    managed = "(blocker_type='resource' AND reason_code='resource_conflict' AND waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL)"
    db.execute(f"CREATE UNIQUE INDEX task_blockers_active ON task_blockers(task_id,blocker_type,COALESCE(source_task_id,''),reason_code) WHERE active=1 AND NOT {managed} AND NOT {DEADLOCK} AND NOT {BLOCKER}")
    db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS external_resource_blocker ON task_blockers(waiting_claim_id) WHERE active=1 AND {BLOCKER}')
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
    if "'external_resource_unavailable'" not in sql:
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='task_assignments' AND type='index' AND sql IS NOT NULL")]
        sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v15', sql, count=1)
        db.execute(sql.replace("'resource_deadlock'", "'resource_deadlock','external_resource_unavailable'"))
        columns = ','.join('"' + r['name'] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
        db.execute(f'INSERT INTO assignments_v15(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
        db.execute('DROP TABLE task_assignments')
        db.execute('ALTER TABLE assignments_v15 RENAME TO task_assignments')
        for index in indexes:
            db.execute(index)


def check(db, claim):
    try:
        observation = resource_probes.probe_resource(claim['resource_type'], claim['resource_key'])
    except Exception:
        observation = resource_probes.Observation('unknown', 'probe_failed')
    return record(db, claim, observation)


def record(db, claim, observation):
    db.execute(f'UPDATE resource_claims SET probe_status=?,probe_reason=?,probe_checked_at={NOW} WHERE claim_id=?',
               (observation.status, observation.reason, claim['claim_id']))
    if observation.status in ('available', 'not_supported'):
        clear(db, claim)
        return True
    exists = db.execute(f'SELECT 1 FROM task_blockers WHERE {BLOCKER} AND active=1 AND waiting_claim_id=?', (claim['claim_id'],)).fetchone()
    if not exists:
        db.execute("""INSERT INTO task_blockers(id,task_id,blocker_type,reason_code,source_type,source_id,waiting_claim_id)
            VALUES (?,?,'resource','external_resource_unavailable','resource_probe',?,?)""",
                   (str(uuid4()), claim['task_id'], claim['claim_id'], claim['claim_id']))
    return False


def clear(db, claim):
    db.execute(f'UPDATE task_blockers SET active=0,resolved_at={NOW} WHERE {BLOCKER} AND active=1 AND waiting_claim_id=?', (claim['claim_id'],))
