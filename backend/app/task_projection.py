"""Read-only, task-centric dashboard projection (Atlas B1). No reconciliation or execution authority.

A node is one durable Task. Hierarchy comes only from tasks.parent_task_id; Agent parent_id,
names and display order are never used. Agents appear only through task_assignments, so a Task
without a current Agent, a replaced Agent and a removed Agent are all represented.

These remain separate fields and are never merged into one status: proposed responsibility
(``planned``), materialized Task (the node itself), Agent assignment (``assignment``), execution
(``status`` and the Agent's own status), child integration (``delegation.result.integration``) and
root application (``integration`` with destination_kind ``project``).

Every read of one page shares a single SQLite snapshot. Columns are explicit: descriptions,
delegation instructions, result summaries, outputs, paths, sessions and error strings are never
read into a node.
"""

import base64
import binascii
import json

from . import plan_phase, providers
from .presentation import CAPABILITIES, waiting_context

MAX_LIMIT = 500
DEFAULT_LIMIT = 200
MAX_ANCESTORS = 500
MAX_DEPTH = 256          # longest ancestry walked for depth/root; deeper chains are reported as truncated
MAX_BLOCKERS = 16
MAX_PREVIOUS_AGENTS = 8
TITLE_LIMIT = 200
NAME_LIMIT = 80

PENDING_DELEGATION = ('requested', 'materialized', 'result_ready')


class CursorError(ValueError):
    """The cursor is malformed or was issued for a different filter."""


def _cursor(created_at, task_id, root):
    raw = json.dumps([created_at, task_id, root], separators=(',', ':')).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


def _decode(cursor, root):
    if len(cursor) > 1024:
        raise CursorError('Invalid cursor')
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise CursorError('Invalid cursor') from None
    if (not isinstance(value, list) or len(value) != 3 or not all(isinstance(v, str) for v in value[:2])
            or not (value[2] is None or isinstance(value[2], str))):
        raise CursorError('Invalid cursor')
    if value[2] != root:
        raise CursorError('Cursor belongs to a different filter')
    return value[0], value[1]


def _clip(text, limit):
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def _marks(keys):
    return ','.join('?' for _ in keys)


def _grouped(db, sql, keys):
    out = {}
    if keys:
        for row in db.execute(sql.format(marks=_marks(keys)), list(keys)):
            out.setdefault(row[0], []).append(row)
    return out


TASK_COLUMNS = '''t.task_id,t.parent_task_id,t.project_id,t.title,t.status,t.control_intent,
    t.stop_required,t.created_at,t.updated_at'''


def _subtree(root):
    # UNION terminates on malformed cyclic data; the root itself is always included.
    return ('''WITH RECURSIVE family(id) AS (SELECT ? UNION SELECT c.task_id FROM tasks c JOIN family f
               ON c.parent_task_id=f.id) ''', [root])


def _ancestry(db, ids):
    """Parent pointers for every ancestor of ``ids`` (bounded, cycle-safe), and whether the bound cut the walk."""
    parents = {}
    if not ids:
        return parents, False
    bound = len(ids) + MAX_ANCESTORS + MAX_DEPTH
    rows = db.execute('''WITH RECURSIVE up(id) AS (
            SELECT task_id FROM tasks WHERE task_id IN ({marks})
            UNION SELECT t.parent_task_id FROM tasks t JOIN up ON t.task_id=up.id WHERE t.parent_task_id IS NOT NULL
            LIMIT ?)
        SELECT u.id,t.parent_task_id,t.task_id IS NOT NULL FROM up u LEFT JOIN tasks t ON t.task_id=u.id'''
                      .format(marks=_marks(ids)), [*ids, bound]).fetchall()
    for task_id, parent, exists in rows:
        parents[task_id] = (parent, bool(exists))
    return parents, len(rows) >= bound


def _lineage(task_id, parents):
    """(root_task_id, depth, state); state is complete, missing_parent, cycle or truncated."""
    seen, current, depth = {task_id}, task_id, 0
    while True:
        entry = parents.get(current)
        if entry is None:
            return None, None, 'truncated'
        parent, exists = entry
        if not exists:
            return None, None, 'missing_parent'
        if parent is None:
            return current, depth, 'complete'
        if parent in seen:
            return None, None, 'cycle'
        if depth >= MAX_DEPTH:
            return None, None, 'truncated'
        if parent not in parents:
            return None, None, 'truncated'
        if not parents[parent][1]:
            return None, None, 'missing_parent'
        seen.add(parent)
        current, depth = parent, depth + 1


