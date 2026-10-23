"""Stage 2F: dynamic model catalog, entitlement states and per-Agent model/effort configuration.

Discovery runs only against disposable fake CLIs: no model request, no real account.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app import agent_manager as manager, model_catalog, providers
from app.persistence import AgentStore
import provider_test_support as fake
import test_providers as base


CLAUDE_RESPONSE = {'models': [
    {'value': 'default', 'resolvedModel': 'claude-x-opus', 'displayName': 'Default', 'description': 'd',
     'supportsEffort': True, 'supportedEffortLevels': ['low', 'medium', 'high']},
    {'value': 'sonnet', 'resolvedModel': 'claude-x-sonnet', 'displayName': 'Sonnet', 'description': 's',
     'supportsEffort': True, 'supportedEffortLevels': ['low', 'medium']},
    {'value': 'haiku', 'resolvedModel': 'claude-x-haiku', 'displayName': 'Haiku', 'description': 'h'}],
    'account': {'email': 'person@example.test', 'organization': None, 'subscriptionType': 'pro',
                'tokenSource': 'claude.ai', 'apiProvider': 'firstParty'}}


class CatalogStoreTests(unittest.TestCase):
    """Normalization, history, status and admission against an isolated database."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'catalog.sqlite3')
        self.fingerprint = ('cli', 1, None)
        self.account = 'account-a'
        self.enterContext(patch.object(providers, '_fingerprint', side_effect=lambda provider: self.fingerprint))
        # Never the developer's real sign-in files.
        self.enterContext(patch.object(providers, 'account_evidence', side_effect=lambda provider: self.account))
        model_catalog._last_attempt.clear()
        self.addCleanup(model_catalog._last_attempt.clear)

    def discover(self, provider, response, version=(2, 1, 280)):
        target = {'claude': (providers.CLAUDE, 'discover_models', (response, version)),
                  'codex': (providers.CODEX, 'discover_models', (response, 'codex-cli 9.9.9'))}[provider]
        with patch.object(target[0], target[1], return_value=target[2]):
            return model_catalog.refresh(self.store._connection, provider, force=True)

    def view(self, provider, state='ready'):
        with self.store._connection() as db:
            return {m['model_id']: m for m in model_catalog.catalog_view(db, provider, state)['models']}

    def admit(self, provider, model, effort=None, **options):
        with self.store._connection() as db:
            return model_catalog.admission(db, provider, model, effort, **options)

    def test_claude_discovery_reports_models_efforts_aliases_and_plan(self):
        refresh = self.discover('claude', CLAUDE_RESPONSE)
        self.assertEqual((refresh['status'], refresh['account_plan']), ('ok', 'pro'))
        models = self.view('claude')
        self.assertEqual(list(models)[:3], ['default', 'sonnet', 'haiku'])
        # Documented models the account was not offered stay visible: a family alias as
        # not available (plan not specified), a full model name as unverified.
        self.assertEqual((models['opus']['status'], models['opus']['plan_requirement'], models['opus']['selectable']),
                         ('not_offered', None, False))
        self.assertIn('does not offer it to this account', models['opus']['explanation'])
        self.assertIn('usage credits', models['fable']['explanation'])  # The documentation's own statement.
        self.assertEqual(models['claude-opus-4-6']['status'], 'entitlement_unknown')
        self.assertNotIn('claude-x-sonnet', models)  # Listed through its alias already.
        self.assertEqual(models['sonnet']['supported_efforts'], ['low', 'medium'])
        self.assertEqual(models['haiku']['supported_efforts'], [])
        self.assertEqual(models['sonnet']['resolved_model'], 'claude-x-sonnet')
        self.assertEqual(models['sonnet']['status'], 'available')
        self.assertIsNone(models['sonnet']['plan_requirement'])  # Never inferred from a model family.
        with self.store._connection() as db:
            dump = '\n'.join(str(tuple(r)) for t in ('model_catalog', 'model_catalog_refreshes')
                             for r in db.execute(f'SELECT * FROM {t}'))
        self.assertNotIn('person@example.test', dump)  # The account identity is only hashed.

    def test_new_removed_renamed_and_re_resolved_models_keep_history(self):
        first = json.loads(json.dumps(CLAUDE_RESPONSE))
        first['models'].append({'value': 'team-custom', 'displayName': 'Team custom', 'description': 'c'})
        self.discover('claude', first)
        changed = json.loads(json.dumps(CLAUDE_RESPONSE))
        changed['models'][1]['resolvedModel'] = 'claude-x-sonnet-2'  # Alias resolution changed.
        changed['models'][2] = {'value': 'haiku-next', 'displayName': 'Haiku Next', 'description': 'n'}  # Renamed.
        changed['models'].append({'value': 'brand-new', 'displayName': 'Brand New', 'description': 'b',
                                  'supportsEffort': True, 'supportedEffortLevels': ['high']})
        self.discover('claude', changed)
        models = self.view('claude')
        self.assertEqual(models['sonnet']['resolved_model'], 'claude-x-sonnet-2')
        self.assertEqual(models['sonnet']['previous_resolved_model'], 'claude-x-sonnet')
        self.assertEqual(models['team-custom']['status'], 'removed')  # Kept identifiable, never selectable.
        self.assertFalse(models['team-custom']['selectable'])
        # A documented family alias no longer offered is shown as not available to this account.
        self.assertEqual(models['haiku']['status'], 'not_offered')
        self.assertTrue(models['haiku-next']['selectable'])
        self.assertEqual(models['brand-new']['supported_efforts'], ['high'])
        with self.assertRaisesRegex(providers.ProviderRefused, 'No longer listed'):
            self.admit('claude', 'team-custom')
        with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
            self.admit('claude', 'haiku', explicit_attempt=True)  # Never admitted, even by an attempt.

    def test_codex_catalog_plan_text_is_provider_verbatim_and_hidden_models_stay_hidden(self):
        catalog = {'identity': 'acct-1', 'models': [
            {'slug': 'm1', 'display_name': 'M1', 'visibility': 'list', 'default_reasoning_level': 'medium',
             'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}], 'upgrade': None},
            {'slug': 'locked-known', 'display_name': 'Locked', 'visibility': 'list',
             'supported_reasoning_levels': [], 'upgrade': {'x': 1}, 'availability_nux': {'message': 'Requires ChatGPT Pro'}},
            {'slug': 'locked-unknown', 'display_name': 'Locked 2', 'visibility': 'list',
             'supported_reasoning_levels': [], 'upgrade': {'x': 1}},
            {'slug': 'internal', 'display_name': 'Internal', 'visibility': 'hide', 'supported_reasoning_levels': []}]}
        self.discover('codex', catalog)
        models = self.view('codex')
        self.assertNotIn('internal', models)
        self.assertEqual((models['locked-known']['status'], models['locked-known']['explanation']),
                         ('requires_plan', 'Provider: Requires ChatGPT Pro'))
        self.assertEqual(models['locked-unknown']['status'], 'unavailable_subscription')
        self.assertIn('Required plan not specified by provider', models['locked-unknown']['explanation'])
        self.assertNotIn('Pro', models['locked-unknown']['explanation'])  # No invented plan.
        self.assertEqual(models['m1']['default_effort'], 'medium')
        with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
            self.admit('codex', 'locked-known')
        with self.assertRaisesRegex(providers.ProviderRefused, 'does not list effort'):
            self.admit('codex', 'm1', 'high')
        self.assertEqual(self.admit('codex', 'm1', 'low')['entitlement_state'], 'cli_listed')
        # Codex does not name its default model: an effort every runnable model lists is safe.
        self.assertEqual(self.admit('codex', None, 'low')['entitlement_state'], 'provider_default')
        with self.assertRaisesRegex(providers.ProviderRefused, 'default model'):
            self.admit('codex', None, 'xhigh')

    def test_unknown_entitlement_requires_an_explicit_user_attempt_and_never_an_automated_one(self):
        self.discover('claude', CLAUDE_RESPONSE)
        with self.assertRaisesRegex(providers.ProviderRefused, 'not verified'):
            self.admit('claude', 'claude-unlisted')
        self.assertEqual(self.admit('claude', 'claude-unlisted', explicit_attempt=True)['entitlement_state'],
                         'explicit_attempt')
        with self.assertRaisesRegex(providers.ProviderRefused, 'automated launches never attempt'):
            self.admit('claude', 'claude-unlisted', explicit_attempt=True, automated=True)
        with self.assertRaisesRegex(providers.ProviderRefused, 'Effort levels'):
            self.admit('claude', 'claude-unlisted', 'high', explicit_attempt=True)
        self.assertEqual(self.admit('claude', None, 'medium')['entitlement_state'], 'provider_default')
        with self.assertRaises(providers.ProviderRefused):
            self.admit('claude', None, 'max')  # Not listed for the default model.

    def test_gemini_compatibility_catalog_is_never_verified_access(self):
        with patch.object(providers, 'probe', return_value=providers.Readiness('gemini', providers.READY, version='0.30.0')):
            model_catalog.refresh(self.store._connection, 'gemini', force=True)
        models = self.view('gemini')
        self.assertEqual(sorted(models), ['auto', 'flash', 'flash-lite', 'pro'])
        self.assertTrue(all(m['supported_efforts'] == [] for m in models.values()))
        # Auto is intentional routing (unverified access: an explicit attempt).
        self.assertEqual((models['auto']['status'], models['auto']['requires_attempt'], models['auto']['routing']),
                         ('entitlement_unknown', True, 'auto'))
        # A specific model is shown, but never as an enforceable pin.
        for alias in ('pro', 'flash', 'flash-lite'):
            self.assertEqual((models[alias]['status'], models[alias]['selectable'], models[alias]['requires_attempt']),
                             ('pin_not_enforceable', False, False))
        self.assertIn('cannot guarantee', models['pro']['explanation'])
        with self.store._connection() as db:
            self.assertFalse(model_catalog.catalog_view(db, 'gemini')['entitlement_verified'])
        for model in ('pro', 'gemini-2.5-pro'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot guarantee'):
                self.admit('gemini', model, explicit_attempt=True)  # Refused before launch, attempt or not.
        self.assertEqual(self.admit('gemini', 'auto', explicit_attempt=True)['entitlement_state'], 'explicit_attempt')
        self.assertEqual(self.admit('gemini', None)['entitlement_state'], 'provider_default')

    def test_staleness_cli_change_and_offline_fallback(self):
        self.discover('claude', CLAUDE_RESPONSE)
        self.assertEqual(self.view('claude')['sonnet']['status'], 'available')
        self.fingerprint = ('cli', 2, None)  # The CLI was upgraded (or downgraded) since discovery.
        models = self.view('claude')
        self.assertEqual(models['sonnet']['status'], 'catalog_stale')  # Last-known, shown as stale.
        self.assertTrue(models['sonnet']['requires_attempt'])
        with self.assertRaises(providers.ProviderRefused):
            self.admit('claude', 'sonnet')  # Stale evidence never grants execution.
        with patch.object(providers.CLAUDE, 'discover_models', side_effect=providers.ProviderRefused('claude', 'x', 'offline')):
            model_catalog.refresh(self.store._connection, 'claude', force=True)
        with self.store._connection() as db:
            view = model_catalog.catalog_view(db, 'claude')
        self.assertFalse(view['refresh']['fresh'])
        self.assertEqual(view['refresh']['last_status'], 'failed')
        self.assertIn('sonnet', [m['model_id'] for m in view['models']])
        self.fingerprint = ('cli', 1, None)
        with self.store._connection() as db:
            db.execute("UPDATE model_catalog_refreshes SET finished_at='2020-01-01T00:00:00.000Z'")
            db.commit()
        self.assertEqual(self.view('claude')['sonnet']['status'], 'catalog_stale')  # TTL elapsed.

    def test_account_change_never_reuses_another_accounts_entitlement(self):
        self.discover('claude', CLAUDE_RESPONSE)
        other = json.loads(json.dumps(CLAUDE_RESPONSE))
        other['account'] = {**other['account'], 'email': 'other@example.test', 'organization': 'org-2'}
        other['models'] = other['models'][:1]
        self.discover('claude', other)
        models = self.view('claude')
        self.assertFalse(models['sonnet']['selectable'])  # The other account's entitlement is not reused.
        with self.assertRaises(providers.ProviderRefused):
            self.admit('claude', 'sonnet')
        with self.store._connection() as db:
            scopes = {r[0] for r in db.execute("SELECT DISTINCT scope_key FROM model_catalog WHERE provider='claude'")}
        self.assertEqual(len(scopes), 2)

    def test_provider_denial_marks_the_model_unavailable_without_a_plan(self):
        self.discover('claude', CLAUDE_RESPONSE)
        model_catalog.record_denial(self.store._connection, 'claude', 'sonnet', 'model_not_found')
        with self.store._connection() as db:
            self.assertTrue(model_catalog.needs_refresh(db, 'claude'))  # The signal makes the catalog stale.
            scope = model_catalog.current_scope(db, 'claude')
            entry = model_catalog.lookup(db, 'claude', 'sonnet')
            self.assertIsNone(scope if scope and scope['fresh'] else None)
        self.discover('claude', CLAUDE_RESPONSE)
        sonnet = self.view('claude')['sonnet']
        self.assertEqual(sonnet['status'], 'unavailable_subscription')
        self.assertIn('Required plan not specified by provider', sonnet['explanation'])
        self.assertIsNotNone(entry)

    def test_quota_states_are_separate_from_entitlement(self):
        self.discover('claude', CLAUDE_RESPONSE)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, 'claude', [dict(window_id='weekly_scoped', scope_key='Sonnet',
                                                         model_scope='Sonnet', label='Weekly (Sonnet)', status='exhausted',
                                                         used_percent=100.0)], source='claude_usage_command')
        models = self.view('claude')
        self.assertEqual(models['sonnet']['status'], 'model_quota_exhausted')
        self.assertEqual(models['haiku']['status'], 'available')  # A model-specific limit, not shared.

    def test_gemini_evidence_rules(self):
        check = providers.model_enforcement
        # Init reports the selected Pro; the usage statistics report only Flash.
        state, note = check('gemini', 'gemini-2.5-pro', ['gemini-2.5-flash'], 'gemini-2.5-pro')
        self.assertEqual(state, 'mismatch')
        self.assertIn('Configured at start: gemini-2.5-pro', note)
        # Pro and Flash in aggregate statistics: the primary model is not identified.
        self.assertEqual(check('gemini', 'gemini-2.5-pro', ['gemini-2.5-pro', 'gemini-2.5-flash'], 'gemini-2.5-pro')[0],
                         'alternatives')
        # Selected model only in init, no usage statistics: insufficient evidence.
        self.assertEqual(check('gemini', 'gemini-2.5-pro', [], 'gemini-2.5-pro')[0], 'insufficient')
        self.assertEqual(check('gemini', 'gemini-2.5-pro', ['gemini-2.5-pro'])[0], 'verified')
        self.assertEqual(check('gemini', 'flash', ['gemini-9-flash-lite'])[0], 'mismatch')  # Another family.
        self.assertEqual(check('gemini', 'auto', ['x'])[0], 'routed')
        self.assertEqual(check('gemini', None, [])[0], 'routed')
        self.assertEqual(check('claude', 'sonnet', ['other'])[0], 'verified')  # Claude has its own comparison.

    def test_gemini_init_model_is_never_usage_evidence(self):
        from app.provider_events import GeminiEvents
        events = GeminiEvents()
        for event in ({'type': 'init', 'session_id': '11111111-1111-4111-8111-111111111111', 'model': 'gemini-2.5-pro'},
                      {'type': 'message', 'role': 'assistant', 'content': 'done', 'delta': True},
                      {'type': 'result', 'status': 'success', 'stats': {'models': {'gemini-2.5-flash': {}}}}):
            events.feed(json.dumps(event) + '\n')
        self.assertEqual(events.initial_model, 'gemini-2.5-pro')
        self.assertEqual(events.models, {'gemini-2.5-flash'})

    # ---- final review, finding 2: stale or previous-account evidence is never a current-account claim

    def test_previous_account_restrictions_are_not_presented_as_current(self):
        catalog = {'identity': 'acct-1', 'models': [
            {'slug': 'm1', 'display_name': 'M1', 'supported_reasoning_levels': [{'effort': 'low'}]},
            {'slug': 'locked', 'display_name': 'Locked', 'supported_reasoning_levels': [], 'upgrade': {'x': 1},
             'availability_nux': {'message': 'Requires ChatGPT Pro'}},
            {'slug': 'nomention', 'display_name': 'No mention', 'supported_reasoning_levels': [], 'upgrade': {'x': 1}}]}
        self.discover('codex', catalog)
        self.assertEqual(self.view('codex')['locked']['status'], 'requires_plan')
        self.account = 'account-b'  # Another sign-in; current-account discovery has not run.
        models = self.view('codex')
        for model_id in ('m1', 'locked', 'nomention'):
            self.assertEqual(models[model_id]['status'], 'catalog_stale', model_id)
            self.assertIn('Availability not verified', models[model_id]['explanation'])
            self.assertNotIn('this account', models[model_id]['explanation'].replace('current account', ''))
        self.assertNotIn('Requires ChatGPT Pro', models['locked']['explanation'])
        self.assertEqual(models['locked']['plan_requirement'], 'Requires ChatGPT Pro')  # History is kept.
        with self.assertRaises(providers.ProviderRefused):
            self.admit('codex', 'm1')  # Stale data never grants access.

    def test_incomplete_discovery_leaves_last_known_entries_unverified(self):
        self.discover('claude', CLAUDE_RESPONSE)
        model_catalog.record_denial(self.store._connection, 'claude', 'sonnet', 'model_not_found')
        with patch.object(providers.CLAUDE, 'discover_models', side_effect=providers.ProviderRefused('claude', 'x', 'offline')):
            model_catalog.refresh(self.store._connection, 'claude', force=True)
        models = self.view('claude')
        self.assertEqual({models[m]['status'] for m in ('sonnet', 'opus', 'default')}, {'catalog_stale'})

    def test_provider_denial_and_list_omission_are_distinct(self):
        self.discover('claude', CLAUDE_RESPONSE)
        model_catalog.record_denial(self.store._connection, 'claude', 'sonnet', 'model_not_found')
        self.discover('claude', CLAUDE_RESPONSE)
        models = self.view('claude')
        self.assertEqual(models['sonnet']['status'], 'unavailable_subscription')
        self.assertIn('provider refused', models['sonnet']['explanation'])
        self.assertEqual((models['opus']['status'], models['opus']['status_label']), ('not_offered', 'Not offered to this account'))
        self.assertFalse(models['opus']['selectable'])
        self.assertIsNone(models['opus']['plan_requirement'])  # No plan is inferred from an omission.

    def test_session_compatibility_per_provider(self):
        self.discover('codex', {'identity': 'a', 'models': [
            {'slug': 'm1', 'display_name': 'M1', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}],
             'supports_reasoning_effort_updates': True},
            {'slug': 'm2', 'display_name': 'M2', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}]}]})
        with self.store._connection() as db:
            self.assertTrue(model_catalog.session_compatible(db, 'claude', ('sonnet', 'low'), ('opus', 'high')))
            self.assertFalse(model_catalog.session_compatible(db, 'codex', ('m1', 'low'), ('m2', 'low')))
            self.assertTrue(model_catalog.session_compatible(db, 'codex', ('m1', 'low'), ('m1', 'high')))
            self.assertFalse(model_catalog.session_compatible(db, 'codex', ('m2', 'low'), ('m2', 'high')))
            self.assertFalse(model_catalog.session_compatible(db, 'gemini', ('pro', None), ('flash', None)))
            self.assertTrue(model_catalog.session_compatible(db, 'gemini', ('pro', None), ('pro', None)))

    # ---- review follow-up, finding 1: entitlement freshness is bound to the signed-in account

    def test_account_change_makes_the_catalog_stale_without_a_refresh(self):
        self.discover('claude', CLAUDE_RESPONSE)
        self.assertEqual(self.admit('claude', 'sonnet')['entitlement_state'], 'cli_listed')
        self.account = 'account-b'  # Another sign-in; nothing was refreshed.
        with self.store._connection() as db:
            scope = model_catalog.current_scope(db, 'claude')
            self.assertFalse(scope['fresh'])
            self.assertTrue(model_catalog.needs_refresh(db, 'claude'))
            view = model_catalog.catalog_view(db, 'claude')
        self.assertTrue(view['refresh']['account_changed'])
        self.assertEqual({m['model_id']: m['status'] for m in view['models']}['sonnet'], 'catalog_stale')
        with self.assertRaises(providers.ProviderRefused):
            self.admit('claude', 'sonnet')  # The previous account's eligibility is never admitted.
        self.discover('claude', CLAUDE_RESPONSE)
        with self.store._connection() as db:
            scopes = {r[0] for r in db.execute("SELECT DISTINCT scope_key FROM model_catalog WHERE provider='claude'")}
        self.assertEqual(len(scopes), 2)  # The new account has its own scope.

    def test_an_account_without_exposed_identity_is_trusted_only_briefly(self):
        self.account = None
        self.discover('claude', CLAUDE_RESPONSE)
        with self.store._connection() as db:
            self.assertTrue(model_catalog.current_scope(db, 'claude')['fresh'])
            self.assertFalse(model_catalog.catalog_view(db, 'claude')['refresh']['identity_exposed'])
            db.execute("UPDATE model_catalog_refreshes SET finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','-5 minutes')")
            db.commit()
            self.assertFalse(model_catalog.current_scope(db, 'claude')['fresh'])
        with self.assertRaises(providers.ProviderRefused):
            self.admit('claude', 'sonnet')

    def test_a_changed_account_bypasses_refresh_coalescing(self):
        calls = []

        def discovery():
            calls.append(1)
            return CLAUDE_RESPONSE, (2, 1, 280)
        with patch.object(providers.CLAUDE, 'discover_models', side_effect=discovery):
            model_catalog.refresh(self.store._connection, 'claude', trigger='admission')
            model_catalog.refresh(self.store._connection, 'claude', trigger='admission')
            self.assertEqual(len(calls), 1)  # Coalesced for the same account.
            self.account = 'account-b'
            model_catalog.refresh(self.store._connection, 'claude', trigger='admission')
        self.assertEqual(len(calls), 2)


