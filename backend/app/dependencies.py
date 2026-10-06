"""Transactional work graph, blocker reconciliation and shared control planning.

No process or filesystem operations occur here. Callers hold BEGIN IMMEDIATE
for mutations; preview uses the same planner on a read transaction.
"""
import json
import re
from uuid import uuid4

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
TERMINAL = ('completed', 'canceled')


def migrate(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
    if 'block_resume_status' not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN block_resume_status TEXT CHECK(block_resume_status IN ('pending','waiting','blocked'))")
        db.execute('ALTER TABLE tasks ADD COLUMN stop_required INTEGER NOT NULL DEFAULT 0 CHECK(stop_required IN (0,1))')
        db.execute('ALTER TABLE tasks ADD COLUMN legacy_pause INTEGER NOT NULL DEFAULT 0 CHECK(legacy_pause IN (0,1))')
        db.execute("UPDATE tasks SET legacy_pause=1 WHERE control_intent='paused'")
    db.execute(f'''CREATE TABLE IF NOT EXISTS task_dependencies (
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        depends_on_task_id TEXT NOT NULL REFERENCES tasks(task_id),
        created_at TEXT NOT NULL DEFAULT ({NOW}),
        PRIMARY KEY(task_id,depends_on_task_id), CHECK(task_id<>depends_on_task_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS dependency_reverse ON task_dependencies(depends_on_task_id,task_id)')
    for table, kind in (('task_blockers', 'blocker_type'), ('task_replan_reasons', 'reason_type')):
        hard = ',hard INTEGER NOT NULL DEFAULT 1 CHECK(hard IN (0,1))' if table == 'task_blockers' else ''
        db.execute(f'''CREATE TABLE IF NOT EXISTS {table} (
            id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(task_id),
            {kind} TEXT NOT NULL,source_task_id TEXT REFERENCES tasks(task_id),reason_code TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            created_at TEXT NOT NULL DEFAULT ({NOW}),resolved_at TEXT {hard})''')
        db.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_active ON {table}(task_id,{kind},COALESCE(source_task_id,''),reason_code) WHERE active=1")
    db.execute(f'''CREATE TABLE IF NOT EXISTS task_control_operations (
        operation_id TEXT PRIMARY KEY,root_task_id TEXT NOT NULL REFERENCES tasks(task_id),
        action TEXT NOT NULL CHECK(action IN ('pause','cancel')),
        status TEXT NOT NULL CHECK(status IN ('applying','completed','recovery_required')),
        released INTEGER NOT NULL DEFAULT 0 CHECK(released IN (0,1)),
        impact TEXT NOT NULL,failures TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL DEFAULT ({NOW}),updated_at TEXT NOT NULL DEFAULT ({NOW}))''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS active_control_root ON task_control_operations(root_task_id,action) WHERE released=0')
    db.execute(f'''CREATE TABLE IF NOT EXISTS task_control_members (
        operation_id TEXT NOT NULL REFERENCES task_control_operations(operation_id),
        task_id TEXT NOT NULL REFERENCES tasks(task_id),released_at TEXT,
        PRIMARY KEY(operation_id,task_id))''')
    # Widen the existing assignment CHECK without changing identities/history.
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
    if "'blocked'" not in sql:
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_assignments' AND sql IS NOT NULL")]
        sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE task_assignments_v12', sql, count=1)
        db.execute(sql.replace("'paused','canceled'", "'paused','blocked','canceled'"))
        columns = ','.join('"' + r['name'] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
        db.execute(f'INSERT INTO task_assignments_v12(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
        db.execute('DROP TABLE task_assignments')
        db.execute('ALTER TABLE task_assignments_v12 RENAME TO task_assignments')
        for index in indexes:
            db.execute(index)


def task(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if row is None:
        raise LookupError('Task not found')
    return dict(row)


def hard_blocked(db, task_id):
    return db.execute('SELECT 1 FROM task_blockers WHERE task_id=? AND active=1 AND hard=1', (task_id,)).fetchone() is not None


def describe(db, row):
    result = dict(row)
    result['active_blockers'] = [dict(r) for r in db.execute('SELECT * FROM task_blockers WHERE task_id=? AND active=1 ORDER BY rowid', (row['task_id'],))]
    result['replanning_reasons'] = [dict(r) for r in db.execute('SELECT * FROM task_replan_reasons WHERE task_id=? AND active=1 ORDER BY rowid', (row['task_id'],))]
    result['replanning_required'] = bool(result['replanning_reasons'])
    return result


def set_reasons(db, table, kind_column, kind, desired):
    existing = {(r['task_id'], r['source_task_id'], r['reason_code']): r['id'] for r in db.execute(
        f'SELECT * FROM {table} WHERE {kind_column}=? AND active=1', (kind,))}
    for key, identifier in existing.items():
        if key not in desired:
            db.execute(f'UPDATE {table} SET active=0,resolved_at={NOW} WHERE id=?', (identifier,))
    for key in sorted(desired - existing.keys()):
        db.execute(f'INSERT INTO {table}(id,task_id,{kind_column},source_task_id,reason_code) VALUES (?,?,?,?,?)', (str(uuid4()), key[0], kind, key[1], key[2]))


def reconcile(db):
    """Materialize reasons and safe status restoration in the lifecycle transaction."""
    tasks = {r['task_id']: dict(r) for r in db.execute('SELECT * FROM tasks')}
    reasons = set()
    for edge in db.execute('SELECT * FROM task_dependencies'):
        source = tasks[edge['depends_on_task_id']]
        if source['status'] == 'completed':
            continue
        state = source['control_intent'] if source['control_intent'] != 'active' else source['status']
        reason = 'dependency_' + (state if state in ('paused', 'canceled') else 'incomplete')
        reasons.add((edge['task_id'], edge['depends_on_task_id'], reason))
    set_reasons(db, 'task_blockers', 'blocker_type', 'dependency', reasons)
    set_reasons(db, 'task_replan_reasons', 'reason_type', 'dependency', {r for r in reasons if r[2] == 'dependency_canceled'})
    hierarchical = set()
    for source in tasks.values():
        if source['control_intent'] != 'canceled' and source['status'] != 'canceled':
            continue
        current, seen = source['parent_task_id'], set()
        while current and current not in seen:
            seen.add(current)
            ancestor = tasks[current]
            if ancestor['status'] not in TERMINAL and ancestor['control_intent'] != 'canceled':
                hierarchical.add((current, source['task_id'], 'child_canceled'))
            current = ancestor['parent_task_id']
    set_reasons(db, 'task_replan_reasons', 'reason_type', 'hierarchy', hierarchical)
    for record in tasks.values():
        key = record['task_id']
        if record['status'] in TERMINAL:
            continue
        runtime = db.execute('''SELECT g.status FROM task_assignments a JOIN agents g ON g.agent_id=a.agent_id
            WHERE a.task_id=? AND a.ended_at IS NULL''', (key,)).fetchone()
        running = runtime is not None and runtime['status'] == 'running'
        blocked = hard_blocked(db, key)
        restore = record['block_resume_status']
        if blocked and restore is None:
            previous = record['resume_status'] if record['control_intent'] == 'paused' else record['status']
            restore = previous if previous in ('waiting', 'blocked') else 'pending'
            db.execute('UPDATE tasks SET block_resume_status=? WHERE task_id=?', (restore, key))
        if blocked and running:
            db.execute('UPDATE tasks SET stop_required=1 WHERE task_id=?', (key,))
        # User intent outranks dependency status. Keep the restoration layer until
        # Resume so a now-resolved dependency cannot strand paused work as blocked.
        if record['control_intent'] != 'active' or running:
            continue
        if blocked:
            db.execute(f"UPDATE tasks SET status='blocked',updated_at={NOW} WHERE task_id=? AND status<>'blocked'", (key,))
        elif restore is not None and not record['stop_required']:
            db.execute(f'UPDATE tasks SET status=?,block_resume_status=NULL,updated_at={NOW} WHERE task_id=?', (restore, key))


def change_edge(db, task_id, source_id, remove=False):
    dependent, source = task(db, task_id), task(db, source_id)
    if not remove:
        if dependent['status'] in TERMINAL or dependent['control_intent'] == 'canceled':
            raise ValueError('Terminal work cannot acquire dependencies')
        if not dependent['project_id'] or dependent['project_id'] != source['project_id']:
            raise ValueError('Dependencies require the same resolved Project')
        seen, pending = set(), [source_id]
        graph = {}
        for edge in db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies'):
            graph.setdefault(edge[0], []).append(edge[1])
        while pending:
            current = pending.pop()
            if current == task_id:
                raise ValueError('Dependency would create a cycle')
            if current not in seen:
                seen.add(current)
                pending.extend(graph.get(current, ()))
        db.execute('INSERT OR IGNORE INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)', (task_id, source_id))
    else:
        db.execute('DELETE FROM task_dependencies WHERE task_id=? AND depends_on_task_id=?', (task_id, source_id))
    reconcile(db)


def impact(db, root_id, action):
    if action not in ('pause', 'cancel'):
        raise ValueError('Unknown work control action')
    root = task(db, root_id)
    if root['status'] == 'completed' or (action == 'pause' and (root['status'] == 'canceled' or root['control_intent'] == 'canceled')):
        raise ValueError('Terminal work cannot be controlled')
    tasks = {r['task_id']: dict(r) for r in db.execute('SELECT * FROM tasks WHERE project_id IS ?', (root['project_id'],))}
    children, reverse = {}, {}
    for t in tasks.values():
        children.setdefault(t['parent_task_id'], []).append(t['task_id'])
    for edge in db.execute('SELECT * FROM task_dependencies'):
        reverse.setdefault(edge['depends_on_task_id'], []).append(edge['task_id'])
    subtree, pending = set(), [root_id]
    while pending:
        key = pending.pop()
        if key not in subtree:
            subtree.add(key)
            pending.extend(children.get(key, ()))
    affected = {k for k in subtree if tasks[k]['status'] not in TERMINAL and tasks[k]['control_intent'] != 'canceled'}
    # A retry must still include unfinished cancellation from a prior phase.
    if action == 'cancel':
        affected |= {k for k in subtree if tasks[k]['status'] not in TERMINAL}
    # Only direct prerequisites acquire paused/canceled reasons. A dependent
    # of an already-incomplete dependent stays incomplete, not canceled.
    external = {key for source in affected for key in reverse.get(source, ())
                if key in tasks and key not in subtree and tasks[key]['status'] not in TERMINAL}
    replanning = set(external) if action == 'cancel' else set()
    if action == 'cancel':
        for key in affected:
            current, seen = tasks[key]['parent_task_id'], set()
            while current and current not in seen:
                seen.add(current)
                if current not in affected and tasks[current]['status'] not in TERMINAL:
                    replanning.add(current)
                current = tasks[current]['parent_task_id']
    runtime = [dict(r) for r in db.execute('''SELECT a.task_id,a.agent_id FROM task_assignments a
        JOIN agents g ON g.agent_id=a.agent_id WHERE a.ended_at IS NULL AND g.status='running' ''')
        if r['task_id'] in affected | external]
    return dict(target_task_id=root_id, action=action, affected_task_ids=sorted(affected),
                descendant_task_ids=sorted(subtree - {root_id}), external_dependent_task_ids=sorted(external),
                workers_to_stop=runtime, completed_descendants=sorted(k for k in subtree - {root_id} if tasks[k]['status'] == 'completed'),
                canceled_descendants=sorted(k for k in subtree - {root_id} if tasks[k]['status'] == 'canceled'),
                blocked_task_ids=sorted(k for k in external if tasks[k]['control_intent'] == 'active'),
                replanning_task_ids=sorted(replanning))


def begin_control(db, root_id, intent):
    if intent == 'active':
        return resume(db, root_id)
    if intent not in ('paused', 'canceled'):
        raise ValueError('Invalid work control intent')
    action = 'pause' if intent == 'paused' else 'cancel'
    plan = impact(db, root_id, action)
    operation = db.execute('SELECT * FROM task_control_operations WHERE root_task_id=? AND action=? AND released=0', (root_id, action)).fetchone()
    identifier = operation['operation_id'] if operation else str(uuid4())
    if operation is None:
        db.execute("INSERT INTO task_control_operations(operation_id,root_task_id,action,status,impact) VALUES (?,?,?,'applying',?)", (identifier, root_id, action, json.dumps(plan)))
    else:
        db.execute(f"UPDATE task_control_operations SET status='applying',impact=?,failures='[]',updated_at={NOW} WHERE operation_id=?", (json.dumps(plan), identifier))
    for key in plan['affected_task_ids']:
        record = task(db, key)
        db.execute('INSERT OR IGNORE INTO task_control_members(operation_id,task_id) VALUES (?,?)', (identifier, key))
        restore = record['resume_status'] if record['control_intent'] == 'paused' else (record['status'] if record['status'] in ('waiting', 'blocked') else 'pending')
        db.execute(f'UPDATE tasks SET control_intent=?,resume_status=?,updated_at={NOW} WHERE task_id=?', (intent, restore if intent == 'paused' else None, key))
        if intent == 'paused' and key == root_id:
            # An explicit root Pause adopts its pre-v12 independent pause into
            # this operation. Descendants retain their independent ownership.
            db.execute('UPDATE tasks SET legacy_pause=0 WHERE task_id=?', (key,))
        if intent == 'canceled':
            db.execute('UPDATE tasks SET legacy_pause=0 WHERE task_id=?', (key,))
            db.execute(f'''UPDATE task_control_members SET released_at={NOW} WHERE task_id=? AND operation_id IN
                (SELECT operation_id FROM task_control_operations WHERE action='pause')''', (key,))
    reconcile(db)
    return identifier


def resume(db, root_id):
    root = task(db, root_id)
    if root['status'] != 'paused' or root['control_intent'] != 'paused':
        raise ValueError('Only safely paused work can resume')
    operation = db.execute("SELECT * FROM task_control_operations WHERE root_task_id=? AND action='pause' AND released=0", (root_id,)).fetchone()
    if operation:
        members = [r[0] for r in db.execute('SELECT task_id FROM task_control_members WHERE operation_id=? AND released_at IS NULL', (operation['operation_id'],))]
    elif root['legacy_pause']:
        members = [root_id]
    else:
        raise ValueError('Pause belongs to an ancestor operation; resume that operation first')
    for key in members:
        current = task(db, key)
        if current['status'] == 'in_progress' or db.execute("SELECT 1 FROM task_assignments a JOIN agents g ON g.agent_id=a.agent_id WHERE a.task_id=? AND a.ended_at IS NULL AND g.status='running'", (key,)).fetchone():
            raise ValueError('A pause operation still has an unsettled worker')
    if operation:
        db.execute(f'UPDATE task_control_members SET released_at={NOW} WHERE operation_id=? AND released_at IS NULL', (operation['operation_id'],))
        db.execute(f'UPDATE task_control_operations SET released=1,updated_at={NOW} WHERE operation_id=?', (operation['operation_id'],))
    else:
        db.execute('UPDATE tasks SET legacy_pause=0 WHERE task_id=?', (root_id,))
    for key in members:
        current = task(db, key)
        if current['status'] in TERMINAL or current['control_intent'] == 'canceled':
            continue
        owned = current['legacy_pause'] or db.execute("SELECT 1 FROM task_control_members m JOIN task_control_operations o USING(operation_id) WHERE m.task_id=? AND m.released_at IS NULL AND o.action='pause' AND o.released=0", (key,)).fetchone()
        if not owned:
            target = current['resume_status'] or 'pending'
            db.execute(f"UPDATE tasks SET control_intent='active',status=?,resume_status=NULL,updated_at={NOW} WHERE task_id=?", (target, key))
    reconcile(db)
    return operation['operation_id'] if operation else None


def finish_operations(db, failures=()):
    # Explicit failures preserve phase-3 truth; startup can subsequently finish
    # only after the existing process recovery has reconciled assignments.
    for operation in db.execute("SELECT * FROM task_control_operations WHERE released=0").fetchall():
        plan = json.loads(operation['impact'])
        keys = set(plan['affected_task_ids']) | set(plan['external_dependent_task_ids'])
        own_failures = [f for f in failures if f['task_id'] in keys]
        outstanding = any(task(db, key)['stop_required'] or (
            task(db, key)['control_intent'] in ('paused', 'canceled') and task(db, key)['status'] not in ('paused', 'canceled', 'completed')) for key in keys)
        status = 'recovery_required' if own_failures or outstanding else 'completed'
        db.execute(f'UPDATE task_control_operations SET status=?,failures=?,updated_at={NOW} WHERE operation_id=?', (status, json.dumps(own_failures), operation['operation_id']))
