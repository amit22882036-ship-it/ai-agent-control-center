"""Local durable agent records; no process or notification recovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock


class AgentStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('CONTROL_CENTER_DB_PATH')
                         or Path(__file__).resolve().parents[2] / 'data' / 'control_center.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
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
            # Validate versioned databases instead of silently recreating missing tables.
            try:
                db.execute('''SELECT agent_id, parent_id, task, agent_type, sandbox, status,
                    session_id, waiting_question, similar_decisions_enabled, similar_examples,
                    always_decide_enabled, always_decide_configured, creation_order FROM agents LIMIT 0''')
                db.execute('SELECT agent_id, sequence, line FROM output LIMIT 0')
            except sqlite3.DatabaseError as exc:
                raise RuntimeError('Incompatible control-center database schema') from exc

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

    def save_agent(self, record, output_lines=(), truncate_output_to=None):
        """Commit metadata and any new output together, retaining creation order."""
        values = dict(record)
        values['similar_examples'] = json.dumps(values['similar_examples'], ensure_ascii=False)
        with self._connection() as db:
            db.execute('''INSERT INTO agents (agent_id, parent_id, task, agent_type, sandbox,
                status, session_id, waiting_question, similar_decisions_enabled, similar_examples,
                always_decide_enabled, always_decide_configured)
                VALUES (:agent_id, :parent_id, :task, :agent_type, :sandbox, :status, :session_id,
                :waiting_question, :similar_decisions_enabled, :similar_examples,
                :always_decide_enabled, :always_decide_configured)
                ON CONFLICT(agent_id) DO UPDATE SET parent_id=excluded.parent_id,
                task=excluded.task, agent_type=excluded.agent_type, sandbox=excluded.sandbox,
                status=excluded.status, session_id=excluded.session_id, waiting_question=excluded.waiting_question,
                similar_decisions_enabled=excluded.similar_decisions_enabled, similar_examples=excluded.similar_examples,
                always_decide_enabled=excluded.always_decide_enabled,
                always_decide_configured=excluded.always_decide_configured''', values)
            if truncate_output_to is not None:
                db.execute('DELETE FROM output WHERE agent_id=? AND sequence>=?',
                           (record['agent_id'], truncate_output_to))
            if output_lines:
                sequence = db.execute('SELECT COALESCE(MAX(sequence)+1, 0) FROM output WHERE agent_id=?',
                                      (record['agent_id'],)).fetchone()[0]
                db.executemany('INSERT INTO output (agent_id, sequence, line) VALUES (?, ?, ?)',
                               [(record['agent_id'], sequence + index, line) for index, line in enumerate(output_lines)])

    def load_agents(self):
        with self._connection() as db:
            records = [dict(row) for row in db.execute('SELECT * FROM agents ORDER BY creation_order')]
            for record in records:
                record['similar_examples'] = json.loads(record['similar_examples'])
                record['output'] = [row[0] for row in db.execute(
                    'SELECT line FROM output WHERE agent_id=? ORDER BY sequence', (record['agent_id'],))]
            return records
