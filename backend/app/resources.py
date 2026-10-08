"""Managed resource coordination. Mutations require the caller's SQL transaction.

This is not an OS lock/probe. Existing owners are retained; missing Task bundles
acquire atomically in durable, compatibility-aware acquisition order.
"""
import os
from pathlib import PureWindowsPath
import re
from uuid import uuid4

from .project_domain import canonical_path, contains
from . import resource_coordination as coordination
from . import external_resources as external
from . import runtime_resources, resource_probes

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
TYPES = ('file_path', 'port', 'database', 'docker_resource', 'generic')
MODES = ('advisory', 'shared', 'exclusive')
MANAGED_BLOCKER = "(blocker_type='resource' AND reason_code='resource_conflict' AND waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL)"


def migrate(db):
    if 'stop_reason' not in {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}:
        db.execute('ALTER TABLE tasks ADD COLUMN stop_reason TEXT')
    db.execute(f'''CREATE TABLE IF NOT EXISTS resource_claims (
        claim_id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),
        assignment_id TEXT REFERENCES task_assignments(assignment_id),
        resource_type TEXT NOT NULL,resource_key TEXT NOT NULL,
        scope TEXT NOT NULL CHECK(scope IN ('project','global')),
        mode TEXT NOT NULL CHECK(mode IN ('advisory','shared','exclusive')),
        lifetime TEXT NOT NULL CHECK(lifetime IN ('task','worker')),
        recursive INTEGER NOT NULL DEFAULT 0 CHECK(recursive IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('active','waiting','suspended','released')),
        created_at TEXT NOT NULL DEFAULT ({NOW}),acquired_at TEXT,suspended_at TEXT,released_at TEXT,
        CHECK((lifetime='task' AND assignment_id IS NULL) OR (lifetime='worker' AND assignment_id IS NOT NULL)))''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS resource_claim_identity ON resource_claims
        (task_id,COALESCE(assignment_id,''),resource_type,resource_key,scope,mode,lifetime,recursive) WHERE status<>'released' ''')
    db.execute('CREATE INDEX IF NOT EXISTS resource_claim_lookup ON resource_claims(resource_type,scope,resource_key,status)')
    db.execute('CREATE INDEX IF NOT EXISTS resource_claim_assignment ON resource_claims(assignment_id,status)')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(task_blockers)')}
    if 'source_type' not in columns:
        db.execute('ALTER TABLE task_blockers ADD COLUMN source_type TEXT')
        db.execute('ALTER TABLE task_blockers ADD COLUMN source_id TEXT')
        db.execute('ALTER TABLE task_blockers ADD COLUMN waiting_claim_id TEXT REFERENCES resource_claims(claim_id)')
        db.execute('ALTER TABLE task_blockers ADD COLUMN owning_claim_id TEXT REFERENCES resource_claims(claim_id)')
    db.execute('DROP INDEX IF EXISTS task_blockers_active')
    db.execute(f"CREATE UNIQUE INDEX task_blockers_active ON task_blockers(task_id,blocker_type,COALESCE(source_task_id,''),reason_code) WHERE active=1 AND NOT {MANAGED_BLOCKER}")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS resource_blocker_active ON task_blockers(waiting_claim_id,owning_claim_id) WHERE active=1 AND blocker_type='resource'")
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
    if "'resource_conflict'" not in sql:
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='task_assignments' AND type='index' AND sql IS NOT NULL")]
        sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v13', sql, count=1)
        db.execute(sql.replace("'blocked'", "'blocked','resource_conflict'"))
        columns = ','.join('"' + r['name'] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
        db.execute(f'INSERT INTO assignments_v13(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
        db.execute('DROP TABLE task_assignments')
        db.execute('ALTER TABLE assignments_v13 RENAME TO task_assignments')
        for index in indexes:
            db.execute(index)


def normalize(db, task, resource_type, resource_key, mode=None, lifetime='task', scope=None, recursive=False):
    if resource_type not in TYPES or lifetime not in ('task', 'worker'):
        raise ValueError('Invalid resource type or claim lifetime')
    mode = mode or ('advisory' if resource_type == 'file_path' else 'exclusive')
    if mode not in MODES:
        raise ValueError('Invalid resource claim mode')
    scope = scope or {'file_path': 'project', 'port': 'global', 'docker_resource': 'global'}.get(resource_type)
    if scope not in ('project', 'global'):
        raise ValueError('An explicit project or global resource scope is required')
    if resource_type == 'file_path' and scope != 'project':
        raise ValueError('File paths require Project scope')
    if resource_type in ('port', 'docker_resource') and scope != 'global':
        raise ValueError('Ports and Docker resources require global scope')
    if scope == 'project' and not task['project_id']:
        raise ValueError('Resource requires resolved Project ownership')
    if not isinstance(recursive, bool) or (recursive and resource_type != 'file_path'):
        raise ValueError('Recursive claims are supported only for file paths')
    if not isinstance(resource_key, str) or not resource_key.strip() or len(resource_key) > 500 or any(ord(c) < 32 for c in resource_key):
        raise ValueError('Resource key must contain 1 to 500 printable characters')
    key = resource_key.strip()
    if resource_type == 'file_path':
        key = key.replace('\\', '/')
        parts = [p for p in key.split('/') if p not in ('', '.')]
        if (key.startswith('/') or PureWindowsPath(key).drive or '..' in parts
                or any(c in key for c in ':*?<>|"')
                or any(p.endswith((' ', '.')) or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', p) for p in parts)):
            raise ValueError('File resource must be a safe Project-relative logical path')
        project = db.execute('SELECT root_path,root_path_key FROM projects WHERE project_id=?', (task['project_id'],)).fetchone()
        root, root_key = canonical_path(project['root_path'])
        resolved, resolved_key = canonical_path(os.path.join(root, *parts))
        if not contains(root_key, resolved_key):
            raise ValueError('File resource escapes its Project')
        key = os.path.normcase(os.path.relpath(resolved, root)).replace('\\', '/')
    elif resource_type == 'port':
        match = re.fullmatch(r'(?:(tcp|udp):)?(\d{1,5})', key.lower())
        if not match or not 1 <= int(match[2]) <= 65535:
            raise ValueError('Port resource must be tcp:<1-65535> or udp:<1-65535>')
        key = f'{match[1] or "tcp"}:{int(match[2])}'
    else:
        if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]*:.+', key):
            raise ValueError('Resource key requires a namespace and a non-empty identity')
        namespace, identity = key.split(':', 1)
        namespace = namespace.lower()
        if resource_type == 'database':
            if any(c in key for c in ('@', '?', '#', '=', '://')) or re.search(r'(?i)\b(password|passwd|pwd|token|secret)\b', key):
                raise ValueError('Database identity must not contain credentials, DSNs or secrets')
            if namespace == 'sqlite':
                if not os.path.isabs(identity):
                    raise ValueError('SQLite resource identity requires an absolute database path')
                _, identity = canonical_path(identity)
        if resource_type == 'docker_resource' and namespace not in ('container', 'network', 'volume'):
            raise ValueError('Docker resource requires container, network or volume namespace')
        key = namespace + ':' + identity
    return dict(resource_type=resource_type, resource_key=key, mode=mode, lifetime=lifetime, scope=scope, recursive=int(recursive))


def claims(db):
    return [dict(r) for r in db.execute('''SELECT c.rowid AS claim_order,c.*,
        w.wait_sequence,w.waiting_since,
        CASE WHEN c.scope='global' THEN 'machine' ELSE t.project_id END AS scope_key
        FROM resource_claims c JOIN tasks t USING(task_id)
        LEFT JOIN resource_waits w ON w.claim_id=c.claim_id AND w.closed_at IS NULL ORDER BY c.rowid''')]


def pending(db, task_id):
    return db.execute("SELECT 1 FROM resource_claims WHERE task_id=? AND mode<>'advisory' AND status IN ('waiting','suspended')", (task_id,)).fetchone() is not None


def end_reason(db, task_id):
    if db.execute(f'SELECT 1 FROM task_blockers WHERE task_id=? AND active=1 AND {coordination.DEADLOCK}', (task_id,)).fetchone():
        return 'resource_deadlock'
    reason = db.execute('SELECT stop_reason FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if reason and reason[0]:
        return reason[0]
    if db.execute(f'SELECT 1 FROM task_blockers WHERE task_id=? AND active=1 AND {external.BLOCKER}', (task_id,)).fetchone():
        return 'external_resource_unavailable'
    return 'resource_conflict' if db.execute(f"SELECT 1 FROM task_blockers WHERE task_id=? AND active=1 AND {MANAGED_BLOCKER}", (task_id,)).fetchone() else 'blocked'


def overlaps(a, b):
    if (a['resource_type'], a['scope'], a['scope_key']) != (b['resource_type'], b['scope'], b['scope_key']):
        return False
    x, y = a['resource_key'], b['resource_key']
    return x == y or (a['resource_type'] == 'file_path' and (
        (a['recursive'] and (x == '.' or y.startswith(x + '/')))
        or (b['recursive'] and (y == '.' or x.startswith(y + '/')))))


def conflicts(a, b):
    return (a['task_id'] != b['task_id'] and a['mode'] != 'advisory' and b['mode'] != 'advisory'
            and 'exclusive' in (a['mode'], b['mode']) and overlaps(a, b))


def _state(db, claim, state):
    if state == 'released':
        external.clear(db, claim)
    if state != 'waiting':
        coordination.close_wait(db, claim, state)
    if claim['status'] == state:
        return
    timestamp = {'active': 'acquired_at', 'suspended': 'suspended_at', 'released': 'released_at'}.get(state)
    extra = f',{timestamp}={NOW}' if timestamp else ''
    db.execute(f'UPDATE resource_claims SET status=?{extra} WHERE claim_id=?', (state, claim['claim_id']))
    claim['status'] = state


def reconcile(db, tasks, triggering_claim_id=None, preflight_tasks=()):
    """Lifecycle release, atomic bundle acquisition, and source-specific blockers.

    Keep physical holders until runtime termination is durably recognized. Never
    grant a waiter merely because Pause/Cancel intent was written before a stop.
    """
    rows = claims(db)
    assignments = {r['assignment_id']: dict(r) for r in db.execute('SELECT * FROM task_assignments')}
    running = {r[0] for r in db.execute("SELECT a.task_id FROM task_assignments a JOIN agents g USING(agent_id) WHERE a.ended_at IS NULL AND g.status='running'")}
    externally_blocked = {r[0] for r in db.execute(f"SELECT task_id FROM task_blockers WHERE active=1 AND hard=1 AND NOT {MANAGED_BLOCKER} AND NOT {coordination.DEADLOCK} AND NOT {external.BLOCKER}")}
    suspended = set()
    for key, task in tasks.items():
        if (task['control_intent'] != 'active' or key in externally_blocked
                or (task['stop_required'] and task['stop_reason'] not in ('resource_conflict', 'resource_deadlock', 'external_resource_unavailable'))
                or (task['status'] == 'blocked' and task['block_resume_status'] in (None, 'blocked'))):
            suspended.add(key)
    for c in rows:
        task = tasks[c['task_id']]
        assignment = assignments.get(c['assignment_id'])
        if c['status'] == 'released':
            continue
        if ((c['lifetime'] == 'worker' and (not assignment or assignment['ended_at'] is not None))
                or task['status'] in ('completed', 'canceled')
                or (task['control_intent'] == 'canceled' and c['task_id'] not in running)):
            _state(db, c, 'released')
        elif c['mode'] != 'advisory' and c['task_id'] in suspended:
            if c['status'] != 'active' or c['task_id'] not in running:
                _state(db, c, 'suspended')
        elif c['mode'] == 'advisory':
            _state(db, c, 'active')
    # Active claims of a continuing assignment may be bound by its own worker.
    # A new assignment has no such authority and must freshly check retained
    # Task-scoped claims. Never use a historical available result as proof.
    continuing = {a['task_id'] for a in assignments.values() if a['ended_at'] is None}
    checked = {}
    for c in rows:
        if (c['mode'] != 'advisory' and c['status'] != 'released' and c['task_id'] in preflight_tasks
                and not (c['status'] == 'active' and c['task_id'] in continuing
                         and not runtime_resources.invalidated(db, c['claim_id']))
                and (c['status'] == 'active' or c['probe_status'] in ('unavailable', 'unknown'))):
            checked[c['claim_id']] = external.check(db, c)
    owners = [c for c in rows if c['status'] == 'active']
    groups = {}
    for c in rows:
        if c['status'] in ('waiting', 'suspended') and c['task_id'] not in suspended:
            _state(db, c, 'waiting')
            coordination.enqueue(db, c)
            groups.setdefault(c['task_id'], []).append(c)
    for bundle in sorted(groups.values(), key=lambda b: min(c['wait_sequence'] for c in b)):
        # Every member must pass ownership AND older incompatible waiters.
        # Same-Task claims never block each other. Retain already-held claims.
        barriers = [o for o in rows if o['status'] == 'waiting' and o['wait_sequence'] is not None]
        if (not any(conflicts(c, owner) for c in bundle for owner in owners)
                and not any(o['wait_sequence'] < c['wait_sequence'] and conflicts(c, o)
                            for c in bundle for o in barriers)):
            # Check every missing member; no partial grants if any probe fails.
            for c in bundle:
                if c['claim_id'] not in checked:
                    already_owned = (c['resource_type'] == 'port' and c['task_id'] in continuing
                                     and any(o['task_id'] == c['task_id'] and o['mode'] != 'advisory'
                                             and overlaps(c, o) for o in owners))
                    # A second declaration of a port already held by this live
                    # assignment must not bind against the worker's own socket.
                    checked[c['claim_id']] = already_owned or external.check(db, c)
            if any(checked.get(c['claim_id']) is False for c in rows if c['task_id'] == bundle[0]['task_id']):
                continue
            for c in bundle:
                _state(db, c, 'active')
            owners.extend(bundle)
    graph = coordination.edges(rows, conflicts)
    by_id = {c['claim_id']: c for c in rows}
    desired = {(e['waiting_claim_id'], e['blocking_claim_id']):
               (by_id[e['waiting_claim_id']], by_id[e['blocking_claim_id']], e['edge_kind']) for e in graph}
    # Suspended claims retain overlap explanation, but reserve no queue place
    # and are not graph participants.
    desired.update({(c['claim_id'], owner['claim_id']): (c, owner, 'active_owner') for c in rows
               if c['status'] == 'suspended' for owner in owners if conflicts(c, owner)})
    existing = {(r['waiting_claim_id'], r['owning_claim_id']): r['id'] for r in db.execute(f"SELECT * FROM task_blockers WHERE {MANAGED_BLOCKER} AND active=1")}
    for pair, identifier in existing.items():
        if pair not in desired:
            db.execute(f'UPDATE task_blockers SET active=0,resolved_at={NOW} WHERE id=?', (identifier,))
    for pair, (c, owner, kind) in desired.items():
        if pair not in existing:
            db.execute('''INSERT INTO task_blockers(id,task_id,blocker_type,source_task_id,reason_code,
                source_type,source_id,waiting_claim_id,owning_claim_id,edge_kind)
                VALUES (?,?,'resource',?,'resource_conflict','resource_claim',?,?,?,?)''',
                (str(uuid4()), c['task_id'], owner['task_id'], owner['claim_id'], *pair, kind))
        else:
            db.execute('UPDATE task_blockers SET edge_kind=? WHERE id=?', (kind, existing[pair]))
    coordination.reconcile(db, graph, triggering_claim_id)
    runtime_resources.sync(db)


def create(db, task, **options):
    if task['status'] in ('completed', 'canceled') or task['control_intent'] == 'canceled':
        raise ValueError('Terminal work cannot acquire resource claims')
    value = normalize(db, task, **options)
    assignment_id = None
    if value['lifetime'] == 'worker':
        assignment = db.execute('SELECT * FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (task['task_id'],)).fetchone()
        if not assignment:
            raise ValueError('Worker claim requires an active TaskAssignment')
        assignment_id = assignment['assignment_id']
    rows = claims(db)
    candidate = dict(value, task_id=task['task_id'], assignment_id=assignment_id,
                     scope_key='machine' if value['scope'] == 'global' else task['project_id'])
    for c in rows:
        if c['status'] != 'released' and all(c[k] == v for k, v in candidate.items()):
            return c['claim_id']
    identifier = str(uuid4())
    db.execute('''INSERT INTO resource_claims(claim_id,task_id,assignment_id,resource_type,resource_key,scope,mode,lifetime,recursive,status)
        VALUES (?,?,?,?,?,?,?,?,?,'waiting')''',
        (identifier, task['task_id'], assignment_id, value['resource_type'], value['resource_key'], value['scope'], value['mode'], value['lifetime'], value['recursive']))
    return identifier


def release(db, task_id, claim_id):
    c = db.execute('SELECT * FROM resource_claims WHERE task_id=? AND claim_id=?', (task_id, claim_id)).fetchone()
    if c is None:
        raise LookupError('Resource claim not found for Task')
    # Explicit release is a managed declaration change, not an OS operation.
    _state(db, dict(c), 'released')


def inspect(db, task_id):
    rows = claims(db)
    graph = coordination.edges(rows, conflicts)
    incidents = coordination.inspect(db, task_id=task_id)
    result = []
    for c in rows:
        if c['task_id'] != task_id:
            continue
        item = {k: v for k, v in c.items() if k != 'claim_order'}
        others = [o for o in rows if o['task_id'] != task_id and o['status'] != 'released'
                  and c['status'] != 'released' and overlaps(c, o)]
        item['runtime_capabilities'] = resource_probes.capabilities(c['resource_type'])
        item['runtime_ownership'] = runtime_resources.history(db, c['claim_id'])
        item['current_ownership'] = next((r for r in reversed(item['runtime_ownership']) if r['ended_at'] is None), None)
        item['potential_overlap'] = bool(others)
        item['overlapping_claims'] = [{k: o[k] for k in ('claim_id', 'task_id', 'mode', 'status')} for o in others]
        item['conflicting_claim_ids'] = [o['claim_id'] for o in others if o['status'] == 'active' and conflicts(c, o)]
        blocked = [e for e in graph if e['waiting_claim_id'] == c['claim_id']]
        item['blocked_by'] = blocked
        item['blocked_by_claim_ids'] = sorted({e['blocking_claim_id'] for e in blocked})
        item['blocked_by_task_ids'] = sorted({e['blocking_task_id'] for e in blocked})
        item['queue_position'] = (1 + sum(o['status'] == 'waiting' and o['wait_sequence'] is not None
                                          and o['wait_sequence'] < c['wait_sequence'] and overlaps(c, o)
                                          for o in rows)) if c['wait_sequence'] is not None else None
        item['deadlock_ids'] = [d['deadlock_id'] for d in incidents if d['status'] == 'open']
        result.append(item)
    return result
