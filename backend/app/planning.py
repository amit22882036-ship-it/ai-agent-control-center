"""Non-executing, provider-neutral planning revisions. Callers own transactions."""
import json
import re
import unicodedata

from . import delegation_protocol, dependencies, work_control

PROTOCOL = 'control-center.planning.v1'
MAX_PAYLOAD = 65536
MAX_ITEMS = 16
MAX_DECISIONS = 16
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"


def text(value, limit=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(c) in ('Cs', 'Cf', 'Cc') and c not in '\n\t' for c in value)):
        raise ValueError('Expected bounded planning text without unsafe control characters')
    return value


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('Invalid planning identity')
    return value


def is_envelope(message):
    # Only complete object-shaped assistant messages, never prose or fences.
    if not isinstance(message, str) or not message.lstrip().startswith('{'):
        return False
    try:
        value = delegation_protocol.decode(message)
    except (ValueError, RecursionError):
        return False  # Existing structured control detection rejects malformed objects.
    return isinstance(value, dict) and isinstance(value.get('protocol'), str) and value['protocol'].startswith('control-center.planning.')


def validate(message):
    try:
        if not isinstance(message, str) or len(message.encode('utf-8')) > MAX_PAYLOAD:
            raise ValueError('Planning payload exceeds 64 KiB')
        value = delegation_protocol.decode(message)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('Malformed or oversized planning payload') from exc
    if not isinstance(value, dict) or set(value) != {'protocol', 'decision_key', 'base_revision', 'decision', 'rationale', 'items', 'references'}:
        raise ValueError('Invalid planning envelope fields')
    if value['protocol'] != PROTOCOL:
        raise ValueError('Unsupported planning protocol')
    key(value['decision_key'])
    if type(value['base_revision']) is not int or not 0 <= value['base_revision'] < 2147483647:
        raise ValueError('Invalid base revision')
    if value['decision'] not in ('independent', 'propose'):
        raise ValueError('Invalid planning decision')
    text(value['rationale'])
    items = value['items']
    if not isinstance(items, list) or len(items) > MAX_ITEMS or bool(items) != (value['decision'] == 'propose'):
        raise ValueError('Independent decisions have no items; proposals have 1-16 items')
    edges = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {'proposal_id', 'deliverable', 'responsibility', 'depends_on', 'parallel'}:
            raise ValueError('Invalid proposed item fields')
        identifier = key(item['proposal_id'])
        if identifier in edges:
            raise ValueError('Duplicate proposal identity')
        text(item['deliverable'])
        text(item['responsibility'])
        deps = item['depends_on']
        if not isinstance(deps, list) or len(deps) > MAX_ITEMS or type(item['parallel']) is not bool:
            raise ValueError('Invalid proposed dependencies or parallel flag')
        for dep in deps:
            key(dep)
        if len(set(deps)) != len(deps):
            raise ValueError('Duplicate proposed dependency')
        edges[identifier] = deps
    def visit(identifier, active, done):
        if identifier not in edges:
            raise ValueError('Unknown proposal dependency')
        if identifier in active:
            raise ValueError('Cyclic proposal dependency')
        if identifier not in done:
            for dep in edges[identifier]:
                visit(dep, active | {identifier}, done)
            done.add(identifier)
    done = set()
    for identifier in edges:
        visit(identifier, set(), done)
    refs = value['references']
    if not isinstance(refs, list) or len(refs) > MAX_ITEMS:
        raise ValueError('Invalid existing-work references')
    seen = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {'task_id'}:
            raise ValueError('Only existing Task references are supported')
        from .delegation_continuation import identifier
        identifier(ref['task_id'])
        if ref['task_id'] in seen:
            raise ValueError('Duplicate existing-work reference')
        seen.add(ref['task_id'])
    return value


def migrate(db):
    if 'planning_enabled' not in {r['name'] for r in db.execute('PRAGMA table_info(agents)')}:
        db.execute('ALTER TABLE agents ADD COLUMN planning_enabled INTEGER NOT NULL DEFAULT 0 CHECK(planning_enabled IN (0,1))')
    db.execute(f'''CREATE TABLE IF NOT EXISTS planning_revisions (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), revision INTEGER NOT NULL CHECK(revision>0),
        project_id TEXT NOT NULL REFERENCES projects(project_id),
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        generation TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
        decision_key TEXT NOT NULL, content TEXT NOT NULL,
        accepted_at TEXT NOT NULL DEFAULT ({NOW}),
        PRIMARY KEY(task_id,revision), UNIQUE(task_id,decision_key), UNIQUE(task_id,generation,message_id))''')


