"""Recursive admission gates using Task ancestry and existing durable journals.

Callers hold BEGIN IMMEDIATE for ownership changes. No separate tree/Worker
registry, lifetime quota, timer, or provider calls are introduced here.
"""
import os

from . import work_control


def migrate(db):
    if 'recursive_delegation_enabled' not in {r['name'] for r in db.execute('PRAGMA table_info(agents)')}:
        db.execute('ALTER TABLE agents ADD COLUMN recursive_delegation_enabled INTEGER NOT NULL DEFAULT 0 CHECK(recursive_delegation_enabled IN (0,1))')
    if 'admission_error' not in {r['name'] for r in db.execute('PRAGMA table_info(delegations)')}:
        db.execute('ALTER TABLE delegations ADD COLUMN admission_error TEXT')


def _positive_setting(name, default):
    value = os.environ.get(name, str(default))
    try:
        number = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{name} must be a positive integer') from exc
    if number < 1:
        raise ValueError(f'{name} must be a positive integer')
    return number


def maximum_depth():
    return _positive_setting('CONTROL_CENTER_DELEGATION_DEPTH', 4)


def maximum_children_per_parent():
    return _positive_setting('CONTROL_CENTER_DELEGATION_CHILDREN_PER_PARENT', 16)


def maximum_concurrent_children():
    return _positive_setting('CONTROL_CENTER_DELEGATION_CONCURRENT_CHILDREN', 4)


def check_depth(db, task_id, *, child=False, limit=None):
    path = work_control.require_ancestors(db, task_id)
    depth = len(path) - 1 + int(child)
    maximum = maximum_depth()
    if limit is not None:
        maximum = min(maximum, limit)
    if depth > maximum:
        raise ValueError(f'Delegation depth limit reached: depth {depth}, maximum {maximum}; explicit policy review required')
    return path


def activation(db, task_id, provider, sandbox):
    """Validate explicit root activation or delegation of that same capability."""
    path = check_depth(db, task_id)
    if provider != 'codex':
        raise ValueError('Recursive delegation requires Codex')
    if len(path) == 1:
        return
    parent = db.execute('''SELECT g.* FROM task_assignments a JOIN agents g USING(agent_id)
        WHERE a.task_id=? AND a.ended_at IS NULL''', (path[1]['task_id'],)).fetchone()
    if (not parent or not parent['recursive_delegation_enabled']
            or not parent['hybrid_delegation_enabled'] or not parent['child_materialization_enabled']
            or not parent['delegation_protocol_enabled'] or parent['agent_type'] != provider
            or parent['sandbox'] != sandbox):
        raise ValueError('Parent has not authorized recursive provider/sandbox capability')


def inherited(provider):
    enabled = bool(provider['recursive_delegation_enabled'])
    if enabled and (provider['agent_type'] != 'codex' or not all(provider[k] for k in
            ('delegation_protocol_enabled', 'child_materialization_enabled', 'hybrid_delegation_enabled'))):
        raise ValueError('Invalid recursive Parent capability')
    return dict(delegation_protocol_enabled=enabled, child_materialization_enabled=enabled,
                hybrid_delegation_enabled=enabled, recursive_delegation_enabled=enabled,
                planning_enabled=enabled and bool(provider['planning_enabled']))


def occupied(db):
    """Running, waiting, and uncertain delegated executions, not dormant Parents."""
    return {r[0] for r in db.execute('''SELECT DISTINCT d.child_task_id FROM delegations d
        WHERE d.child_task_id IS NOT NULL AND (
          EXISTS (SELECT 1 FROM task_assignments a JOIN agents g USING(agent_id)
            WHERE a.task_id=d.child_task_id AND a.ended_at IS NULL AND g.status IN ('running','waiting'))
          OR EXISTS (SELECT 1 FROM delegation_materializations m WHERE m.delegation_id=d.delegation_id
            AND m.phase IN ('launching','recovery_required'))
          OR EXISTS (SELECT 1 FROM delegation_continuations c WHERE c.task_id=d.child_task_id
            AND c.phase IN ('launching','recovery_required')))''')}


def require_capacity(db, task_id, maximum=None):
    maximum = maximum_concurrent_children() if maximum is None else maximum
    if len(occupied(db) - {task_id}) >= maximum:
        raise ValueError('Child concurrency capacity reached; pending work retained')


def worker_boundary(db, task_id):
    if db.execute('SELECT 1 FROM delegations WHERE child_task_id=?', (task_id,)).fetchone():
        check_depth(db, task_id)
        require_capacity(db, task_id)


def require_known_ownership(db, task_id):
    if db.execute('''SELECT 1 FROM delegation_materializations m JOIN delegations d USING(delegation_id)
        WHERE d.child_task_id=? AND m.phase IN ('launching','recovery_required')''', (task_id,)).fetchone() or db.execute(
            "SELECT 1 FROM delegation_continuations WHERE task_id=? AND phase IN ('launching','recovery_required')", (task_id,)).fetchone():
        raise ValueError('Delegated process ownership requires inspection before filesystem integration')


def root_slot(db, task_id, timestamp, *, burst=8, window_seconds=60):
    root = work_control.ancestry(db, task_id)[-1]['task_id']
    prior = db.execute('''WITH RECURSIVE tree(task_id) AS (
        SELECT ? UNION SELECT t.task_id FROM tasks t JOIN tree p ON t.parent_task_id=p.task_id)
        SELECT r.not_before FROM delegation_rounds r JOIN tree USING(task_id)
        WHERE r.accepted_at IS NOT NULL ORDER BY r.not_before DESC,r.rowid DESC LIMIT 1 OFFSET ?''',
        (root, burst - 1)).fetchone()
    return max(timestamp, prior[0] + window_seconds) if prior else timestamp


def require_inherited(provider, agent_type, sandbox, capabilities):
    """Recheck the actual launch request against fresh durable Parent authority."""
    allowed = inherited(provider)
    if (provider['agent_type'] != agent_type
            or (agent_type == 'codex' and provider['sandbox'] != sandbox)
            or any(capabilities.get(key, False) and not value for key, value in allowed.items())):
        raise ValueError('Inherited Child capabilities exceed current Parent authorization')
