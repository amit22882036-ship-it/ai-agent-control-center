"""Local durable agent records; no process or notification recovery."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock
from uuid import uuid4
from . import work_control, dependencies, resources, resource_coordination, external_resources, runtime_resources
from .task_domain import task_title, validate_task_status, validate_end_reason
from .agent_names import default_display_name, validate_display_color
from .output_history import OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT
from .project_domain import DEFAULT_ROOT, canonical_path, contains, project_name


class AgentStore:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('CONTROL_CENTER_DB_PATH')
                         or Path(__file__).resolve().parents[2] / 'data' / 'control_center.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16):
                raise RuntimeError(f'Unsupported control-center database schema version: {version}')
            if version < 16:
                db.execute('PRAGMA foreign_keys = OFF')
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
                    version = 5
                if version == 5:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._migrate_task_hierarchy(db)
                    db.execute('PRAGMA user_version = 6')
                    version = 6
                db.execute('SELECT parent_task_id FROM tasks LIMIT 0')
                if version == 6:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._migrate_projects(db)
                    db.execute('PRAGMA user_version = 7')
                    version = 7
                db.execute('SELECT project_id FROM tasks LIMIT 0')
                db.execute('SELECT project_id, name, root_path, root_path_key, created_at, updated_at FROM projects LIMIT 0')
                if version == 7:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._migrate_workspaces(db)
                    db.execute('PRAGMA user_version = 8')
                    version = 8
                db.execute('SELECT workspace_id,task_id,project_id,workspace_path,workspace_path_key,base_snapshot,origin_kind,source_task_id,created_at,updated_at FROM task_workspaces LIMIT 0')
                if version == 8:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    db.execute("""CREATE TABLE IF NOT EXISTS agent_source_context (
                        agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
                        base_snapshot TEXT NOT NULL)""")
                    # No filesystem side effects during migration. Existing v8
                    # sessions remember the source baseline of their assignment.
                    db.execute("""INSERT OR IGNORE INTO agent_source_context(agent_id,base_snapshot)
                        SELECT a.agent_id,w.base_snapshot FROM task_assignments a
                        JOIN task_workspaces w ON w.task_id=a.task_id""")
                    db.execute('PRAGMA user_version = 9')
                    version = 9
                db.execute('SELECT agent_id,base_snapshot FROM agent_source_context LIMIT 0')
                if version == 9:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    self._migrate_integrations(db)
                    db.execute('PRAGMA user_version = 10')
                    version = 10
                db.execute('SELECT integration_order, integration_id, plan FROM integrations LIMIT 0')
                db.execute('SELECT integration_order FROM agent_source_context LIMIT 0')
                if version == 10:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    work_control.migrate(db)
                    db.execute('PRAGMA user_version = 11')
                    version = 11
                db.execute('SELECT control_intent,resume_status FROM tasks LIMIT 0')
                if version == 11:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    dependencies.migrate(db)
                    if db.execute('PRAGMA foreign_key_check').fetchone():
                        raise sqlite3.IntegrityError('Invalid dependency migration references')
                    db.execute('PRAGMA user_version = 12')
                    version = 12
                db.execute('SELECT block_resume_status,stop_required,legacy_pause FROM tasks LIMIT 0')
                db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies LIMIT 0')
                if version == 12:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    resources.migrate(db)
                    if db.execute('PRAGMA foreign_key_check').fetchone():
                        raise sqlite3.IntegrityError('Invalid resource migration references')
                    db.execute('PRAGMA user_version = 13')
                    version = 13
                if version == 13:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    resource_coordination.migrate(db)
                    if db.execute('PRAGMA foreign_key_check').fetchone():
                        raise sqlite3.IntegrityError('Invalid coordination migration references')
                    db.execute('PRAGMA user_version = 14')
                    version = 14
                if version == 14:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    external_resources.migrate(db)
                    if db.execute('PRAGMA foreign_key_check').fetchone():
                        raise sqlite3.IntegrityError('Invalid external resource migration references')
                    db.execute('PRAGMA user_version = 15')
                    version = 15
                if version == 15:
                    if not db.in_transaction:
                        db.execute('BEGIN')
                    runtime_resources.migrate(db)
                    db.execute('PRAGMA user_version = 16')
                db.execute('SELECT ownership_id,generation,state FROM resource_ownership LIMIT 0')
                db.execute('SELECT claim_id,assignment_id,probe_status,probe_checked_at,probe_reason FROM resource_claims LIMIT 0')
                db.execute('SELECT stop_reason FROM tasks LIMIT 0')
                db.execute('SELECT source_type,source_id,waiting_claim_id,owning_claim_id FROM task_blockers LIMIT 0')
                db.execute('SELECT wait_sequence,claim_id,closed_at FROM resource_waits LIMIT 0')
                db.execute('SELECT deadlock_id,signature,edges FROM resource_deadlocks LIMIT 0')
                db.execute('SELECT deadlock_id,edge_kind FROM task_blockers LIMIT 0')
                db.execute('SELECT deadlock_id FROM task_replan_reasons LIMIT 0')
            except sqlite3.DatabaseError as exc:
                raise RuntimeError('Incompatible control-center database schema') from exc

    @staticmethod
    def _migrate_integrations(db):
        db.execute("""CREATE TABLE IF NOT EXISTS integrations (
            integration_order INTEGER PRIMARY KEY AUTOINCREMENT,
            integration_id TEXT NOT NULL UNIQUE,
            source_task_id TEXT NOT NULL REFERENCES tasks(task_id),
            destination_kind TEXT NOT NULL CHECK(destination_kind IN ('task','project')),
            destination_id TEXT NOT NULL, destination_key TEXT NOT NULL,
            base_snapshot TEXT NOT NULL, source_snapshot TEXT NOT NULL,
            destination_before_snapshot TEXT NOT NULL, result_snapshot TEXT,
            status TEXT NOT NULL CHECK(status IN ('preparing','ready','applying','applied','noop',
                'conflict','failed','source_changed','destination_changed','recovery_required')),
            conflict_paths TEXT NOT NULL DEFAULT '[]', changed_paths TEXT NOT NULL DEFAULT '[]',
            plan TEXT NOT NULL DEFAULT '[]',
            changed_paths_remaining INTEGER NOT NULL DEFAULT 0, conflict_paths_remaining INTEGER NOT NULL DEFAULT 0,
            validation_status TEXT NOT NULL DEFAULT 'not_run' CHECK(validation_status='not_run'),
            failure_reason TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            applied_at TEXT
        )""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS integration_destination_active
            ON integrations(destination_key) WHERE status IN ('preparing','ready','applying','recovery_required')""")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS integration_source_active
            ON integrations(source_task_id) WHERE status IN ('preparing','ready','applying','recovery_required')""")
        if 'integration_order' not in {r['name'] for r in db.execute('PRAGMA table_info(agent_source_context)')}:
            db.execute('ALTER TABLE agent_source_context ADD COLUMN integration_order INTEGER NOT NULL DEFAULT 0')

    @staticmethod
    def _integration_record(row):
        if row is None:
            return None
        record = dict(row)
        for field in ('conflict_paths', 'changed_paths', 'plan'):
            record[field] = json.loads(record[field])
        return record

    def get_integration(self, integration_id):
        with self._connection() as db:
            return self._integration_record(db.execute('SELECT * FROM integrations WHERE integration_id=?', (integration_id,)).fetchone())

    def list_integrations(self, task_id=None):
        with self._connection() as db:
            rows = db.execute('SELECT * FROM integrations' + (' WHERE source_task_id=?' if task_id else '') +
                              ' ORDER BY integration_order', (task_id,) if task_id else ())
            return [self._integration_record(r) for r in rows]

    def claim_integration(self, record):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("""SELECT 1 FROM integrations WHERE status IN ('preparing','ready','applying','recovery_required')
                AND (destination_key IN (?,?) OR source_task_id=? OR (?='task' AND source_task_id=?))""",
                (record['destination_key'], 'task:' + record['source_task_id'], record['source_task_id'],
                 record['destination_kind'], record['destination_id'])).fetchone():
                raise ValueError('Destination has an active integration or requires recovery')
            existing = db.execute("""SELECT * FROM integrations WHERE source_task_id=? AND destination_key=?
                AND source_snapshot=? AND status='applied' ORDER BY integration_order DESC LIMIT 1""",
                (record['source_task_id'], record['destination_key'], record['source_snapshot'])).fetchone()
            if existing:
                return self._integration_record(existing)
            fields = ('integration_id','source_task_id','destination_kind','destination_id','destination_key',
                      'base_snapshot','source_snapshot','destination_before_snapshot','status')
            try:
                db.execute('INSERT INTO integrations (' + ','.join(fields) + ') VALUES (?,?,?,?,?,?,?,?,?)',
                           tuple(record[k] for k in fields))
            except sqlite3.IntegrityError:
                raise ValueError('Source or destination already has an active integration') from None
        return self.get_integration(record['integration_id'])

    def update_integration(self, integration_id, status, **fields):
        allowed = {'result_snapshot','conflict_paths','changed_paths','plan','failure_reason','changed_paths_remaining','conflict_paths_remaining'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid integration metadata')
        values = {k: json.dumps(v) if k in ('conflict_paths','changed_paths','plan') else v for k,v in fields.items()}
        with self._connection() as db:
            db.execute("UPDATE integrations SET status=?,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')" +
                       (",applied_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')" if status == 'applied' else '') +
                       ''.join(',' + k + '=?' for k in values) + ' WHERE integration_id=?',
                       (status, *values.values(), integration_id))
        return self.get_integration(integration_id)

    def check_task_integrations(self, task_id):
        with self._connection() as db:
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task is None:
                raise LookupError('Task not found')
            upstream = 'task:' + task['parent_task_id'] if task['parent_task_id'] else 'project:' + str(task['project_id'])
            if db.execute("""SELECT 1 FROM integrations WHERE status IN ('preparing','ready','applying','recovery_required')
                AND (source_task_id=? OR destination_key IN (?,?))""", (task_id, 'task:' + task_id, upstream)).fetchone():
                raise ValueError('Task source has an active integration or requires recovery')

    def session_integrations(self, agent_id, task_id):
        with self._connection() as db:
            row = db.execute('SELECT integration_order FROM agent_source_context WHERE agent_id=?', (agent_id,)).fetchone()
            since = row[0] if row else 0
            return [self._integration_record(r) for r in db.execute("""SELECT * FROM integrations
                WHERE destination_key=? AND status='applied' AND integration_order>? ORDER BY integration_order""",
                ('task:' + task_id, since))]

    def integration_cursor(self, task_id):
        with self._connection() as db:
            return db.execute("SELECT COALESCE(MAX(integration_order),0) FROM integrations WHERE destination_key=? AND status='applied'",
                              ('task:' + task_id,)).fetchone()[0]

    @staticmethod
    def _migrate_workspaces(db):
        # Historical Tasks have no isolated filesystem state to reconstruct.
        db.execute("""CREATE TABLE IF NOT EXISTS task_workspaces (
            workspace_id TEXT PRIMARY KEY NOT NULL,
            task_id TEXT NOT NULL UNIQUE REFERENCES tasks(task_id),
            project_id TEXT NOT NULL REFERENCES projects(project_id),
            workspace_path TEXT NOT NULL, workspace_path_key TEXT NOT NULL UNIQUE,
            base_snapshot TEXT NOT NULL,
            origin_kind TEXT NOT NULL CHECK(origin_kind IN ('project_snapshot','parent_task_snapshot','legacy_project_snapshot')),
            source_task_id TEXT REFERENCES tasks(task_id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")

    def get_task_workspace(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM task_workspaces WHERE task_id=?', (task_id,)).fetchone()
            return dict(row) if row else None

    def update_workspace_base(self, task_id, expected, snapshot):
        with self._connection() as db:
            changed = db.execute("""UPDATE task_workspaces SET base_snapshot=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=? AND base_snapshot=?""",
                (snapshot, task_id, expected))
            if changed.rowcount != 1:
                raise ValueError('Task Workspace base changed during refresh; retry after checking its state')

    def get_agent_source_context(self, agent_id):
        with self._connection() as db:
            row = db.execute('SELECT base_snapshot FROM agent_source_context WHERE agent_id=?', (agent_id,)).fetchone()
            return row[0] if row else None

    def acknowledge_agent_source_context(self, agent_id, snapshot, integration_order=0):
        with self._connection() as db:
            db.execute('INSERT INTO agent_source_context(agent_id,base_snapshot,integration_order) VALUES (?,?,?) ON CONFLICT(agent_id) '
                       'DO UPDATE SET base_snapshot=excluded.base_snapshot,integration_order=excluded.integration_order',
                       (agent_id, snapshot, integration_order))

    def workspace_for_path(self, key):
        with self._connection() as db:
            row = db.execute('SELECT * FROM task_workspaces WHERE workspace_path_key=?', (key,)).fetchone()
            return dict(row) if row else None

    def list_task_workspaces(self):
        with self._connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM task_workspaces')]

    def save_task_workspace(self, record):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (record['task_id'],)).fetchone()
            if task is None or task['project_id'] != record['project_id']:
                raise ValueError('Task Workspace ownership mismatch')
            for project in db.execute('SELECT root_path_key FROM projects'):
                key, root = record['workspace_path_key'], project['root_path_key']
                if contains(key, root) or contains(root, key):
                    raise ValueError('Task Workspace storage must not overlap a canonical Project')
            for existing in db.execute('SELECT workspace_path_key FROM task_workspaces'):
                key, other = record['workspace_path_key'], existing['workspace_path_key']
                if contains(key, other) or contains(other, key):
                    raise ValueError('Task Workspace paths must not overlap')
            if record['source_task_id'] is not None:
                source = db.execute('SELECT * FROM tasks WHERE task_id=?', (record['source_task_id'],)).fetchone()
                if source is None or source['project_id'] != task['project_id'] or source['task_id'] != task['parent_task_id']:
                    raise ValueError('Task Workspace source must be its same-Project parent')
            fields = ('workspace_id','task_id','project_id','workspace_path','workspace_path_key',
                      'base_snapshot','origin_kind','source_task_id')
            db.execute('INSERT INTO task_workspaces(' + ','.join(fields) + ') VALUES (?,?,?,?,?,?,?,?)',
                       tuple(record[key] for key in fields))

    @staticmethod
    def _migrate_projects(db):
        db.execute("""CREATE TABLE IF NOT EXISTS projects (
            project_id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
            root_path TEXT NOT NULL, root_path_key TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )""")
        if 'project_id' not in {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}:
            db.execute('ALTER TABLE tasks ADD COLUMN project_id TEXT REFERENCES projects(project_id)')
        # v6 has no persisted execution cwd. Assignment history, task text and
        # hierarchy cannot supply that evidence: leave every legacy Task unresolved.
        db.execute('CREATE INDEX IF NOT EXISTS task_project ON tasks(project_id)')

    @staticmethod
    def _public_project(row):
        if row is None:
            return None
        return {key: row[key] for key in ('project_id', 'name', 'root_path', 'created_at', 'updated_at')}

    @staticmethod
    def _project_for_key(db, key):
        matches = [row for row in db.execute('SELECT * FROM projects')
                   if contains(row['root_path_key'], key)]
        if len(matches) > 1:
            raise ValueError('Ambiguous Project roots')
        return matches[0] if matches else None

    @classmethod
    def _register_project(cls, db, root, key, name=None, *, ensure=False):
        if ensure:
            existing = cls._project_for_key(db, key)
            if existing is not None:
                return cls._public_project(existing)
        for existing in db.execute('SELECT root_path_key FROM projects'):
            other = existing['root_path_key']
            if contains(other, key) or contains(key, other):
                raise ValueError('Project root duplicates or overlaps a registered Project')
        for existing in db.execute('SELECT workspace_path_key FROM task_workspaces'):
            other = existing['workspace_path_key']
            if contains(other, key) or contains(key, other):
                raise ValueError('Project root must not overlap a Task Workspace')
        project_id = str(uuid4())
        db.execute('INSERT INTO projects(project_id,name,root_path,root_path_key) VALUES (?,?,?,?)',
                   (project_id, name or project_name(Path(root).name or 'Project'), root, key))
        return cls._public_project(db.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone())

    def create_project(self, name, root_path):
        name = project_name(name)
        root, key = canonical_path(root_path, require_directory=True)
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._register_project(db, root, key, name)

    def ensure_project_for_root(self, path):
        root, key = canonical_path(path, require_directory=True)
        with self._connection() as db:
            # Serializes overlap checks across independent connections/processes.
            db.execute('BEGIN IMMEDIATE')
            return self._register_project(db, root, key, ensure=True)

    def resolve_project_for_path(self, path):
        _, key = canonical_path(path)
        with self._connection() as db:
            return self._public_project(self._project_for_key(db, key))

    def get_project(self, project_id):
        with self._connection() as db:
            return self._public_project(db.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone())

    def list_projects(self):
        with self._connection() as db:
            return [self._public_project(row) for row in db.execute('SELECT * FROM projects ORDER BY rowid')]

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

    @staticmethod
    def _migrate_task_hierarchy(db):
        columns = {row['name'] for row in db.execute('PRAGMA table_info(tasks)')}
        if 'parent_task_id' not in columns:
            db.execute('ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(task_id) CHECK(parent_task_id <> task_id)')
        else:
            for row in db.execute('SELECT task_id, parent_task_id FROM tasks').fetchall():
                AgentStore._validate_task_parent(db, row['task_id'], row['parent_task_id'])
        db.execute('CREATE INDEX IF NOT EXISTS task_parent ON tasks(parent_task_id)')

    @staticmethod
    def _validate_task_parent(db, task_id, parent_task_id):
        seen = {task_id}
        current = parent_task_id
        while current is not None:
            if current in seen:
                raise ValueError('Task parent relationship would contain a cycle')
            seen.add(current)
            row = db.execute('SELECT parent_task_id FROM tasks WHERE task_id=?', (current,)).fetchone()
            if row is None:
                raise LookupError('Parent Task not found')
            current = row['parent_task_id']

    def get_task_parent(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT p.* FROM tasks t JOIN tasks p ON p.task_id=t.parent_task_id WHERE t.task_id=?', (task_id,)).fetchone()
            return dependencies.describe(db, row) if row else None

    def get_task_children(self, task_id):
        with self._connection() as db:
            return [dependencies.describe(db, row) for row in db.execute('SELECT * FROM tasks WHERE parent_task_id=? ORDER BY rowid', (task_id,))]

    def get_task_descendants(self, task_id):
        # One snapshot, iterative traversal even for malformed cyclic data.
        children = {}
        for task in self.list_tasks():
            children.setdefault(task['parent_task_id'], []).append(task)
        seen, pending, result = {task_id}, [task_id], []
        while pending:
            for child in children.get(pending.pop(), []):
                key = child['task_id']
                if key not in seen:
                    seen.add(key)
                    result.append(child)
                    pending.append(key)
        return result

    def create_task(self, description, title=None, status='pending', parent_task_id=None,
                    project_id=None, *, root_path=None):
        validate_task_status(status)
        if status in ('in_progress', 'waiting'):
            raise ValueError('Working tasks require an active assignment')
        task_id = str(uuid4())
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._validate_task_parent(db, task_id, parent_task_id)
            if parent_task_id is not None:
                work_control.require_active(db.execute('SELECT * FROM tasks WHERE task_id=?', (parent_task_id,)).fetchone(), db=db)
                parent_project = db.execute('SELECT project_id FROM tasks WHERE task_id=?', (parent_task_id,)).fetchone()[0]
                if parent_project is None:
                    raise ValueError('Parent Task has unresolved Project ownership')
                if project_id is not None and project_id != parent_project:
                    raise ValueError('Child Task must belong to the parent Project')
                project_id = parent_project
            if project_id is None:
                root, key = canonical_path(root_path or DEFAULT_ROOT, require_directory=True)
                project_id = self._register_project(db, root, key, ensure=True)['project_id']
            elif db.execute('SELECT 1 FROM projects WHERE project_id=?', (project_id,)).fetchone() is None:
                raise LookupError('Project not found')
            # Project ensure and Task insertion commit or roll back together.
            db.execute('INSERT INTO tasks(task_id,title,description,status,parent_task_id,project_id) VALUES (?,?,?,?,?,?)',
                       (task_id, task_title(description) if title is None else title, description, status, parent_task_id, project_id))
            if status in ('canceled', 'paused'):
                db.execute('UPDATE tasks SET control_intent=?,resume_status=? WHERE task_id=?', (status, 'pending' if status == 'paused' else None, task_id))
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
                item = dependencies.describe(db, row)
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
        before = dependencies.task(db, assignment['task_id'])
        work_control.sync(db, assignment, status)
        if dependencies.task(db, assignment['task_id']) != before:
            dependencies.reconcile(db)
        runtime_resources.sync(db)

    def get_task(self, task_id):
        with self._connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            return dependencies.describe(db, row) if row else None

    def list_tasks(self):
        with self._connection() as db:
            return [dependencies.describe(db, row) for row in db.execute('SELECT * FROM tasks ORDER BY rowid')]

    def create_assignment(self, task_id, agent_id):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM task_assignments WHERE ended_at IS NULL AND (task_id=? OR agent_id=?)',
                          (task_id, agent_id)).fetchone():
                raise ValueError('Task or agent already has an active assignment')
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if task:
                work_control.require_active(task, pending=True, db=db)
            if task and task['status'] != 'pending':
                raise ValueError('Only pending tasks can be assigned')
            assignment_id = str(uuid4())
            db.execute('INSERT INTO task_assignments(assignment_id,task_id,agent_id) VALUES (?,?,?)',
                       (assignment_id, task_id, agent_id))
            db.execute("UPDATE tasks SET status='in_progress', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?", (task_id,))
            dependencies.reconcile(db)
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
            db.execute('BEGIN IMMEDIATE')
            original = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (assignment_id,)).fetchone()
            task = db.execute('SELECT * FROM tasks WHERE task_id=?', (original['task_id'],)).fetchone() if original else None
            if task and task['control_intent'] != 'active':
                reason = task['control_intent']
            elif task and (task['stop_required'] or dependencies.hard_blocked(db, task['task_id'])):
                reason = resources.end_reason(db, task['task_id'])
            updated = db.execute("""UPDATE task_assignments SET ended_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), ended_reason=?
                WHERE assignment_id=? AND ended_at IS NULL""", (reason, assignment_id))
            row = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (assignment_id,)).fetchone()
            if updated.rowcount:
                target = task['control_intent'] if task['control_intent'] != 'active' else (reason if reason in ('completed', 'canceled', 'paused', 'blocked') else 'pending')
                if reason in ('resource_conflict', 'resource_deadlock', 'external_resource_unavailable'):
                    target = 'blocked'
                if task['status'] == 'blocked' and task['control_intent'] == 'active' and reason not in ('canceled', 'paused', 'completed'):
                    target = 'blocked'
                db.execute("UPDATE tasks SET status=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?", (target, row['task_id']))
                db.execute("UPDATE tasks SET stop_required=0,stop_reason=NULL,block_resume_status=CASE WHEN block_resume_status='waiting' THEN 'pending' ELSE block_resume_status END WHERE task_id=?", (row['task_id'],))
                if target in ('paused', 'canceled'):
                    db.execute('UPDATE tasks SET control_intent=?,resume_status=? WHERE task_id=?',
                               (target, 'pending' if target == 'paused' else None, row['task_id']))
            dependencies.reconcile(db)
            return dict(row) if row else None

    def reconcile_task_recovery(self):
        """Startup only: reconcile active assignments without reattaching workers.

        Waiting workers retain their resumable assignment; old running workers
        recover stopped. Unassigned working tasks cannot remain working.
        """
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            runtime_resources.recover(db)
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
            db.execute("UPDATE tasks SET status=control_intent WHERE control_intent<>'active' AND NOT EXISTS (SELECT 1 FROM task_assignments a WHERE a.task_id=tasks.task_id AND a.ended_at IS NULL)")
            db.execute("""UPDATE tasks SET status='pending', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
                WHERE control_intent='active' AND status IN ('in_progress','waiting') AND NOT EXISTS
                (SELECT 1 FROM task_assignments a WHERE a.task_id=tasks.task_id AND a.ended_at IS NULL)""")

            dependencies.reconcile(db)
            dependencies.finish_operations(db)

    def request_work_control(self, task_id, intent):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            dependencies.begin_control(db, task_id, intent)
        return self.get_task(task_id)

    def finalize_work_control(self, task_id):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            assignment = db.execute('SELECT * FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (task_id,)).fetchone()
            if assignment:
                status = db.execute('SELECT status FROM agents WHERE agent_id=?', (assignment['agent_id'],)).fetchone()[0]
                work_control.sync(db, assignment, status)
            else:
                db.execute("UPDATE tasks SET status=control_intent WHERE task_id=? AND control_intent<>'active'", (task_id,))
            dependencies.reconcile(db)
        return self.get_task(task_id)

    def change_dependency(self, task_id, source_id, remove=False):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            dependencies.change_edge(db, task_id, source_id, remove)
        return self.get_task(task_id)

    def create_resource_claim(self, task_id, **options):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            task = dependencies.task(db, task_id)
            identifier = resources.create(db, task, **options)
            dependencies.reconcile(db, triggering_claim_id=identifier)
        return identifier

    def release_resource_claim(self, task_id, claim_id):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            dependencies.task(db, task_id)
            resources.release(db, task_id, claim_id)
            dependencies.reconcile(db)

    def preflight_task(self, task_id):
        # Commit observations/gates even when the caller subsequently rejects
        # execution. Reads never call this method.
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            dependencies.task(db, task_id)
            dependencies.reconcile(db, preflight_tasks={task_id})
        return self.get_task(task_id)

    def resource_deadlocks(self, *, task_id=None, deadlock_id=None):
        with self._connection() as db:
            db.execute('BEGIN')
            if task_id is not None:
                dependencies.task(db, task_id)
            result = resource_coordination.inspect(db, task_id=task_id, deadlock_id=deadlock_id)
            if deadlock_id is not None:
                if not result:
                    raise LookupError('Resource deadlock not found')
                return result[0]
            return result

    def unsettled_tasks(self):
        with self._connection() as db:
            return [r[0] for r in db.execute('SELECT task_id FROM tasks WHERE stop_required=1')]

    def resource_claims(self, task_id):
        with self._connection() as db:
            db.execute('BEGIN')
            dependencies.task(db, task_id)
            return resources.inspect(db, task_id)

    def list_dependencies(self, task_id, reverse=False):
        with self._connection() as db:
            dependencies.task(db, task_id)
            column = 'depends_on_task_id' if reverse else 'task_id'
            return [dict(r) for r in db.execute(f'SELECT * FROM task_dependencies WHERE {column}=? ORDER BY rowid', (task_id,))]

    def control_impact(self, task_id, action):
        with self._connection() as db:
            db.execute('BEGIN')
            return dependencies.impact(db, task_id, action)

    def control_operations(self, task_id):
        with self._connection() as db:
            dependencies.task(db, task_id)
            result = []
            for r in db.execute('SELECT * FROM task_control_operations WHERE root_task_id=? ORDER BY rowid', (task_id,)):
                item = dict(r)
                item['impact'] = json.loads(item['impact'])
                item['failures'] = json.loads(item['failures'])
                result.append(item)
            return result

    def finish_control_operations(self, failures=()):
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            dependencies.reconcile(db)
            dependencies.finish_operations(db, failures)

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
                if existing_assignment is None:
                    work_control.require_active(task, pending=True, db=db)
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
                db.execute('INSERT OR IGNORE INTO agent_source_context(agent_id,base_snapshot,integration_order) '
                           'SELECT ?,base_snapshot,(SELECT COALESCE(MAX(integration_order),0) FROM integrations '
                           "WHERE destination_key=? AND status='applied') FROM task_workspaces WHERE task_id=?",
                           (record['agent_id'], 'task:' + assignment_task_id, assignment_task_id))
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
