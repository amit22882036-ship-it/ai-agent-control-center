"""Durable acquisition episodes and explainable Task-level deadlock incidents.

All mutations run inside the lifecycle caller's transaction. No runtime/OS work.
"""
import json
import re
from uuid import uuid4

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
DEADLOCK = "(reason_code='resource_deadlock' AND deadlock_id IS NOT NULL)"


def migrate(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS resource_waits (
        wait_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        claim_id TEXT NOT NULL REFERENCES resource_claims(claim_id),
        waiting_since TEXT,closed_at TEXT,outcome TEXT,
        CHECK ((closed_at IS NULL AND outcome IS NULL) OR
               (closed_at IS NOT NULL AND outcome IN ('active','suspended','released'))))''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS resource_wait_open ON resource_waits(claim_id) WHERE closed_at IS NULL')
    # v13 never recorded wait-entry time. Persist a conservative creation-order
    # backfill, explicitly leaving the unknown timestamp NULL.
    for row in db.execute("SELECT claim_id FROM resource_claims WHERE status='waiting' AND mode<>'advisory' ORDER BY rowid").fetchall():
        db.execute('INSERT INTO resource_waits(claim_id) SELECT ? WHERE NOT EXISTS (SELECT 1 FROM resource_waits WHERE claim_id=?)', (row[0], row[0]))
    db.execute(f'''CREATE TABLE IF NOT EXISTS resource_deadlocks (
        deadlock_id TEXT PRIMARY KEY,signature TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('open','resolved')),
        detected_at TEXT NOT NULL DEFAULT ({NOW}),resolved_at TEXT,
        triggering_claim_id TEXT REFERENCES resource_claims(claim_id),edges TEXT NOT NULL)''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS resource_deadlock_open ON resource_deadlocks(signature) WHERE status='open'")
    db.execute('''CREATE TABLE IF NOT EXISTS resource_deadlock_members (
        deadlock_id TEXT NOT NULL REFERENCES resource_deadlocks(deadlock_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),PRIMARY KEY(deadlock_id,task_id))''')
    for table in ('task_blockers', 'task_replan_reasons'):
        if 'deadlock_id' not in {r['name'] for r in db.execute(f'PRAGMA table_info({table})')}:
            db.execute(f'ALTER TABLE {table} ADD COLUMN deadlock_id TEXT REFERENCES resource_deadlocks(deadlock_id)')
        db.execute(f'DROP INDEX IF EXISTS {table}_active')
        kind = 'blocker_type' if table == 'task_blockers' else 'reason_type'
        resource = " AND NOT (blocker_type='resource' AND reason_code='resource_conflict' AND waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL)" if table == 'task_blockers' else ''
        db.execute(f"CREATE UNIQUE INDEX {table}_active ON {table}(task_id,{kind},COALESCE(source_task_id,''),reason_code) WHERE active=1 AND NOT {DEADLOCK}{resource}")
        db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {table}_deadlock ON {table}(task_id,deadlock_id) WHERE active=1 AND {DEADLOCK}')
    if 'edge_kind' not in {r['name'] for r in db.execute('PRAGMA table_info(task_blockers)')}:
        db.execute('ALTER TABLE task_blockers ADD COLUMN edge_kind TEXT')
        db.execute("UPDATE task_blockers SET edge_kind='active_owner' WHERE waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL")
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
    if "'resource_deadlock'" not in sql:
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='task_assignments' AND type='index' AND sql IS NOT NULL")]
        sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v14', sql, count=1)
        db.execute(sql.replace("'resource_conflict'", "'resource_conflict','resource_deadlock'"))
        columns = ','.join('"' + r['name'] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
        db.execute(f'INSERT INTO assignments_v14(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
        db.execute('DROP TABLE task_assignments')
        db.execute('ALTER TABLE assignments_v14 RENAME TO task_assignments')
        for index in indexes:
            db.execute(index)


def enqueue(db, claim):
    if claim.get('wait_sequence') is not None:
        return
    row = db.execute(f'INSERT INTO resource_waits(claim_id,waiting_since) VALUES (?,{NOW}) RETURNING wait_sequence,waiting_since', (claim['claim_id'],)).fetchone()
    claim.update(dict(row))


def close_wait(db, claim, outcome):
    db.execute(f'UPDATE resource_waits SET closed_at={NOW},outcome=? WHERE claim_id=? AND closed_at IS NULL', (outcome, claim['claim_id']))
    claim['wait_sequence'] = claim['waiting_since'] = None


def edges(rows, conflicts):
    """Only participating waits create edges; suspended declarations do not."""
    result = []
    for c in rows:
        if c['status'] != 'waiting' or c['mode'] == 'advisory':
            continue
        for other in rows:
            if not conflicts(c, other):
                continue
            kind = None
            if other['status'] == 'active':
                kind = 'active_owner'
            elif (other['status'] == 'waiting' and other['wait_sequence'] is not None
                  and c['wait_sequence'] is not None and other['wait_sequence'] < c['wait_sequence']):
                kind = 'queue_precedence'
            if kind:
                result.append(dict(waiting_task_id=c['task_id'], blocking_task_id=other['task_id'],
                                   waiting_claim_id=c['claim_id'], blocking_claim_id=other['claim_id'],
                                   resource_type=c['resource_type'], resource_key=c['resource_key'],
                                   scope=c['scope'], scope_key=c['scope_key'],
                                   blocking_resource_key=other['resource_key'], edge_kind=kind))
    return result


def cycles(edges):
    """Enumerate simple directed edge cycles, starting only at their least Task.

    Iterative DFS avoids Python recursion limits. Parallel provenance edges are
    distinct cycles; rotations are identical. No SCC-only merging of incidents.
    """
    adjacency = {}
    for edge in edges:
        adjacency.setdefault(edge['waiting_task_id'], []).append(edge)
    for start in sorted(adjacency):
        stack = [(start, frozenset((start,)), [])]
        while stack:
            node, visited, path = stack.pop()
            for edge in adjacency.get(node, ()):
                target = edge['blocking_task_id']
                if target == start and len(visited) > 1:
                    cycle = path + [edge]
                    signature = json.dumps([(e['waiting_claim_id'], e['blocking_claim_id'], e['edge_kind']) for e in cycle], separators=(',', ':'))
                    yield signature, cycle
                elif target > start and target not in visited:
                    stack.append((target, visited | {target}, path + [edge]))


def reconcile(db, graph, triggering_claim_id=None):
    desired = dict(cycles(graph))
    existing = {r['signature']: dict(r) for r in db.execute("SELECT * FROM resource_deadlocks WHERE status='open'")}
    for signature, incident in existing.items():
        if signature not in desired:
            identifier = incident['deadlock_id']
            db.execute(f"UPDATE resource_deadlocks SET status='resolved',resolved_at={NOW} WHERE deadlock_id=?", (identifier,))
            for table in ('task_blockers', 'task_replan_reasons'):
                db.execute(f'UPDATE {table} SET active=0,resolved_at={NOW} WHERE deadlock_id=? AND active=1', (identifier,))
    for signature, cycle in desired.items():
        if signature in existing:
            continue
        identifier = str(uuid4())
        db.execute("INSERT INTO resource_deadlocks(deadlock_id,signature,status,triggering_claim_id,edges) VALUES (?,?,'open',?,?)", (identifier, signature, triggering_claim_id, json.dumps(cycle)))
        for task_id in sorted({e['waiting_task_id'] for e in cycle}):
            db.execute('INSERT INTO resource_deadlock_members VALUES (?,?)', (identifier, task_id))
            for table, kind in (('task_blockers', 'blocker_type'), ('task_replan_reasons', 'reason_type')):
                db.execute(f"INSERT INTO {table}(id,task_id,{kind},reason_code,deadlock_id) VALUES (?,?,'resource_deadlock','resource_deadlock',?)", (str(uuid4()), task_id, identifier))


def inspect(db, *, task_id=None, deadlock_id=None):
    query = 'SELECT d.* FROM resource_deadlocks d'
    args = ()
    if task_id is not None:
        query += ' JOIN resource_deadlock_members m USING(deadlock_id) WHERE m.task_id=?'
        args = (task_id,)
    elif deadlock_id is not None:
        query += ' WHERE d.deadlock_id=?'
        args = (deadlock_id,)
    result = []
    for row in db.execute(query + ' ORDER BY d.rowid', args):
        item = dict(row)
        item['edges'] = json.loads(item['edges'])
        item['task_ids'] = sorted({e['waiting_task_id'] for e in item['edges']})
        item['claim_ids'] = sorted({e[k] for e in item['edges'] for k in ('waiting_claim_id', 'blocking_claim_id')})
        result.append(item)
    return result
