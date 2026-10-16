"""Plan-to-Delegation provenance. Existing Tasks/claims own execution authority.

Mutation helpers require the caller's BEGIN IMMEDIATE transaction. No process,
filesystem, provider call, or startup scheduler is introduced here.
"""
import json
from uuid import UUID, uuid5

from . import planning, delegation_protocol, delegation_continuation, delegation_handoff
from . import delegation_materialization, delegations, dependencies, work_control, work_intents, resources

PROTOCOL = 'control-center.promotion.v1'
MAX_CONTEXT = 16384


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def is_envelope(message):
    if not isinstance(message, str) or not message.lstrip().startswith('{'):
        return False
    try:
        value = delegation_protocol.decode(message)
    except (ValueError, RecursionError):
        return False
    return isinstance(value, dict) and isinstance(value.get('protocol'), str) and value['protocol'].startswith('control-center.promotion.')


def validate(message):
    if not isinstance(message, str) or len(message.encode('utf-8')) > planning.MAX_PAYLOAD:
        raise ValueError('Promotion exceeds 64 KiB')
    value = delegation_protocol.decode(message)
    if not isinstance(value, dict) or set(value) != {'protocol', 'task_id', 'revision', 'decision_key', 'items', 'continuation'}:
        raise ValueError('Invalid promotion fields')
    if value['protocol'] != PROTOCOL:
        raise ValueError('Unsupported promotion protocol')
    delegation_continuation.identifier(value['task_id'])
    planning.key(value['decision_key'])
    if type(value['revision']) is not int or not 1 <= value['revision'] < 2147483647:
        raise ValueError('Invalid accepted plan revision')
    items = value['items']
    if not isinstance(items, list) or not 1 <= len(items) <= planning.MAX_ITEMS:
        raise ValueError('Promotion requires 1-16 selected items')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {'proposal_id', 'scope', 'resources', 'depends_on_task_ids', 'reuse_task_id'}:
            raise ValueError('Invalid promotion item fields')
        key = planning.key(item['proposal_id'])
        if key in seen:
            raise ValueError('Duplicate selected proposal')
        seen.add(key)
        scope = item['scope']
        if scope is not None:
            if not isinstance(scope, dict) or set(scope) != {'namespace', 'key', 'from_intent_id'}:
                raise ValueError('Invalid responsibility scope')
            work_intents.normalize(scope['namespace'], scope['key'])
            if scope['from_intent_id'] is not None:
                delegation_continuation.identifier(scope['from_intent_id'])
        claims = item['resources']
        if not isinstance(claims, list) or len(claims) > 8:
            raise ValueError('At most eight resource declarations per item')
        for claim in claims:
            if not isinstance(claim, dict) or set(claim) != {'resource_type', 'resource_key', 'scope', 'mode', 'recursive'}:
                raise ValueError('Invalid resource declaration fields')
            if any(not isinstance(claim[k], str) for k in ('resource_type', 'resource_key', 'scope', 'mode')) or type(claim['recursive']) is not bool:
                raise ValueError('Invalid resource declaration types')
        refs = item['depends_on_task_ids']
        if not isinstance(refs, list) or len(refs) > 16:
            raise ValueError('At most sixteen Task prerequisites')
        for ref in refs:
            delegation_continuation.identifier(ref)
        if len(set(refs)) != len(refs):
            raise ValueError('Duplicate Task prerequisite')
        if item['reuse_task_id'] is not None:
            delegation_continuation.identifier(item['reuse_task_id'])
    # Reuse the closed v2 continuation contract without extending it.
    delegation_continuation.validate(encoded(dict(protocol=delegation_continuation.PROTOCOL,
        action='request_delegations', requests=[dict(request_key='validation', instruction='validation')],
        continuation=value['continuation'])))
    return value


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS promotion_decisions (
        task_id TEXT NOT NULL, decision_key TEXT NOT NULL, revision INTEGER NOT NULL,
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        generation TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
        content TEXT NOT NULL, receipt TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(task_id,decision_key), UNIQUE(task_id,generation,message_id),
        FOREIGN KEY(task_id,revision) REFERENCES planning_revisions(task_id,revision))''')
    db.execute('''CREATE TABLE IF NOT EXISTS promotion_items (
        task_id TEXT NOT NULL, proposal_id TEXT NOT NULL, decision_key TEXT NOT NULL,
        revision INTEGER NOT NULL, proposal TEXT NOT NULL, coordination TEXT NOT NULL,
        delegation_id TEXT NOT NULL UNIQUE REFERENCES delegations(delegation_id),
        disposition TEXT NOT NULL CHECK(disposition IN ('pending','ready','blocked','conflict','unauthorized','reuse_available','invalid')), reason TEXT NOT NULL,
        PRIMARY KEY(task_id,proposal_id),
        FOREIGN KEY(task_id,decision_key) REFERENCES promotion_decisions(task_id,decision_key),
        FOREIGN KEY(task_id,revision) REFERENCES planning_revisions(task_id,revision))''')


def _origin(db, origin, session_id):
    task = dependencies.task(db, origin.parent_task_id)
    agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (origin.agent_id,)).fetchone()
    assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (origin.assignment_id,)).fetchone()
    if (not agent or not all(agent[k] for k in ('planning_enabled', 'delegation_protocol_enabled',
            'child_materialization_enabled', 'hybrid_delegation_enabled'))
            or agent['agent_type'] != 'codex' or agent['sandbox'] not in ('read-only', 'workspace-write')
            or agent['execution_generation'] != origin.generation or not session_id or agent['session_id'] != session_id
            or not assignment or assignment['agent_id'] != origin.agent_id or assignment['task_id'] != task['task_id']
            or task['project_id'] != origin.project_id):
        raise ValueError('Promotion authority is missing or stale')
    return task, agent, assignment


def accept(db, origin, message, *, session_id, message_id):
    value = validate(message)
    planning.text(message_id, 128)
    task, agent, assignment = _origin(db, origin, session_id)
    if value['task_id'] != task['task_id']:
        raise ValueError('Promotion Task does not match trusted execution origin')
    previous = db.execute('SELECT * FROM promotion_decisions WHERE task_id=? AND decision_key=?',
                          (task['task_id'], value['decision_key'])).fetchone()
    if previous:
        if previous['content'] != encoded(value):
            raise ValueError('Conflicting promotion decision replay')
        return json.loads(previous['receipt'])  # Receipt only, never another launch.
    if assignment['ended_at'] is not None or agent['status'] != 'running':
        raise ValueError('Promotion requires a running current assignment')
    work_control.require_active(task, db=db)
    latest = planning.current(db, task['task_id'])
    if not latest or latest['revision'] != value['revision']:
        raise ValueError('Promotion must reference the current accepted plan revision')
    proposals = {p['proposal_id']: p for p in latest['content']['items']}
    graph = {r['proposal_id']: json.loads(r['proposal'])['depends_on'] for r in db.execute(
        'SELECT proposal_id,proposal FROM promotion_items WHERE task_id=?', (task['task_id'],))}
    for item in value['items']:
        from . import replanning
        if replanning.withdrawn(db, task['task_id'], item['proposal_id']):
            raise ValueError('Proposal was explicitly withdrawn; use a new identity')
        if item['proposal_id'] not in proposals:
            raise ValueError('Unknown selected proposal')
        graph.setdefault(item['proposal_id'], proposals[item['proposal_id']]['depends_on'])
    # Iterative traversal avoids a Python recursion-depth lifetime round limit.
    visited, active = set(), set()
    for key in graph:
        stack = [(key, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                active.remove(node)
                visited.add(node)
            elif node in active:
                raise ValueError('Proposed dependencies conflict with durable promotion history: cycle')
            elif node not in visited:
                active.add(node)
                stack.append((node, True))
                stack.extend((dep, False) for dep in graph.get(node, ()))
    requests = []
    for item in value['items']:
        if item['proposal_id'] not in proposals:
            raise ValueError('Unknown selected proposal')
        # Validate the entire structural request, including resource identities,
        # before writing any admission or handoff state.
        normalized = [resources.normalize(db, task, **c) for c in item['resources']]
        if len({encoded(c) for c in normalized}) != len(normalized):
            raise ValueError('Duplicate resource declaration')
        for ref in item['depends_on_task_ids'] + ([item['reuse_task_id']] if item['reuse_task_id'] else []):
            target = dependencies.task(db, ref)
            if target['project_id'] != task['project_id']:
                raise ValueError('Task reference is outside the originating Project')
            if ref in {p['task_id'] for p in work_control.ancestry(db, task['task_id'])}:
                raise ValueError('Child cannot require its own Parent or ancestor completion')
        old = db.execute('SELECT * FROM promotion_items WHERE task_id=? AND proposal_id=?',
                         (task['task_id'], item['proposal_id'])).fetchone()
        if old:
            if old['proposal'] != encoded(proposals[item['proposal_id']]) or old['coordination'] != encoded(item):
                raise ValueError('Previously promoted responsibility is immutable; active replanning is not supported')
            record = delegations.get(db, old['delegation_id'])
            requests.append(dict(request_key=record['request_key'], instruction=record['instruction']))
        else:
            proposal = proposals[item['proposal_id']]
            requests.append(dict(request_key='promotion-' + str(uuid5(UUID(task['task_id']), item['proposal_id'])),
                instruction=proposal['deliverable'] + '\nResponsibility: ' + proposal['responsibility']))
    receipt = delegation_handoff.accept(db, origin, encoded(dict(protocol=delegation_continuation.PROTOCOL,
        action='request_delegations', requests=requests, continuation=value['continuation'])),
        session_id=session_id, message_id=message_id)
    db.execute('''INSERT INTO promotion_decisions(task_id,decision_key,revision,agent_id,assignment_id,
        generation,session_id,message_id,content,receipt) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (task['task_id'], value['decision_key'], value['revision'], origin.agent_id, origin.assignment_id,
         origin.generation, session_id, message_id, encoded(value), encoded(receipt)))
    for item, request in zip(value['items'], requests):
        record = db.execute('SELECT delegation_id FROM delegations WHERE parent_task_id=? AND request_key=?',
                            (task['task_id'], request['request_key'])).fetchone()
        db.execute('''INSERT OR IGNORE INTO promotion_items(task_id,proposal_id,decision_key,revision,
            proposal,coordination,delegation_id,disposition,reason) VALUES (?,?,?,?,?,?,?,'pending','Admission pending')''',
            (task['task_id'], item['proposal_id'], value['decision_key'], value['revision'],
             encoded(proposals[item['proposal_id']]), encoded(item), record[0]))
    # All selected identities exist before dependency resolution; attachment,
    # real claims and provenance commit atomically with the accepted handoff.
    for item in value['items']:
        row = db.execute('SELECT delegation_id FROM promotion_items WHERE task_id=? AND proposal_id=?',
                         (task['task_id'], item['proposal_id'])).fetchone()
        prepare(db, row[0])
    return receipt


