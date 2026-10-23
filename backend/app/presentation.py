"""Read-only, bounded dashboard projections. No reconciliation or execution authority.

Queries share a SQLite snapshot. Explicit columns intentionally exclude paths,
provider messages, result output, protocol payloads and arbitrary error strings.
"""

from . import providers

CAPABILITIES = ('planning_enabled', 'delegation_protocol_enabled',
                'child_materialization_enabled', 'hybrid_delegation_enabled',
                'recursive_delegation_enabled')


def waiting_context(status, question, task_status, blockers, phase, current, pending=None, provider_condition=None):
    if not current:
        return None
    if 'process_ownership_unknown' in blockers:
        # Not a lost watcher: the end of its process tree is not established.
        return {'kind': 'recovery', 'label': 'Process end unconfirmed'}
    if task_status == 'paused':
        return {'kind': 'control', 'label': 'Task paused'}
    if phase == 'recovery_required' or any('recovery' in code for code in blockers):
        return {'kind': 'recovery', 'label': 'Recovery required'}
    if status == 'running':
        return None
    if phase == 'waiting' and pending == 0 and all(code == 'delegation_requested' for code in blockers):
        return {'kind': 'continuation', 'label': 'Awaiting Parent continuation'}
    if blockers:
        if all(code.startswith('dependency_') for code in blockers):
            return {'kind': 'dependency', 'label': 'Waiting for dependencies'}
        if all(code == 'delegation_requested' for code in blockers):
            return {'kind': 'delegation', 'label': 'Waiting for delegated results'}
        return {'kind': 'coordination', 'label': 'Coordination blocked'}
    if phase == 'waiting':
        return {'kind': 'delegation', 'label': 'Waiting for delegated results'}
    if status == 'waiting' and question:
        return {'kind': 'user', 'label': 'Waiting for you'}
    if status == 'waiting' and provider_condition:
        # Stage 2F: a provider limit, sign-in or availability wait (closed enum).
        return {'kind': 'provider', 'label': provider_condition['label'], 'condition': provider_condition['kind']}
    if status == 'waiting' or task_status in ('waiting', 'blocked'):
        return {'kind': 'unknown', 'label': 'Waiting'}
    return None


AGENT_QUERY = '''WITH latest_assignment AS (SELECT agent_id,MAX(rowid) AS latest FROM task_assignments GROUP BY agent_id)
    SELECT g.creation_order,g.agent_id,g.parent_id,g.status,g.task,
    g.display_name,g.display_color,g.agent_type,g.model,g.provider_condition,g.sandbox,g.waiting_question,
    g.planning_enabled,g.delegation_protocol_enabled,g.child_materialization_enabled,
    g.hybrid_delegation_enabled,g.recursive_delegation_enabled,
    a.assignment_id,a.ended_at,a.ended_reason,t.task_id,t.status AS task_status,t.project_id,
    c.phase AS continuation_phase,
    (SELECT COUNT(*) FROM delegations d WHERE d.parent_task_id=t.task_id AND d.status IN ('requested','materialized','result_ready')) AS pending_responsibilities,
    (SELECT MAX(revision) FROM planning_revisions p WHERE p.task_id=t.task_id) AS plan_revision
    FROM agents g LEFT JOIN latest_assignment x ON x.agent_id=g.agent_id
    LEFT JOIN task_assignments a ON a.rowid=x.latest
    LEFT JOIN tasks t ON t.task_id=a.task_id
    LEFT JOIN delegation_continuations c ON c.task_id=t.task_id AND c.assignment_id=a.assignment_id
'''