class AccountEvidenceTests(unittest.TestCase):
    """Local identity evidence: identifiers only, hashed; never a token."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {'USERPROFILE': str(self.home), 'CODEX_HOME': str(self.home / '.codex'),
                                                  'GEMINI_CLI_HOME': str(self.home)}))
        os.environ.pop('CLAUDE_CONFIG_DIR', None)

    def write(self, relative, value):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_codex_account_switch_changes_the_evidence_and_no_secret_leaves(self):
        self.assertIsNone(providers.account_evidence('codex'))
        secret = 'secret-access-token-value'
        self.write('.codex/auth.json', {'auth_mode': 'chatgpt', 'tokens': {'account_id': 'acct-a', 'access_token': secret}})
        first = providers.account_evidence('codex')
        self.write('.codex/auth.json', {'auth_mode': 'chatgpt', 'tokens': {'account_id': 'acct-a', 'access_token': 'rotated'}})
        self.assertEqual(providers.account_evidence('codex'), first)  # A token refresh is not an account change.
        self.write('.codex/auth.json', {'auth_mode': 'chatgpt', 'tokens': {'account_id': 'acct-b', 'access_token': secret}})
        self.assertNotEqual(providers.account_evidence('codex'), first)
        self.assertNotIn(secret, first)
        self.assertNotIn('acct-a', first)

    def test_claude_plan_tier_or_organization_change_changes_the_evidence(self):
        account = {'accountUuid': 'u-1', 'organizationUuid': 'o-1', 'seatTier': 'standard', 'emailAddress': 'p@example.test',
                   'profileFetchedAt': 1}
        self.write('.claude.json', {'oauthAccount': account})
        first = providers.account_evidence('claude')
        self.write('.claude.json', {'oauthAccount': {**account, 'profileFetchedAt': 2}})
        self.assertEqual(providers.account_evidence('claude'), first)
        self.write('.claude.json', {'oauthAccount': {**account, 'seatTier': 'premium'}})
        self.assertNotEqual(providers.account_evidence('claude'), first)
        self.write('.claude.json', {'projects': {}})
        self.assertIsNone(providers.account_evidence('claude'))  # Not exposed.


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class AgentConfigurationTests(unittest.TestCase):
    """Real supervised fake-CLI runs with per-Agent and per-prompt model and effort."""
    rows = base.SupervisedFakeProviderTests.rows
    wait_status = base.SupervisedFakeProviderTests.wait_status

    def setUp(self):
        base.SupervisedFakeProviderTests.setUp(self)
        model_catalog._last_attempt.clear()
        self.addCleanup(model_catalog._last_attempt.clear)

    def tearDown(self):
        base.SupervisedFakeProviderTests.tearDown(self)
        manager.agent_efforts.clear()
        manager.agent_session_configs.clear()

    def run_args(self, provider):
        return fake.runs(self.log, provider)[-1]['args']

    def test_claude_discovery_never_sends_a_prompt(self):
        view = manager.model_catalog_view('claude', refresh=True)
        self.assertEqual([m['model_id'] for m in view['models']][:4], ['default', 'sonnet', 'opus', 'haiku'])
        self.assertEqual(next(m for m in view['models'] if m['model_id'] == 'fable')['status'], 'not_offered')
        self.assertEqual(view['refresh']['account_plan'], 'max')
        entries = fake.read_log(self.log)
        self.assertTrue(any(e['kind'] == 'discovery' for e in entries))
        self.assertFalse(any(e['kind'] in ('run', 'UNEXPECTED_USER_MESSAGE') for e in entries))

    def test_independent_agent_defaults_and_effective_snapshots(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='slow', FAKE_SLOW_SECONDS='2'):
            first = manager.start_agent('First', 'claude', 'read-only', model='sonnet', effort='low')
            second = manager.start_agent('Second', 'claude', 'read-only', model='opus', effort='xhigh')
            third = manager.start_agent('Third', 'codex', 'read-only', model='gpt-fake-2', effort='xhigh')
        for agent_id in (first, second, third):
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        runs = {tuple(r['args']): r for r in fake.runs(self.log)}
        claude = [r['args'] for r in fake.runs(self.log, 'claude')]
        self.assertIn(['sonnet', 'low'], [[a[a.index('--model') + 1], a[a.index('--effort') + 1]] for a in claude])
        self.assertIn(['opus', 'xhigh'], [[a[a.index('--model') + 1], a[a.index('--effort') + 1]] for a in claude])
        self.assertTrue(all('--settings' in a for a in claude))  # No automatic model switching.
        codex = self.run_args('codex')
        self.assertIn('model_reasoning_effort=xhigh', codex)
        self.assertEqual(manager.get_agent(first, include_output=False)['effort'], 'low')
        self.assertEqual(manager.get_agent(second, include_output=False)['effort'], 'xhigh')
        snapshot = manager.get_agent(first, include_output=False)['latest_execution']
        self.assertEqual((snapshot['model'], snapshot['requested_effort'], snapshot['config_source'],
                          snapshot['entitlement_state'], snapshot['resolved_model'], snapshot['effective_model']),
                         ('sonnet', 'low', 'agent_default', 'cli_listed', 'claude-fake-sonnet', 'claude-fake-sonnet'))
        self.assertIsNone(snapshot['effective_effort'])  # Not reported by the provider: never claimed.
        self.assertTrue(runs)

    def test_next_prompt_override_applies_once_and_keeps_the_claude_session(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            agent_id = manager.start_agent('Iterate', 'claude', 'read-only', model='sonnet', effort='medium')
            self.wait_status(agent_id, 'waiting')
            session = manager.agent_sessions[agent_id]
            manager.reply_agent(agent_id, 'Use the parser', {'model': 'opus', 'model_set': True, 'effort': 'high',
                                                              'effort_set': True})
            self.wait_status(agent_id, 'waiting')
        args = self.run_args('claude')
        self.assertEqual((args[args.index('--model') + 1], args[args.index('--effort') + 1]), ('opus', 'high'))
        self.assertEqual(args[args.index('--resume') + 1], session)  # Claude applies them to the same session.
        self.assertEqual(manager.get_agent(agent_id, include_output=False)['latest_execution']['config_source'], 'override')
        self.assertEqual((manager.agent_models[agent_id], manager.agent_efforts[agent_id]), ('sonnet', 'medium'))
        manager.reply_agent(agent_id, 'Continue')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        args = self.run_args('claude')
        self.assertEqual((args[args.index('--model') + 1], args[args.index('--effort') + 1]), ('sonnet', 'medium'))
        history = self.rows("SELECT model,requested_effort,config_source FROM executions WHERE agent_id=? ORDER BY rowid", agent_id)
        self.assertEqual([(h['model'], h['requested_effort'], h['config_source']) for h in history],
                         [('sonnet', 'medium', 'agent_default'), ('opus', 'high', 'override'),
                          ('sonnet', 'medium', 'agent_default')])

    def test_codex_effort_update_keeps_the_session_and_a_model_change_needs_confirmation(self):
        with patch.dict(os.environ, FAKE_CODEX_SCENARIO='waiting'):
            agent_id = manager.start_agent('Codex work', 'codex', 'read-only', model='gpt-fake-1', effort='low')
            self.wait_status(agent_id, 'waiting')
            session = manager.agent_sessions[agent_id]
            self.assertIsNotNone(session)
            self.assertIn('model_reasoning_effort=low', self.run_args('codex'))
            # The catalog says this model accepts effort updates in an existing session.
            manager.reply_agent(agent_id, 'Go on', {'effort': 'high', 'effort_set': True})
            self.wait_status(agent_id, 'waiting')
            args = self.run_args('codex')
            self.assertEqual(args[args.index('resume') + 1], session)
            self.assertIn('model_reasoning_effort=high', args)
            runs = len(fake.runs(self.log, 'codex'))
            with self.assertRaisesRegex(ValueError, 'Confirm a new session'):
                manager.reply_agent(agent_id, 'Use the other model', {'model': 'gpt-fake-2', 'model_set': True})
            self.assertEqual((manager.agent_statuses[agent_id], len(fake.runs(self.log, 'codex'))), ('waiting', runs))
        with patch.dict(os.environ, FAKE_CODEX_SCENARIO='success'):
            manager.reply_agent(agent_id, 'Use the other model', {'model': 'gpt-fake-2', 'model_set': True,
                                                                  'confirm_new_session': True})
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        run = fake.runs(self.log, 'codex')[-1]
        self.assertNotIn('resume', run['args'])
        self.assertEqual(run['args'][run['args'].index('--model') + 1], 'gpt-fake-2')
        self.assertIn('so this is a new session', run['prompt'])
        self.assertIn('Which module?', run['prompt'])
        self.assertIn('Use the other model', run['prompt'])
        self.assertEqual((manager.agent_models[agent_id], manager.agent_efforts[agent_id]), ('gpt-fake-1', 'low'))

    def test_defaults_change_is_explicit_and_recorded(self):
        agent_id = manager.start_agent('Defaults', 'claude', 'read-only', model='sonnet', effort='low')
        self.wait_status(agent_id, 'finished')
        updated = manager.update_agent_defaults(agent_id, model='opus', model_set=True, effort='high', effort_set=True)
        self.assertEqual((updated['model'], updated['effort']), ('opus', 'high'))
        change = updated['provider_history'][-1]
        self.assertEqual((change['boundary'], change['from_model'], change['to_model']), ('defaults', 'sonnet', 'opus'))
        with self.assertRaisesRegex(providers.ProviderRefused, 'does not list effort'):
            manager.update_agent_defaults(agent_id, model='haiku', model_set=True)  # haiku has no effort levels.

    def test_unlisted_and_locked_models_are_rejected_by_the_backend(self):
        with self.assertRaisesRegex(providers.ProviderRefused, 'not verified'):
            manager.start_agent('Work', 'claude', 'read-only', model='claude-unlisted')
        self.assertEqual(fake.runs(self.log), [])
        agent_id = manager.start_agent('Work', 'claude', 'read-only', model='claude-unlisted', attempt_unverified=True)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertEqual(manager.get_agent(agent_id, include_output=False)['latest_execution']['entitlement_state'],
                         'explicit_attempt')
        with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
            manager.start_agent('Work', 'codex', 'read-only', model='gpt-fake-locked')
        with self.assertRaisesRegex(providers.ProviderRefused, 'does not list effort'):
            manager.start_agent('Work', 'claude', 'read-only', model='sonnet', effort='xhigh')
        with self.assertRaisesRegex(providers.ProviderRefused, 'reasoning control'):
            manager.start_agent('Work', 'gemini', 'read-only', effort='high')

    def test_subscription_change_between_selection_and_launch_is_enforced(self):
        manager.model_catalog_view('codex', refresh=True)
        self.assertTrue(next(m for m in manager.model_catalog_view('codex')['models'] if m['model_id'] == 'gpt-fake-1')['selectable'])
        locked = json.loads(json.dumps(fake.DEFAULT_CODEX_MODELS_JSON))
        locked['models'][0]['upgrade'] = {'plan': 'higher'}
        with patch.dict(os.environ, FAKE_CODEX_MODELS=json.dumps(locked)):
            manager.model_catalog_view('codex', refresh=True)
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
                manager.start_agent('Work', 'codex', 'read-only', model='gpt-fake-1')

    def test_account_switch_while_the_ui_is_open(self):
        manager.model_catalog_view('claude', refresh=True)
        other_models = [m for m in json.loads(json.dumps(fake.DEFAULT_CLAUDE_MODELS_JSON)) if m['value'] != 'opus']
        with patch.dict(os.environ, FAKE_CLAUDE_ACCOUNT=json.dumps({'email': 'b@example.test', 'subscriptionType': 'pro',
                                                                     'tokenSource': 'claude.ai', 'apiProvider': 'firstParty'}),
                        FAKE_CLAUDE_MODELS=json.dumps(other_models)):
            view = manager.model_catalog_view('claude', refresh=True)
            opus = next(m for m in view['models'] if m['model_id'] == 'opus')
            self.assertEqual((opus['status'], opus['selectable']), ('not_offered', False))
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
                manager.start_agent('Work', 'claude', 'read-only', model='opus')

    def test_unsupported_and_slow_discovery(self):
        with patch.dict(os.environ, FAKE_CODEX_NO_DISCOVERY='1'):
            view = manager.model_catalog_view('codex', refresh=True)
        self.assertEqual(view['refresh']['last_status'], 'unsupported')
        self.assertEqual(view['models'], [])
        model_catalog._last_attempt.clear()
        with patch.object(providers, 'DISCOVERY_TIMEOUT', 1), patch.dict(os.environ, FAKE_CODEX_DISCOVERY_SLEEP='5'):
            started = time.monotonic()
            view = manager.model_catalog_view('codex', refresh=True)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual(view['refresh']['last_status'], 'failed')

    def test_concurrent_refreshes_are_coalesced(self):
        with ThreadPoolExecutor(6) as pool:
            list(pool.map(lambda _: model_catalog.refresh(manager._store._connection, 'codex', trigger='panel_stale'),
                          range(6)))
        discoveries = [e for e in fake.read_log(self.log) if e['kind'] == 'discovery' and 'models' in e['args']]
        self.assertEqual(len(discoveries), 1)

    def test_quota_before_a_session_exists_waits_and_resumes_in_a_new_session(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='limit_presession'):
            agent_id = manager.start_agent('Early limit', 'claude', 'read-only')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        view = manager.get_agent(agent_id, include_output=False)
        self.assertIsNone(view['session_id'])
        self.assertFalse(view['provider_condition']['session_preserved'])
        windows = self.rows("SELECT window_id,kind,status FROM provider_usage_windows")
        self.assertEqual([(w['window_id'], w['kind'], w['status']) for w in windows], [('limit:five_hour', 'five_hour', 'exhausted')])
        with manager._store._connection() as db:
            db.execute("UPDATE provider_usage_windows SET observed_at='2020-01-01T00:00:00Z'")
            db.commit()
        runs = len(fake.runs(self.log, 'claude'))
        manager.continue_agent(agent_id, explicit_retry=True)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        run = fake.runs(self.log, 'claude')[-1]
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs + 1)
        self.assertNotIn('--resume', run['args'])
        self.assertIn('before the provider started a session', run['prompt'])
        self.assertEqual(manager._agent_task_id(agent_id), view['task_ids'][0])

    def test_claude_fallback_chain_blocks_explicit_models_and_reported_mismatch_is_shown(self):
        settings = Path(os.environ['USERPROFILE']) / '.claude'
        settings.mkdir(parents=True, exist_ok=True)
        (settings / 'settings.json').write_text(json.dumps({'fallbackModel': ['haiku']}), encoding='utf-8')
        providers.invalidate()
        with self.assertRaisesRegex(providers.ProviderRefused, 'fallbackModel'):
            manager.start_agent('Work', 'claude', 'read-only', model='sonnet')
        (settings / 'settings.json').unlink()
        providers.invalidate()
        with patch.dict(os.environ, FAKE_CLAUDE_INIT_MODEL='claude-other-model'):
            agent_id = manager.start_agent('Work', 'claude', 'read-only', model='sonnet')
            self.wait_status(agent_id, 'finished')
        snapshot = manager.get_agent(agent_id, include_output=False)['latest_execution']
        self.assertIn('not enforced', snapshot['effective_note'])


    # ---- review follow-up regressions

    def test_codex_account_switch_is_enforced_without_a_manual_refresh(self):
        auth = Path(os.environ['CODEX_HOME']) / 'auth.json'
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'account_id': 'acct-a'}}), encoding='utf-8')
        manager.model_catalog_view('codex', refresh=True)
        auth.write_text(json.dumps({'auth_mode': 'chatgpt', 'tokens': {'account_id': 'acct-b'}}), encoding='utf-8')
        locked = json.loads(json.dumps(fake.DEFAULT_CODEX_MODELS_JSON))
        locked['identity'] = 'fake-account-2'
        locked['models'][0]['upgrade'] = {'plan': 'higher'}
        with patch.dict(os.environ, FAKE_CODEX_MODELS=json.dumps(locked)):
            # Within the coalescing window, and with no refresh by the user.
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
                manager.start_agent('Work', 'codex', 'read-only', model='gpt-fake-1')
        self.assertEqual(fake.runs(self.log, 'codex'), [])

    def test_codex_environment_defaults_are_admitted_like_a_selection(self):
        manager.model_catalog_view('codex', refresh=True)
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-fake-locked'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot run'):
                manager.start_agent('Work', 'codex', 'read-only')
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-fake-1', CONTROL_CENTER_CODEX_REASONING_EFFORT='xhigh'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'does not list effort'):
                manager.start_agent('Work', 'codex', 'read-only')
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-fake-unlisted'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'not verified'):
                manager.start_agent('Work', 'codex', 'read-only')
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_REASONING_EFFORT='xhigh'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'default model'):
                manager.start_agent('Work', 'codex', 'read-only')  # Not every runnable model lists xhigh.
        self.assertEqual(fake.runs(self.log, 'codex'), [])
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-fake-2', CONTROL_CENTER_CODEX_REASONING_EFFORT='xhigh',
                        FAKE_CODEX_SCENARIO='slow', FAKE_SLOW_SECONDS='2'):
            legacy = manager.start_agent('Legacy', 'codex', 'read-only')
            chosen = manager.start_agent('Chosen', 'codex', 'read-only', model='gpt-fake-1', effort='low')
        for agent_id in (legacy, chosen):
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        argv = {tuple(r['args'][r['args'].index('--model') + 1:r['args'].index('--model') + 2]): r['args']
                for r in fake.runs(self.log, 'codex')}
        self.assertIn('model_reasoning_effort=xhigh', argv[('gpt-fake-2',)])
        self.assertIn('model_reasoning_effort=low', argv[('gpt-fake-1',)])  # No configuration leaks between Agents.
        history = self.rows('SELECT agent_id,model,requested_effort,config_source FROM executions')
        by_agent = {h['agent_id']: (h['model'], h['requested_effort'], h['config_source']) for h in history}
        self.assertEqual(by_agent[legacy], ('gpt-fake-2', 'xhigh', 'agent_default+environment'))
        self.assertEqual(by_agent[chosen], ('gpt-fake-1', 'low', 'agent_default'))
        self.assertIsNone(manager.agent_models[legacy])  # The Agent default stays "provider default".

    def test_gemini_explicit_selection_is_refused_before_launch_and_recovery_is_manual(self):
        for model in ('pro', 'gemini-2.5-pro'):
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot guarantee'):
                manager.start_agent('Work', 'gemini', 'read-only', model=model, attempt_unverified=True)
        self.assertEqual(fake.runs(self.log, 'gemini'), [])  # Nothing ran: no fallback, no usage.
        with patch.dict(os.environ, FAKE_GEMINI_SCENARIO='waiting'):
            agent_id = manager.start_agent('Work', 'gemini', 'read-only')
            self.wait_status(agent_id, 'waiting')
            runs = len(fake.runs(self.log, 'gemini'))
            with self.assertRaisesRegex(providers.ProviderRefused, 'cannot guarantee'):
                manager.reply_agent(agent_id, 'Use Pro', {'model': 'pro', 'model_set': True, 'attempt_unverified': True,
                                                          'confirm_new_session': True})
            self.assertEqual((manager.agent_statuses[agent_id], len(fake.runs(self.log, 'gemini'))), ('waiting', runs))
        manager.reply_agent(agent_id, 'Continue')  # The Agent, its session and output are intact.
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertNotIn('--model', fake.runs(self.log, 'gemini')[-1]['args'])

    def test_gemini_auto_routing_records_observed_models_not_the_init_model(self):
        with patch.dict(os.environ, FAKE_GEMINI_INIT_MODEL='gemini-2.5-pro', FAKE_GEMINI_MODELS='gemini-2.5-flash'):
            routed = manager.start_agent('Work', 'gemini', 'read-only', model='auto', attempt_unverified=True)
            self.assertEqual(self.wait_status(routed, 'finished'), 'finished')
        latest = manager.get_agent(routed, include_output=False)['latest_execution']
        self.assertEqual(latest['effective_model'], 'gemini-2.5-flash')  # Usage statistics, never the init model.
        self.assertIn('routed this run', latest['effective_note'])
        self.assertIn('Configured at start: gemini-2.5-pro', latest['effective_note'])

    def test_gemini_pin_reaching_a_launch_is_never_completed_without_evidence(self):
        # Defense in depth: even if a pin reached a launch, the Task is not completed as model-verified.
        allow = {'entitlement_state': 'explicit_attempt', 'catalog_scope': None, 'catalog_revision': None, 'resolved_model': None}
        cases = (dict(FAKE_GEMINI_INIT_MODEL='gemini-2.5-pro', FAKE_GEMINI_MODELS='gemini-2.5-flash'),  # Fell back.
                 dict(FAKE_GEMINI_MODELS='gemini-2.5-pro,gemini-2.5-flash'),  # Primary not identified.
                 dict(FAKE_GEMINI_NO_STATS='1'))  # Only the init event names the model.
        with patch.object(model_catalog, 'admission', return_value=allow):
            for env in cases:
                with patch.dict(os.environ, env):
                    agent_id = manager.start_agent('Work', 'gemini', 'read-only', model='gemini-2.5-pro')
                    self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting', env)
                view = manager.get_agent(agent_id)
                self.assertEqual(view['provider_condition']['kind'], 'model_not_enforced', env)
                self.assertEqual(view['provider_condition']['label'], 'Selected model not verified')
                self.assertIsNotNone(manager._agent_task_id(agent_id))  # No false Task completion.
            runs = len(fake.runs(self.log, 'gemini'))
            time.sleep(1)
            self.assertEqual(len(fake.runs(self.log, 'gemini')), runs)  # Nothing restarts automatically.
            with patch.dict(os.environ, FAKE_GEMINI_MODELS='gemini-2.5-pro'):
                manager.continue_agent(agent_id)  # The user's explicit choice; same Agent and session.
                self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')


if __name__ == '__main__':
    unittest.main()
