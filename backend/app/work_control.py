"""Durable work intent. All DB helpers run inside their caller's transaction."""
import re
import sqlite3


def require_active(task, *, pending=False):
    if task is None:
        raise LookupError('Task not found')
    if task['control_intent'] != 'active' or task['status'] in ('paused', 'blocked', 'canceled', 'completed'):
        raise ValueError('Task work control prevents continuation')
    if pending and task['status'] != 'pending':
        raise ValueError('Only pending tasks can be assigned')


def migrate(db):
    # SQLite cannot widen CHECK constraints in place. Preserve row order, all
    # columns and indexes; foreign keys are disabled only on this migration connection.
    for table, old, new in (
        ('tasks', "'waiting','completed'", "'waiting','blocked','paused','completed'"),
        ('task_assignments', "'reassigned','canceled'", "'reassigned','paused','canceled'"),
    ):
        sql = db.execute('SELECT sql FROM sqlite_master WHERE type="table" AND name=?', (table,)).fetchone()[0]
        indexes = [r[0] for r in db.execute('SELECT sql FROM sqlite_master WHERE type="index" AND tbl_name=? AND sql IS NOT NULL', (table,))]
        columns = ','.join('"' + r['name'] + '"' for r in db.execute(f'PRAGMA table_info({table})'))
        sql = re.sub(r'CREATE TABLE ["`]?'+table+r'["`]?', 'CREATE TABLE '+table+'_v11', sql.replace(old, new), count=1)
        db.execute(sql)
        db.execute(f'INSERT INTO {table}_v11(rowid,{columns}) SELECT rowid,{columns} FROM {table}')
        db.execute(f'DROP TABLE {table}')
        db.execute(f'ALTER TABLE {table}_v11 RENAME TO {table}')
        for index in indexes:
            db.execute(index)
    columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
    if 'control_intent' not in columns:
        db.execute("ALTER TABLE tasks ADD COLUMN control_intent TEXT NOT NULL DEFAULT 'active' CHECK(control_intent IN ('active','paused','canceled'))")
        db.execute("ALTER TABLE tasks ADD COLUMN resume_status TEXT CHECK(resume_status IN ('pending','waiting','blocked'))")
        db.execute("UPDATE tasks SET control_intent='canceled' WHERE status='canceled'")
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise sqlite3.IntegrityError('Work control migration found invalid references')


def request(db, task_id, intent):
    if intent not in ('active', 'paused', 'canceled'):
        raise ValueError('Invalid work control intent')
    task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if task is None:
        raise LookupError('Task not found')
    if db.execute('SELECT 1 FROM tasks WHERE parent_task_id=?', (task_id,)).fetchone():
        raise ValueError('impact_analysis_required: work with descendants cannot be controlled yet')
    if task['status'] == 'completed' or ((task['status'] == 'canceled' or task['control_intent'] == 'canceled') and intent != 'canceled'):
        raise ValueError('Terminal work cannot be paused, resumed, or canceled')
    if intent == 'active':
        if task['status'] != 'paused' or task['control_intent'] != 'paused':
            raise ValueError('Only safely paused work can resume')
        db.execute("UPDATE tasks SET status=resume_status,control_intent='active',resume_status=NULL,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?", (task_id,))
        return
    resume = task['resume_status'] if task['control_intent'] == 'paused' else (
        task['status'] if task['status'] in ('waiting', 'blocked') else 'pending')
    db.execute("UPDATE tasks SET control_intent=?,resume_status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?",
               (intent, resume if intent == 'paused' else None, task_id))


def sync(db, assignment, runtime_status):
    task = db.execute('SELECT * FROM tasks WHERE task_id=?', (assignment['task_id'],)).fetchone()
    intent = task['control_intent']
    if intent == 'active':
        target = {'running': 'in_progress', 'waiting': 'waiting', 'finished': 'completed', 'stopped': 'pending'}[runtime_status]
        if task['status'] in ('blocked', 'paused', 'completed', 'canceled'):
            target = task['status']
        reason = 'completed' if runtime_status == 'finished' else 'stopped'
        end = runtime_status in ('finished', 'stopped')
    else:
        dormant_wait = intent == 'paused' and task['resume_status'] == 'waiting' and runtime_status == 'waiting'
        target = intent if runtime_status != 'running' else task['status']
        end = runtime_status != 'running' and not dormant_wait
        reason = intent
        if end:
            db.execute("UPDATE agents SET status='stopped' WHERE agent_id=?", (assignment['agent_id'],))
            if intent == 'paused' and task['resume_status'] == 'waiting':
                # A separate Stop may explicitly discard a dormant continuation.
                # Resuming must not create an orphan waiting Task without it.
                db.execute("UPDATE tasks SET resume_status='pending' WHERE task_id=?", (task['task_id'],))
    db.execute("UPDATE tasks SET status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=? AND status<>?", (target, task['task_id'], target))
    if end:
        db.execute("UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),ended_reason=? WHERE assignment_id=? AND ended_at IS NULL", (reason, assignment['assignment_id']))
