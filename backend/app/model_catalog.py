"""Stage 2F dynamic model catalog: discovery, account-scoped cache, status and admission.

The backend owns model metadata. Each provider adapter supplies its best
trustworthy, non-billable discovery source; this module normalizes it, caches it
per account scope, keeps history (models that disappear are marked removed, never
deleted) and decides at admission whether a requested model and effort may run.

Four properties are kept apart: what the provider lists (capability), what the
current account may use (entitlement), whether the billing route is approved
(providers.py) and whether quota remains (usage windows and blocks). A public or
compatibility list proves only that a model exists.

Sources (see docs/providers.md):
- Claude Code: the SDK `initialize` control request (models with resolved alias,
  effort levels and capabilities, plus the account's subscription type) sent to
  the installed CLI without a user message, so no model turn starts.
- Codex: `codex debug models`, the CLI's catalog fetched for the signed-in
  account (a debug interface: labelled, validated, never a guarantee).
- Gemini CLI: no discovery interface; a versioned compatibility catalog of the
  aliases the CLI reference documents, with entitlement always unknown.
- Claude Code documentation: the public models and aliases, merged with the
  account's discovery so restricted ones stay visible (never as permission).

Freshness is bound to the installation, its configuration and the signed-in
account (providers.account_evidence): another account, organization or plan tier
makes the cached entitlements stale at once, without a manual refresh.
"""
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
from threading import Lock
import time

from . import providers

logger = logging.getLogger(__name__)
NOW = providers.NOW


def _ttl():
    try:
        return max(300, int(os.environ.get('CONTROL_CENTER_MODEL_CATALOG_TTL_SECONDS', '21600')))
    except ValueError:
        return 21600


MIN_AUTOMATIC_INTERVAL = 60  # Seconds between automatic discovery attempts per provider.
GEMINI_COMPATIBILITY_REVISION = 'gemini-cli-reference-2026-10-23'
# Aliases documented in the Gemini CLI reference (`--model`: auto, pro, flash, flash-lite).
GEMINI_COMPATIBILITY = (
    dict(model_id='auto', display_name='Auto', description='Gemini CLI routes between models automatically.',
         routing='auto'),
    dict(model_id='pro', display_name='Pro', description='Gemini CLI alias for its Pro model.', routing='alias'),
    dict(model_id='flash', display_name='Flash', description='Gemini CLI alias for its Flash model.', routing='alias'),
    dict(model_id='flash-lite', display_name='Flash-Lite', description='Gemini CLI alias for its Flash-Lite model.',
         routing='alias'),
)

STATUS_LABELS = {
    'available': 'Available',
    'deprecated': 'Deprecated',
    'unavailable_subscription': 'Not available on this account',
    'requires_plan': 'Requires a specific plan',
    'org_restricted': 'Restricted by organization',
    'unsupported_cli': 'Not supported by installed CLI',
    'provider_disabled': 'Provider disabled',
    'provider_not_authenticated': 'Provider not signed in',
    'removed': 'Removed by provider',
    'temporarily_unavailable': 'Temporarily unavailable',
    'model_quota_exhausted': 'Model quota exhausted',
    'shared_quota_exhausted': 'Shared quota exhausted',
    'billing_blocked': 'Billing policy blocked',
    'entitlement_unknown': 'Availability not verified',
    'catalog_stale': 'Catalog information stale',
    'not_offered': 'Not offered to this account',
    'pin_not_enforceable': 'Exact model not enforceable',
}
# Claude Code documentation (code.claude.com/docs/en/model-config, read 2026-10-23):
# models and aliases that exist publicly. Existence is never account permission: an
# entry the installed CLI does not offer to this account is shown, never admitted as
# listed. Notes are the documentation's own statements; plans are never inferred.
CLAUDE_DOCUMENTED_REVISION = 'claude-code-model-config-2026-10-23'
_FABLE_NOTE = ('Claude Code documentation: depending on your plan and seat tier, Fable usage can bill to usage credits '
               "instead of drawing on your plan's included limits.")