def _nodes(db, rows):
    keys = list({r['task_id'] for r in rows if r['task_id']})
    blockers = {}
    if keys:
        for row in db.execute('SELECT task_id,reason_code FROM task_blockers WHERE active=1 AND task_id IN ('
                              + ','.join('?' for _ in keys) + ')', keys):
            blockers.setdefault(row['task_id'], []).append(row['reason_code'])
    from . import agent_removal
    removals = agent_removal.agent_states(db, [r['agent_id'] for r in rows])
    result = []
    for row in rows:
        node = {key: row[key] for key in ('agent_id', 'parent_id', 'status', 'task', 'display_name',
                'display_color', 'agent_type', 'model', 'sandbox', 'waiting_question', 'task_id', 'task_status', 'plan_revision')}
        node['provider_condition'] = providers.condition_view(row['provider_condition'])
        current = row['assignment_id'] is not None and row['ended_at'] is None
        node['historical_assignment'] = bool(row['assignment_id'] and not current)
        node['autonomous'] = all(row[key] for key in CAPABILITIES)
        node['waiting_context'] = waiting_context(row['status'], row['waiting_question'], row['task_status'],
            blockers.get(row['task_id'], []), row['continuation_phase'], current, row['pending_responsibilities'],
            node['provider_condition'])
        removal = removals.get(row['agent_id'])
        node['removal'] = removal
        node['removed'] = bool(removal and removal['state'] == 'removed')
        # The Agent's last Task waits for an explicit new owner (Stage 2D.2).
        node['responsibility_awaiting'] = bool(node['removed'] and row['task_id'] and row['ended_reason'] == 'removed'
                                               and row['task_status'] not in ('completed', 'canceled') and not db.execute(
                                                   'SELECT 1 FROM task_assignments WHERE task_id=? AND ended_at IS NULL',
                                                   (row['task_id'],)).fetchone())
        if removal and removal['state'] != 'removed':
            node['waiting_context'] = ({'kind': 'recovery', 'label': 'Removal blocked'} if removal['state'] == 'blocked'
                                       else {'kind': 'removal', 'label': 'Removal in progress'})
        elif node['removed']:
            node['waiting_context'] = {'kind': 'removed', 'label': 'Removed'}
        result.append(node)
    return result


def workspace(store, *, limit=500, after=0):
    with store._connection() as db:
        db.execute('BEGIN')
        rows = db.execute(AGENT_QUERY + ' WHERE (?=0 OR g.creation_order<?) ORDER BY g.creation_order DESC LIMIT ?',
                          (after, after, limit + 1)).fetchall()
        nodes = _nodes(db, rows[:limit])
        # Counts use durable identities, never number of displayed historical Workers.
        counts = dict(db.execute('SELECT status,COUNT(*) FROM tasks GROUP BY status').fetchall())
        active = db.execute("SELECT COUNT(*) FROM task_assignments a JOIN agents g USING(agent_id) WHERE a.ended_at IS NULL AND g.status='running'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM delegations WHERE status IN ('requested','materialized','result_ready')").fetchone()[0]
        attention = db.execute("""SELECT COUNT(*) FROM task_assignments a JOIN agents g USING(agent_id)
            JOIN tasks t USING(task_id) WHERE a.ended_at IS NULL AND g.status='waiting'
            AND t.status!='paused' AND g.waiting_question IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task_blockers b WHERE b.task_id=t.task_id AND b.active=1)
            AND NOT EXISTS (SELECT 1 FROM delegation_continuations c WHERE c.task_id=t.task_id AND c.assignment_id=a.assignment_id AND c.phase IN ('waiting','recovery_required'))""").fetchone()[0]
        return {'agents': nodes, 'next_after': rows[limit - 1]['creation_order'] if len(rows) > limit else None,
                'overview': {'active_agents': active, 'completed_tasks': counts.get('completed', 0),
                    'waiting_tasks': counts.get('waiting', 0) + counts.get('blocked', 0),
                    'needs_input': attention, 'pending_responsibilities': pending}}


def agent_context(store, agent_id):
    with store._connection() as db:
        db.execute('BEGIN')
        rows = db.execute(AGENT_QUERY + ' WHERE g.agent_id=?', (agent_id,)).fetchall()
        return _nodes(db, rows)[0] if rows else None


def hierarchy(store, agent_id):
    """A bounded connected Agent family, independent of workspace pagination."""
    with store._connection() as db:
        db.execute('BEGIN')
        ids = [r[0] for r in db.execute('''WITH RECURSIVE family(id) AS (
            SELECT agent_id FROM agents WHERE agent_id=?
            UNION
            SELECT g.parent_id FROM agents g JOIN family f ON g.agent_id=f.id
                JOIN agents p ON p.agent_id=g.parent_id
            UNION
            SELECT g.agent_id FROM agents g JOIN family f ON g.parent_id=f.id
            LIMIT 501
        ) SELECT id FROM family''', (agent_id,))]
        if not ids:
            raise LookupError('Agent not found')
        rows = db.execute(AGENT_QUERY + ' WHERE g.agent_id IN (' + ','.join('?' for _ in ids[:500])
                          + ') ORDER BY g.creation_order', ids[:500]).fetchall()
        nodes = _nodes(db, rows)
        known = {node['agent_id'] for node in nodes}
        return {'agents': nodes, 'truncated': len(ids) > 500,
                'incomplete_ancestry': any(n['parent_id'] and n['parent_id'] not in known for n in nodes)}


