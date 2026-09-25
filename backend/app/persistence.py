"""Local durable agent records; no process or notification recovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4
from .task_domain import task_title, validate_task_status, validate_end_reason
from .agent_names import default_display_name, validate_display_color
from .output_history import OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT


class AgentStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('CONTROL_CENTER_DB_PATH')
                         or Path(__file__).resolve().parents[2] / 'data' / 'control_center.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise RuntimeError(f'Unsupported control-center database schema version: {version}')
            if version == 0:
                if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                    raise RuntimeError('Unversioned non-empty control-center database is unsupported')
                db.execute('''CREATE TABLE agents (
                    creation_order INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL UNIQUE, parent_id TEXT, task TEXT NOT NULL,
                    agent_type TEXT NOT NULL, sandbox TEXT, status TEXT NOT NULL,
                    session_id TEXT, waiting_question TEXT,
                    similar_decisions_enabled INTEGER NOT NULL, similar_examples TEXT NOT NULL,
                    always_decide_enabled INTEGER NOT NULL, always_decide_configured INTEGER NOT NULL
                )''')
                db.execute('''CREATE TABLE output (
                    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                    sequence INTEGER NOT NULL, line TEXT NOT NULL,
                    PRIMARY KEY (agent_id, sequence)
                )''')
                db.execute('PRAGMA user_version = 1')
                version = 1
            # Validate versioned databases instead of silently recreating missing tables.
            try:
                db.execute('''SELECT agent_id, parent_id, task, agent_type, sandbox, status,
                    session_id, waiting_question, similar_decisions_enabled, similar_examples,
                    always_decide_enabled, always_decide_configured, creation_order FROM agents LIMIT 0''')
                db.execute('SELECT agent_id, sequence, line FROM output LIMIT 0')
                if version == 1:
                    # Explicit transactional v1 -> v2 migration, retaining every row.
                    db.execute('BEGIN')
                    db.execute("ALTER TABLE agents ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
                    for row in db.execute('SELECT agent_id, task FROM agents').fetchall():
                        db.execute('UPDATE agents SET display_name=? WHERE agent_id=?',
                                   (default_display_name(row['task']), row['agent_id']))
                    db.execute('PRAGMA user_version = 2')
                    version = 2
                db.execute('SELECT display_name FROM agents LIMIT 0')
                if version == 2:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    db.execute("ALTER TABLE agents ADD COLUMN display_color TEXT NOT NULL DEFAULT 'neutral' CHECK(display_color IN ('neutral','violet','blue','cyan','green','yellow','orange','red','pink'))")
                    db.execute("""CREATE TABLE agent_name_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                        name TEXT NOT NULL,
                        changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    )""")
                    db.execute('CREATE INDEX agent_name_history_agent ON agent_name_history(agent_id, id)')
                    db.execute('INSERT INTO agent_name_history(agent_id, name) SELECT agent_id, display_name FROM agents ORDER BY creation_order')
                    db.execute('PRAGMA user_version = 3')
                    version = 3
                db.execute('SELECT display_color FROM agents LIMIT 0')
                db.execute('SELECT id, agent_id, name, changed_at FROM agent_name_history LIMIT 0')
                if version == 3:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._migrate_tasks(db)
                    db.execute('PRAGMA user_version = 4')
                    version = 4
                db.execute('SELECT task_id, title, description, status, created_at, updated_at FROM tasks LIMIT 0')
                db.execute('SELECT assignment_id, task_id, agent_id, assigned_at, ended_at, ended_reason FROM task_assignments LIMIT 0')
                if version == 4:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._backfill_unrepresented_agents(db)
                    db.execute('PRAGMA user_version = 5')
            except sqlite3.DatabaseError as exc:
                raise RuntimeError('Incompatible control-center database schema') from exc

    @staticmethod
    def _migrate_tasks(db):
        db.execute("""CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','in_progress','waiting','completed','canceled')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        db.execute("""CREATE TABLE task_assignments (
            assignment_id TEXT PRIMARY KEY NOT NULL,
            task_id TEXT NOT NULL REFERENCES tasks(task_id),
            agent_id TEXT NOT NULL REFERENCES agents(agent_id),
            assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            ended_at TEXT,
            ended_reason TEXT CHECK(ended_reason IN ('completed','stopped','reassigned','canceled')),
            CHECK ((ended_at IS NULL AND ended_reason IS NULL) OR
                   (ended_at IS NOT NULL AND ended_reason IS NOT NULL))
        )""")
        db.execute('CREATE UNIQUE INDEX task_active_assignment ON task_assignments(task_id) WHERE ended_at IS NULL')
        db.execute('CREATE UNIQUE INDEX agent_active_task ON task_assignments(agent_id) WHERE ended_at IS NULL')
        db.execute('CREATE INDEX task_assignment_history ON task_assignments(task_id, assigned_at)')
        mapping = {'running': ('in_progress', None), 'waiting': ('waiting', None),
                   'finished': ('completed', 'completed'), 'stopped': ('pending', 'stopped')}
        for agent in db.execute('SELECT agent_id, task, status FROM agents ORDER BY creation_order').fetchall():
            status, reason = mapping[agent['status']]
            task_id = str(uuid4())
            db.execute('INSERT INTO tasks(task_id,title,description,status) VALUES (?,?,?,?)',
                       (task_id, task_title(agent['task']), agent['task'], status))
            db.execute("""INSERT INTO task_assignments(assignment_id,task_id,agent_id,ended_at,ended_reason)
                VALUES (?,?,?,CASE WHEN ? IS NOT NULL THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,?)""",
                       (str(uuid4()), task_id, agent['agent_id'], reason, reason))

    def create_task(self, description, title=None, status='pending'):
        validate_task_status(status)
        if status in ('in_progress', 'waiting'):
            raise ValueError('Working tasks require an active assignment')
        task_id = str(uuid4())
        with self._connection() as db:
            db.execute('INSERT INTO tasks(task_id,title,description,status) VALUES (?,?,?,?)',
                       (task_id, task_title(description) if title is None else title, description, status))
        return self.get_task(task_id)

    @staticmethod
    def _backfill_unrepresented_agents(db):
        mapping = {'running': ('in_progress', None), 'waiting': ('waiting', None),
                   'finished': ('completed', 'completed'), 'stopped': ('pending', 'stopped')}
        rows = db.execute('''SELECT agent_id, task, status FROM agents g WHERE NOT EXISTS
            (SELECT 1 FROM task_assignments a WHERE a.agent_id=g.agent_id) ORDER BY creation_order''').fetchall()
        for agent in rows:
            status, reason = mapping[agent['status']]
            task_id = str(uuid4())
            db.execute('INSERT INTO tasks(task_id,title,description,status) VALUES (?,?,?,?)',
                       (task_id, task_title(agent['task']), agent['task'], status))
            db.execute("""INSERT INTO task_assignments(assignment_id,task_id,agent_id,ended_at,ended_reason)
                VALUES (?,?,?,CASE WHEN ? IS NOT NULL THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,?)""",
                       (str(uuid4()), task_id, agent['agent_id'], reason, reason))

    def task_summaries(self, task_id=None):
        # One joined snapshot, with no output/session loading or per-task queries.
        with self._connection() as db:
            rows = db.execute('''SELECT t.*, a.assignment_id, a.agent_id, g.display_name AS agent_display_name,
                g.display_color AS agent_display_color, g.status AS agent_status
                FROM tasks t LEFT JOIN task_assignments a ON a.task_id=t.task_id AND a.ended_at IS NULL
                LEFT JOIN agents g ON g.agent_id=a.agent_id
                WHERE (? IS NULL OR t.task_id=?) ORDER BY t.rowid''', (task_id, task_id)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                assignment = {key: item.pop(key) for key in ('assignment_id', 'agent_id', 'agent_display_name',
                                                               'agent_display_color', 'agent_status')}
                item['current_assignment'] = assignment if assignment['assignment_id'] else None
                result.append(item)
            return result

    def agent_task_ids(self, agent_id):
        with self._connection() as db:
            return [row[0] for row in db.execute(
                'SELECT task_id FROM task_assignments WHERE agent_id=? ORDER BY rowid DESC', (agent_id,))]

    @staticmethod
    def _sync_task(db, agent_id, status):
        assignment = db.execute('SELECT * FROM task_assignments WHERE agent_id=? AND ended_at IS NULL',
                                (agent_id,)).fetchone()
        if assignment is None:
            return
        target = {'running': 'in_progress', 'waiting': 'waiting', 'finished': 'completed', 'stopped': 'pending'}[status]
        db.execute("UPDATE tasks SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=? AND status<>?",
                   (target, assignment['task_id'], target))
        if status in ('finished', 'stopped'):
            db.execute("UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), ended_reason=? WHERE assignment_id=?",
                       ('completed' if status == 'finished' else 'stopped', assignment['assignment_id']))

    def get_task(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            return dict(row) if row else None

    def list_tasks(self):
        with self._connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY rowid')]

    def create_assignment(self, task_id, agent_id):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM task_assignments WHERE ended_at IS NULL AND (task_id=? OR agent_id=?)',
                          (task_id, agent_id)).fetchone():
                raise ValueError('Task or agent already has an active assignment')
            task = db.execute('SELECT status FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task and task['status'] != 'pending':
                raise ValueError('Only pending tasks can be assigned')
            assignment_id = str(uuid4())
            db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id) VALUES (?,?,?)',
                       (assignment_id, task_id, agent_id))
            db.execute("UPDATE tasks SET status='in_progress', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?", (task_id,))
            return dict(db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (assignment_id,)).fetchone())

    def get_active_assignment_for_task(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
            return dict(row) if row else None

    def get_active_assignment_for_agent(self, agent_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM task_assignments WHERE agent_id=? AND ended_at IS NULL', (agent_id,)).fetchone()
            return dict(row) if row else None

    def list_task_assignments(self, task_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_assignments WHERE task_id=? ORDER BY rowid', (task_id,))]

    def end_assignment(self, assignment_id, reason):
        validate_end_reason(reason)
        with self._connection() as db:
            updated = db.execute("""UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), ended_reason=?
                WHERE assignment_id=? AND ended_at IS NULL""", (reason, assignment_id))
            row = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (assignment_id,)).fetchone()
            if updated.rowcount:
                target = reason if reason in ('completed', 'canceled') else 'pending'
                db.execute("UPDATE tasks SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?", (target, row['task_id']))
            return dict(row) if row else None

    def reconcile_task_recovery(self):
        """Startup only: reconcile active assignments without reattaching workers.

        Waiting workers retain their resumable assignment; old running workers
        recover stopped. Unassigned working tasks cannot remain working.
        """
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for agent in db.execute("SELECT agent_id FROM agents WHERE status='running'").fetchall():
                key = agent['agent_id']
                db.execute("UPDATE agents SET status='stopped' WHERE agent_id=?", (key,))
                seq = db.execute('SELECT COALESCE(MAX(sequence)+1,0) FROM output WHERE agent_id=?', (key,)).fetchone()[0]
                marker = ['--- Backend Restart ---',
                          'Agent was running when the backend stopped and cannot be safely reattached. Marked stopped.']
                db.executemany('INSERT INTO output(agent_id,sequence,line) VALUES (?,?,?)',
                               [(key, seq + i, line) for i, line in enumerate(marker)])
            rows = db.execute('''SELECT a.assignment_id, a.task_id, a.agent_id, g.status, t.status AS task_status FROM task_assignments a
                JOIN tasks t ON t.task_id=a.task_id
                JOIN agents g ON g.agent_id=a.agent_id WHERE a.ended_at IS NULL''').fetchall()
            for row in rows:
                status = row['status']
                agent_id = row['agent_id']
                task_status = row['task_status']
                if task_status in ('completed', 'canceled'):
                    db.execute("UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), ended_reason=? WHERE assignment_id=?",
                               (task_status, row['assignment_id']))
                    db.execute("UPDATE agents SET status='stopped' WHERE agent_id=? AND status='waiting'", (agent_id,))
                else:
                    self._sync_task(db, agent_id, status)
            db.execute("""UPDATE tasks SET status='pending', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE status IN ('in_progress','waiting') AND NOT EXISTS
                (SELECT 1 FROM task_assignments a WHERE a.task_id=tasks.task_id AND a.ended_at IS NULL)""")

    @contextmanager
    def _connection(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute('PRAGMA foreign_keys = ON')
                with db:
                    yield db
            finally:
                db.close()

    def save_agent(self, record, output_lines=(), output_entries=None, assignment_task_id=None):
        """Commit metadata and any new output together, retaining creation order."""
        values = dict(record)
        values.setdefault('display_name', default_display_name(values['task']))
        values['display_color'] = validate_display_color(values.get('display_color', 'neutral'))
        values['similar_examples'] = json.dumps(values['similar_examples'], ensure_ascii=False)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing_assignment = None
            if assignment_task_id is not None:
                existing_assignment = db.execute('SELECT assignment_id FROM task_assignments WHERE task_id=? AND agent_id=?',
                                                 (assignment_task_id, record['agent_id'])).fetchone()
                task = db.execute('SELECT * FROM tasks WHERE task_id=?', (assignment_task_id,)).fetchone()
                if task is None:
                    raise LookupError('Task not found')
                if existing_assignment is None and (task['status'] != 'pending' or db.execute(
                        'SELECT 1 FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (assignment_task_id,)).fetchone()):
                    raise ValueError('Only an unassigned pending Task can start an agent')
                if values['task'] != task['description']:
                    raise ValueError('Agent work must match Task description')
            db.execute('''INSERT INTO agents (agent_id, parent_id, task, agent_type, sandbox,
                status, session_id, waiting_question, similar_decisions_enabled, similar_examples,
                always_decide_enabled, always_decide_configured, display_name, display_color)
                VALUES (:agent_id, :parent_id, :task, :agent_type, :sandbox, :status, :session_id,
                :waiting_question, :similar_decisions_enabled, :similar_examples,
                :always_decide_enabled, :always_decide_configured, :display_name, :display_color)
                ON CONFLICT(agent_id) DO UPDATE SET parent_id=excluded.parent_id,
                display_name=excluded.display_name, display_color=excluded.display_color, agent_type=excluded.agent_type, sandbox=excluded.sandbox,
                status=excluded.status, session_id=excluded.session_id, waiting_question=excluded.waiting_question,
                similar_decisions_enabled=excluded.similar_decisions_enabled, similar_examples=excluded.similar_examples,
                always_decide_enabled=excluded.always_decide_enabled,
                always_decide_configured=excluded.always_decide_configured''', values)
            if assignment_task_id is not None and existing_assignment is None:
                db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id) VALUES (?,?,?)',
                           (str(uuid4()), assignment_task_id, record['agent_id']))
            self._sync_task(db, record['agent_id'], record['status'])
            latest = db.execute('SELECT name FROM agent_name_history WHERE agent_id=? ORDER BY id DESC LIMIT 1',
                                (record['agent_id'],)).fetchone()
            if latest is None or latest['name'] != values['display_name']:
                db.execute('INSERT INTO agent_name_history(agent_id, name) VALUES (?, ?)',
                           (record['agent_id'], values['display_name']))
            if output_lines:
                sequence = db.execute('SELECT COALESCE(MAX(sequence)+1, 0) FROM output WHERE agent_id=?',
                                      (record['agent_id'],)).fetchone()[0]
                db.executemany('INSERT INTO output (agent_id, sequence, line) VALUES (?, ?, ?)',
                               [(record['agent_id'], sequence + index, line) for index, line in enumerate(output_lines)])
            if output_entries:
                # Retrying an uncertain commit is safe; never replace existing text.
                for seq, text in output_entries:
                    inserted = db.execute('INSERT INTO output (agent_id, sequence, line) VALUES (?, ?, ?) '
                                          'ON CONFLICT(agent_id, sequence) DO NOTHING',
                                          (record['agent_id'], seq, text))
                    if not inserted.rowcount:
                        existing = db.execute('SELECT line FROM output WHERE agent_id=? AND sequence=?',
                                              (record['agent_id'], seq)).fetchone()[0]
                        if existing != text:
                            raise sqlite3.IntegrityError('Output sequence collision with different text')

    def name_history(self, agent_id):
        with self._connection() as db:
            return [dict(row) for row in db.execute(
                'SELECT name, changed_at FROM agent_name_history WHERE agent_id=? ORDER BY id DESC',
                (agent_id,))]

    def read_output(self, agent_id, limit=OUTPUT_PAGE_SIZE, after=None, before=None):
        if not 1 <= limit <= OUTPUT_MAX_LIMIT or (after is not None and before is not None):
            raise ValueError('Use a limit between 1 and 1000 and only one output cursor')
        if (after is not None and after < 0) or (before is not None and before < 0):
            raise ValueError('Output cursors must be non-negative')
        with self._connection() as db:
            where, params = 'agent_id=?', [agent_id]
            if after is not None:
                where += ' AND sequence>?'
                params.append(after)
            if before is not None:
                where += ' AND sequence<?'
                params.append(before)
            order = 'ASC' if after is not None else 'DESC'
            rows = db.execute(f'SELECT sequence, line FROM output WHERE {where} '
                              f'ORDER BY sequence {order} LIMIT ?', [*params, limit]).fetchall()
            items = sorted(({'seq': row[0], 'text': row[1]} for row in rows), key=lambda item: item['seq'])
            oldest = items[0]['seq'] if items else (before if before is not None else after)
            newest = items[-1]['seq'] if items else after
            has_older = oldest is not None and db.execute(
                'SELECT 1 FROM output WHERE agent_id=? AND sequence<? LIMIT 1', (agent_id, oldest)).fetchone() is not None
            has_newer = newest is not None and db.execute(
                'SELECT 1 FROM output WHERE agent_id=? AND sequence>? LIMIT 1', (agent_id, newest)).fetchone() is not None
            return {'agent_id': agent_id, 'items': items, 'has_older': has_older, 'has_newer': has_newer}

    def full_output(self, agent_id):
        with self._connection() as db:
            return [row[0] for row in db.execute(
                'SELECT line FROM output WHERE agent_id=? ORDER BY sequence', (agent_id,))]

    def load_agents(self, output_limit=None):
        with self._connection() as db:
            records = [dict(row) for row in db.execute('SELECT * FROM agents ORDER BY creation_order')]
            for record in records:
                record['similar_examples'] = json.loads(record['similar_examples'])
                key = record['agent_id']
                record['next_sequence'] = db.execute(
                    'SELECT COALESCE(MAX(sequence)+1, 0) FROM output WHERE agent_id=?', (key,)).fetchone()[0]
                if output_limit is None:
                    record['output'] = [row[0] for row in db.execute(
                        'SELECT line FROM output WHERE agent_id=? ORDER BY sequence', (key,))]
                else:
                    record['output'] = [row[0] for row in db.execute(
                        'SELECT line FROM output WHERE agent_id=? ORDER BY sequence DESC LIMIT ?',
                        (key, output_limit))][::-1]
            return records