def row_for(db, delegation_id):
    return db.execute('SELECT * FROM promotion_items WHERE delegation_id=?', (delegation_id,)).fetchone()


def _set(db, identifier, disposition, reason):
    db.execute('UPDATE promotion_items SET disposition=?,reason=? WHERE delegation_id=?', (disposition, reason, identifier))
    db.execute('UPDATE delegations SET admission_error=? WHERE delegation_id=?',
               (None if disposition == 'ready' else reason, identifier))
    if disposition in ('unauthorized', 'invalid'):
        progress = delegation_materialization.inspect(db, identifier)
        record = delegations.get(db, identifier)
        child_id = record['child_task_id']
        if (child_id and progress and progress['phase'] in ('attached', 'workspace_ready')
                and not db.execute('SELECT 1 FROM task_assignments WHERE task_id=?', (child_id,)).fetchone()):
            # No Worker was admitted. Never use this path for uncertain launches.
            for intent in work_intents.inspect(db, child_id):
                work_intents.release(db, child_id, intent['intent_id'])
            for claim in resources.inspect(db, child_id):
                resources.release(db, child_id, claim['claim_id'])
            dependencies.reconcile(db)


def prepare(db, identifier, limits=None):
    row = row_for(db, identifier)
    if row is None:
        return None
    record = delegations.get(db, identifier)
    progress = delegation_materialization.inspect(db, identifier)
    if progress and progress['phase'] in ('launching', 'started', 'recovery_required'):
        return record  # Never amend responsibilities beneath an admitted Worker.
    if row['disposition'] in ('unauthorized', 'reuse_available', 'invalid'):
        return record
    item, proposal = json.loads(row['coordination']), json.loads(row['proposal'])
    scope = item['scope']
    if scope is None:
        _set(db, identifier, 'conflict', 'Responsibility is ambiguous; explicit structured scope required')
        return record
    if item['reuse_task_id']:
        target = dependencies.task(db, item['reuse_task_id'])
        _set(db, identifier, 'reuse_available' if target['status'] == 'completed' else 'conflict',
             'Existing work requires explicit result review; it is not automatically accepted or re-executed')
        return record
    parent = dependencies.task(db, row['task_id'])
    namespace, key = work_intents.normalize(scope['namespace'], scope['key'])
    source = scope['from_intent_id']
    if source:
        intent = db.execute('SELECT * FROM work_intents WHERE intent_id=?', (source,)).fetchone()
        ancestors = {p['task_id'] for p in work_control.ancestry(db, parent['task_id'])}
        if (not intent or intent['task_id'] not in ancestors or intent['project_id'] != parent['project_id']
                or intent['namespace'] != namespace or not work_intents.contains(intent['normalized_key'], key)
                or intent['status'] != 'active' or not intent['holds_authority']):
            _set(db, identifier, 'unauthorized', 'Scope source is not an authoritative containing ancestor intent')
            return record
    # Known equal/containing scopes use the existing Project-local intent model.
    # No prose similarity or cross-Project Task data is used as authority.
    candidate = dict(task_id=record['child_task_id'], project_id=parent['project_id'], namespace=namespace,
                     normalized_key=key, delegated_from_intent_id=source, intent_id='candidate')
    intents = work_intents.rows(db)
    by_id = {r['intent_id']: r for r in intents}
    for other in intents:
        if not work_intents.overlaps(candidate, other):
            continue
        if other['status'] == 'released':
            if (record['child_task_id'] is None and other['normalized_key'] == key
                    and dependencies.task(db, other['task_id'])['status'] == 'completed'):
                _set(db, identifier, 'reuse_available', 'Completed Project work declares the same scope; inspect results before allocating duplicate work')
                return record
            continue
        if work_intents.delegated(candidate, other, by_id):
            continue
        _set(db, identifier, 'conflict', 'Known responsibility overlaps existing Project work; inspect work intents')
        return record
    db.execute('SAVEPOINT promotion_item')
    try:
        record = delegation_materialization.attach(db, identifier, limits)
        child = dependencies.task(db, record['child_task_id'])
        work_intents.create(db, child, namespace, key, 'single_owner', source)
        for claim in item['resources']:
            resources.create(db, child, **claim)
        missing = []
        prerequisites = list(item['depends_on_task_ids'])
        for dependency in proposal['depends_on']:
            target = db.execute('''SELECT d.child_task_id,d.status FROM promotion_items p JOIN delegations d USING(delegation_id)
                WHERE p.task_id=? AND p.proposal_id=?''', (row['task_id'], dependency)).fetchone()
            if not target or not target[0]:
                missing.append(dependency)
            else:
                prerequisites.append(target[0])
                if target['status'] != 'acknowledged':
                    missing.append(dependency)
        for target in prerequisites:
            dependencies.change_edge(db, child['task_id'], target)
        # Unknown proposal dependencies are explicit blockers, not fake Tasks.
        db.execute("UPDATE task_blockers SET active=0,resolved_at=" + delegations.NOW +
                   " WHERE task_id=? AND blocker_type='promotion' AND active=1", (child['task_id'],))
        if missing:
            from uuid import uuid4
            db.execute('''INSERT INTO task_blockers(id,task_id,blocker_type,reason_code)
                VALUES (?,?,'promotion','proposal_dependency_unavailable')''', (str(uuid4()), child['task_id']))
        dependencies.reconcile(db)
        child = dependencies.task(db, child['task_id'])
        if missing:
            _set(db, identifier, 'blocked', ('Proposal prerequisites lack accepted integrated results: ' + ', '.join(missing))[:500])
        elif dependencies.hard_blocked(db, child['task_id']) or resources.pending(db, child['task_id']):
            _set(db, identifier, 'blocked', 'Current Task dependency or resource gates prevent launch; inspect Task blockers and claims')
        else:
            _set(db, identifier, 'ready', 'Admitted responsibility; Worker launch still requires all runtime gates')
        db.execute('RELEASE promotion_item')
    except (ValueError, LookupError) as exc:
        db.execute('ROLLBACK TO promotion_item')
        db.execute('RELEASE promotion_item')
        # Invalid dependency cycles are definitive; operational gates remain
        # inspectable and can be rechecked by an existing bounded backend pass.
        invalid = 'cycle' in str(exc).lower()
        _set(db, identifier, 'invalid' if invalid else 'blocked', str(exc)[:500])
    return delegations.get(db, identifier)


