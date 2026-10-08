"""Durable execution evidence; never infer Worker ownership from an occupied port.

Managed claims remain the authority for fairness/deadlock edges. This service
cannot grant a claim. Socket leases exist only inside a serialized launch attempt.
"""
from contextlib import contextmanager, ExitStack
from threading import Lock
from uuid import uuid4

from . import resource_probes, external_resources

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
LIVE = "ended_at IS NULL"
_reservation_lock = Lock()
_live_reservations = set()


class ResourceUnavailable(ValueError):
    """The external blocker is already committed when this reaches the caller."""


def _forget_reservation(identifier):
    with _reservation_lock:
        _live_reservations.discard(identifier)


def migrate(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS resource_ownership (
        ownership_id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES resource_claims(claim_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        assignment_id TEXT REFERENCES task_assignments(assignment_id), agent_id TEXT,
        generation INTEGER NOT NULL CHECK(generation>0),
        resource_type TEXT NOT NULL,resource_key TEXT NOT NULL,scope TEXT NOT NULL,lifetime TEXT NOT NULL,
        adapter TEXT NOT NULL,capability TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved_by_controller','handoff_pending','runtime_unverified',
            'coordination_only','suspended','released','ownership_lost')),
        created_at TEXT NOT NULL DEFAULT ({NOW}),updated_at TEXT NOT NULL DEFAULT ({NOW}),
        reserved_at TEXT,handoff_at TEXT,ended_at TEXT,reason TEXT,
        verification_status TEXT,verified_at TEXT,
        UNIQUE(claim_id,generation))''')
    db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS resource_ownership_live ON resource_ownership(claim_id) WHERE {LIVE}')
    db.execute('CREATE INDEX IF NOT EXISTS resource_ownership_assignment ON resource_ownership(assignment_id)')


def history(db, claim_id):
    rows = [dict(r) for r in db.execute('SELECT * FROM resource_ownership WHERE claim_id=? ORDER BY generation', (claim_id,))]
    for row in rows:
        with _reservation_lock:
            held = row['ownership_id'] in _live_reservations
        row['controller_reservation_live'] = held and row['state'] == 'reserved_by_controller'
        row['recorded_state'] = row['state']
        row['recovery_required'] = row['state'] == 'reserved_by_controller' and not held
        if row['recovery_required']:
            # An uncertain DB commit can leave evidence after the socket closed.
            # Inspection is read-only, but must not advertise a physical guarantee.
            row['state'] = 'ownership_lost'
    return rows


def invalidated(db, claim_id):
    row = db.execute('SELECT state FROM resource_ownership WHERE claim_id=? ORDER BY generation DESC LIMIT 1', (claim_id,)).fetchone()
    return bool(row and row[0] in ('ownership_lost', 'suspended', 'released'))


def finish(db, identifier, state, reason):
    db.execute(f'UPDATE resource_ownership SET state=?,reason=?,ended_at={NOW},updated_at={NOW} WHERE ownership_id=? AND {LIVE}',
               (state, reason, identifier))


def insert(db, claim, agent_id, assignment_id, state, reason):
    identifier = str(uuid4())
    generation = db.execute('SELECT COALESCE(MAX(generation),0)+1 FROM resource_ownership WHERE claim_id=?', (claim['claim_id'],)).fetchone()[0]
    port = claim['resource_type'] == 'port'
    db.execute('''INSERT INTO resource_ownership(ownership_id,claim_id,task_id,assignment_id,agent_id,generation,
        resource_type,resource_key,scope,lifetime,adapter,capability,state,reason,verification_status,verified_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (identifier, claim['claim_id'], claim['task_id'], assignment_id, agent_id, generation,
         claim['resource_type'], claim['resource_key'], claim['scope'], claim['lifetime'],
         'socket_bind' if port else 'managed_only', 'controller_reservable' if port else 'unsupported',
         state, reason, claim['probe_status'], claim['probe_checked_at']))
    if state == 'reserved_by_controller':
        db.execute(f'UPDATE resource_ownership SET reserved_at={NOW},verified_at={NOW},verification_status=? WHERE ownership_id=?', ('available', identifier))
    return identifier