CLAUDE_DOCUMENTED = (
    # Family aliases appear in Claude Code's model list for accounts that can use them.
    dict(model_id='fable', display_name='Fable', kind='family', note=_FABLE_NOTE),
    dict(model_id='opus', display_name='Opus', kind='family', note=None),
    dict(model_id='sonnet', display_name='Sonnet', kind='family', note=None),
    dict(model_id='haiku', display_name='Haiku', kind='family', note=None),
    # Modes: not necessarily listed, so their absence proves nothing.
    dict(model_id='best', display_name='Best', kind='mode',
         note='Claude Code documentation: Fable where available to you, otherwise the same model as opus. ' + _FABLE_NOTE),
    dict(model_id='opusplan', display_name='Opus Plan', kind='mode',
         note='Claude Code documentation: opus during plan mode, then sonnet for execution.'),
    # Full model names the documentation lists.
    *(dict(model_id=name, display_name=name, kind='version', note=_FABLE_NOTE if 'fable' in name else None)
      for name in ('claude-fable-5-1', 'claude-fable-5', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-sonnet-5',
                   'claude-haiku-5-5', 'claude-opus-4-8', 'claude-opus-4-6', 'claude-sonnet-4-6', 'claude-sonnet-4-5',
                   'claude-haiku-4-5')),
)
GEMINI_PIN_REASON = 'Gemini CLI cannot guarantee that a specific model runs: its fallback to another model is on by default with no documented way to disable it, and its output does not identify which model produced the response. Choose Auto (Gemini CLI routes intentionally) or the provider default.'
UNEXPOSED_IDENTITY_SECONDS = 120  # Without account evidence, a catalog is trusted only briefly.
SELECTABLE = ('available', 'deprecated')
ATTEMPTABLE = ('entitlement_unknown', 'catalog_stale')