def require_ready(db, identifier):
    from . import replanning
    replanning.hold_replacement(db, identifier)
    row = row_for(db, identifier)
    if row and row['disposition'] != 'ready':
        raise ValueError(row['reason'])
    if not row:
        return
    progress = delegation_materialization.inspect(db, identifier)
    if progress and progress['phase'] in ('started', 'recovery_required'):
        return  # Existing materialization journal prevents another launch.
    record = delegations.get(db, identifier)
    child = dependencies.task(db, record['child_task_id'])
    item = json.loads(row['coordination'])
    scope = item['scope']
    namespace, key = work_intents.normalize(scope['namespace'], scope['key'])
    if scope['from_intent_id']:
        source = db.execute('SELECT status,holds_authority FROM work_intents WHERE intent_id=?', (scope['from_intent_id'],)).fetchone()
        if not source or source['status'] != 'active' or not source['holds_authority']:
            raise ValueError('Promotion scope source is no longer authoritative')
    if not any(r['namespace'] == namespace and r['normalized_key'] == key
               and r['status'] == 'active' and r['holds_authority'] and r['mode'] == 'single_owner'
               for r in work_intents.inspect(db, child['task_id'])):
        raise ValueError('Promotion responsibility is no longer authoritative')
    work_control.require_active(child, db=db)
    declared = resources.inspect(db, child['task_id'])
    for claim in item['resources']:
        normalized = resources.normalize(db, child, **claim)
        if not any(r['status'] == 'active' and all(r[k] == v for k, v in normalized.items()) for r in declared):
            raise ValueError('Promotion resource declaration is no longer granted')
    prerequisites = list(item['depends_on_task_ids'])
    for key in json.loads(row['proposal'])['depends_on']:
        target = db.execute('''SELECT d.child_task_id,d.status FROM promotion_items p JOIN delegations d USING(delegation_id)
            WHERE p.task_id=? AND p.proposal_id=?''', (row['task_id'], key)).fetchone()
        if not target or not target[0] or target['status'] != 'acknowledged':
            raise ValueError('Proposed prerequisite lacks an accepted integrated result')
        prerequisites.append(target[0])
    for target in prerequisites:
        task = dependencies.task(db, target)
        if task['status'] != 'completed' or task['control_intent'] != 'active':
            raise ValueError('Promotion prerequisite is not successfully completed')