def sync(db):
    """Called inside lifecycle commits, after claim coordination, never from GET.

    No physical ownership is manufactured for active declarations. Running
    assignments gain explicitly unverified evidence; unresolved stops retain it.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='resource_ownership'").fetchone():
        return  # Earlier schema migrations can reconcile before v16 is installed.
    for row in db.execute(f'SELECT * FROM resource_ownership WHERE {LIVE}').fetchall():
        claim = db.execute('SELECT * FROM resource_claims WHERE claim_id=?', (row['claim_id'],)).fetchone()
        assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (row['assignment_id'],)).fetchone()
        if assignment and assignment['ended_at'] is not None:
            finish(db, row['ownership_id'], 'released', assignment['ended_reason'])
        elif claim['status'] in ('released', 'suspended'):
            finish(db, row['ownership_id'], claim['status'], 'claim_' + claim['status'])
    for claim in db.execute("SELECT * FROM resource_claims WHERE status='active' AND mode<>'advisory'").fetchall():
        assignment = db.execute('''SELECT a.* FROM task_assignments a JOIN agents g USING(agent_id)
            WHERE a.task_id=? AND a.ended_at IS NULL AND g.status='running' ''', (claim['task_id'],)).fetchone()
        if not assignment:
            continue
        current = db.execute(f'SELECT * FROM resource_ownership WHERE claim_id=? AND {LIVE}', (claim['claim_id'],)).fetchone()
        if current and current['agent_id'] == assignment['agent_id'] and current['state'] == 'handoff_pending':
            db.execute(f"UPDATE resource_ownership SET assignment_id=?,state=?,reason='worker_ownership_not_verifiable',updated_at={NOW} WHERE ownership_id=? AND state='handoff_pending'",
                       (assignment['assignment_id'], 'runtime_unverified' if claim['resource_type'] == 'port' else 'coordination_only', current['ownership_id']))
        elif not current:
            insert(db, claim, assignment['agent_id'], assignment['assignment_id'],
                   'runtime_unverified' if claim['resource_type'] == 'port' else 'coordination_only',
                   'worker_ownership_not_verifiable')


def recover(db):
    for row in db.execute(f'SELECT ownership_id FROM resource_ownership WHERE {LIVE}').fetchall():
        finish(db, row[0], 'ownership_lost', 'backend_restart_no_reattachment')
    # Upgraded waiting assignments can predate all runtime evidence. Record the
    # recovery observation (unknown, already ended), never invented ownership.
    for claim in db.execute("""SELECT c.*,a.agent_id,a.assignment_id AS current_assignment
        FROM resource_claims c JOIN task_assignments a USING(task_id)
        WHERE a.ended_at IS NULL AND c.status='active' AND c.mode<>'advisory'
        AND NOT EXISTS (SELECT 1 FROM resource_ownership r WHERE r.claim_id=c.claim_id)""").fetchall():
        identifier = insert(db, claim, claim['agent_id'], claim['current_assignment'],
                            'ownership_lost', 'recovery_no_runtime_evidence')
        db.execute(f"UPDATE resource_ownership SET ended_at={NOW},verified_at={NOW},verification_status='unknown' WHERE ownership_id=?", (identifier,))


@contextmanager
def launch(store, task_id, agent_id):
    """Prepare a complete bundle, commit reservation evidence, release before spawn.

    Only this attempt's UUIDs can be compensated. Existing same-assignment use
    remains unverified and is never tested by binding against the Worker's port.
    """
    if store is None or task_id is None:
        yield
        return
    from . import dependencies, work_control
    identifiers, failed = [], False
    with ExitStack() as sockets:
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            work_control.require_active(dependencies.task(db, task_id), db=db)
            claims = db.execute("SELECT * FROM resource_claims WHERE task_id=? AND mode<>'advisory' AND status<>'released' ORDER BY rowid", (task_id,)).fetchall()
            bound = set()
            for claim in claims:
                if claim['status'] != 'active':
                    raise ValueError('Resource bundle is not granted')
                current = db.execute(f'SELECT * FROM resource_ownership WHERE claim_id=? AND {LIVE}', (claim['claim_id'],)).fetchone()
                if current:
                    if current['agent_id'] == agent_id and current['state'] in ('runtime_unverified', 'coordination_only'):
                        continue
                    raise ValueError('Resource launch attempt already in progress')
                # A same-assignment legacy claim may already be in use. Do not
                # bind unless recovery explicitly invalidated its evidence.
                assignment = db.execute('SELECT * FROM task_assignments WHERE task_id=? AND agent_id=? AND ended_at IS NULL', (task_id, agent_id)).fetchone()
                continuing = assignment is not None and not invalidated(db, claim['claim_id'])
                port = claim['resource_type'] == 'port'
                if port and not continuing and claim['resource_key'] not in bound:
                    try:
                        resource_probes.reserve_port(sockets, claim['resource_key'])
                        bound.add(claim['resource_key'])
                    except Exception as exc:
                        external_resources.record(db, claim, resource_probes.failure(exc))
                        failed = True
                        break
                state = 'reserved_by_controller' if port and not continuing else 'handoff_pending'
                identifier = insert(db, claim, agent_id, assignment['assignment_id'] if assignment else None,
                                    state, 'launch_preparation')
                identifiers.append(identifier)
                if state == 'reserved_by_controller':
                    with _reservation_lock:
                        _live_reservations.add(identifier)
                    sockets.callback(_forget_reservation, identifier)
            if failed:
                sockets.close()
                for identifier in identifiers:
                    finish(db, identifier, 'released', 'bundle_acquisition_failed')
                dependencies.reconcile(db)
        if failed:
            raise ResourceUnavailable('Required external resource is unavailable or unknown')
        try:
            # Persist the loss of the controller guarantee BEFORE closing sockets.
            # A crash at either point is conservatively invalidated on recovery.
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                work_control.require_active(dependencies.task(db, task_id), db=db)
                current_bundle = {r[0] for r in db.execute("SELECT claim_id FROM resource_claims WHERE task_id=? AND mode<>'advisory' AND status='active'", (task_id,))}
                if current_bundle != {c['claim_id'] for c in claims}:
                    raise ValueError('Resource bundle changed during launch preparation')
                for identifier in identifiers:
                    changed = db.execute(f"UPDATE resource_ownership SET state='handoff_pending',handoff_at={NOW},updated_at={NOW},reason='non_atomic_worker_handoff' WHERE ownership_id=? AND {LIVE}", (identifier,))
                    if not changed.rowcount:
                        raise ValueError('Resource ownership generation is no longer current')
            sockets.close()
            yield
        except BaseException:
            sockets.close()
            with store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                for identifier in identifiers:
                    finish(db, identifier, 'released', 'launch_failed')
            raise
