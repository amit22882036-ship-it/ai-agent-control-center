"""Quality-first, usage-aware Auto allocation of a delegated Child's provider, model and effort.

Auto is a user-authorized policy on a Task subtree (never something a worker can
grant itself). A Parent's promotion item may ask for `allocation: {"mode": "auto"}`
with its structured assessment of the work. At the existing promotion preparation
boundary (inside its transaction, before any Child admission) this module chooses
among the candidates that assessment names and records a durable, explainable
decision. The decision is written to the Delegation's existing child_provider /
child_model / child_effort fields, so the unchanged launch and admission path
enforces it exactly like an explicit request; nothing switches after admission.

Order of concerns (priorities, not a weighted score):
1. Hard eligibility: the policy allowlists, enablement, billing acknowledgment,
   readiness, delegated-Child capability, account entitlement and effort support
   (model_catalog.admission with automated=True: never an unverified model), and
   every known usage limit. Nothing here is overridden by suitability.
2. Suitability: only the evidence available - the Parent's own assessment
   (`preferred` before `acceptable`) or, without one, the user's profile. No model
   is ranked by name, age, price or provider. Without evidence the request is
   held with an explanation instead of guessing.
3. Usage distinguishes comparably suitable candidates only: a fresh available
   reading before an unknown one. Percentages are never compared across providers,
   low but nonzero quota stays eligible, unknown quota is never a number.
4. The user's preference order, then a deterministic order, break remaining ties.

No discovery, probe or model request runs here: only durable state and the cached
catalog are read, so no transaction waits on an external process.
"""
import json
import re

from . import model_catalog, provider_policy, providers

NOW = providers.NOW
CATEGORIES = ('architecture', 'implementation', 'bug_fix', 'review', 'security', 'testing', 'documentation',
              'data_analysis', 'planning', 'general')
COMPLEXITY = ('low', 'medium', 'high')
CAPABILITIES = ('coordination',)  # The Child may coordinate its own Children (a managed-capable provider).
LEVELS = ('preferred', 'acceptable')
PROVIDER_DEFAULT = 'provider_default'
MAX_CANDIDATES = 8
# A transparent mapping from the Parent's stated complexity to an effort level, used
# only when the model lists that level; otherwise the provider default applies.
EFFORT_FOR_COMPLEXITY = {'low': 'low', 'medium': 'medium', 'high': 'high'}
SPEC = re.compile(r'^(?P<provider>[a-z]+)(?:/(?P<model>[A-Za-z0-9][A-Za-z0-9._-]{0,127}))?(?:@(?P<effort>[a-z]+))?$')