def activity(store, agent_id):
    with store._connection() as db:
        db.execute('BEGIN')
        rows = db.execute(AGENT_QUERY + ' WHERE g.agent_id=?', (agent_id,)).fetchall()
        if not rows:
            raise LookupError('Agent not found')
        node = _nodes(db, rows)[0]
        task_id = node['task_id']
        result = {'context': node, 'events': [], 'delegations': [], 'blockers': [], 'result': None}
        if not task_id:
            return result
        # Only records owned by this assigned Task. No global resource owner details.
        result['blockers'] = [r[0] for r in db.execute(
            'SELECT DISTINCT reason_code FROM task_blockers WHERE task_id=? AND active=1 LIMIT 20', (task_id,))]
        own_result = db.execute('''SELECT r.outcome,r.phase,i.status AS integration_status
            FROM delegation_results r JOIN delegations d USING(delegation_id)
            LEFT JOIN integrations i ON i.integration_id=r.integration_id
            WHERE r.child_task_id=? AND d.project_id=? ORDER BY r.rowid DESC LIMIT 1''', (task_id, rows[0]['project_id'])).fetchone()
        result['result'] = dict(own_result) if own_result else None
        result['delegations'] = [dict(r) for r in db.execute('''SELECT d.delegation_id,d.child_task_id,d.status,
            substr(d.instruction,1,160) AS responsibility,p.disposition AS admission,m.phase AS materialization,
            r.outcome,r.phase AS result_phase,i.status AS integration_status,
            (SELECT a.agent_id FROM task_assignments a WHERE a.task_id=d.child_task_id ORDER BY a.rowid DESC LIMIT 1) AS agent_id
            FROM delegations d LEFT JOIN promotion_items p USING(delegation_id)
            LEFT JOIN delegation_materializations m USING(delegation_id)
            LEFT JOIN delegation_results r ON r.rowid=(SELECT MAX(x.rowid) FROM delegation_results x WHERE x.delegation_id=d.delegation_id)
            LEFT JOIN integrations i ON i.integration_id=r.integration_id
            WHERE d.parent_task_id=? AND d.project_id=? ORDER BY d.rowid DESC LIMIT 30''', (task_id, rows[0]['project_id']))]
        events = []
        def add(kind, identifier, label, at):
            events.append({'id': kind + ':' + str(identifier), 'kind': kind, 'label': label, 'at': at})
        # Only the validated decision enum is projected, never the provider's rationale prose.
        for r in db.execute('''SELECT revision,accepted_at,json_extract(content,'$.decision') FROM planning_revisions
            WHERE task_id=? ORDER BY revision DESC LIMIT 30''', (task_id,)):
            add('plan', r[0], f'Plan revision {r[0]} accepted: {r[2]}', r[1])
        for r in db.execute('SELECT decision_key,created_at FROM promotion_decisions WHERE task_id=? ORDER BY rowid DESC LIMIT 30', (task_id,)):
            add('promotion', r[0], 'Work promotion accepted', r[1])
        for r in db.execute('SELECT decision_id,created_at FROM replan_decisions WHERE task_id=? ORDER BY rowid DESC LIMIT 30', (task_id,)):
            add('replan', r[0], 'Replanning decision accepted', r[1])
        for r in db.execute('''SELECT x.transition_id,x.action,x.phase FROM replan_transitions x
            JOIN replan_decisions d USING(decision_id) WHERE d.task_id=? ORDER BY x.rowid DESC LIMIT 30''', (task_id,)):
            # No transition timestamp is persisted. Never invent one from read time.
            add('transition', r[0], f'Responsibility {r[1]}: {r[2]}', None)
        for r in db.execute('SELECT assignment_id,assigned_at,ended_at,ended_reason FROM task_assignments WHERE task_id=? ORDER BY rowid DESC LIMIT 30', (task_id,)):
            add('start', r[0], 'Worker assigned', r[1])
            if r[2]:
                add('end', r[0], 'Assignment ended: ' + r[3], r[2])
        for r in db.execute('''SELECT r.result_id,r.phase,r.outcome,r.updated_at FROM delegation_results r
            JOIN delegations d USING(delegation_id) WHERE d.parent_task_id=? AND d.project_id=? ORDER BY r.rowid DESC LIMIT 30''', (task_id, rows[0]['project_id'])):
            add('result', r[0], f'Child result: {r[2]} · {r[1]}', r[3])
        result['events'] = sorted(events, key=lambda e: (e['at'] or '', e['id']), reverse=True)[:30]
        result['bounded'] = True
        return result
