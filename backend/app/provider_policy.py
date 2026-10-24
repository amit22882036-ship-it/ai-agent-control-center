"""Stage 2G hard provider permissions: the global allowlist, Task-subtree restrictions and
the defaults for new root Agents.

Permission is separate from installation, sign-in and enablement (providers.py) and from
the Auto allocation candidate policy (allocation.py). It is a hard boundary: every new
provider process launch checks it inside the existing admission transaction, and Auto
allocation, promotion and delegation treat it as an eligibility gate.

- Global: the real providers the user allows. An empty set means no real provider is
  allowed (only Mock runs). It never means "all".
- Task: an optional restriction on a Task and its subtree. The effective set of a Task is
  the global set intersected with every restriction on the Task and its ancestors, so a
  descendant can narrow but never widen.
- Mock is a development and testing provider: it is not part of either set, is never an
  automatic fallback, and stays available for explicit use.

Edits carry a monotonic revision (`provider_policy_revisions`, AUTOINCREMENT, never
reused) checked in the same write transaction.
"""
import json

from . import model_catalog, providers

NOW = providers.NOW
STALE = object()
# Far beyond any real hierarchy. A deeper (or cyclic, or broken) ancestry fails closed: it
# never ends the walk early and never returns a wider set than an ancestor allows.
MAX_ANCESTRY = 10000


