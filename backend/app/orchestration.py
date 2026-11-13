"""control-center.orchestration.v1: one final message carrying an explicit delegation decision.

The executing Agent decides whether and what to delegate. Its single standalone final message
lists the responsibilities it proposes AND promotes. The backend derives the Task, plan revision
and internal decision keys from the trusted execution origin and current database state, then
records the decision through the existing planning and promotion operations inside the caller's
one BEGIN IMMEDIATE transaction. Planning and promotion stay distinct records: the derived
planning revision is descriptive, and only the items listed in `delegate` are promoted.
No process, provider call or scheduler is introduced here.
"""
import json
from uuid import UUID, uuid5

from . import delegation_protocol, planning, promotion
from .delegation_protocol import ControlRejected, rejected_as

PROTOCOL = 'control-center.orchestration.v1'
FIELDS = {'protocol', 'decision_key', 'rationale', 'delegate'}
OPTIONAL_FIELDS = {'continuation'}
ITEM_FIELDS = {'proposal_id', 'deliverable', 'responsibility', 'scope'}
OPTIONAL_ITEM_FIELDS = {'depends_on', 'parallel', 'resources', 'depends_on_task_ids', 'reuse_task_id',
                        'provider', 'model', 'allocation'}
REJECTION_REASON_LIMIT = 500
_PLACEHOLDER_TASK = '00000000-0000-4000-8000-000000000000'  # Structural validation only, never stored.
# Company and product names agents naturally write for a Child's provider, mapped to Control Center's ids.
PROVIDER_ALIASES = {'openai': 'codex', 'openai-codex': 'codex', 'codex-cli': 'codex', 'gpt': 'codex',
                    'anthropic': 'claude', 'claude-code': 'claude', 'claude_code': 'claude',
                    'google': 'gemini', 'gemini-cli': 'gemini', 'gemini_cli': 'gemini'}


def provider_id(value):
    """A Child provider as Control Center names it (case-insensitive, common aliases); unknown stays as written."""
    if not isinstance(value, str):
        return value
    key = value.strip().lower()
    return PROVIDER_ALIASES.get(key, key)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def is_envelope(message):
    if not isinstance(message, str) or not message.lstrip().startswith('{'):
        return False
    try:
        value = delegation_protocol.decode(message)
    except (ValueError, RecursionError):
        return False
    return (isinstance(value, dict) and isinstance(value.get('protocol'), str)
            and value['protocol'].startswith('control-center.orchestration.'))


def _parts(item):
    """The proposal (planning.v1 item) and promotion (promotion.v1 item) halves of one entry."""
    depends_on = item.get('depends_on', [])
    proposal = dict(proposal_id=item['proposal_id'], deliverable=item['deliverable'],
                    responsibility=item['responsibility'], depends_on=depends_on,
                    parallel=item.get('parallel', not depends_on))
    scope = item['scope']
    from .resources import agent_declaration
    selection = dict(proposal_id=item['proposal_id'],
                     scope=dict(namespace=scope.get('namespace'), key=scope.get('key'),
                                from_intent_id=scope.get('from_intent_id')),
                     resources=[agent_declaration(claim) for claim in item.get('resources', [])]
                     if isinstance(item.get('resources', []), list) else item.get('resources'),
                     depends_on_task_ids=item.get('depends_on_task_ids', []),
                     reuse_task_id=item.get('reuse_task_id'))
    selection.update({k: item[k] for k in ('provider', 'model', 'allocation') if k in item})
    if 'provider' in selection:
        selection['provider'] = provider_id(selection['provider'])
    return proposal, selection