def _nodes(db, rows, parents):
    ids = [r['task_id'] for r in rows]
    children = {row[0]: row[1] for row in db.execute(
        'SELECT parent_task_id,COUNT(*) FROM tasks WHERE parent_task_id IN ({}) GROUP BY parent_task_id'
        .format(_marks(ids)), ids)} if ids else {}
    blockers = _grouped(db, 'SELECT task_id,reason_code FROM task_blockers WHERE active=1 AND task_id IN ({marks}) '
                        'ORDER BY created_at,id', ids)
    # Assignment history: newest first, bounded per Task; total is the full count.
    history = {}
    if ids:
        for row in db.execute('''SELECT task_id,assignment_id,agent_id,assigned_at,ended_at,ended_reason,total FROM (
                SELECT a.*,ROW_NUMBER() OVER w AS n,COUNT(*) OVER (PARTITION BY task_id) AS total FROM task_assignments a
                WHERE task_id IN ({}) WINDOW w AS (PARTITION BY task_id ORDER BY (ended_at IS NULL) DESC,assigned_at DESC,rowid DESC))
            WHERE n<=? ORDER BY task_id,n'''.format(_marks(ids)), [*ids, MAX_PREVIOUS_AGENTS + 2]):
            history.setdefault(row['task_id'], []).append(row)
    agent_ids = sorted({h[0]['agent_id'] for h in history.values()})
    agents = {row['agent_id']: row for row in db.execute(
        '''SELECT agent_id,status,display_name,display_color,agent_type,model,effort,provider_condition,
           waiting_question IS NOT NULL AND waiting_question<>'' AS asks,
           COALESCE(substr(waiting_question,1,{n})=?,0) AS plan_approval,removed_at,{caps}
           FROM agents WHERE agent_id IN ({marks})'''.format(caps=','.join(CAPABILITIES), marks=_marks(agent_ids),
                                                             n=len(plan_phase.APPROVAL_PREFIX)),
        [plan_phase.APPROVAL_PREFIX, *agent_ids])} if agent_ids else {}
    from . import agent_removal
    removals = agent_removal.agent_states(db, agent_ids) if agent_ids else {}
    continuations = {row['task_id']: row for row in db.execute(
        'SELECT task_id,assignment_id,phase,mode FROM delegation_continuations WHERE task_id IN ({})'.format(_marks(ids)),
        ids)} if ids else {}
    # This Task as a delegated child: its delegation and the latest recorded result.
    as_child = {row['child_task_id']: row for row in db.execute(
        '''SELECT d.child_task_id,d.delegation_id,d.parent_task_id,d.status,r.outcome,r.phase AS result_phase,
               i.integration_id,i.status AS integration_status,i.applied_at
           FROM delegations d
           LEFT JOIN (SELECT delegation_id,outcome,phase,integration_id,
                      ROW_NUMBER() OVER (PARTITION BY delegation_id ORDER BY created_at DESC,rowid DESC) AS n
                      FROM delegation_results) r ON r.delegation_id=d.delegation_id AND r.n=1
           LEFT JOIN integrations i ON i.integration_id=r.integration_id
           WHERE d.child_task_id IN ({})'''.format(_marks(ids)), ids)} if ids else {}
    # This Task as a parent: responsibilities by state, and proposals not yet materialized.
    delegated, planned = {}, {}
    if ids:
        for task_id, status, n in db.execute('SELECT parent_task_id,status,COUNT(*) FROM delegations '
                                             'WHERE parent_task_id IN ({}) GROUP BY parent_task_id,status'.format(_marks(ids)), ids):
            delegated.setdefault(task_id, {})[status] = n
        for task_id, disposition, n in db.execute(
                '''SELECT d.parent_task_id,COALESCE(p.disposition,'direct'),COUNT(*) FROM delegations d
                   LEFT JOIN promotion_items p ON p.delegation_id=d.delegation_id
                   WHERE d.status='requested' AND d.parent_task_id IN ({}) GROUP BY 1,2'''.format(_marks(ids)), ids):
            planned.setdefault(task_id, {})[disposition] = n
    revisions = dict(db.execute('SELECT task_id,MAX(revision) FROM planning_revisions WHERE task_id IN ({}) GROUP BY task_id'
                                .format(_marks(ids)), ids).fetchall()) if ids else {}
    rounds = dict(db.execute('SELECT task_id,COUNT(*) FROM delegation_rounds WHERE accepted_at IS NOT NULL '
                             'AND task_id IN ({}) GROUP BY task_id'.format(_marks(ids)), ids).fetchall()) if ids else {}
    integrations = {row['source_task_id']: row for row in db.execute(
        '''SELECT source_task_id,integration_id,destination_kind,status,validation_status,created_at,applied_at FROM (
               SELECT *,ROW_NUMBER() OVER (PARTITION BY source_task_id ORDER BY integration_order DESC) AS n
               FROM integrations WHERE source_task_id IN ({})) WHERE n=1'''.format(_marks(ids)), ids)} if ids else {}
    transfers = {row['task_id']: row for row in db.execute(
        '''SELECT task_id,phase,to_agent_id FROM (
               SELECT *,ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY created_at DESC,rowid DESC) AS n
               FROM responsibility_transfers WHERE task_id IN ({})) WHERE n=1'''.format(_marks(ids)), ids)} if ids else {}
    # The latest refused control message per Task and assignment (a structured stop's reason).
    rejections = {(row['task_id'], row['assignment_id']): row for row in db.execute(
        '''SELECT task_id,assignment_id,kind,reason,created_at FROM (
               SELECT *,ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY rejection_id DESC) AS n
               FROM control_rejections WHERE task_id IN ({})) WHERE n=1'''.format(_marks(ids)), ids)} if ids else {}

    result = []
    for row in rows:
        task_id = row['task_id']
        assignments = history.get(task_id, [])
        current = assignments[0] if assignments and assignments[0]['ended_at'] is None else None
        ended = [a for a in assignments if a['ended_at'] is not None]
        last_ended = ended[0] if ended else None
        shown = current or last_ended
        agent = agents.get(shown['agent_id']) if shown else None
        removal = removals.get(shown['agent_id']) if shown else None
        task_blockers = [b['reason_code'] for b in blockers.get(task_id, [])]
        continuation = continuations.get(task_id)
        counts = delegated.get(task_id, {})
        pending = sum(counts.get(s, 0) for s in PENDING_DELEGATION)
        condition = providers.condition_view(agent['provider_condition']) if agent else None
        context = None
        if current and agent:
            phase = continuation['phase'] if continuation and continuation['assignment_id'] == current['assignment_id'] else None
            context = waiting_context(agent['status'], 'asked' if agent['asks'] else None, row['status'],
                                      task_blockers, phase, True, pending, condition)
        if removal and removal['state'] != 'removed':
            context = ({'kind': 'recovery', 'label': 'Removal blocked'} if removal['state'] == 'blocked'
                       else {'kind': 'removal', 'label': 'Removal in progress'})
        removed = bool(removal and removal['state'] == 'removed')
        lineage = _lineage(task_id, parents)
        previous = []
        for a in ended:
            if a['agent_id'] not in previous and (not current or a['agent_id'] != current['agent_id']):
                previous.append(a['agent_id'])
        child = as_child.get(task_id)
        integration = integrations.get(task_id)
        transfer = transfers.get(task_id)
        result.append({
            'task_id': task_id,
            'parent_task_id': row['parent_task_id'],
            'project_id': row['project_id'],
            'title': _clip(row['title'], TITLE_LIMIT),
            'status': row['status'],
            'control_intent': row['control_intent'],
            'stop_required': bool(row['stop_required']),
            'created_at': row['created_at'],
            'updated_at': row['updated_at'],
            'root_task_id': lineage[0],
            'depth': lineage[1],
            'ancestry': lineage[2],
            'child_count': children.get(task_id, 0),
            'blockers': task_blockers[:MAX_BLOCKERS],
            'blockers_more': max(0, len(task_blockers) - MAX_BLOCKERS),
            'assignment': {
                'current': None if not current else {k: current[k] for k in ('assignment_id', 'agent_id', 'assigned_at')},
                'last_ended': None if not last_ended else {k: last_ended[k] for k in (
                    'assignment_id', 'agent_id', 'assigned_at', 'ended_at', 'ended_reason')},
                'count': assignments[0]['total'] if assignments else 0,
                'previous_agent_ids': previous[:MAX_PREVIOUS_AGENTS],
            },
            'agent': None if not agent else {
                'agent_id': agent['agent_id'],
                'role': 'current' if current else 'last',
                'status': agent['status'],
                'display_name': _clip(agent['display_name'], NAME_LIMIT),
                'display_color': agent['display_color'],
                'agent_type': agent['agent_type'],
                'model': _clip(agent['model'], NAME_LIMIT),
                'effort': _clip(agent['effort'], 32),
                'asks_user': bool(agent['asks']),
                # Its question is a plan waiting for the person's approval (shown as such, with its own actions).
                'awaits_plan_approval': bool(agent['asks']) and bool(agent['plan_approval']),
                'provider_condition': condition,
                'autonomous': all(agent[key] for key in CAPABILITIES),
                'removed': removed,
                'removal': None if not removal else {k: removal[k] for k in ('state', 'mode', 'failure', 'removed_at')},
            },
            'waiting_context': context,
            # Nobody works on it because its last agent's control message was refused: the reason, so the
            # Task never reads as simply "not started".
            'control_rejection': None if current or not last_ended or (task_id, last_ended['assignment_id']) not in rejections
            or row['status'] in ('completed', 'canceled') else {
                'kind': rejections[(task_id, last_ended['assignment_id'])]['kind'],
                'reason': _clip(rejections[(task_id, last_ended['assignment_id'])]['reason'], 500),
                'at': rejections[(task_id, last_ended['assignment_id'])]['created_at']},
            # The last owner was removed and nobody owns the Task yet (Stage 2D.2).
            'responsibility_awaiting': bool(not current and last_ended and last_ended['ended_reason'] == 'removed'
                                            and row['status'] not in ('completed', 'canceled')),
            'transfer': None if not transfer else {'phase': transfer['phase'], 'to_agent_id': transfer['to_agent_id']},
            'continuation': None if not continuation else {
                'phase': continuation['phase'], 'mode': continuation['mode'],
                'current_assignment': bool(current and continuation['assignment_id'] == current['assignment_id'])},
            'delegation': None if not child else {
                'delegation_id': child['delegation_id'], 'parent_task_id': child['parent_task_id'], 'status': child['status'],
                'result': None if child['outcome'] is None else {
                    'outcome': child['outcome'], 'phase': child['result_phase'],
                    'integration': None if child['integration_id'] is None else {
                        'integration_id': child['integration_id'], 'status': child['integration_status'],
                        'applied_at': child['applied_at']}}},
            'delegations': {status: counts.get(status, 0) for status in
                            ('requested', 'materialized', 'result_ready', 'acknowledged', 'canceled')},
            'planned': {'unmaterialized': sum(planned.get(task_id, {}).values()),
                        'by_disposition': planned.get(task_id, {})},
            'plan_revision': revisions.get(task_id),
            'rounds': rounds.get(task_id, 0),
            'integration': None if not integration else {k: integration[k] for k in (
                'integration_id', 'destination_kind', 'status', 'validation_status', 'created_at', 'applied_at')},
        })
    return result


