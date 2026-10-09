"""Durable work intent. All DB helpers run inside their caller's transaction."""
import re
import sqlite3
from . import dependencies, resources


def require_active(task, *, pending=False, db=None):
    if task is None:
        raise LookupError('Task not found')
    blocked = dependencies.hard_blocked(db, task['task_id']) if db is not None else any(b['hard'] for b in task.get('active_blockers', ()))
    resource_pending = resources.pending(db, task['task_id']) if db is not None else task.get('resource_claims_pending', False)
    if task['stop_required'] or blocked or resource_pending:
        raise ValueError('Task has execution blockers or an unsettled worker')
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


def sync(db, assignment, runtime_status):
    task = db.execute('SELECT * FROM tasks WHERE task_id=?', (assignment['task_id'],)).fetchone()
    intent = task['control_intent']
    blocked = dependencies.hard_blocked(db, task['task_id'])
    if ('orchestration_handoff' in task.keys() and task['orchestration_handoff']
            and intent != 'canceled' and runtime_status != 'running'):
        # A dormant orchestration continuation is not a user question and not
        # an ended assignment. Cancel still follows the ordinary termination path.
        target = 'paused' if intent == 'paused' else 'blocked'
        end, reason = False, None
        db.execute("UPDATE agents SET status='stopped',waiting_question=NULL WHERE agent_id=?", (assignment['agent_id'],))
        db.execute('UPDATE tasks SET stop_required=0,stop_reason=NULL WHERE task_id=?', (task['task_id'],))
    elif intent == 'active' and (blocked or task['stop_required']):
        target = 'in_progress' if runtime_status == 'running' else 'blocked'
        reason = resources.end_reason(db, task['task_id'])
        end = runtime_status in ('finished', 'stopped')
        if end:
            db.execute("UPDATE agents SET status='stopped' WHERE agent_id=?", (assignment['agent_id'],))
    elif intent == 'active':
        target = {'running': 'in_progress', 'waiting': 'waiting', 'finished': 'completed', 'stopped': 'pending'}[runtime_status]
        if task['status'] in ('blocked', 'paused', 'completed', 'canceled'):
            target = task['status']
        reason = 'completed' if runtime_status == 'finished' else 'stopped'
        end = runtime_status in ('finished', 'stopped')
    else:
        dormant_wait = intent == 'paused' and (task['resume_status'] == 'waiting' or task['block_resume_status'] == 'waiting') and runtime_status == 'waiting'
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
        db.execute('UPDATE tasks SET stop_required=0,stop_reason=NULL WHERE task_id=?', (task['task_id'],))
        if task['block_resume_status'] == 'waiting':
            db.execute("UPDATE tasks SET block_resume_status='pending' WHERE task_id=?", (task['task_id'],))
        db.execute("UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),ended_reason=? WHERE assignment_id=? AND ended_at IS NULL", (reason, assignment['assignment_id']))