def inspect(db, task_id, limit=None):
    result = []
    query = 'SELECT * FROM promotion_items WHERE task_id=? ORDER BY rowid'
    params = (task_id,)
    if limit is not None:
        query += ' DESC LIMIT ?'
        params += (limit,)
    for row in db.execute(query, params):
        value = dict(row)
        record = delegations.get(db, row['delegation_id'])
        value.update(child_task_id=record['child_task_id'], delegation_status=record['status'],
                     materialization=record.get('materialization'))
        child = dependencies.task(db, record['child_task_id']) if record['child_task_id'] else None
        value['child_status'] = child['status'] if child else None
        value['admission_disposition'] = value['disposition']
        if record['status'] in ('acknowledged', 'canceled', 'result_ready'):
            value['disposition'] = record['status']
        elif record.get('materialization'):
            progress = record['materialization']
            if progress['phase'] in ('launching', 'started', 'recovery_required'):
                value['disposition'] = progress['phase']
                if progress['phase'] == 'started' and child:
                    value['disposition'] = 'completed_pending_review' if child['status'] == 'completed' else child['status']
                    value['reason'] = 'Child lifecycle and result review are governed by the existing Task and Delegation journals'
            elif progress['last_error']:
                value['disposition'] = 'blocked'
                value['reason'] = progress['last_error']
            elif child and (child['status'] in ('blocked', 'paused', 'canceled') or child['control_intent'] != 'active'):
                value['disposition'] = child['control_intent'] if child['control_intent'] != 'active' else child['status']
        value['proposal'] = json.loads(value['proposal'])
        value['coordination'] = json.loads(value['coordination'])
        result.append(value)
    return result