def history(db, task_id):
    dependencies.task(db, task_id)
    return [{**dict(row), 'content': json.loads(row['content'])} for row in db.execute(
        'SELECT * FROM planning_revisions WHERE task_id=? ORDER BY revision', (task_id,))]


def current(db, task_id):
    dependencies.task(db, task_id)
    row = db.execute('SELECT * FROM planning_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1', (task_id,)).fetchone()
    return {**dict(row), 'content': json.loads(row['content'])} if row else None


def accept(db, origin, message, *, session_id, message_id):
    value = validate(message)
    text(message_id, 128)
    task = dependencies.task(db, origin.parent_task_id)
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (origin.agent_id,)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (origin.assignment_id,)).fetchone()
    if (not agent or not agent['planning_enabled'] or agent['execution_generation'] != origin.generation
            or not session_id or agent['session_id'] != session_id or not assignment
            or assignment['agent_id'] != origin.agent_id or assignment['task_id'] != task['task_id']
            or task['project_id'] != origin.project_id):
        raise ValueError('Stale or mismatched planning execution authority')
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    previous = db.execute('SELECT * FROM planning_revisions WHERE task_id=? AND decision_key=?',
                          (task['task_id'], value['decision_key'])).fetchone()
    if previous:
        if previous['content'] != encoded:
            raise ValueError('Conflicting replay of planning decision')
        # Read-only receipt recovery is allowed after completion; no authority is restored.
        return {**dict(previous), 'content': value}
    if assignment['ended_at'] is not None or agent['status'] != 'running':
        raise ValueError('Planning requires a running current assignment')
    work_control.require_active(task, db=db)
    latest = current(db, task['task_id'])
    if value['base_revision'] != (latest['revision'] if latest else 0):
        raise ValueError('Stale planning base revision')
    # References are conservative: same Project and same Task tree only.
    root = work_control.ancestry(db, task['task_id'])[-1]['task_id']
    for ref in value['references']:
        target = dependencies.task(db, ref['task_id'])
        if target['project_id'] != task['project_id'] or work_control.ancestry(db, ref['task_id'])[-1]['task_id'] != root:
            raise ValueError('Existing-work reference is outside the authorized Task tree')
    db.execute('''INSERT INTO planning_revisions(task_id,revision,project_id,agent_id,assignment_id,
        generation,session_id,message_id,decision_key,content) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (task['task_id'], value['base_revision'] + 1, task['project_id'], origin.agent_id, origin.assignment_id,
         origin.generation, session_id, message_id, value['decision_key'], encoded))
    return current(db, task['task_id'])


OPTIONAL_LEAD = '''Planning is optional; continue ordinary work. Consider independent deliverables, dependencies,
parallel benefit and coordination cost. Do not delegate merely because a task is long.
If useful, emit'''


def instruction(revision, lead=OPTIONAL_LEAD):
    title = 'Optional planning metadata' if lead == OPTIONAL_LEAD else 'Planning metadata'
    return f'''\n\n{title} ({PROTOCOL}); current accepted revision: {revision}.
{lead} a complete standalone assistant JSON message with exactly:
protocol, decision_key (stable ASCII key), base_revision ({revision}), decision (independent or propose),
rationale (nonblank, <=2000 characters), items, references.
Independent decisions have items=[]; propose has 1-16 items with exactly proposal_id (stable ASCII key),
deliverable, responsibility (each nonblank <=2000 characters), depends_on (proposal IDs), parallel (boolean).
references is [] or up to 16 objects containing only task_id for known existing Tasks in this Task tree.
Keys use 1-64 ASCII letters, digits, underscore, dot or hyphen, starting with a letter or digit.
This is a descriptive proposal only: it grants no execution, delegation or ownership permission.
Do not invent existing Task IDs. Do not wrap the JSON in fences. Do not emit planning as your final
response: continue the actual work and finish with an ordinary response or an independently authorized
control action. If you cannot emit an intermediate assistant message, skip planning entirely.
At most 16 decisions per invocation, 64 KiB each. Revisions replace the proposed item set only;
preserve proposal IDs for retained items. Increment base_revision for each additional decision.
Never make another model call solely to plan. A plan-only final response is not Task completion.
'''
