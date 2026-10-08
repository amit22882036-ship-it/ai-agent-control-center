"""Structured Task responsibilities, independent of concrete resource ownership.

All mutations run inside the caller's BEGIN IMMEDIATE transaction. No provider,
filesystem, process, dependency, or resource-deadlock inference occurs here.
"""
import re
from uuid import uuid4

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
BLOCKER = "(blocker_type='work_intent' AND waiting_intent_id IS NOT NULL AND owning_intent_id IS NOT NULL)"


def migrate(db):
    db.execute(f'''CREATE TABLE IF NOT EXISTS work_intents (
        intent_id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        namespace TEXT NOT NULL,key TEXT NOT NULL,normalized_key TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('advisory','single_owner')),
        status TEXT NOT NULL CHECK(status IN ('active','suspended','released')),
        delegated_from_intent_id TEXT REFERENCES work_intents(intent_id),
        activation_order INTEGER NOT NULL UNIQUE,
        holds_authority INTEGER NOT NULL DEFAULT 0 CHECK(holds_authority IN (0,1)),
        created_at TEXT NOT NULL DEFAULT ({NOW}),updated_at TEXT NOT NULL DEFAULT ({NOW}),
        released_at TEXT,release_reason TEXT)''')
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS work_intent_live_identity ON work_intents
        (task_id,namespace,normalized_key,mode,COALESCE(delegated_from_intent_id,'')) WHERE status<>'released' """)
    db.execute('CREATE INDEX IF NOT EXISTS work_intent_project ON work_intents(project_id,namespace,status)')
    columns = {r['name'] for r in db.execute('PRAGMA table_info(task_blockers)')}
    if 'waiting_intent_id' not in columns:
        db.execute('ALTER TABLE task_blockers ADD COLUMN waiting_intent_id TEXT REFERENCES work_intents(intent_id)')
        db.execute('ALTER TABLE task_blockers ADD COLUMN owning_intent_id TEXT REFERENCES work_intents(intent_id)')
    # Preserve every existing partial-index exception and its provenance.
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_blockers_active'").fetchone()[0]
    if 'waiting_intent_id' not in sql:
        db.execute('DROP INDEX task_blockers_active')
        db.execute(sql + f' AND NOT {BLOCKER}')
    db.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS work_intent_blocker ON task_blockers(waiting_intent_id,owning_intent_id) WHERE active=1 AND {BLOCKER}')


def normalize(namespace, key):
    if not isinstance(namespace, str) or not isinstance(key, str):
        raise ValueError('Work intent namespace and key must be strings')
    if len(namespace) > 64 or len(key) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in namespace + key):
        raise ValueError('Work intent namespace/key is too long or contains control characters')
    namespace = namespace.strip().lower()
    parts = [part.strip().lower() for part in key.strip().strip('/').split('/')]
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', namespace):
        raise ValueError('Namespace must be a simple logical identifier')
    if any(not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{0,63}', p) for p in parts):
        raise ValueError('Key requires non-empty logical segments; traversal, backslashes and encoded paths are not allowed')
    normalized = '/'.join(parts)
    if len(normalized) > 256:
        raise ValueError('Normalized work intent key must be at most 256 characters')
    return namespace, normalized


def contains(a, b):
    return a == b or b.startswith(a + '/')


def overlaps(a, b):
    return (a['task_id'] != b['task_id'] and a['project_id'] == b['project_id']
            and a['namespace'] == b['namespace']
            and (contains(a['normalized_key'], b['normalized_key']) or contains(b['normalized_key'], a['normalized_key'])))


def rows(db):
    return [dict(r) for r in db.execute('SELECT * FROM work_intents ORDER BY activation_order')]


def delegated(a, b, by_id):
    # Explicit delegation chains permit recursive responsibility, never siblings.
    for child, ancestor in ((a, b), (b, a)):
        current, seen = child['delegated_from_intent_id'], set()
        while current and current not in seen:
            if current == ancestor['intent_id']:
                return True
            seen.add(current)
            current = by_id[current]['delegated_from_intent_id'] if current in by_id else None
    return False


def plan(items):
    owners, preceding, desired = [], [], {}
    by_id = {r['intent_id']: r for r in items}
    for candidate in items:
        if candidate['status'] != 'active':
            continue
        blockers = [owner for owner in preceding if overlaps(candidate, owner)
                    and 'single_owner' in (candidate['mode'], owner['mode'])
                    and not delegated(candidate, owner, by_id)]
        if blockers:
            for owner in blockers:
                desired[(candidate['intent_id'], owner['intent_id'])] = (candidate, owner)
        else:
            owners.append(candidate)
        # Earlier active declarations remain precedence barriers even if gated;
        # promotion can never preempt a later Worker that was allowed to run.
        preceding.append(candidate)
    return {r['intent_id'] for r in owners}, desired


def next_order(db):
    return db.execute('SELECT COALESCE(MAX(activation_order),0)+1 FROM work_intents').fetchone()[0]


def create(db, task, namespace, key, mode='advisory', delegated_from_intent_id=None):
    if task['status'] in ('completed', 'canceled') or task['control_intent'] == 'canceled':
        raise ValueError('Terminal work cannot declare work intents')
    if not task['project_id']:
        raise ValueError('Work intent requires resolved Project ownership')
    namespace, normalized = normalize(namespace, key)
    if mode not in ('advisory', 'single_owner'):
        raise ValueError('Invalid work intent mode')
    if delegated_from_intent_id is not None:
        source = db.execute('SELECT * FROM work_intents WHERE intent_id=?', (delegated_from_intent_id,)).fetchone()
        if not source or source['project_id'] != task['project_id'] or source['namespace'] != namespace or not contains(source['normalized_key'], normalized):
            raise ValueError('Delegation requires an existing same-Project intent containing this namespace and scope')
        parent, seen = task['parent_task_id'], set()
        while parent and parent not in seen and parent != source['task_id']:
            seen.add(parent)
            record = db.execute('SELECT parent_task_id FROM tasks WHERE task_id=?', (parent,)).fetchone()
            parent = record[0] if record else None
        if not parent or parent != source['task_id']:
            raise ValueError('Delegation source must belong to an ancestor Task')
    existing = db.execute("""SELECT intent_id FROM work_intents WHERE task_id=? AND namespace=? AND normalized_key=?
        AND mode=? AND delegated_from_intent_id IS ? AND status<>'released' """,
        (task['task_id'], namespace, normalized, mode, delegated_from_intent_id)).fetchone()
    if existing:
        return existing[0]
    running = {r[0] for r in db.execute("SELECT a.task_id FROM task_assignments a JOIN agents g USING(agent_id) WHERE a.ended_at IS NULL AND g.status='running'")}
    identifier = str(uuid4())
    db.execute('''INSERT INTO work_intents(intent_id,task_id,project_id,namespace,key,normalized_key,mode,
        status,delegated_from_intent_id,activation_order) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (identifier, task['task_id'], task['project_id'], namespace, key.strip(), normalized, mode,
         'suspended' if task['control_intent'] == 'paused' and task['task_id'] not in running else 'active', delegated_from_intent_id, next_order(db)))
    _, desired = plan(rows(db))
    losers = {c['task_id'] for c, _ in desired.values()}
    if losers & running:
        raise ValueError('Work intent conflicts with active single-owner responsibility; pause or stop the competing work before declaring it')
    return identifier


def release(db, task_id, intent_id):
    intent = db.execute('SELECT * FROM work_intents WHERE task_id=? AND intent_id=?', (task_id, intent_id)).fetchone()
    if not intent:
        raise LookupError('Work intent not found for Task')
    db.execute(f"UPDATE work_intents SET status='released',holds_authority=0,released_at={NOW},updated_at={NOW},release_reason='explicit_release' WHERE intent_id=? AND status<>'released'", (intent_id,))


def reconcile(db, tasks):
    # Earlier schema migrations may invoke the shared lifecycle before v17 exists.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='work_intents'").fetchone():
        return False
    running = {r[0] for r in db.execute("SELECT a.task_id FROM task_assignments a JOIN agents g USING(agent_id) WHERE a.ended_at IS NULL AND g.status='running'")}
    for intent in rows(db):
        if intent['status'] == 'released':
            continue
        task = tasks[intent['task_id']]
        # Failed/unconfirmed termination must not give another Worker authority.
        terminal = task['status'] in ('completed', 'canceled') or (task['control_intent'] == 'canceled' and task['task_id'] not in running)
        state = 'released' if terminal else ('suspended' if task['control_intent'] == 'paused' and task['task_id'] not in running else 'active')
        if state != intent['status']:
            extra = f',activation_order={next_order(db)}' if state == 'active' else ''
            if state == 'released':
                reason = 'completed' if task['status'] == 'completed' else 'canceled'
                extra += f",released_at={NOW},release_reason='{reason}'"
            db.execute(f'UPDATE work_intents SET status=?,updated_at={NOW}{extra} WHERE intent_id=?', (state, intent['intent_id']))
    items = rows(db)
    owners, desired = plan(items)
    for intent in items:
        authority = int(intent['intent_id'] in owners)
        if intent['holds_authority'] != authority:
            db.execute(f'UPDATE work_intents SET holds_authority=?,updated_at={NOW} WHERE intent_id=?', (authority, intent['intent_id']))
    existing = {(r['waiting_intent_id'], r['owning_intent_id']): r['id'] for r in db.execute(f'SELECT * FROM task_blockers WHERE active=1 AND {BLOCKER}')}
    for pair, identifier in existing.items():
        if pair not in desired:
            db.execute(f'UPDATE task_blockers SET active=0,resolved_at={NOW} WHERE id=?', (identifier,))
    for pair, (candidate, owner) in desired.items():
        if pair not in existing:
            db.execute('''INSERT INTO task_blockers(id,task_id,blocker_type,source_task_id,reason_code,
                source_type,source_id,waiting_intent_id,owning_intent_id)
                VALUES (?,?,'work_intent',?,'work_intent_conflict','work_intent',?,?,?)''',
                (str(uuid4()), candidate['task_id'], owner['task_id'], owner['intent_id'], *pair))
    return existing.keys() != desired.keys()


def inspect(db, task_id):
    return [dict(r) for r in db.execute('SELECT * FROM work_intents WHERE task_id=? ORDER BY rowid', (task_id,))]


def inspect_overlaps(db, task_id):
    items = rows(db)
    by_id = {r['intent_id']: r for r in items}
    pairs = {(r['waiting_intent_id'], r['owning_intent_id']) for r in db.execute(f'SELECT * FROM task_blockers WHERE active=1 AND {BLOCKER}')}
    result = []
    for intent in items:
        if intent['task_id'] != task_id or intent['status'] == 'released':
            continue
        for other in items:
            if other['status'] == 'released' or not overlaps(intent, other):
                continue
            delegated_work = delegated(intent, other, by_id)
            blocking = not delegated_work and intent['status'] == other['status'] == 'active' and 'single_owner' in (intent['mode'], other['mode'])
            a, b = intent['intent_id'], other['intent_id']
            blocked = intent['task_id'] if (a, b) in pairs else (other['task_id'] if (b, a) in pairs else None)
            result.append(dict(intent_id=a, other_task_id=other['task_id'], other_intent_id=b,
                namespace=other['namespace'], normalized_key=other['normalized_key'], mode=other['mode'],
                status=other['status'], holds_authority=bool(other['holds_authority']),
                relationship='equal' if intent['normalized_key'] == other['normalized_key'] else ('ancestor' if contains(intent['normalized_key'], other['normalized_key']) else 'descendant'),
                classification='delegated' if delegated_work else ('blocking' if blocking else 'advisory'),
                blocked_task_id=blocked))
    return result