def context(db, task_id):
    # Relevant declarations only; machine-global contention conveys no foreign
    # Project data. The size limit covers the entire serialized awareness object.
    rows, related, scopes = [], [], []
    task = dependencies.task(db, task_id)
    items = inspect(db, task_id, limit=16)
    own_tasks = {task_id} | {r['child_task_id'] for r in items}
    for item in items:
        row = {k: item[k] for k in ('proposal_id', 'revision', 'disposition', 'reason', 'child_task_id', 'delegation_status')}
        if len(encoded(dict(promotions=rows + [row], related_work=related)).encode()) > MAX_CONTEXT:
            break
        rows.append(row)
        scope = item['coordination']['scope']
        if scope:
            scopes.append(work_intents.normalize(scope['namespace'], scope['key']))
    seen = set()
    for scope_namespace, scope_key in scopes:
        candidates = db.execute('''SELECT i.*,t.title,t.status AS task_status FROM work_intents i
            JOIN tasks t USING(task_id) WHERE i.project_id=? AND i.namespace=? ORDER BY i.rowid DESC''',
            (task['project_id'], scope_namespace))
        for candidate in candidates:
            if candidate['task_id'] in own_tasks or candidate['intent_id'] in seen:
                continue
            if not (work_intents.contains(scope_key, candidate['normalized_key']) or work_intents.contains(candidate['normalized_key'], scope_key)):
                continue
            value = dict(task_id=candidate['task_id'], title=candidate['title'][:120], status=candidate['task_status'],
                         namespace=scope_namespace, key=candidate['normalized_key'], intent_status=candidate['status'])
            if len(related) >= 8 or len(encoded(dict(promotions=rows, related_work=related + [value])).encode()) > MAX_CONTEXT:
                return encoded(dict(promotions=rows, related_work=related))
            seen.add(candidate['intent_id'])
            related.append(value)
    return encoded(dict(promotions=rows, related_work=related))