def migrate_v37(db):
    """The global policy starts from what each provider could already do: a provider that was
    enabled stays allowed, one that was not stays not allowed (nothing widens or is revoked)."""
    db.execute('''CREATE TABLE IF NOT EXISTS provider_policy_revisions (
        revision INTEGER PRIMARY KEY AUTOINCREMENT,
        scope TEXT NOT NULL CHECK(scope IN ('global','task')),
        task_id TEXT,
        action TEXT NOT NULL CHECK(action IN ('set','clear','migrate')),
        changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    db.execute('''CREATE TABLE IF NOT EXISTS provider_global_policy (
        policy_id INTEGER PRIMARY KEY CHECK(policy_id=1),
        allowed_providers TEXT NOT NULL,
        default_provider TEXT, default_model TEXT, default_effort TEXT,
        revision INTEGER NOT NULL,
        updated_by TEXT NOT NULL CHECK(updated_by IN ('user','migration')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    db.execute('''CREATE TABLE IF NOT EXISTS task_provider_restrictions (
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
        providers TEXT NOT NULL,
        revision INTEGER NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    if db.execute('SELECT 1 FROM provider_global_policy WHERE policy_id=1').fetchone() is None:
        allowed = [p for p in providers.REAL if providers.settings(db, p)['enabled']]
        revision = db.execute("INSERT INTO provider_policy_revisions(scope,action) VALUES ('global','migrate')").lastrowid
        db.execute('''INSERT INTO provider_global_policy(policy_id,allowed_providers,revision,updated_by)
            VALUES (1,?,?,'migration')''', (json.dumps(allowed), revision))


def _has(db, table):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


def _revision(db, scope, action, task_id=None):
    return db.execute('INSERT INTO provider_policy_revisions(scope,task_id,action) VALUES (?,?,?)',
                      (scope, task_id, action)).lastrowid


def _conflict():
    return ValueError('The provider permissions changed since they were opened; reload them and reapply your changes.')


def _providers(value, *, field='providers'):
    if not isinstance(value, list) or any(p not in providers.REAL for p in value):
        raise ValueError(f'{field} lists only real providers: ' + ', '.join(providers.REAL))
    return list(dict.fromkeys(value))


# ---------------------------------------------------------------- global policy

def global_policy(db):
    """The global policy, or None when persistence predates v37 (then nothing is restricted here)."""
    if not _has(db, 'provider_global_policy'):
        return None
    row = db.execute('SELECT * FROM provider_global_policy WHERE policy_id=1').fetchone()
    if row is None:
        return None
    return dict(allowed=json.loads(row['allowed_providers']), default_provider=row['default_provider'],
                default_model=row['default_model'], default_effort=row['default_effort'], revision=row['revision'],
                updated_by=row['updated_by'], updated_at=row['updated_at'])


def default_problem(db, policy, provider, model, effort):
    """Why a saved default cannot be used now (None: it is valid). Never substitutes another."""
    if provider is None:
        return None
    if provider == 'mock':
        return 'Mock has no model or effort.' if model or effort else None
    if provider not in policy['allowed']:
        return f'{providers.LABELS[provider]} is not in your allowed providers.'
    if model is None and effort is None:
        return None
    try:
        model_catalog.admission(db, provider, model, effort)
    except providers.ProviderRefused as exc:
        return str(exc).split(': ', 1)[-1]
    return None


def set_global(db, *, allowed, default_provider=None, default_model=None, default_effort=None,
               expected_revision=STALE):
    """The user's global allowlist and new-root defaults (caller's write transaction)."""
    allowed = _providers(allowed, field='allowed')
    if default_provider is not None and default_provider not in providers.IDS:
        raise ValueError('Unknown default provider')
    providers.validate_model(default_model)
    providers.validate_effort(default_effort)
    if default_provider is None and (default_model or default_effort):
        raise ValueError('A default model or effort needs a default provider.')
    current = global_policy(db)
    if current is None:
        raise ValueError('Provider permissions are unavailable; the database has not been migrated.')
    if expected_revision is not STALE and expected_revision != current['revision']:
        raise _conflict()
    problem = default_problem(db, dict(allowed=allowed), default_provider, default_model, default_effort)
    if problem:
        raise ValueError(f'The default cannot be saved: {problem} Choose an allowed provider, model and effort.')
    revision = _revision(db, 'global', 'set')
    db.execute(f'''UPDATE provider_global_policy SET allowed_providers=?,default_provider=?,default_model=?,
        default_effort=?,revision=?,updated_by='user',updated_at={NOW} WHERE policy_id=1''',
        (json.dumps(allowed), default_provider, default_model, default_effort, revision))
    return global_policy(db)


# ---------------------------------------------------------------- Task restrictions

class AncestryUnverifiable(ValueError):
    """The Task's ancestry could not be established; no real provider is permitted for it."""


def _chain(db, task_id):
    """The Task and every ancestor, nearest first: one bounded, non-recursive query.

    The whole chain is returned or AncestryUnverifiable is raised (missing Task or
    ancestor, cycle, or deeper than MAX_ANCESTRY); a partial chain is never used.
    """
    rows = db.execute('''WITH RECURSIVE chain(task_id, title, parent_task_id, depth) AS (
            SELECT task_id, title, parent_task_id, 0 FROM tasks WHERE task_id=?
            UNION ALL
            SELECT t.task_id, t.title, t.parent_task_id, c.depth + 1 FROM tasks t JOIN chain c
                ON t.task_id = c.parent_task_id WHERE c.depth < ?)
        SELECT task_id, title, parent_task_id FROM chain ORDER BY depth''', (task_id, MAX_ANCESTRY)).fetchall()
    if not rows:
        raise AncestryUnverifiable('the Task was not found')
    seen = set()
    for row in rows:
        if row['task_id'] in seen:
            raise AncestryUnverifiable('its ancestry contains a cycle')
        seen.add(row['task_id'])
    if rows[-1]['parent_task_id'] is not None:
        if len(rows) > MAX_ANCESTRY:
            raise AncestryUnverifiable(f'it has more than {MAX_ANCESTRY} ancestors')
        raise AncestryUnverifiable('an ancestor Task is missing')
    return [dict(row) for row in rows]


def own_restriction(db, task_id):
    if not _has(db, 'task_provider_restrictions'):
        return None
    row = db.execute('SELECT * FROM task_provider_restrictions WHERE task_id=?', (task_id,)).fetchone()
    return dict(task_id=row['task_id'], providers=json.loads(row['providers']), revision=row['revision'],
                updated_at=row['updated_at']) if row else None


def effective(db, task_id=None):
    """(effective real providers, explanation list). None as the set means no policy is stored
    (pre-v37 persistence): only the existing readiness rules then apply. An ancestry that
    cannot be verified yields the empty set (fail closed) with an `unverified` source."""
    policy = global_policy(db)
    if policy is None:
        return None, []
    allowed = list(policy['allowed'])
    sources = [dict(scope='global', task_id=None, title=None, providers=list(policy['allowed']))]
    if not task_id:
        return allowed, sources
    try:
        chain = _chain(db, task_id)
    except AncestryUnverifiable as exc:
        return [], sources + [dict(scope='unverified', task_id=task_id, title=None, providers=[], reason=str(exc))]
    for task in chain:
        own = own_restriction(db, task['task_id'])
        if own is not None:
            allowed = [p for p in allowed if p in own['providers']]
            sources.append(dict(scope='task', task_id=task['task_id'], title=task['title'], providers=own['providers']))
    return allowed, sources


def inherited(db, task_id):
    """The effective set from the global policy and the ancestors only (what a local restriction may narrow)."""
    task = db.execute('SELECT parent_task_id FROM tasks WHERE task_id=?', (task_id,)).fetchone()
    if task is None:
        raise LookupError('Task not found')
    allowed, sources = effective(db, task['parent_task_id']) if task['parent_task_id'] else effective(db)
    return allowed, sources


def set_restriction(db, task_id, providers_list, expected_revision=STALE):
    """A Task-subtree restriction. It may only narrow what the global policy and ancestors allow."""
    chosen = _providers(providers_list)
    allowed, _ = inherited(db, task_id)
    if allowed is None:
        raise ValueError('Provider permissions are unavailable; the database has not been migrated.')
    wider = [p for p in chosen if p not in allowed]
    if wider:
        raise ValueError('A Task restriction can only narrow inherited permissions; not allowed above this Task: '
                         + ', '.join(providers.LABELS[p] for p in wider) + '.')
    own = own_restriction(db, task_id)
    if expected_revision is not STALE and (own['revision'] if own else None) != expected_revision:
        raise _conflict()
    revision = _revision(db, 'task', 'set', task_id)
    db.execute(f'''INSERT INTO task_provider_restrictions(task_id,providers,revision) VALUES (?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET providers=excluded.providers,revision=excluded.revision,updated_at={NOW}''',
        (task_id, json.dumps(chosen), revision))
    return own_restriction(db, task_id)


def clear_restriction(db, task_id, expected_revision=STALE):
    own = own_restriction(db, task_id)
    if expected_revision is not STALE and (own['revision'] if own else None) != expected_revision:
        raise _conflict()
    if own is None:
        raise ValueError('This Task has no provider restriction of its own to remove.')
    _revision(db, 'task', 'clear', task_id)
    db.execute('DELETE FROM task_provider_restrictions WHERE task_id=?', (task_id,))


# ---------------------------------------------------------------- enforcement

class ProviderNotPermitted(providers.ProviderRefused):
    """A hard permission refusal (never followed by another provider)."""


def denial(db, provider, task_id=None):
    """Why `provider` may not start new work for this Task (None: permitted)."""
    if provider not in providers.REAL:
        return None  # Mock: explicit development use only, never restricted or substituted.
    allowed, sources = effective(db, task_id)
    if allowed is None or provider in allowed:
        return None
    label = providers.LABELS[provider]
    unverified = next((s for s in sources if s['scope'] == 'unverified'), None)
    if unverified:
        return (f'{label} is not permitted: the Task hierarchy could not be verified ({unverified["reason"]}), so no real '
                'provider may start for it. No other provider is substituted.')
    blocking = next((s for s in reversed(sources) if s['scope'] == 'task' and provider not in s['providers']), None)
    if blocking is None:
        return f'{label} is not in your allowed providers. Allow it in AI Providers to use it. No other provider is substituted.'
    title = f' "{blocking["title"]}"' if blocking.get('title') else ''
    return (f'{label} is not allowed for this Task: the provider restriction on Task{title} excludes it. A Child or '
            'later instruction cannot widen it. No other provider is substituted.')


def require_allowed(db, provider, task_id=None):
    reason = denial(db, provider, task_id)
    if reason:
        raise ProviderNotPermitted(provider, 'not_permitted', reason)


def view(db, provider, task_id=None):
    reason = denial(db, provider, task_id)
    allowed, _ = effective(db, task_id)
    return dict(allowed=reason is None, reason=reason, effective=allowed)