def migrate_v33(db):
    """v32 -> v33. Additive and idempotent."""
    agent_columns = {r['name'] for r in db.execute('PRAGMA table_info(agents)')}
    if 'effort' not in agent_columns:
        db.execute('ALTER TABLE agents ADD COLUMN effort TEXT')
    if 'session_config' not in agent_columns:
        db.execute('ALTER TABLE agents ADD COLUMN session_config TEXT')
    execution_columns = {r['name'] for r in db.execute('PRAGMA table_info(executions)')}
    for name in ('requested_effort', 'effective_model', 'effective_effort', 'config_source', 'catalog_scope',
                 'catalog_revision', 'entitlement_state', 'resolved_model', 'effective_note', 'configured_at'):
        if name not in execution_columns:
            db.execute(f'ALTER TABLE executions ADD COLUMN {name} TEXT')
    change_columns = {r['name'] for r in db.execute('PRAGMA table_info(agent_provider_changes)')}
    for name in ('from_effort', 'to_effort'):
        if name not in change_columns:
            db.execute(f'ALTER TABLE agent_provider_changes ADD COLUMN {name} TEXT')
    db.execute(f'''CREATE TABLE IF NOT EXISTS model_catalog (
        provider TEXT NOT NULL CHECK(provider IN ('codex','claude','gemini')),
        scope_key TEXT NOT NULL,
        model_id TEXT NOT NULL CHECK(length(model_id) BETWEEN 1 AND 128),
        aliases TEXT NOT NULL DEFAULT '[]',
        display_name TEXT NOT NULL,
        family TEXT,
        description TEXT,
        listing TEXT NOT NULL CHECK(listing IN ('listed','hidden','deprecated','compatibility')),
        supported_efforts TEXT NOT NULL DEFAULT '[]',
        default_effort TEXT,
        capabilities TEXT NOT NULL DEFAULT '{{}}',
        entitlement TEXT NOT NULL CHECK(entitlement IN ('cli_listed','unknown','unavailable')),
        plan_requirement TEXT,
        plan_source TEXT,
        org_restricted INTEGER CHECK(org_restricted IS NULL OR org_restricted IN (0,1)),
        min_cli_version TEXT,
        resolved_model TEXT,
        previous_resolved_model TEXT,
        replacement TEXT,
        source TEXT NOT NULL,
        confidence TEXT NOT NULL,
        first_seen_at TEXT NOT NULL DEFAULT ({NOW}),
        last_seen_at TEXT NOT NULL DEFAULT ({NOW}),
        removed_at TEXT,
        PRIMARY KEY(provider, scope_key, model_id))''')
    db.execute(f'''CREATE TABLE IF NOT EXISTS model_catalog_refreshes (
        refresh_id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        scope_key TEXT,
        scope_label TEXT,
        account_plan TEXT,
        account_plan_source TEXT,
        cli_version TEXT,
        fingerprint TEXT,
        source TEXT,
        status TEXT NOT NULL CHECK(status IN ('ok','failed','unsupported')),
        reason TEXT,
        trigger TEXT,
        model_count INTEGER,
        revision TEXT,
        started_at TEXT NOT NULL DEFAULT ({NOW}),
        finished_at TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS model_catalog_refresh_provider ON model_catalog_refreshes(provider, refresh_id)')
    db.execute(f'''CREATE TABLE IF NOT EXISTS model_denials (
        denial_id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        scope_key TEXT NOT NULL,
        model_id TEXT NOT NULL,
        reason TEXT NOT NULL,
        source TEXT NOT NULL,
        execution_id TEXT,
        observed_at TEXT NOT NULL DEFAULT ({NOW}),
        cleared_at TEXT)''')


def _hash(*parts):
    return sha256(json.dumps(parts, default=str).encode()).hexdigest()[:24]


def _age(stamp):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- normalization

def normalize_claude(response, version):
    models = response.get('models') if isinstance(response, dict) else None
    if not isinstance(models, list):
        raise ValueError('initialize response has no model list')
    account = response.get('account') if isinstance(response.get('account'), dict) else {}
    result = []
    for item in models:
        if not isinstance(item, dict) or not isinstance(item.get('value'), str) or not item['value']:
            raise ValueError('invalid model entry')
        efforts = item.get('supportedEffortLevels')
        efforts = [e for e in efforts if isinstance(e, str)] if isinstance(efforts, list) else []
        result.append(dict(
            model_id=item['value'][:128], aliases=[], display_name=str(item.get('displayName') or item['value'])[:120],
            description=str(item.get('description') or '')[:300] or None, listing='listed',
            supported_efforts=efforts if item.get('supportsEffort') is not False else [],
            default_effort=None,
            capabilities={k: item[k] for k in ('supportsEffort', 'supportsAdaptiveThinking', 'supportsFastMode',
                                               'supportsAutoMode') if isinstance(item.get(k), bool)},
            entitlement='cli_listed', plan_requirement=None, plan_source=None, org_restricted=None,
            resolved_model=item.get('resolvedModel') if isinstance(item.get('resolvedModel'), str) else None,
            source='claude_code_initialize', confidence='cli_reported'))
    plan = account.get('subscriptionType') if isinstance(account.get('subscriptionType'), str) else None
    # Entitlement belongs to the account and its plan: a plan change is a new scope.
    identity = _hash('claude', account.get('email'), account.get('organization'), account.get('tokenSource'),
                     account.get('apiProvider'), plan)
    label = (f'Claude Code {".".join(map(str, version)) if version else "?"} · '
             + (f'plan reported as "{plan}"' if plan else 'plan not reported')
             + (' · organization account' if account.get('organization') else ''))
    return result, identity, label, plan


def normalize_codex(data, version):
    result = []
    for item in data.get('models', []):
        if not isinstance(item, dict) or not isinstance(item.get('slug'), str) or not item['slug']:
            raise ValueError('invalid model entry')
        levels = item.get('supported_reasoning_levels') if isinstance(item.get('supported_reasoning_levels'), list) else []
        efforts = [l.get('effort') if isinstance(l, dict) else l for l in levels]
        efforts = [e for e in efforts if isinstance(e, str)]
        upgrade = item.get('upgrade')
        nux = item.get('availability_nux') if isinstance(item.get('availability_nux'), dict) else {}
        unavailable = upgrade not in (None, False, {}, '')
        message = nux.get('message') if isinstance(nux.get('message'), str) else None
        # Provider wording only, verbatim; a plan is never inferred from a model family.
        plan_text = (upgrade if isinstance(upgrade, str) else message) if unavailable else None
        result.append(dict(
            model_id=item['slug'][:128], aliases=[], display_name=str(item.get('display_name') or item['slug'])[:120],
            description=str(item.get('description') or '')[:300] or None,
            listing='listed' if item.get('visibility') in (None, 'list') else 'hidden',
            supported_efforts=efforts, default_effort=item.get('default_reasoning_level') if isinstance(
                item.get('default_reasoning_level'), str) else None,
            capabilities={k: item[k] for k in ('supports_reasoning_effort_updates', 'supported_in_api')
                          if isinstance(item.get(k), bool)},
            entitlement='unavailable' if unavailable else 'cli_listed',
            plan_requirement=plan_text[:200] if plan_text else None,
            plan_source='codex_model_catalog' if plan_text else None, org_restricted=None, resolved_model=None,
            source='codex_debug_models', confidence='cli_debug_interface'))
    identity_field = data.get('identity') if isinstance(data.get('identity'), str) else None
    if identity_field:
        identity = _hash('codex', identity_field)
    else:
        # No account identity reported: scope to the sign-in file's state (never its content).
        auth = Path(os.environ.get('CODEX_HOME') or Path(os.environ.get('USERPROFILE') or Path.home()) / '.codex') / 'auth.json'
        try:
            stat = auth.stat()
            identity = _hash('codex-auth', stat.st_mtime_ns, stat.st_size)
        except OSError:
            identity = _hash('codex-unknown-account')
    return result, identity, f'Codex {version or "?"} · ChatGPT sign-in', None


def merge_claude_documented(models):
    """Add the documented models the account's discovery did not report.

    A family alias Claude Code does not offer to this account is shown as not
    available (its plan stays "not specified by provider"); a mode or a full model
    name it does not list stays unverified (an explicit attempt), because Claude
    Code's list does not enumerate every runnable version.
    """
    listed = {m['model_id'] for m in models} | {m['resolved_model'] for m in models if m['resolved_model']}
    notes = {d['model_id']: d['note'] for d in CLAUDE_DOCUMENTED if d['note']}
    for model in models:
        if notes.get(model['model_id']):
            model['capabilities'] = {**model['capabilities'], 'doc_note': notes[model['model_id']]}
    for item in CLAUDE_DOCUMENTED:
        if item['model_id'] in listed:
            continue
        family = item['kind'] == 'family'
        models.append(dict(
            model_id=item['model_id'], aliases=[], display_name=item['display_name'], description=None,
            listing='compatibility', supported_efforts=[], default_effort=None,  # A public, not account, listing.
            capabilities={'doc_note': item['note'], 'not_offered': family} if item['note'] else {'not_offered': family},
            entitlement='unavailable' if family else 'unknown', plan_requirement=None, plan_source=None,
            org_restricted=None, resolved_model=None, source=CLAUDE_DOCUMENTED_REVISION, confidence='documented'))
    return models


def normalize_gemini():
    result = [dict(model_id=m['model_id'], aliases=[], display_name=m['display_name'], description=m['description'],
                   listing='compatibility', supported_efforts=[], default_effort=None,
                   capabilities={'routing': m['routing']}, entitlement='unknown', plan_requirement=None, plan_source=None,
                   org_restricted=None, resolved_model=None, source=GEMINI_COMPATIBILITY_REVISION,
                   confidence='documented_alias') for m in GEMINI_COMPATIBILITY]
    identity = _hash('gemini', providers.GEMINI.config_fingerprint())
    return result, identity, 'Gemini CLI · compatibility catalog (no account discovery)', None


# ---------------------------------------------------------------- refresh (coalesced, bounded)

_locks = {provider: Lock() for provider in providers.REAL}


def scope_fingerprint(provider):
    """Installation, configuration and account evidence the cached catalog must still describe."""
    return json.dumps([providers._fingerprint(provider), providers.account_evidence(provider)], default=str)


def _identity_exposed(fingerprint):
    try:
        return json.loads(fingerprint)[1] is not None
    except (ValueError, TypeError, IndexError, KeyError):
        return False
_last_attempt: dict[str, float] = {}


def _store_catalog(db, provider, scope_key, models):
    seen = set()
    for model in models:
        seen.add(model['model_id'])
        row = db.execute('SELECT resolved_model FROM model_catalog WHERE provider=? AND scope_key=? AND model_id=?',
                         (provider, scope_key, model['model_id'])).fetchone()
        previous = row['resolved_model'] if row and row['resolved_model'] != model['resolved_model'] else None
        db.execute(f'''INSERT INTO model_catalog(provider,scope_key,model_id,aliases,display_name,family,description,
            listing,supported_efforts,default_effort,capabilities,entitlement,plan_requirement,plan_source,org_restricted,
            resolved_model,source,confidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(provider,scope_key,model_id) DO UPDATE SET aliases=excluded.aliases,
            display_name=excluded.display_name,description=excluded.description,listing=excluded.listing,
            supported_efforts=excluded.supported_efforts,default_effort=excluded.default_effort,
            capabilities=excluded.capabilities,entitlement=excluded.entitlement,plan_requirement=excluded.plan_requirement,
            plan_source=excluded.plan_source,org_restricted=excluded.org_restricted,
            previous_resolved_model=COALESCE(?, model_catalog.previous_resolved_model),
            resolved_model=excluded.resolved_model,source=excluded.source,confidence=excluded.confidence,
            last_seen_at={NOW},removed_at=NULL''',
            (provider, scope_key, model['model_id'], json.dumps(model['aliases']), model['display_name'],
             model.get('family'), model['description'], model['listing'], json.dumps(model['supported_efforts']),
             model['default_effort'], json.dumps(model['capabilities']), model['entitlement'],
             model['plan_requirement'], model['plan_source'], model['org_restricted'], model['resolved_model'],
             model['source'], model['confidence'], previous))
    marks = ','.join('?' for _ in seen) or "''"
    # Models no longer listed stay identifiable (history); they are only marked removed.
    db.execute(f'''UPDATE model_catalog SET removed_at={NOW} WHERE provider=? AND scope_key=? AND removed_at IS NULL
        AND model_id NOT IN ({marks})''', (provider, scope_key, *seen))


def refresh(connect, provider, *, force=False, trigger='user'):
    """One discovery for a provider; concurrent callers share it. Never a model request."""
    if provider not in providers.REAL or connect is None:
        return None
    lock = _locks[provider]
    if not lock.acquire(timeout=providers.DISCOVERY_TIMEOUT + 15):
        raise providers.ProviderRefused(provider, 'discovery_busy', 'Model discovery is already running.')
    try:
        fingerprint = scope_fingerprint(provider)
        with connect() as db:
            latest = latest_refresh(db, provider)
        recent = _last_attempt.get(provider)
        if (not force and recent is not None and time.monotonic() - recent < MIN_AUTOMATIC_INTERVAL and latest
                and latest['fingerprint'] == fingerprint):
            return latest  # Coalesced: a discovery just ran for this installation and account.
        _last_attempt[provider] = time.monotonic()
        status, reason, models, scope_key, label, plan, version = 'ok', None, [], None, None, None, None
        try:
            if provider == 'claude':
                response, version_tuple = providers.CLAUDE.discover_models()
                version = '.'.join(map(str, version_tuple)) if version_tuple else None
                if version_tuple is not None and version_tuple < providers.CLAUDE_RESOLVED_MODEL_VERSION:
                    logger.info('Claude Code %s reports no resolved alias models', version)
                models, identity, label, plan = normalize_claude(response, version_tuple)
                models = merge_claude_documented(models)
            elif provider == 'codex':
                data, version = providers.CODEX.discover_models()
                models, identity, label, plan = normalize_codex(data, version)
            else:
                found = providers.probe('gemini')
                version = found.version
                models, identity, label, plan = normalize_gemini()
            scope_key = _hash(provider, identity, version, fingerprint)
        except providers.ProviderRefused as exc:
            status = 'unsupported' if exc.code in ('discovery_unsupported',) else 'failed'
            reason = str(exc)[:300]
        except Exception as exc:  # Unrecognized output is a failed discovery, never a catalog.
            status, reason = 'failed', f'Model discovery failed ({type(exc).__name__}).'
            logger.exception('Model discovery failed for %s', provider)
        revision = _hash(provider, scope_key, [m['model_id'] for m in models], [m['supported_efforts'] for m in models])[:12] if models else None
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if status == 'ok':
                _store_catalog(db, provider, scope_key, models)
            db.execute(f'''INSERT INTO model_catalog_refreshes(provider,scope_key,scope_label,account_plan,
                account_plan_source,cli_version,fingerprint,source,status,reason,trigger,model_count,revision,finished_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,{NOW})''',
                (provider, scope_key, label, plan, 'claude_code_initialize' if plan else None, version, fingerprint,
                 models[0]['source'] if models else None, status, reason, trigger, len(models), revision))
            db.commit()
        with connect() as db:
            return latest_refresh(db, provider)
    finally:
        lock.release()


def latest_refresh(db, provider):
    if not providers._has(db, 'model_catalog_refreshes'):
        return None
    row = db.execute('SELECT * FROM model_catalog_refreshes WHERE provider=? ORDER BY refresh_id DESC LIMIT 1',
                     (provider,)).fetchone()
    return dict(row) if row else None


def current_scope(db, provider):
    """The latest successful discovery, if it still describes this installation and configuration."""
    if not providers._has(db, 'model_catalog_refreshes'):
        return None
    row = db.execute("""SELECT * FROM model_catalog_refreshes WHERE provider=? AND status='ok'
        ORDER BY refresh_id DESC LIMIT 1""", (provider,)).fetchone()
    if row is None:
        return None
    scope = dict(row)
    age = _age(scope['finished_at'])
    scope['installation_matches'] = scope['fingerprint'] == scope_fingerprint(provider)
    scope['identity_exposed'] = _identity_exposed(scope['fingerprint'])
    # Without local account evidence an account change cannot be noticed: such a
    # catalog is trusted only briefly, so admission rediscovers it.
    limit = _ttl() if scope['identity_exposed'] else UNEXPOSED_IDENTITY_SECONDS
    scope['fresh'] = bool(scope['installation_matches'] and age is not None and age <= limit)
    return scope


def needs_refresh(db, provider):
    scope = current_scope(db, provider)
    return scope is None or not scope['fresh']


def ensure_fresh(connect, provider, *, trigger='admission'):
    """Before admission: refresh untrustworthy evidence (bounded, coalesced). Failures leave it stale."""
    if provider not in providers.REAL or connect is None:
        return
    with connect() as db:
        stale = needs_refresh(db, provider)
    if stale:
        try:
            refresh(connect, provider, trigger=trigger)
        except providers.ProviderRefused:
            logger.info('Model discovery for %s could not refresh before admission', provider)


def mark_stale(connect, provider):
    """A provider signal (model unavailable, entitlement denial) makes the cached catalog untrustworthy."""
    _last_attempt.pop(provider, None)
    if connect is None:
        return
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""UPDATE model_catalog_refreshes SET fingerprint='stale' WHERE refresh_id=(
                SELECT MAX(refresh_id) FROM model_catalog_refreshes WHERE provider=? AND status='ok')""", (provider,))
    except Exception:
        logger.exception('Could not mark the %s model catalog stale', provider)


def record_denial(connect, provider, model, reason, *, execution_id=None, source='provider_error'):
    if connect is None or provider not in providers.REAL or not model:
        return
    try:
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            scope = current_scope(db, provider)
            db.execute('INSERT INTO model_denials(provider,scope_key,model_id,reason,source,execution_id) VALUES (?,?,?,?,?,?)',
                       (provider, scope['scope_key'] if scope else 'unknown', model, reason[:300], source, execution_id))
    except Exception:
        logger.exception('Could not record a model denial for %s', provider)
    mark_stale(connect, provider)


# ---------------------------------------------------------------- lookup, status and admission

def _entries(db, provider, scope_key):
    if not scope_key or not providers._has(db, 'model_catalog'):
        return []
    rows = []
    for row in db.execute('SELECT * FROM model_catalog WHERE provider=? AND scope_key=? ORDER BY rowid', (provider, scope_key)):
        entry = dict(row)
        for key in ('aliases', 'supported_efforts'):
            entry[key] = json.loads(entry[key] or '[]')
        entry['capabilities'] = json.loads(entry['capabilities'] or '{}')
        rows.append(entry)
    return rows


def lookup(db, provider, model, scope=None):
    scope = scope if scope is not None else current_scope(db, provider)
    if scope is None or not model:
        return None
    for entry in _entries(db, provider, scope['scope_key']):
        if model == entry['model_id'] or model in entry['aliases']:
            return entry
    return None


def _denied(db, provider, scope_key, model):
    if not providers._has(db, 'model_denials'):
        return None
    row = db.execute('''SELECT * FROM model_denials WHERE provider=? AND model_id=? AND cleared_at IS NULL
        AND scope_key IN (?, 'unknown') ORDER BY denial_id DESC LIMIT 1''', (provider, model, scope_key or 'unknown')).fetchone()
    return dict(row) if row else None


def model_status(db, provider, entry, scope, provider_state=None):
    """(status code, explanation). Capability, entitlement, billing and quota stay distinct."""
    if provider_state in ('disabled',):
        return 'provider_disabled', 'The provider is disabled.'
    if provider_state in ('not_authenticated',):
        return 'provider_not_authenticated', 'The provider is not signed in.'
    if provider_state in ('billing_blocked',):
        return 'billing_blocked', 'The billing route is not approved or acknowledged.'
    if provider == 'gemini' and entry['model_id'] != 'auto':
        return 'pin_not_enforceable', GEMINI_PIN_REASON  # A property of the CLI, not of the account.
    if scope is None or not scope['fresh']:
        # Stale or previous-account evidence is history, never a claim about the current
        # account: neither a restriction nor a permission until discovery succeeds again.
        if scope is not None and not scope.get('installation_matches', True):
            return 'catalog_stale', ('Availability not verified: the sign-in, account, CLI or configuration changed '
                                     'since this list was read, and it is not reused for the current account.')
        return 'catalog_stale', 'Availability not verified: the model list is stale.'
    if entry.get('removed_at'):
        return 'removed', 'No longer listed by the provider.'
    denial = _denied(db, provider, scope['scope_key'] if scope else None, entry['model_id'])
    if denial:
        return 'unavailable_subscription', ('The provider refused this model for this account. Required plan not '
                                            f'specified by provider ({denial["reason"]}).')
    note = entry['capabilities'].get('doc_note')
    suffix = f' {note}' if note else ''
    if entry['entitlement'] == 'unavailable':
        if entry['plan_requirement']:
            return 'requires_plan', f'Provider: {entry["plan_requirement"]}{suffix}'
        if entry['capabilities'].get('not_offered'):
            # An omission from the account's list, not an explicit provider denial.
            return 'not_offered', ('Documented by Claude Code, but the installed CLI does not offer it to this account. '
                                   'Required plan not specified by provider.' + suffix)
        return 'unavailable_subscription', 'Not available on this account. Required plan not specified by provider.' + suffix
    if entry.get('org_restricted'):
        return 'org_restricted', 'Restricted by your organization.'
    if entry.get('min_cli_version') and providers.version_tuple(scope.get('cli_version') if scope else None) \
            and providers.version_tuple(entry['min_cli_version']) > providers.version_tuple(scope['cli_version']):
        return 'unsupported_cli', f'Requires {providers.LABELS[provider]} {entry["min_cli_version"]} or later.'
    limits = [c for c in providers.constraints(db, provider, entry['model_id']) if c['hard']]
    if limits:
        scoped = any(c.get('model_scope') for c in limits)
        return ('model_quota_exhausted' if scoped else 'shared_quota_exhausted'), providers.describe_constraints(limits)
    if entry['entitlement'] == 'unknown':
        text = 'Listed in public documentation; access for this account is not verified.'
        if provider == 'gemini':
            text += ' Gemini CLI chooses the model; the models in its usage statistics are recorded.'
        return 'entitlement_unknown', text + suffix
    if entry['listing'] == 'deprecated':
        return 'deprecated', 'Deprecated by the provider.' + suffix
    return 'available', f'Listed by {providers.LABELS[provider]} for this account.' + suffix


def catalog_view(db, provider, provider_state=None):
    """The model list for the UI: grouped, with status, effort and provenance. No discovery runs."""
    if provider == 'mock':
        return {'provider': 'mock', 'models': [], 'refresh': None, 'groups': {}, 'entitlement_verified': False,
                'source_note': 'Mock agents have no model.'}
    scope = current_scope(db, provider)
    if scope is None:
        # Last-known catalog of any scope, shown as stale (never reused for entitlement).
        row = db.execute("""SELECT * FROM model_catalog_refreshes WHERE provider=? AND status='ok'
            ORDER BY refresh_id DESC LIMIT 1""", (provider,)).fetchone() if providers._has(db, 'model_catalog_refreshes') else None
        scope = dict(row, fresh=False, installation_matches=False) if row else None
    models = []
    for entry in (_entries(db, provider, scope['scope_key']) if scope else []):
        if entry['listing'] == 'hidden':
            continue
        status, explanation = model_status(db, provider, entry, scope, provider_state)
        models.append({'model_id': entry['model_id'], 'display_name': entry['display_name'],
                       'description': entry['description'], 'status': status, 'status_label': STATUS_LABELS[status],
                       'explanation': explanation, 'selectable': status in SELECTABLE,
                       'requires_attempt': status in ATTEMPTABLE,
                       'supported_efforts': entry['supported_efforts'], 'default_effort': entry['default_effort'],
                       'resolved_model': entry['resolved_model'], 'previous_resolved_model': entry['previous_resolved_model'],
                       'plan_requirement': entry['plan_requirement'], 'plan_source': entry['plan_source'],
                       'source': entry['source'], 'confidence': entry['confidence'],
                       'routing': entry['capabilities'].get('routing'), 'last_seen_at': entry['last_seen_at'],
                       'supports_effort_updates': entry['capabilities'].get('supports_reasoning_effort_updates') is True,
                       'removed_at': entry['removed_at']})
    latest = latest_refresh(db, provider)
    return {'provider': provider, 'models': models,
            'refresh': {'last_success_at': scope['finished_at'] if scope else None,
                        'fresh': bool(scope and scope['fresh']),
                        'scope_label': scope['scope_label'] if scope else None,
                        'account_plan': scope['account_plan'] if scope else None,
                        'account_plan_source': scope['account_plan_source'] if scope else None,
                        'identity_exposed': bool(scope and scope.get('identity_exposed')),
                        'account_changed': bool(scope and not scope.get('installation_matches', True)),
                        'last_attempt_at': latest['finished_at'] if latest else None,
                        'last_status': latest['status'] if latest else None,
                        'last_reason': latest['reason'] if latest else None},
            'entitlement_verified': provider != 'gemini' and bool(scope and scope['fresh']),
            'source_note': {'claude': 'Models Claude Code reports for this account at initialization (no model request).',
                            'codex': 'The catalog the Codex CLI fetched for this sign-in (debug interface; not a guarantee).',
                            'gemini': 'Documented Gemini CLI aliases; Gemini CLI exposes no account model list.'}[provider]}


def admission(db, provider, model, effort, *, explicit_attempt=False, automated=False):
    """Whether this model and effort may run now. Raises ProviderRefused; returns the snapshot fields."""
    if provider == 'mock':
        return {'entitlement_state': 'not_applicable', 'catalog_scope': None, 'catalog_revision': None,
                'resolved_model': None}
    label = providers.LABELS[provider]
    if provider == 'gemini' and model not in (None, 'auto'):
        # Refused before launch: a pin Gemini CLI cannot enforce is never presented as one.
        raise providers.ProviderRefused('gemini', 'pin_not_enforceable', f'{model} cannot be selected. {GEMINI_PIN_REASON}')
    scope = current_scope(db, provider)
    base = {'catalog_scope': scope['scope_key'] if scope else None,
            'catalog_revision': scope['revision'] if scope else None}
    if model is None:
        if effort is None:
            return {**base, 'entitlement_state': 'provider_default', 'resolved_model': None}
        default = lookup(db, provider, 'default', scope) if provider == 'claude' else None
        if default and scope and scope['fresh'] and effort in default['supported_efforts']:
            return {**base, 'entitlement_state': 'provider_default', 'resolved_model': default['resolved_model']}
        if provider == 'codex' and scope and scope['fresh']:
            # Codex does not name its default model: an effort is accepted only when every
            # model the account may run lists it, whichever one the default is.
            runnable = [e for e in _entries(db, provider, scope['scope_key']) if e['listing'] == 'listed'
                        and model_status(db, provider, e, scope)[0] in SELECTABLE]
            if runnable and all(effort in e['supported_efforts'] for e in runnable):
                return {**base, 'entitlement_state': 'provider_default', 'resolved_model': None}
        raise providers.ProviderRefused(provider, 'unsupported_effort', f'Effort "{effort}" cannot be verified for '
                                        f"{label}'s default model.", 'Choose a model whose effort levels are listed.')
    entry = lookup(db, provider, model, scope)
    if entry is not None:
        status, explanation = model_status(db, provider, entry, scope)
        if status in ('removed', 'unavailable_subscription', 'requires_plan', 'org_restricted', 'unsupported_cli',
                      'not_offered', 'pin_not_enforceable'):
            raise providers.ProviderRefused(provider, status, f'{entry["display_name"]} cannot run: {explanation}',
                                            'Choose another model explicitly; none is substituted.')
        if effort is not None and effort not in entry['supported_efforts']:
            raise providers.ProviderRefused(provider, 'unsupported_effort',
                                            f'{entry["display_name"]} does not list effort "{effort}".',
                                            'Choose one of: ' + (', '.join(entry['supported_efforts']) or 'provider default') + '.')
        if status in SELECTABLE:
            return {**base, 'entitlement_state': 'cli_listed', 'resolved_model': entry['resolved_model']}
    else:
        denial = _denied(db, provider, scope['scope_key'] if scope else None, model)
        if denial:
            raise providers.ProviderRefused(provider, 'unavailable_subscription', f'{model} was refused for this account '
                                            '(required plan not specified by provider).')
        if effort is not None:
            raise providers.ProviderRefused(provider, 'unsupported_effort', f'Effort levels of {model} are not known.',
                                            'Use the provider default effort for an unlisted model.')
    if automated:
        raise providers.ProviderRefused(provider, 'entitlement_unknown', f'{model} is not verified for this account, '
                                        'and automated launches never attempt unverified models.')
    if not explicit_attempt:
        raise providers.ProviderRefused(provider, 'entitlement_unknown', f'Access to {model} is not verified for this '
                                        'account.', 'Confirm an explicit attempt: it uses your approved subscription '
                                        'route, may consume included usage and is not retried automatically.')
    return {**base, 'entitlement_state': 'explicit_attempt', 'resolved_model': entry['resolved_model'] if entry else None}


def session_compatible(db, provider, previous, requested):
    """Whether a session started with `previous` (model, effort) can continue with `requested`."""
    if previous is None or tuple(previous) == tuple(requested):
        return True
    (old_model, old_effort), (new_model, new_effort) = previous, requested
    if provider == 'claude':
        return True  # --resume with --model/--effort applies them to the resumed session.
    if old_model != new_model:
        return False
    if provider == 'codex' and old_effort != new_effort:
        entry = lookup(db, provider, new_model) if new_model else None
        return bool(entry and entry['capabilities'].get('supports_reasoning_effort_updates'))
    return old_effort == new_effort