def history(db, task_id):
    return [{**dict(r), 'content': json.loads(r['content']), 'receipt': json.loads(r['receipt'])}
            for r in db.execute('SELECT * FROM promotion_decisions WHERE task_id=? ORDER BY rowid', (task_id,))]


def instruction(task_id):
    return f'''\nOptional explicit promotion ({PROTOCOL}) for Task {task_id}.
Planning alone never executes work. Only when useful and authorized for this execution, select accepted
proposal IDs by emitting a FINAL standalone JSON object with exactly protocol, task_id, revision,
decision_key (stable ASCII key), items (1-16), continuation.
Each item has exactly proposal_id, scope, resources, depends_on_task_ids, reuse_task_id.
scope is null (blocked as ambiguous) or {{"namespace":"component","key":"logical/scope","from_intent_id":null}}.
Only reference known containing ancestor intents; never invent IDs. Declare resources using existing
resource_type, resource_key, scope, mode, recursive fields (max 8); use [] when none are needed.
depends_on_task_ids is [] or known same-Project prerequisite UUIDs (max 16); reuse_task_id is null or
a known Task requiring review instead of duplicate execution. No resource identity may contain secrets.
continuation is null to wait, or the existing Hybrid {{"instruction":"...","intent_ids":["known UUID"]}}.
All selected work is required. Blocked/rejected work is never successful completion. A retained proposal
can be promoted only once across revisions. Do not change already promoted responsibility.
No promotion is needed for ordinary independent completion. Context is informative, not a lease.
'''