def tasks(store, *, limit=DEFAULT_LIMIT, after=None, root=None):
    """One page of Tasks, newest first, with every ancestor needed to place them in the hierarchy."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    position = _decode(after, root) if after else None
    with store._connection() as db:
        db.execute('BEGIN')  # every query below reads one snapshot
        prefix, params = ('', [])
        scope = ''
        if root is not None:
            if not db.execute('SELECT 1 FROM tasks WHERE task_id=?', (root,)).fetchone():
                raise LookupError('Task not found')
            prefix, params = _subtree(root)
            scope = ' AND t.task_id IN (SELECT id FROM family)'
        where = ' WHERE 1=1' + scope
        args = list(params)
        if position:
            where += ' AND (t.created_at,t.task_id)<(?,?)'
            args += list(position)
        rows = db.execute(prefix + 'SELECT ' + TASK_COLUMNS + ' FROM tasks t' + where
                          + ' ORDER BY t.created_at DESC,t.task_id DESC LIMIT ?', [*args, limit + 1]).fetchall()
        total = db.execute(prefix + 'SELECT COUNT(*) FROM tasks t WHERE 1=1' + scope, params).fetchone()[0]
        page = rows[:limit]
        ids = [r['task_id'] for r in page]
        parents, cut = _ancestry(db, ids)
        in_page = set(ids)
        # Ancestors outside this page, nearest first, so every node can be placed now.
        needed, seen = [], set(in_page)
        for task_id in ids:
            parent = parents.get(task_id, (None, False))[0]
            while parent and parent not in seen and parents.get(parent, (None, False))[1]:
                seen.add(parent)
                needed.append(parent)
                parent = parents[parent][0]
        ancestor_ids = needed[:MAX_ANCESTORS]
        ancestor_rows = []
        if ancestor_ids:
            found = {r['task_id']: r for r in db.execute('SELECT ' + TASK_COLUMNS + ' FROM tasks t WHERE t.task_id IN ('
                                                          + _marks(ancestor_ids) + ')', ancestor_ids)}
            ancestor_rows = [found[i] for i in ancestor_ids if i in found]
        nodes = _nodes(db, page + ancestor_rows, parents)
        return {
            'tasks': nodes[:len(page)],
            'ancestors': nodes[len(page):],
            # Some page Task could not be placed: its ancestry ends in 'truncated', never a guessed parent.
            'ancestors_truncated': cut or len(needed) > MAX_ANCESTORS,
            'next_after': _cursor(page[-1]['created_at'], page[-1]['task_id'], root) if len(rows) > limit else None,
            'total': total,
            'root': root,
        }