def validate(message):
    """Strict whole-envelope validation; returns the canonical decision with defaults applied."""
    if not isinstance(message, str) or len(message.encode('utf-8')) > planning.MAX_PAYLOAD:
        raise ValueError('Orchestration decision exceeds 64 KiB')
    try:
        value = delegation_protocol.decode(message)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Malformed orchestration decision JSON') from exc
    if not isinstance(value, dict) or not FIELDS <= set(value) or not set(value) <= FIELDS | OPTIONAL_FIELDS:
        raise ValueError('Invalid orchestration decision fields: expected protocol, decision_key, rationale, '
                         'delegate and optional continuation')
    if value['protocol'] != PROTOCOL:
        raise ValueError('Unsupported orchestration protocol')
    planning.key(value['decision_key'])
    planning.text(value['rationale'])
    items = value['delegate']
    if not isinstance(items, list) or not 1 <= len(items) <= planning.MAX_ITEMS:
        raise ValueError('An orchestration decision delegates 1-16 responsibilities; when not delegating, '
                         'finish normally without a control message')
    proposals, selections, seen = [], [], set()
    for item in items:
        if not isinstance(item, dict) or not ITEM_FIELDS <= set(item) or not set(item) <= ITEM_FIELDS | OPTIONAL_ITEM_FIELDS:
            raise ValueError('Invalid delegated responsibility fields')
        if planning.key(item['proposal_id']) in seen:
            raise ValueError('Duplicate delegated proposal identity')
        seen.add(item['proposal_id'])
        planning.text(item['deliverable'])
        planning.text(item['responsibility'])
        scope = item['scope']
        if not isinstance(scope, dict) or not {'namespace', 'key'} <= set(scope) \
                or not set(scope) <= {'namespace', 'key', 'from_intent_id'}:
            raise ValueError('Each delegated responsibility needs scope {"namespace","key"} '
                             '(and optionally a known from_intent_id)')
        deps = item.get('depends_on', [])
        if not isinstance(deps, list) or len(deps) > planning.MAX_ITEMS:
            raise ValueError('Invalid delegated dependencies')
        for dep in deps:
            planning.key(dep)
        if len(set(deps)) != len(deps) or item['proposal_id'] in deps:
            raise ValueError('Duplicate or self-referencing delegated dependency')
        if 'parallel' in item and type(item['parallel']) is not bool:
            raise ValueError('Invalid parallel flag')
        proposal, selection = _parts(item)
        proposals.append(proposal)
        selections.append(selection)
    continuation = value.get('continuation')
    # The promotion half reuses the existing promotion.v1 validator (scope, resources,
    # Task references, provider/model, Auto allocation and the Hybrid continuation).
    promotion.validate(json.dumps(dict(protocol=promotion.PROTOCOL, task_id=_PLACEHOLDER_TASK, revision=1,
                                       decision_key=value['decision_key'], items=selections,
                                       continuation=continuation)))
    canonical = [dict(proposal, **{k: v for k, v in selection.items() if k != 'proposal_id'})
                 for proposal, selection in zip(proposals, selections)]
    return dict(protocol=PROTOCOL, decision_key=value['decision_key'], rationale=value['rationale'],
                delegate=canonical, continuation=continuation)


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS orchestration_decisions (
        task_id TEXT NOT NULL REFERENCES tasks(task_id), decision_key TEXT NOT NULL,
        plan_revision INTEGER NOT NULL, promotion_decision_key TEXT NOT NULL,
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        assignment_id TEXT NOT NULL REFERENCES task_assignments(assignment_id),
        generation TEXT NOT NULL, session_id TEXT NOT NULL, message_id TEXT NOT NULL,
        content TEXT NOT NULL, receipt TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(task_id,decision_key), UNIQUE(task_id,generation,message_id),
        FOREIGN KEY(task_id,plan_revision) REFERENCES planning_revisions(task_id,revision),
        FOREIGN KEY(task_id,promotion_decision_key) REFERENCES promotion_decisions(task_id,decision_key))''')
    db.execute('''CREATE TABLE IF NOT EXISTS control_rejections (
        rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        task_id TEXT REFERENCES tasks(task_id), assignment_id TEXT REFERENCES task_assignments(assignment_id),
        generation TEXT, kind TEXT NOT NULL CHECK(kind IN ('format','missing_step','authority','admission','internal')),
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    db.execute('CREATE INDEX IF NOT EXISTS control_rejections_task ON control_rejections(task_id, rejection_id)')


def derived_key(task_id, decision_key, part):
    """A stable internal planning/promotion key for one orchestration decision (valid planning key)."""
    return str(uuid5(UUID(task_id), f'control-center.orchestration\0{decision_key}\0{part}'))


def accept(db, origin, message, *, session_id, message_id):
    """Accept one decision in the caller's BEGIN IMMEDIATE transaction; any failure rolls it all back."""
    from . import agent_removal, work_control
    with rejected_as('format'):
        value = validate(message)
    warnings = _reconcile_models(db, value)
    planning.text(message_id, 128)
    with rejected_as('authority'):
        # Task, Project, Agent, assignment, generation, session and all four capabilities come
        # from the trusted origin; the message carries no identity, revision or generation.
        task, agent, assignment = promotion._origin(db, origin, session_id)
    content = encoded(value)
    # Replay is decided by the decision itself, before any revision is derived.
    previous = db.execute('SELECT * FROM orchestration_decisions WHERE task_id=? AND decision_key=?',
                          (task['task_id'], value['decision_key'])).fetchone()
    if previous:
        if previous['content'] != content:
            raise ControlRejected('admission', 'Conflicting replay of orchestration decision '
                                  f'{value["decision_key"]!r}; use a new decision_key for a different decision')
        return json.loads(previous['receipt'])  # Receipt only, never another revision or launch.
    with rejected_as('authority'):
        agent_removal.require_operational(db, origin.agent_id)
        if assignment['ended_at'] is not None or agent['status'] != 'running':
            raise ValueError('An orchestration decision requires a running current assignment')
        work_control.require_active(task, db=db)
    plan_key = derived_key(task['task_id'], value['decision_key'], 'plan')
    promotion_key = derived_key(task['task_id'], value['decision_key'], 'promotion')
    for table in ('planning_revisions', 'promotion_decisions'):
        if db.execute(f'SELECT 1 FROM {table} WHERE task_id=? AND decision_key IN (?,?)',
                      (task['task_id'], plan_key, promotion_key)).fetchone():
            raise ControlRejected('admission', 'Orchestration decision identity collides with existing records')
    latest = planning.current(db, task['task_id'])
    proposals = [{k: item[k] for k in ('proposal_id', 'deliverable', 'responsibility', 'depends_on', 'parallel')}
                 for item in value['delegate']]
    chosen = {p['proposal_id'] for p in proposals}
    current_items = {p['proposal_id']: p for p in latest['content']['items']} if latest else {}
    # Promoted responsibilities live durably in promotion_items (frozen proposal snapshots); the plan
    # is only the descriptive current proposal set, so history imposes no lifetime ceiling on it.
    promoted = {r[0]: json.loads(r[1]) for r in db.execute(
        'SELECT proposal_id,proposal FROM promotion_items WHERE task_id=?', (task['task_id'],))}
    # Unlisted proposals of the current revision that were never promoted are retained unchanged.
    retained = [p for p in current_items.values() if p['proposal_id'] not in chosen and p['proposal_id'] not in promoted]
    # A promoted proposal is carried only when an included proposal depends on it (transitively), using
    # its frozen snapshot, so cross-round dependencies still validate against the plan.
    included = {p['proposal_id'] for p in retained + proposals}
    carried, pending = [], [d for p in retained + proposals for d in p['depends_on']]
    while pending:
        dependency = pending.pop()
        if dependency in included or dependency not in promoted:
            continue  # Already present, or unknown: planning validation rejects unknown references.
        included.add(dependency)
        carried.append(promoted[dependency])
        pending.extend(promoted[dependency]['depends_on'])
    if len(retained) + len(carried) + len(proposals) > planning.MAX_ITEMS:
        raise ControlRejected('admission', 'The plan revision would exceed 16 proposals (this decision, retained '
                              'unpromoted proposals and promoted prerequisites it depends on)')
    plan = dict(protocol=planning.PROTOCOL, decision_key=plan_key, base_revision=latest['revision'] if latest else 0,
                decision='propose', rationale=value['rationale'], items=retained + carried + proposals,
                references=latest['content']['references'] if latest else [])
    accepted = planning.accept(db, origin, json.dumps(plan), session_id=session_id, message_id=message_id)
    selections = [{k: v for k, v in item.items() if k not in ('deliverable', 'responsibility', 'depends_on', 'parallel')}
                  for item in value['delegate']]
    receipt = promotion.accept(db, origin, json.dumps(dict(
        protocol=promotion.PROTOCOL, task_id=task['task_id'], revision=accepted['revision'],
        decision_key=promotion_key, items=selections, continuation=value['continuation'])),
        session_id=session_id, message_id=message_id)
    db.execute('''INSERT INTO orchestration_decisions(task_id,decision_key,plan_revision,promotion_decision_key,
        agent_id,assignment_id,generation,session_id,message_id,content,receipt) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (task['task_id'], value['decision_key'], accepted['revision'], promotion_key, origin.agent_id,
         origin.assignment_id, origin.generation, session_id, message_id, content, encoded(receipt)))
    return dict(receipt, warnings=warnings) if warnings else receipt


def _reconcile_models(db, value):
    """A requested Child model that the provider's current catalog does not list is dropped, so the Child
    uses its provider's default (the lead's provider: its configured model) instead of refusing the whole
    decision. Without a current catalog nothing is changed and admission decides as before."""
    from . import model_catalog
    warnings = []
    for item in value['delegate']:
        model, provider = item.get('model'), item.get('provider')
        if not model or not provider:
            continue
        try:
            scope = model_catalog.current_scope(db, provider)
        except Exception:  # A catalog read never decides acceptance.
            continue
        if scope is not None and model_catalog.lookup(db, provider, model, scope) is None:
            item.pop('model')
            warnings.append(f"{item['proposal_id']}: model {model!r} is not listed for {provider}; the helper uses "
                            "its provider's default model instead")
    return warnings


def history(db, task_id):
    return [{**dict(r), 'content': json.loads(r['content']), 'receipt': json.loads(r['receipt'])}
            for r in db.execute('SELECT * FROM orchestration_decisions WHERE task_id=? ORDER BY rowid', (task_id,))]


def record_rejection(db, *, agent_id, task_id, assignment_id, generation, kind, reason):
    reason = ''.join(c if c.isprintable() else ' ' for c in str(reason))[:REJECTION_REASON_LIMIT]
    db.execute('''INSERT INTO control_rejections(agent_id,task_id,assignment_id,generation,kind,reason)
        VALUES (?,?,?,?,?,?)''', (agent_id, task_id, assignment_id, generation, kind, reason))


def latest_rejection(db, task_id, assignment_id=None):
    row = db.execute('''SELECT kind,reason,created_at,assignment_id FROM control_rejections WHERE task_id=?
        AND (? IS NULL OR assignment_id=?) ORDER BY rejection_id DESC LIMIT 1''',
        (task_id, assignment_id, assignment_id)).fetchone()
    return dict(row) if row else None


def _resources_accepted():
    from .resources import ACCEPTED
    return ACCEPTED


def retry_prompt(reason):
    return (f'Control Center: your delegation decision was not accepted: {reason}\n'
            'Fix exactly that and send the corrected decision as your final message: one '
            f'{PROTOCOL} JSON object with a NEW decision_key and nothing else (no prose, no Markdown fences). '
            'Do not redo work that is already done.')


def example():
    """One minimal decision that validates against the real schema (shape only)."""
    return json.dumps(dict(protocol=PROTOCOL, decision_key='split-cli',
                           rationale='The CLI is a separable file with a stable parse_row interface.',
                           delegate=[dict(proposal_id='cli', deliverable='JSON command-line interface in cli.py with tests',
                                          responsibility='Edit only cli.py and test_cli.py; call parse_row unchanged.',
                                          scope=dict(namespace='component', key='csv/cli'))]),
                      separators=(',', ':'))


def instruction(db, task_id):
    from . import allocation
    return ('\nDelegation decision (' + PROTOCOL + '). You choose: do the work yourself and finish normally '
            '(no control message), or, when separate Child responsibilities are worth their coordination cost, end '
            'your turn with ONE delegation decision. The decision is your FINAL assistant message and contains only '
            'one JSON object: no prose, no Markdown fences, no second object. The Control Center supplies the Task '
            'identity and plan revisions, validates the decision, records your proposals, promotes exactly the '
            'listed responsibilities, admits and launches the Children, and later returns their results for review.\n'
            'Fields: protocol, decision_key (your stable key for this decision), rationale (<=2000 characters), '
            'delegate (1-16 responsibilities), optional continuation (omit or null to wait for results; or the Hybrid '
            '{"instruction":"...","intent_ids":["known UUID"]} for approved independent work meanwhile).\n'
            'Each responsibility: proposal_id (stable key), deliverable, responsibility (<=2000 characters each), '
            'scope {"namespace":"component","key":"logical/scope"}; optionally depends_on (other proposal_ids), '
            'parallel (true/false), resources (optional, max 8; each exactly resource_type, resource_key, scope, mode, '
            'recursive, for example {"resource_type":"file_path","resource_key":"backend","scope":"project",'
            '"mode":"exclusive","recursive":true} for a folder; ' + _resources_accepted() + '), '
            'depends_on_task_ids and reuse_task_id (known same-Project Task UUIDs only), and either provider (one of '
            'codex, claude or gemini; omit it to use your own) with an optional model (a model id of that provider; '
            'omit it for its default) or, '
            'only when an Auto allocation section follows, allocation.\n'
            'Keys are 1-64 ASCII letters, digits, ".", "_" or "-", starting with a letter or digit. Never invent IDs.\n'
            'Example (shape only; use your own content):\n' + example() + '\n'
            'Every delegated responsibility is required work; blocked or rejected work is never success. Do not do '
            'delegated responsibilities yourself or wait using provider collaboration tools. A proposal_id that was '
            'already promoted cannot be changed. Earlier control formats remain accepted but are never needed.\n'
            + allocation.instruction(db, task_id))