def migrate_v34(db):
    db.execute('''CREATE TABLE IF NOT EXISTS auto_allocation_policies (
        task_id TEXT PRIMARY KEY REFERENCES tasks(task_id),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        providers TEXT NOT NULL, models TEXT NOT NULL DEFAULT '{}', preferences TEXT NOT NULL DEFAULT '[]',
        profile TEXT NOT NULL DEFAULT '{}',
        updated_by TEXT NOT NULL CHECK(updated_by='user'),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    db.execute('''CREATE TABLE IF NOT EXISTS allocation_decisions (
        decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        delegation_id TEXT NOT NULL REFERENCES delegations(delegation_id),
        parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
        proposal_id TEXT, plan_revision INTEGER, policy_task_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('selected','blocked')),
        provider TEXT, model TEXT, effort TEXT,
        request TEXT NOT NULL, candidates TEXT NOT NULL, explanation TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        superseded_at TEXT, superseded_reason TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS allocation_decisions_delegation ON allocation_decisions(delegation_id,decision_id)')
    columns = {r[1] for r in db.execute('PRAGMA table_info(delegations)')}
    if 'child_effort' not in columns:
        db.execute('ALTER TABLE delegations ADD COLUMN child_effort TEXT')


def migrate_v36(db):
    """Policy revisions for optimistic concurrency. AUTOINCREMENT never reuses a value, so a
    token cannot repeat even across a removed and recreated policy or same-millisecond writes."""
    db.execute('''CREATE TABLE IF NOT EXISTS auto_allocation_policy_revisions (
        revision INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        action TEXT NOT NULL CHECK(action IN ('set','clear')),
        changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(auto_allocation_policies)')}
    if 'revision' not in columns:
        db.execute('ALTER TABLE auto_allocation_policies ADD COLUMN revision INTEGER')
    for (task_id,) in db.execute('SELECT task_id FROM auto_allocation_policies WHERE revision IS NULL').fetchall():
        cursor = db.execute("INSERT INTO auto_allocation_policy_revisions(task_id,action) VALUES (?,'set')", (task_id,))
        db.execute('UPDATE auto_allocation_policies SET revision=? WHERE task_id=?', (cursor.lastrowid, task_id))


def migrate_v35(db):
    """Ordinary delegation (v3) allocation: the stored request and the decision's origin."""
    columns = {r[1] for r in db.execute('PRAGMA table_info(delegations)')}
    if 'allocation_request' not in columns:
        db.execute('ALTER TABLE delegations ADD COLUMN allocation_request TEXT')
    columns = {r[1] for r in db.execute('PRAGMA table_info(allocation_decisions)')}
    if 'origin' not in columns:
        db.execute("ALTER TABLE allocation_decisions ADD COLUMN origin TEXT CHECK(origin IS NULL OR origin IN ('promotion','delegation'))")
        # Every decision recorded before v35 came from promotion.
        db.execute("UPDATE allocation_decisions SET origin='promotion' WHERE origin IS NULL")


def _has(db, table):
    return db is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()


# ---------------------------------------------------------------- policy (user-authored only)

def _spec(value):
    match = SPEC.match(value) if isinstance(value, str) else None
    if not match or match['provider'] not in providers.IDS:
        raise ValueError(f'Invalid candidate "{value}": use provider[/model][@effort] with a known provider')
    if match['effort']:
        providers.validate_effort(match['effort'])
    return dict(provider=match['provider'], model=match['model'], effort=match['effort'])


def normalize_policy(enabled, allowed, models=None, preferences=None, profile=None):
    if type(enabled) is not bool:
        raise ValueError('enabled must be true or false')
    allowed = list(dict.fromkeys(allowed or []))
    if enabled and not allowed:
        raise ValueError('Choose at least one provider Auto may use')
    if any(p not in providers.IDS for p in allowed):
        raise ValueError('Unknown provider in the Auto allowlist')
    models = models or {}
    if not isinstance(models, dict) or any(p not in allowed for p in models):
        raise ValueError('Model allowlists must name allowed providers')
    for provider, names in models.items():
        if not isinstance(names, list) or len(names) > 32:
            raise ValueError('A model allowlist is a list of at most 32 identifiers')
        for name in names:
            if name != PROVIDER_DEFAULT:
                providers.validate_model(name)
    preferences = list(preferences or [])
    if len(preferences) > 16:
        raise ValueError('At most 16 preferences')
    for value in preferences:
        _spec(value)
    profile = profile or {}
    if not isinstance(profile, dict) or any(c not in CATEGORIES for c in profile):
        raise ValueError('Profile categories must be one of: ' + ', '.join(CATEGORIES))
    for entry in profile.values():
        if not isinstance(entry, dict) or set(entry) - set(LEVELS):
            raise ValueError('A profile category lists only preferred and acceptable candidates')
        for level in entry.values():
            if not isinstance(level, list) or len(level) > MAX_CANDIDATES:
                raise ValueError(f'At most {MAX_CANDIDATES} candidates per level')
            for value in level:
                _spec(value)
    return dict(enabled=enabled, providers=allowed, models=models, preferences=preferences, profile=profile)


STALE = object()


def own_policy(db, task_id):
    return db.execute('SELECT revision,updated_at FROM auto_allocation_policies WHERE task_id=?', (task_id,)).fetchone()


def _require_version(db, task_id, expected):
    """Optimistic concurrency (in the caller's write transaction): `expected` is the own
    policy's revision the editor loaded, or None when the Task had no policy of its own."""
    if expected is STALE:
        return
    row = own_policy(db, task_id)
    if (row['revision'] if row else None) != expected:
        raise ValueError('The Auto policy changed since it was opened; reload it and reapply your changes.')


def _next_revision(db, task_id, action):
    return db.execute('INSERT INTO auto_allocation_policy_revisions(task_id,action) VALUES (?,?)',
                      (task_id, action)).lastrowid


def set_policy(db, task_id, expected_revision=STALE, **values):
    """The user's explicit Auto policy for a Task and its subtree (the API caller is the user)."""
    policy = normalize_policy(**values)
    _require_version(db, task_id, expected_revision)
    revision = _next_revision(db, task_id, 'set')
    db.execute(f'''INSERT INTO auto_allocation_policies(task_id,enabled,providers,models,preferences,profile,updated_by,revision)
        VALUES (?,?,?,?,?,?,'user',?) ON CONFLICT(task_id) DO UPDATE SET enabled=excluded.enabled,
        providers=excluded.providers,models=excluded.models,preferences=excluded.preferences,
        profile=excluded.profile,revision=excluded.revision,updated_at={NOW}''',
        (task_id, int(policy['enabled']), json.dumps(policy['providers']), json.dumps(policy['models']),
         json.dumps(policy['preferences']), json.dumps(policy['profile']), revision))
    return policy_for(db, task_id)


def clear_policy(db, task_id, expected_revision=STALE):
    """Remove this Task's own policy (the user's explicit choice): the subtree then follows
    an ancestor's policy, or has none. Never touches an ancestor's policy."""
    _require_version(db, task_id, expected_revision)
    if own_policy(db, task_id) is None:
        raise ValueError('This Task has no policy of its own to remove.')
    _next_revision(db, task_id, 'clear')
    db.execute('DELETE FROM auto_allocation_policies WHERE task_id=?', (task_id,))


def policy_for(db, task_id):
    """The nearest policy on the Task or an ancestor (a subtree inherits it), or None."""
    if not _has(db, 'auto_allocation_policies'):
        return None
    current = task_id
    for _ in range(64):
        if current is None:
            return None
        row = db.execute('SELECT * FROM auto_allocation_policies WHERE task_id=?', (current,)).fetchone()
        if row is not None:
            return dict(task_id=row['task_id'], enabled=bool(row['enabled']), providers=json.loads(row['providers']),
                        models=json.loads(row['models']), preferences=json.loads(row['preferences']),
                        profile=json.loads(row['profile']), updated_at=row['updated_at'],
                        revision=row['revision'] if 'revision' in row.keys() else None)
        parent = db.execute('SELECT parent_task_id FROM tasks WHERE task_id=?', (current,)).fetchone()
        current = parent[0] if parent else None
    return None


# ---------------------------------------------------------------- request (the Parent's assessment)

def validate_request(value):
    """Structural validation of a promotion item's `allocation` (no authority is implied)."""
    if not isinstance(value, dict) or value.get('mode') != 'auto':
        raise ValueError('allocation must be {"mode":"auto", ...}')
    allowed = {'mode', 'category', 'complexity', 'capabilities', 'candidates'}
    if set(value) - allowed:
        raise ValueError('Unknown allocation fields')
    if value.get('category', 'general') not in CATEGORIES:
        raise ValueError('allocation category must be one of: ' + ', '.join(CATEGORIES))
    if value.get('complexity') is not None and value['complexity'] not in COMPLEXITY:
        raise ValueError('allocation complexity must be low, medium or high')
    capabilities = value.get('capabilities', [])
    if not isinstance(capabilities, list) or any(c not in CAPABILITIES for c in capabilities):
        raise ValueError('allocation capabilities may only list: ' + ', '.join(CAPABILITIES))
    candidates = value.get('candidates', [])
    if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
        raise ValueError(f'allocation lists at most {MAX_CANDIDATES} candidates')
    for item in candidates:
        if not isinstance(item, dict) or not {'provider', 'suitability'} <= set(item) \
                or set(item) - {'provider', 'model', 'effort', 'suitability', 'rationale'}:
            raise ValueError('An allocation candidate has provider, suitability and optional model, effort, rationale')
        if item['provider'] not in providers.IDS:
            raise ValueError('Unknown candidate provider; use one of ' + ', '.join(providers.IDS))
        if item['suitability'] not in LEVELS:
            raise ValueError('Candidate suitability is preferred or acceptable')
        providers.validate_model(item.get('model'))
        providers.validate_effort(item.get('effort'))
        rationale = item.get('rationale')
        if rationale is not None and (not isinstance(rationale, str) or len(rationale) > 300):
            raise ValueError('A candidate rationale is text of at most 300 characters')
    return value


# ---------------------------------------------------------------- evaluation (read-only)

def _usage(db, provider, model):
    """'available' only on a fresh provider reading that this model's windows have capacity; else 'unknown'."""
    windows = [w for w in providers.usage_windows(db, provider) if providers.window_applies(w, model)]
    fresh = [w for w in windows if w['fresh']]
    if fresh and all(w['status'] == 'available' for w in fresh):
        return 'available'
    return 'unknown'


def _model_allowed(policy, provider, model):
    names = policy['models'].get(provider)
    if not names:
        return True
    return (model or PROVIDER_DEFAULT) in names


def _check(db, policy, parent, request, provider, model, effort):
    """(eligible effort or None, exclusion reason or None, uncertainties)."""
    uncertain = []
    # The hard provider permission (global and Task subtree) comes before any Auto rule.
    denied = provider_policy.denial(db, provider, parent.get('task_id'))
    if denied:
        return None, denied, uncertain
    if provider not in policy['providers']:
        return None, 'not in the providers you allowed for Auto', uncertain
    if not _model_allowed(policy, provider, model):
        return None, 'not in the models you allowed for this provider', uncertain
    reason = providers.adapter(provider).capability('delegated_child')
    if reason:
        return None, reason, uncertain
    if 'coordination' in request.get('capabilities', []) and not providers.managed_capable(provider):
        return None, 'cannot coordinate its own Children (required capability)', uncertain
    if provider == 'mock':
        if model is not None or effort is not None:
            return None, 'Mock has no models or effort levels', uncertain
        return None, None, uncertain
    config = providers.settings(db, provider)
    if not config['enabled']:
        return None, f'{providers.LABELS[provider]} is disabled', uncertain
    if not config['billing_confirmed']:
        return None, 'its billing route is not acknowledged', uncertain
    readiness = providers.cached(provider)
    if readiness is not None and not readiness.ready:
        return None, f'not ready ({readiness.reason or readiness.state})', uncertain
    if readiness is None:
        uncertain.append('readiness not checked recently; rechecked before launch')
    limits = providers.constraints(db, provider, model)
    if limits:
        hard = [c for c in limits if c['hard']]
        if hard:
            return None, 'confirmed usage limit: ' + providers.describe_constraints(hard), uncertain
        return None, ('recorded usage limit not confirmed renewed (automated launches never retry it; refresh usage '
                      'or clear it): ' + providers.describe_constraints(limits)), uncertain
    try:
        model_catalog.admission(db, provider, model, effort, automated=True)
    except providers.ProviderRefused as exc:
        return None, str(exc).split(': ', 1)[-1], uncertain
    return effort, None, uncertain


def _effort_for(db, policy, parent, request, provider, model, explicit):
    """The candidate's effort: as named by the evidence (validated, never changed), else the
    complexity mapping when the model lists it, else the provider default."""
    if explicit:
        return explicit, _check(db, policy, parent, request, provider, model, explicit)
    target = EFFORT_FOR_COMPLEXITY.get(request.get('complexity'))
    if target and provider not in ('gemini', 'mock'):
        effort, reason, uncertain = _check(db, policy, parent, request, provider, model, target)
        if reason is None:
            return target, (effort, reason, uncertain)
    return None, _check(db, policy, parent, request, provider, model, None)


def _evidence(request, policy):
    """[(level, spec, rationale, source)] from the Parent's assessment, else the user's profile."""
    if request.get('candidates'):
        return [(c['suitability'], dict(provider=c['provider'], model=c.get('model'), effort=c.get('effort')),
                 c.get('rationale'), 'parent_assessment') for c in request['candidates']]
    entry = policy['profile'].get(request.get('category', 'general'), {})
    return [(level, _spec(value), None, 'user_profile') for level in LEVELS for value in entry.get(level, [])]


def _label(provider, model, effort):
    return (f'{providers.LABELS.get(provider, provider)} · {model or "provider default model"} · '
            f'{effort + " effort" if effort else "provider default effort"}')


def evaluate(db, policy, parent, request):
    """Choose among the evidenced candidates. Returns the decision fields (nothing is written)."""
    evaluated = []
    for order, (level, spec, rationale, source) in enumerate(_evidence(request, policy)):
        effort, (_, reason, uncertain) = _effort_for(db, policy, parent, request, spec['provider'], spec['model'],
                                                     spec['effort'])
        evaluated.append(dict(provider=spec['provider'], model=spec['model'], effort=effort, level=level,
                              rationale=rationale, source=source, order=order, excluded=reason,
                              usage=None if reason else _usage(db, spec['provider'], spec['model']),
                              uncertain=uncertain))
    eligible = [c for c in evaluated if not c['excluded']]
    preferences = policy['preferences']

    def preference(c):
        # provider: any model and effort; provider/model: any effort of that model;
        # provider[/model]@effort: only that effort.
        for index, value in enumerate(preferences):
            wanted = _spec(value)
            if (wanted['provider'] == c['provider'] and wanted['model'] in (None, c['model'])
                    and wanted['effort'] in (None, c['effort'])):
                return index
        return len(preferences)
    chosen, level = None, None
    for level in LEVELS:
        tier = [c for c in eligible if c['level'] == level]
        if tier:
            # Comparable suitability: usage evidence, the user's preferences, the evidence
            # order, then a deterministic order. Never a cross-provider percentage.
            tier.sort(key=lambda c: (c['usage'] != 'available', preference(c), c['order'], c['provider'],
                                     c['model'] or ''))
            chosen = tier[0]
            break
    status = 'selected' if chosen else 'blocked'
    return dict(status=status, provider=chosen and chosen['provider'], model=chosen and chosen['model'],
                effort=chosen and chosen['effort'], level=chosen and level, candidates=evaluated,
                explanation=explain(request, evaluated, chosen, level))


def explain(request, evaluated, chosen, level):
    category = request.get('category', 'general').replace('_', ' ')
    complexity = f' ({request["complexity"]} complexity)' if request.get('complexity') else ''
    required = request.get('capabilities') or []
    lines = []
    if not evaluated:
        lines.append(f'No suitability evidence for this {category} work: the Parent named no candidates and your Auto '
                     'policy has no profile for this category. Nothing was guessed; choose a provider explicitly or '
                     'add a profile, then retry.')
        return '\n'.join(lines)
    excluded = [c for c in evaluated if c['excluded']]
    if chosen is None:
        lines.append(f'Held: no sufficiently suitable, authorized candidate is available for this {category} work'
                     f'{complexity}. No weaker or unassessed model was substituted.')
    else:
        source = 'the Parent\'s assessment' if chosen['source'] == 'parent_assessment' else 'your Auto profile'
        lines.append(f'Selected {_label(chosen["provider"], chosen["model"], chosen["effort"])} for {category} work'
                     f'{complexity}: rated {level} by {source}.' + (f' Rationale: {chosen["rationale"]}'
                                                                     if chosen['rationale'] else ''))
        peers = [c for c in evaluated if c is not chosen and not c['excluded'] and c['level'] == level]
        if peers:
            lines.append('Also eligible at the same level: ' + '; '.join(_label(c['provider'], c['model'], c['effort'])
                                                                      for c in peers) + '.')
        better = [c for c in excluded if c['level'] == 'preferred' and level == 'acceptable']
        if better:
            lines.append('Preferred candidates were unavailable, so an acceptable one was chosen.')
        lines.append('Usage: ' + ('a fresh provider reading shows capacity.' if chosen['usage'] == 'available'
                                  else 'not reported or not current (unknown, not zero).')
                     + ' Execution may pause if a usage limit is reached; it never switches provider automatically.')
        if chosen['uncertain']:
            lines.append('Uncertain: ' + '; '.join(chosen['uncertain']) + '.')
    if required:
        lines.append('Required capabilities: ' + ', '.join(required) + '.')
    for c in excluded:
        lines.append(f'Excluded {_label(c["provider"], c["model"], c["effort"])} ({c["level"]}): {c["excluded"]}')
    lines.append('Suitability reflects the stated assessment, not a benchmark; no model is ranked by name, age or price.')
    return '\n'.join(line[:600] for line in lines)


# ---------------------------------------------------------------- final admission

class AllocationRevoked(ValueError):
    """The recorded Auto selection is no longer authorized. Raised only inside admission,
    before any process exists, so the unstarted launch can safely return to preparation."""


def require_authorized(db, record):
    """At every admission boundary (the caller's transaction): the Delegation still carries the
    current Auto selection and the user's effective policy still permits it. Reads durable state
    only. Refuses; never reallocates (a later bounded preparation pass reconsiders)."""
    decision = current(db, record['delegation_id'])
    selected = (record.get('child_provider'), record.get('child_model'), record.get('child_effort'))
    if decision is None or decision['status'] != 'selected':
        raise AllocationRevoked('Auto allocation has no current selection; nothing was launched.')
    if (decision['provider'], decision['model'], decision['effort']) != selected:
        raise AllocationRevoked('The Child provider no longer matches its recorded Auto selection; nothing was launched.')
    policy = policy_for(db, record['parent_task_id'])
    label = _label(*selected)
    hard = provider_policy.denial(db, selected[0], record['parent_task_id'])
    if hard:
        raise AllocationRevoked(f'Auto allocation of {label} is no longer permitted: {hard}')
    if policy is None or not policy['enabled']:
        reason = 'the user disabled Auto for this Task'
    elif selected[0] not in policy['providers']:
        reason = f'{providers.LABELS.get(selected[0], selected[0])} is no longer an allowed provider'
    elif not _model_allowed(policy, selected[0], selected[1]):
        reason = f'{selected[1] or "the provider default model"} is no longer an allowed model'
    else:
        return policy
    raise AllocationRevoked(f'Auto allocation of {label} is no longer authorized ({reason}). Nothing was launched and '
                            'no other provider was substituted; the next preparation pass reconsiders it.')


# ---------------------------------------------------------------- durable decisions

def current(db, delegation_id):
    if not _has(db, 'allocation_decisions'):
        return None
    row = db.execute('''SELECT * FROM allocation_decisions WHERE delegation_id=? AND superseded_at IS NULL
        ORDER BY decision_id DESC LIMIT 1''', (delegation_id,)).fetchone()
    return _view(row) if row else None


def history(db, delegation_id):
    if not _has(db, 'allocation_decisions'):
        return []
    return [_view(r) for r in db.execute('SELECT * FROM allocation_decisions WHERE delegation_id=? ORDER BY decision_id',
                                         (delegation_id,))]


def _view(row):
    value = dict(row)
    value['request'] = json.loads(value['request'])
    value['candidates'] = json.loads(value['candidates'])
    return value


def _write(db, record, row, policy, request, result, superseding=None, reason=None):
    if superseding is not None:
        db.execute(f'UPDATE allocation_decisions SET superseded_at={NOW},superseded_reason=? WHERE decision_id=?',
                   (reason, superseding))
    db.execute('''INSERT INTO allocation_decisions(delegation_id,parent_task_id,proposal_id,plan_revision,policy_task_id,
        status,provider,model,effort,request,candidates,explanation,origin) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (record['delegation_id'], record['parent_task_id'], row['proposal_id'] if row else None,
         row['revision'] if row else None, policy and policy['task_id'], result['status'], result['provider'],
         result['model'], result['effort'], json.dumps(request, sort_keys=True), json.dumps(result['candidates']),
         ((f'Reconsidered before admission: {reason}\n') if reason else '') + result['explanation'],
         'promotion' if row else 'delegation'))
    db.execute('UPDATE delegations SET child_provider=?,child_model=?,child_effort=? WHERE delegation_id=?',
               (result['provider'], result['model'], result['effort'], record['delegation_id']))


def decide(db, record, row, request):
    """At the promotion preparation boundary (caller's BEGIN IMMEDIATE). Returns (selected, reason).

    An existing selection is kept while it stays eligible (a fresh ranking never
    reshuffles it); a selection that became ineligible, or a held request, is
    reconsidered only before any Child admission. Callers never invoke this beneath
    an admitted, launching or uncertain Worker.
    """
    previous = current(db, record['delegation_id'])
    policy = policy_for(db, record['parent_task_id'])
    if policy is None or not policy['enabled']:
        reason = ('Auto allocation is not authorized for this Task: the user has not enabled it. A worker cannot '
                  'grant it; choose a provider explicitly or ask the user to enable Auto.')
        if previous is None or previous['status'] != 'blocked' or previous['explanation'] != reason:
            result = dict(status='blocked', provider=None, model=None, effort=None, candidates=[], explanation=reason)
            _write(db, record, row, policy, request, result, previous and previous['decision_id'],
                   'Auto is no longer authorized' if previous else None)
        return False, reason
    parent = db.execute('''SELECT g.* FROM task_assignments a JOIN agents g USING(agent_id) WHERE a.task_id=?
        ORDER BY a.rowid DESC LIMIT 1''', (record['parent_task_id'],)).fetchone()
    parent = dict(parent) if parent else dict(agent_type='mock', sandbox='read-only')
    parent['task_id'] = record['parent_task_id']  # The Child inherits this Task subtree's permissions.
    if previous and previous['status'] == 'selected':
        _, reason, _ = _check(db, policy, parent, request, previous['provider'], previous['model'], previous['effort'])
        if reason is None:
            return True, previous['explanation']
        result = evaluate(db, policy, parent, request)
        _write(db, record, row, policy, request, result, previous['decision_id'],
               f'{_label(previous["provider"], previous["model"], previous["effort"])} became ineligible ({reason})')
    else:
        result = evaluate(db, policy, parent, request)
        if previous is None or previous['explanation'] != result['explanation'] or result['status'] != 'blocked':
            _write(db, record, row, policy, request, result, previous and previous['decision_id'],
                   'reconsidered a held request' if previous else None)
    return result['status'] == 'selected', current(db, record['delegation_id'])['explanation']


def delegation_request(record):
    """An ordinary (v3) delegation's Auto request, or None (explicit, legacy or promotion)."""
    stored = record.get('allocation_request')
    value = json.loads(stored) if stored else None
    return value if value and value.get('mode') == 'auto' else None


def require_delegation_authorized(db, record):
    """Admission check for an ordinary Auto delegation (promotion items use promotion.require_ready)."""
    if delegation_request(record) is not None:
        require_authorized(db, record)


def summary(db, delegation_id):
    """Compact decision view for promotion inspection and the inspector."""
    decision = current(db, delegation_id)
    if decision is None:
        return None
    return {k: decision.get(k) for k in ('decision_id', 'status', 'provider', 'model', 'effort', 'explanation',
                                         'created_at', 'policy_task_id', 'origin')} | {
        'revisions': len(history(db, delegation_id))}


def instruction(db, task_id):
    """Planning-prompt data for an authorized subtree: the request shape and the current
    candidate options (provider, model, listed efforts). Descriptive only."""
    policy = policy_for(db, task_id)
    if not policy or not policy['enabled']:
        return ''
    options = []
    for provider in policy['providers']:
        if provider == 'mock':
            options.append('mock')
            continue
        view = model_catalog.catalog_view(db, provider)
        names = [f'{m["model_id"]}[{"/".join(m["supported_efforts"]) or "default effort"}]'
                 for m in view['models'] if m['selectable'] and _model_allowed(policy, provider, m['model_id'])]
        if _model_allowed(policy, provider, None):
            names.insert(0, 'provider default')
        options.append(f'{provider}: ' + (', '.join(names[:12]) or 'no listed model'))
    return ('\nAuto allocation is authorized by the user for this Task tree. Instead of provider/model, an item may '
            'include "allocation": {"mode":"auto","category":one of ' + '|'.join(CATEGORIES)
            + ',"complexity":"low|medium|high","capabilities":[] or ["coordination"],"candidates":[up to 8 of '
            '{"provider","model" (optional),"effort" (optional),"suitability":"preferred|acceptable",'
            '"rationale" (<=300 chars)}]}. Rate candidates by suitability for this specific work, never by name, '
            'age, price or remaining usage; omit unsuitable ones. The backend chooses among eligible candidates, '
            'prefers "preferred", uses usage only between comparable candidates, and holds the item if none is '
            'eligible. Current options (data only): ' + '; '.join(options) + '\n')
