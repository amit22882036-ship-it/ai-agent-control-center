"""The plan phase of a managed root Task: decide the split first, then execute it.

A lead with helpers used to plan and implement in one non-interactive turn and could delegate only by
ending that turn with a JSON decision. With a split policy, its first turn is a read-only planning turn
whose successful result is the plan itself (control-center.planning.v1). After the plan is recorded the
lead is resumed to execute it (lead_decides, always), or waits for the person's approval (approve).
Everything after the plan uses the existing delegation flow. See docs/delegations.md.
"""
import json

from . import planning
from .work_control import ancestry

POLICIES = ('lead_decides', 'approve', 'always')
PHASES = ('planning', 'awaiting_approval', 'executing')
POLICY_LABELS = {'lead_decides': 'the lead decides', 'approve': 'the lead proposes and the person approves',
                 'always': 'split whenever possible'}
MAX_RETRIES = 1


def migrate(db):
    """v41: the split policy and plan phase of a root Task started with helpers."""
    db.execute(f"""CREATE TABLE IF NOT EXISTS task_plan_phase (
        task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id),
        policy TEXT NOT NULL CHECK(policy IN {POLICIES}),
        phase TEXT NOT NULL CHECK(phase IN {PHASES}),
        retries INTEGER NOT NULL DEFAULT 0 CHECK(retries >= 0),
        plan_revision INTEGER,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")


def validate_policy(policy):
    if policy not in POLICIES:
        raise ValueError('Split policy must be lead_decides, approve or always')
    return policy


def start(db, task_id, policy):
    db.execute('INSERT INTO task_plan_phase(task_id, policy, phase) VALUES (?,?,?)',
               (task_id, validate_policy(policy), 'planning'))


def get(db, task_id):
    """The plan phase of this Task itself (only a root Task started with a split policy has one)."""
    if db is None or task_id is None:
        return None
    row = db.execute('SELECT * FROM task_plan_phase WHERE task_id=?', (task_id,)).fetchone()
    return dict(row) if row else None


def root_policy(db, task_id):
    """The split policy that governs a Task tree, or None (helpers follow the root's choice in their prompts)."""
    try:
        root = ancestry(db, task_id)[-1]['task_id']
    except (LookupError, ValueError, IndexError, TypeError):
        return None
    row = get(db, root)
    return row['policy'] if row else None


def set_phase(db, task_id, phase, plan_revision=None):
    if phase not in PHASES:
        raise ValueError('Unknown plan phase')
    db.execute("""UPDATE task_plan_phase SET phase=?, plan_revision=COALESCE(?, plan_revision),
        updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE task_id=?""", (phase, plan_revision, task_id))


def count_retry(db, task_id):
    db.execute("UPDATE task_plan_phase SET retries=retries+1, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
               'WHERE task_id=?', (task_id,))
    return get(db, task_id)['retries']


def view(row):
    if not row:
        return None
    return {'policy': row['policy'], 'phase': row['phase'], 'retries': row['retries'],
            'plan_revision': row['plan_revision']}


def _policy_line(policy):
    if policy == 'always':
        return ('Split policy "always split when possible": propose a split whenever two or more substantial parts '
                'can be separated. Choose independent only for genuinely small, single-file or strictly sequential '
                'work, and say why.')
    if policy == 'approve':
        return 'The person reviews this plan before any work starts, so make it clear and complete.'
    return 'Choose whichever fits the work better.'


def planning_prompt(policy, request, revision=0):
    """The first turn: the person's request first, then one planning section (no other protocols)."""
    shape = json.dumps(dict(protocol=planning.PROTOCOL, decision_key='plan', base_revision=revision,
                            decision='propose', rationale='...', items=[dict(
                                proposal_id='search-core', deliverable='...', responsibility='...',
                                depends_on=[], parallel=True)], references=[]), separators=(',', ':'))
    return f"""User request:
{request}

Control Center planning turn (helpers are enabled; split policy: {POLICY_LABELS[policy]}).
This turn is for planning only. You have read-only access: read and inspect the workspace, but do not create
or change files and do not start implementing. Decide how the work is split between you (the lead) and
helpers, then end this turn. Your FINAL message must be exactly one JSON object and nothing else (no prose,
no Markdown fences), in this shape:
{shape}
- decision "propose" when the request has two or more separable, substantial parts. items: 1-16 objects
  with exactly proposal_id, deliverable, responsibility (what that helper does and which files or folders it
  owns), depends_on (other proposal_ids) and parallel (true or false). In the rationale, state the shared
  contract the parts need: data model, interfaces between them and folder layout. You will write that
  contract yourself before the parts go to helpers.
- decision "independent" with items [] when one agent fits better: small, single-file or strictly
  sequential work. Explain why in the rationale.
An empty or new project is not by itself a reason to work alone: when the parts become separable once a
shared contract exists, propose the split.
{_policy_line(policy)}
base_revision is {revision}. Keys are 1-64 ASCII letters, digits, ".", "_" or "-", starting with a letter or
digit. rationale, deliverable and responsibility are at most 2000 characters each. The person sees the plan
and its rationale, so write them for the person. A planning turn that ends with anything other than this one
JSON object is not a plan.

"""


def retry_prompt(reason, revision=0):
    return (f'Control Center: your planning message was not accepted: {reason}\n'
            'Reply with only the corrected plan: exactly one control-center.planning.v1 JSON object as your final '
            f'message (base_revision {revision}), with no prose and no Markdown fences. Do not change any files.')


def _execution_steps(revision):
    return (f'- If the plan proposes parts for helpers: first write the shared contract and any groundwork they need '
            '(for example in docs/CONTRACT.md), then end this turn with ONE delegation decision '
            '(control-center.orchestration.v1, described below) promoting exactly those proposal_ids. Do not '
            'implement those parts yourself.\n'
            '- If the plan is independent: implement it yourself and finish normally.\n'
            f'The recorded plan is revision {revision}.')


def execute_text(decision, revision):
    return (f'Control Center: your plan ({decision}) was recorded. You now have your normal access. Execute it.\n'
            + _execution_steps(revision))


def approval_reply_text(answer, revision):
    return (f'Control Center: the person reviewed your plan (revision {revision}) and replied:\n{answer}\n\n'
            'If they approved it, execute it as below. If they asked for changes, first emit a revised planning '
            f'JSON (base_revision {revision}) as its own message right before a tool call, then execute the revised '
            'plan. If they rejected the split, do the work yourself without helpers. You now have your normal '
            'access.\n' + _execution_steps(revision))


# The start of the Needs you question for a plan awaiting approval; the task list recognizes it by this.
APPROVAL_PREFIX = 'Plan ready for your approval.'


def proposal_view(content):
    """The plan the person is asked to approve, as data for the interface: one row per proposed part."""
    if not isinstance(content, dict):
        return None
    items = content.get('items') or [] if content.get('decision') == 'propose' else []
    return {'decision': content.get('decision'), 'rationale': str(content.get('rationale') or '')[:2000],
            'items': [{'proposal_id': str(item.get('proposal_id', ''))[:64], 'deliverable': str(item.get('deliverable', ''))[:2000],
                       'depends_on': [str(d)[:64] for d in (item.get('depends_on') or [])][:16]}
                      for item in items[:16] if isinstance(item, dict)]}


def approval_question(content):
    """The Needs you question for a plan awaiting the person's approval."""
    items = content.get('items') or []
    if content.get('decision') == 'propose' and items:
        parts = '; '.join(f"{item['proposal_id']}: {item['deliverable']}" for item in items[:8])
        summary = f'The lead proposes {len(items)} part(s) for helpers: {parts}.'
    else:
        summary = 'The lead proposes to do the work itself.'
    return (f"{APPROVAL_PREFIX} {summary} Reason: {content.get('rationale', '')} "
            "Reply 'approve' to start, describe the changes you want, or reply 'reject' to have the lead work alone.")
