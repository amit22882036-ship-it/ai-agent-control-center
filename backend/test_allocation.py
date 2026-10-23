"""Quality-first, usage-aware Auto allocation of delegated Children (fake catalogs, isolated databases).

No model request, discovery subprocess or real account is used: catalogs are seeded
through patched discovery and readings are written as provider observations.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app import agent_manager as manager, allocation, model_catalog, promotion, providers
from app.persistence import AgentStore
import test_promotion as base
import test_planning as plans

CODEX = {'identity': 'acct', 'models': [
    {'slug': 'gpt-fast', 'display_name': 'GPT Fast', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}]},
    {'slug': 'gpt-deep', 'display_name': 'GPT Deep',
     'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}, {'effort': 'xhigh'}]},
    {'slug': 'gpt-locked', 'display_name': 'Locked', 'supported_reasoning_levels': [], 'upgrade': {'x': 1}}]}
CLAUDE = {'models': [
    {'value': 'default', 'resolvedModel': 'c-opus', 'displayName': 'Default', 'supportsEffort': True,
     'supportedEffortLevels': ['low', 'medium', 'high']},
    {'value': 'sonnet', 'resolvedModel': 'c-sonnet', 'displayName': 'Sonnet', 'supportsEffort': True,
     'supportedEffortLevels': ['low', 'medium', 'high']},
    {'value': 'opus', 'resolvedModel': 'c-opus', 'displayName': 'Opus', 'supportsEffort': True,
     'supportedEffortLevels': ['low', 'medium', 'high', 'xhigh']}],
    'account': {'email': 'a@example.test', 'subscriptionType': 'max', 'tokenSource': 'claude.ai', 'apiProvider': 'firstParty'}}


def candidate(provider, suitability='preferred', model=None, effort=None, rationale=None):
    value = dict(provider=provider, suitability=suitability)
    for key, item in (('model', model), ('effort', effort), ('rationale', rationale)):
        if item is not None:
            value[key] = item
    return value


def request(*candidates, category='architecture', complexity=None, capabilities=()):
    value = dict(mode='auto', category=category, capabilities=list(capabilities), candidates=list(candidates))
    if complexity:
        value['complexity'] = complexity
    return value


def seed_catalogs(connect, codex=CODEX, claude=CLAUDE):
    with patch.object(providers.CODEX, 'discover_models', return_value=(codex, 'codex-cli 9')), \
            patch.object(providers.CLAUDE, 'discover_models', return_value=(claude, (2, 1, 280))):
        model_catalog.refresh(connect, 'codex', force=True)
        model_catalog.refresh(connect, 'claude', force=True)


class EvaluationTests(unittest.TestCase):
    """The selection rules against an isolated database (read-only evaluation)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'allocation.sqlite3')
        self.enterContext(patch.object(providers, '_fingerprint', side_effect=lambda provider: ('cli', provider)))
        self.account = 'account-a'
        self.enterContext(patch.object(providers, 'account_evidence', side_effect=lambda provider: self.account))
        self.enterContext(patch.object(providers, 'cached', return_value=None))
        model_catalog._last_attempt.clear()
        self.addCleanup(model_catalog._last_attempt.clear)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for provider in ('codex', 'claude', 'gemini'):
                providers.update_settings(db, provider, enabled=True, billing_confirmed=True)
        seed_catalogs(self.store._connection)
        self.policy = dict(allocation.normalize_policy(True, ['codex', 'claude', 'gemini', 'mock']), task_id='root')
        self.parent = dict(agent_type='codex', sandbox='read-only')

    def evaluate(self, value, policy=None):
        with self.store._connection() as db:
            return allocation.evaluate(db, policy or self.policy, self.parent, value)

    def window(self, provider, window_id, status, used=None, model_scope=None, reset_at=None):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, provider, [dict(window_id=window_id, label=window_id, status=status,
                                                        used_percent=used, model_scope=model_scope, reset_at=reset_at)],
                                     source='claude_usage_command')

    def test_quality_first_low_but_nonzero_quota_stays_eligible(self):
        self.window('claude', 'five_hour', 'available', used=92)  # Little remains, but some does.
        result = self.evaluate(request(candidate('claude', 'preferred', 'opus', rationale='Cross-module design'),
                                       candidate('codex', 'acceptable', 'gpt-fast')))
        self.assertEqual((result['status'], result['provider'], result['model']), ('selected', 'claude', 'opus'))
        self.assertIn('rated preferred by the Parent', result['explanation'])
        self.assertIn('Cross-module design', result['explanation'])
        self.assertIn('may pause', result['explanation'])

    def test_more_usage_never_outweighs_suitability(self):
        self.window('codex', 'weekly', 'available', used=5)  # Plenty, with a fresh reading.
        result = self.evaluate(request(candidate('claude', 'preferred', 'sonnet'), candidate('codex', 'acceptable', 'gpt-deep')))
        self.assertEqual(result['provider'], 'claude')  # Its usage is unknown, not zero, and not a reason to downgrade.
        self.assertIn('unknown, not zero', result['explanation'])
        self.assertEqual(next(c for c in result['candidates'] if c['provider'] == 'claude')['usage'], 'unknown')

    def test_confirmed_exhausted_quota_excludes_and_a_comparable_alternative_may_be_chosen(self):
        for windows in (['five_hour'], ['weekly'], ['five_hour', 'weekly']):
            with self.subTest(windows=windows):
                with self.store._connection() as db:
                    db.execute('DELETE FROM provider_usage_windows')
                    db.commit()
                for window in windows:
                    self.window('claude', window, 'exhausted', used=100)
                result = self.evaluate(request(candidate('claude', 'preferred', 'opus'), candidate('codex', 'preferred', 'gpt-deep')))
                self.assertEqual((result['provider'], result['model']), ('codex', 'gpt-deep'))
                self.assertIn('confirmed usage limit', result['explanation'])

    def test_one_window_available_and_another_exhausted_still_blocks(self):
        self.window('claude', 'five_hour', 'available', used=10)
        self.window('claude', 'weekly', 'exhausted', used=100)
        result = self.evaluate(request(candidate('claude', 'preferred', 'opus')))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('No weaker or unassessed model was substituted', result['explanation'])

    def test_model_specific_and_shared_limits(self):
        self.window('claude', 'weekly_scoped', 'exhausted', used=100, model_scope='Opus')
        scoped = self.evaluate(request(candidate('claude', 'preferred', 'opus'), candidate('claude', 'preferred', 'sonnet')))
        self.assertEqual(scoped['model'], 'sonnet')  # Only the model-scoped limit applies to Opus.
        self.window('claude', 'shared', 'exhausted', used=100)
        shared = self.evaluate(request(candidate('claude', 'preferred', 'opus'), candidate('claude', 'preferred', 'sonnet')))
        self.assertEqual(shared['status'], 'blocked')

    def test_unlisted_alternatives_are_never_chosen_and_no_evidence_abstains(self):
        self.window('claude', 'weekly', 'exhausted', used=100)
        held = self.evaluate(request(candidate('claude', 'preferred', 'opus')))  # Codex is eligible but not assessed.
        self.assertEqual((held['status'], held['provider']), ('blocked', None))
        empty = self.evaluate(request())
        self.assertEqual(empty['status'], 'blocked')
        self.assertIn('No suitability evidence', empty['explanation'])
        profiled = dict(self.policy, profile={'architecture': {'preferred': ['codex/gpt-deep@high']}})
        self.assertEqual(self.evaluate(request(), profiled)['model'], 'gpt-deep')  # The user's profile, labelled.
        self.assertIn('your Auto profile', self.evaluate(request(), profiled)['explanation'])

    def test_comparable_candidates_break_ties_by_usage_evidence_then_preferences_then_order(self):
        both = request(candidate('codex', 'preferred', 'gpt-deep'), candidate('claude', 'preferred', 'opus'))
        self.assertEqual(self.evaluate(both)['provider'], 'codex')  # Evidence order, deterministic.
        preferring = dict(self.policy, preferences=['claude/opus'])
        self.assertEqual(self.evaluate(both, preferring)['provider'], 'claude')
        self.window('codex', 'weekly', 'available', used=40)
        self.assertEqual(self.evaluate(both, preferring)['provider'], 'codex')  # A fresh reading outranks no reading.
        result = self.evaluate(both, preferring)
        self.assertIn('Also eligible at the same level', result['explanation'])

    def test_effort_follows_the_model_and_is_never_changed(self):
        high = self.evaluate(request(candidate('codex', 'preferred', 'gpt-deep'), complexity='high'))
        self.assertEqual(high['effort'], 'high')
        fast = self.evaluate(request(candidate('codex', 'preferred', 'gpt-fast'), complexity='high'))
        self.assertIsNone(fast['effort'])  # Not listed for this model: provider default, never max.
        low = self.evaluate(request(candidate('codex', 'preferred', 'gpt-deep'), complexity='low'))
        self.assertEqual(low['effort'], 'low')
        explicit = self.evaluate(request(candidate('codex', 'preferred', 'gpt-fast', effort='xhigh')))
        self.assertEqual(explicit['status'], 'blocked')  # An unsupported named effort excludes, never changes.
        self.assertIn('does not list effort', explicit['explanation'])

    def test_hard_eligibility_is_never_bypassed(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'gemini', enabled=False)
        cases = {
            'gemini': candidate('gemini'),                                # Disabled.
            'gpt-locked': candidate('codex', model='gpt-locked'),        # Account-restricted model.
            'gpt-unlisted': candidate('codex', model='gpt-unlisted'),    # Unknown entitlement: never attempted.
        }
        for name, item in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.evaluate(request(item))['status'], 'blocked')
        narrow = dict(self.policy, providers=['codex'])
        self.assertIn('not in the providers you allowed', self.evaluate(request(candidate('claude')), narrow)['explanation'])
        models = dict(self.policy, models={'codex': ['gpt-fast']})
        self.assertIn('not in the models you allowed', self.evaluate(request(candidate('codex', model='gpt-deep')), models)['explanation'])
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', billing_confirmed=False)
        self.assertIn('billing route is not acknowledged', self.evaluate(request(candidate('codex', model='gpt-deep')))['explanation'])
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', billing_confirmed=True)
        with patch.object(providers, 'cached', return_value=providers.Readiness('codex', providers.NOT_AUTHENTICATED, 'signed out')):
            self.assertIn('not ready', self.evaluate(request(candidate('codex', model='gpt-deep')))['explanation'])
        coordinated = self.evaluate(request(candidate('gemini'), capabilities=['coordination']))
        self.assertEqual(coordinated['status'], 'blocked')

    def test_effort_specific_preferences_match_only_that_effort(self):
        low_high = request(candidate('codex', 'preferred', 'gpt-deep', effort='low'),
                           candidate('codex', 'preferred', 'gpt-deep', effort='high'))
        self.assertEqual(self.evaluate(low_high)['effort'], 'low')  # Deterministic: the assessment's order.
        specific = dict(self.policy, preferences=['codex/gpt-deep@high'])
        self.assertEqual(self.evaluate(low_high, specific)['effort'], 'high')
        broad_model = dict(self.policy, preferences=['codex/gpt-deep'])
        self.assertEqual(self.evaluate(low_high, broad_model)['effort'], 'low')  # Matches both: order decides.
        broad_provider = dict(self.policy, preferences=['claude', 'codex'])
        mixed = request(candidate('codex', 'preferred', 'gpt-deep', effort='low'), candidate('claude', 'preferred', 'opus'))
        self.assertEqual(self.evaluate(mixed, broad_provider)['provider'], 'claude')
        quality = request(candidate('codex', 'preferred', 'gpt-deep', effort='low'),
                          candidate('codex', 'acceptable', 'gpt-deep', effort='high'))
        self.assertEqual(self.evaluate(quality, specific)['effort'], 'low')  # Preferences never outrank suitability.

    def test_gemini_exact_pin_stays_blocked_but_its_default_is_eligible(self):
        pinned = self.evaluate(request(candidate('gemini', model='pro')))
        self.assertEqual(pinned['status'], 'blocked')
        self.assertIn('cannot guarantee', pinned['explanation'])
        default = self.evaluate(request(candidate('gemini'), complexity='high'))
        self.assertEqual((default['provider'], default['model'], default['effort']), ('gemini', None, None))

    def test_unconfirmed_recorded_limit_is_not_retried_by_automation(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO provider_usage_blocks(provider,condition,reason) VALUES ('codex','quota_exhausted','Limit')""")
        result = self.evaluate(request(candidate('codex', model='gpt-deep')))
        self.assertEqual(result['status'], 'blocked')
        self.assertIn('not confirmed renewed', result['explanation'])

    def test_dynamic_catalog_and_account_changes_affect_future_allocations(self):
        self.assertEqual(self.evaluate(request(candidate('codex', model='gpt-new')))['status'], 'blocked')
        updated = json.loads(json.dumps(CODEX))
        updated['models'].append({'slug': 'gpt-new', 'display_name': 'New', 'supported_reasoning_levels': [{'effort': 'low'}]})
        seed_catalogs(self.store._connection, codex=updated)
        self.assertEqual(self.evaluate(request(candidate('codex', model='gpt-new')))['model'], 'gpt-new')
        self.account = 'account-b'  # A different sign-in: the cached entitlement is not reused.
        self.assertEqual(self.evaluate(request(candidate('codex', model='gpt-new')))['status'], 'blocked')
        self.assertEqual(self.evaluate(request(candidate('codex')))['status'], 'selected')  # Provider default.

    def test_no_numbers_or_rankings_are_invented(self):
        result = self.evaluate(request(candidate('codex', 'preferred', 'gpt-deep'), candidate('claude', 'acceptable', 'opus')))
        text = json.dumps(result)
        self.assertNotRegex(text, r'score|benchmark":|%')
        self.assertIn('not a benchmark', result['explanation'])

    def test_request_validation_and_policy_authority(self):
        with self.assertRaises(ValueError):
            allocation.validate_request(dict(mode='auto', providers=['claude']))  # A worker cannot widen policy.
        with self.assertRaises(ValueError):
            allocation.validate_request(request(candidate('auto')))  # Allocation Auto is not a provider.
        with self.assertRaises(ValueError):
            allocation.validate_request(dict(mode='auto', candidates=[dict(provider='codex', suitability='best')]))
        with self.assertRaises(ValueError):
            allocation.normalize_policy(True, [])


class AllocationPromotionTests(unittest.TestCase):
    """Auto through the existing promotion, materialization and launch path."""
    setUp_base = base.PromotionTests.setUp
    patch = base.PromotionTests.patch
    stream = base.PromotionTests.stream
    finish = base.PromotionTests.finish
    launch = base.PromotionTests.launch
    plan = base.PromotionTests.plan
    message = base.PromotionTests.message
    rows = base.PromotionTests.rows
    accepted = base.PromotionTests.accepted
    workspace_fixture = base.PromotionTests.workspace_fixture

    def setUp(self):
        self.setUp_base()
        self.launched = []

        def codex_command(sandbox='read-only', session_id=None, structured=False, model=None, effort=None):
            self.launched.append((model, effort))
            from app.codex_events import StructuredCommand
            return StructuredCommand('exec --json resume ' + str(session_id)) if structured else f'exec {model} {effort}'
        self.patch(patch.object(manager, '_codex_command', side_effect=codex_command))
        self.patch(patch.object(providers, 'account_evidence', return_value='account-a'))
        model_catalog._last_attempt.clear()
        self.addCleanup(model_catalog._last_attempt.clear)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', billing_confirmed=True)
        with patch.object(providers.CODEX, 'discover_models', return_value=(CODEX, 'codex-cli 9')):
            model_catalog.refresh(self.store._connection, 'codex', force=True)

    def authorize(self, allowed=('codex', 'mock'), **values):
        return manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=list(allowed), **values)

    def item(self, key='a', allocation_request=None, **changes):
        return base.selection(key, allocation=allocation_request or request(candidate('codex', model='gpt-deep')), **changes)

    def decisions(self):
        with self.store._connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM allocation_decisions ORDER BY decision_id')]

    def delegation(self, key='a'):
        with self.store._connection() as db:
            return dict(db.execute('''SELECT d.* FROM delegations d JOIN promotion_items p USING(delegation_id)
                WHERE p.proposal_id=?''', (key,)).fetchone())

    def test_auto_requires_the_users_authorization(self):
        self.accepted(items=[self.item()])
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertIn('not authorized', row['reason'])
        self.assertIsNone(self.delegation()['child_provider'])
        self.assertEqual(self.decisions()[-1]['status'], 'blocked')
        # The user's later authorization lets the existing pass reconsider the held item.
        self.authorize()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            promotion.prepare(db, row['delegation_id'])
        self.assertEqual(self.rows()[0]['disposition'], 'ready')
        self.assertEqual((self.delegation()['child_provider'], self.delegation()['child_model']), ('codex', 'gpt-deep'))

    def test_children_receive_their_own_models_and_efforts_through_the_existing_launch(self):
        self.authorize()
        plan = self.plan([plans.item('a'), plans.item('b')])
        self.accepted(items=[self.item('a', request(candidate('codex', model='gpt-deep'), complexity='high')),
                             self.item('b', request(candidate('codex', model='gpt-fast'), complexity='low'))], plan=plan)
        self.assertEqual([r['disposition'] for r in self.rows()], ['ready', 'ready'])
        self.workspace_fixture()
        manager.materialize_delegations(self.task['task_id'])
        self.assertIn(('gpt-deep', 'high'), self.launched)
        self.assertIn(('gpt-fast', 'low'), self.launched)
        children = [a for a in manager.agents if manager.agent_models.get(a) in ('gpt-deep', 'gpt-fast')]
        self.assertEqual(sorted((manager.agent_models[a], manager.agent_efforts[a]) for a in children),
                         [('gpt-deep', 'high'), ('gpt-fast', 'low')])
        view = manager.get_agent(children[0], include_output=False)
        self.assertEqual(view['allocation']['status'], 'selected')
        self.assertIn('Selected Codex', view['allocation']['explanation'])

    def test_explicit_requests_keep_their_semantics_under_an_auto_policy(self):
        self.authorize(allowed=('codex', 'claude'))
        self.accepted(items=[base.selection(provider='claude', model='sonnet')])
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertIn('No other provider is substituted', row['reason'])
        self.assertEqual(self.decisions(), [])  # No Auto decision for an explicit request.
        with self.assertRaises(ValueError):
            promotion.validate(self.message([base.selection(provider='codex', allocation=request(candidate('codex')))]))

    def test_partial_admission_and_dependencies(self):
        self.authorize()
        plan = self.plan([plans.item('a'), plans.item('b'), plans.item('c', depends_on=['b'])])
        self.accepted(items=[self.item('a'), self.item('b', request(candidate('claude', model='opus'))),
                             self.item('c', request(candidate('codex', model='gpt-fast')))], plan=plan)
        dispositions = {r['proposal_id']: r for r in self.rows()}
        self.assertEqual(dispositions['a']['disposition'], 'ready')
        self.assertEqual(dispositions['b']['disposition'], 'blocked')  # Claude is not allowed: held, not downgraded.
        self.assertIn('Auto allocation held', dispositions['b']['reason'])
        self.assertEqual(dispositions['c']['disposition'], 'blocked')  # Its prerequisite has no result.
        self.workspace_fixture()
        manager.materialize_delegations(self.task['task_id'])
        models = [manager.agent_models.get(a) for a in manager.agents]
        self.assertEqual(models.count('gpt-deep'), 1)
        self.assertNotIn('gpt-fast', models)

    def test_decision_is_durable_idempotent_and_reconsidered_only_before_admission(self):
        self.authorize()
        key, process = self.accepted(items=[self.item('a', request(candidate('codex', model='gpt-deep'),
                                                                   candidate('mock')))])
        first = self.decisions()
        self.assertEqual(len(first), 1)
        # A replay of the same decision is a receipt: no new decision, no new Child.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            promotion.prepare(db, self.rows()[0]['delegation_id'])
        self.assertEqual(len(self.decisions()), 1)
        # Codex becomes unavailable before admission: the flexible request is reconsidered.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', enabled=False)
            promotion.prepare(db, self.rows()[0]['delegation_id'])
        decisions = self.decisions()
        self.assertEqual(len(decisions), 2)
        self.assertIsNotNone(decisions[0]['superseded_at'])
        self.assertIn('became ineligible', decisions[1]['explanation'])
        self.assertEqual(self.delegation()['child_provider'], 'mock')
        # Once the Child starts, the selection never changes.
        self.workspace_fixture()
        manager.materialize_delegations(self.task['task_id'])
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', enabled=True)
            promotion.prepare(db, self.rows()[0]['delegation_id'])
            db.execute("UPDATE auto_allocation_policies SET providers='[\"codex\"]'")
            db.commit()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')  # A usage limit after the start changes nothing either.
            providers.record_windows(db, 'codex', [dict(window_id='weekly', label='Weekly', status='exhausted')],
                                     source='claude_usage_command')
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.decisions()), 2)
        self.assertEqual(self.delegation()['child_provider'], 'mock')
        child = next(a for a in manager.agents if manager.agent_types.get(a) == 'mock')
        self.assertEqual(manager.agent_types[child], 'mock')  # No automatic provider switch for the running Child.
        self.assertEqual(len([t for t in self.store.list_tasks() if t['parent_task_id']]), 1)

    def test_restart_and_concurrent_passes_create_one_child(self):
        self.authorize()
        self.accepted(items=[self.item()])
        self.workspace_fixture()
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: manager.materialize_delegations(self.task['task_id']), range(4)))
        self.assertEqual(len([d for d in self.decisions() if d['superseded_at'] is None]), 1)
        self.assertEqual(len([t for t in self.store.list_tasks() if t['parent_task_id']]), 1)
        self.assertEqual([manager.agent_models.get(a) for a in manager.agents].count('gpt-deep'), 1)
        restarted = AgentStore(self.store.path)  # The decision survives a restart unchanged.
        with restarted._connection() as db:
            self.assertEqual(allocation.current(db, self.rows()[0]['delegation_id'])['provider'], 'codex')

    # ---- final review: Auto authorization at the final admission boundary

    def revoke_between_preparation_and_launch(self, change, *, at='final'):
        """Apply `change` after preparation recorded the selection, before the Child launches.
        at='admit': before the materialization admission; 'final': between it and Popen."""
        if at == 'admit':
            original = manager.delegation_materialization.Materializer.prepare

            def prepared(service, identifier):
                record = original(service, identifier)
                change()
                return record
            return patch.object(manager.delegation_materialization.Materializer, 'prepare', prepared)
        original = manager._spawn_with_resources

        def spawn(*args, **kwargs):
            change()  # Committed after admit, before the final admission transaction.
            return original(*args, **kwargs)
        return patch.object(manager, '_spawn_with_resources', side_effect=spawn)

    def children(self):
        return [t for t in self.store.list_tasks() if t['parent_task_id'] == self.task['task_id']]

    def assert_refused_without_process(self, before):
        self.assertEqual(len(self.commands), before)  # No process was created.
        child = self.children()[0]
        self.assertFalse([a for a in self.store.list_task_assignments(child['task_id']) if a['ended_at'] is None])
        with self.store._connection() as db:
            progress = db.execute('SELECT phase,last_error FROM delegation_materializations').fetchone()
        # Back to preparation (never ownership inspection): no process could exist.
        self.assertIn(progress['phase'], ('attached', 'workspace_ready'))
        self.assertIn('AllocationRevoked', progress['last_error'])
        self.assertEqual(self.delegation()['child_provider'], 'codex')  # Nothing substituted at the boundary.

    def test_auto_revoked_after_preparation_is_refused_at_admission(self):
        for at in ('admit', 'final'):
            with self.subTest(at=at):
                self.setUp_clean()
                self.authorize()
                self.accepted(items=[self.item()])
                self.assertEqual(self.rows()[0]['disposition'], 'ready')
                self.workspace_fixture()
                before = len(self.commands)
                with self.revoke_between_preparation_and_launch(
                        lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[]), at=at):
                    manager.materialize_delegations(self.task['task_id'])
                self.assert_refused_without_process(before)
                with self.store._connection() as db:
                    with self.assertRaisesRegex(allocation.AllocationRevoked, 'disabled Auto'):
                        promotion.require_ready(db, self.rows()[0]['delegation_id'])
                # The next bounded pass uses the existing preparation: the revoked policy holds it.
                manager.materialize_delegations(self.task['task_id'])
                self.assertEqual(self.rows()[0]['disposition'], 'blocked')
                self.assertEqual(len(self.commands), before)
                self.assertEqual(len(self.children()), 1)

    def setUp_clean(self):
        self.doCleanups()
        self.setUp()

    def test_removed_provider_or_model_is_refused_before_launch(self):
        changes = {
            'provider': lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['mock']),
            'model': lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['codex'],
                                                                models={'codex': ['gpt-fast']}),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.setUp_clean()
                self.authorize()
                self.accepted(items=[self.item()])
                self.workspace_fixture()
                before = len(self.commands)
                with self.revoke_between_preparation_and_launch(change):
                    manager.materialize_delegations(self.task['task_id'])
                self.assert_refused_without_process(before)
                self.assertIn('no longer an allowed ' + name, self.delegation_error())

    def delegation_error(self):
        with self.store._connection() as db:
            return db.execute('SELECT last_error FROM delegation_materializations').fetchone()[0]

    def test_ancestor_policy_change_reaches_descendant_launches(self):
        ancestor = self.store.create_task('Ancestor', project_id=self.project['project_id'])
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (ancestor['task_id'], self.task['task_id']))
            db.commit()
        manager.set_auto_allocation_policy(ancestor['task_id'], enabled=True, allowed=['codex'])
        self.accepted(items=[self.item()])
        self.assertEqual(self.rows()[0]['disposition'], 'ready')  # Inherited authorization.
        self.workspace_fixture()
        before = len(self.commands)
        with self.revoke_between_preparation_and_launch(
                lambda: manager.set_auto_allocation_policy(ancestor['task_id'], enabled=False, allowed=[])):
            manager.materialize_delegations(self.task['task_id'])
        self.assert_refused_without_process(before)

    def test_policy_change_concurrent_with_launch_is_serialized(self):
        import threading
        self.authorize()
        self.accepted(items=[self.item()])
        self.workspace_fixture()
        observed = []
        original = manager._spawn_process

        def spawn(command, agent_type, *, cwd):
            # Popen happens inside the admission transaction: a concurrent policy write
            # cannot commit in between, so the committed policy still permits this launch.
            with AgentStore(self.store.path)._connection() as db:
                observed.append(allocation.policy_for(db, self.task['task_id'])['enabled'])
            return original(command, agent_type, cwd=cwd)
        self.patch(patch.object(manager, '_spawn_process', side_effect=spawn))
        revoke = threading.Thread(target=lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[]))
        launch = threading.Thread(target=lambda: manager.materialize_delegations(self.task['task_id']))
        launch.start(); revoke.start(); launch.join(30); revoke.join(30)
        self.assertTrue(all(observed), observed)  # Never launched under a committed revocation.
        self.assertLessEqual(len(observed), 1)
        self.assertEqual(len(self.children()), 1)

    def test_explicit_requests_and_started_children_are_unaffected(self):
        manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[])
        self.accepted(items=[base.selection(provider='codex')])
        self.workspace_fixture()
        before = len(self.commands)
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before + 1)  # Explicit: no Auto authorization involved.

    def test_valid_auto_launch_and_later_policy_change_after_start(self):
        self.authorize()
        self.accepted(items=[self.item()])
        self.workspace_fixture()
        before = len(self.commands)
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before + 1)  # Still authorized: starts normally.
        manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[])
        with self.store._connection() as db:
            promotion.require_ready(db, self.rows()[0]['delegation_id'])  # Started: never re-judged.
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before + 1)
        self.assertEqual(len(self.children()), 1)
        self.assertEqual(self.delegation()['child_provider'], 'codex')

    def test_recovery_after_refusal_creates_one_child(self):
        self.authorize()
        self.accepted(items=[self.item()])
        self.workspace_fixture()
        before = len(self.commands)
        with self.revoke_between_preparation_and_launch(
                lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[])):
            manager.materialize_delegations(self.task['task_id'])
        self.authorize()  # The user re-enables Auto; the existing pass reconsiders and launches once.
        manager.materialize_delegations(self.task['task_id'])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before + 1)
        self.assertEqual(len(self.children()), 1)
        self.assertEqual([manager.agent_models.get(a) for a in manager.agents].count('gpt-deep'), 1)

    def test_policy_editing_is_versioned_and_local_overrides_never_touch_the_ancestor(self):
        ancestor = self.store.create_task('Ancestor', project_id=self.project['project_id'])
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (ancestor['task_id'], self.task['task_id']))
            db.commit()
        root = manager.set_auto_allocation_policy(ancestor['task_id'], enabled=True, allowed=['codex', 'claude'],
                                                  expected_revision=None)
        view = manager.auto_allocation_view(self.task['task_id'])
        self.assertTrue(view['inherited'])
        self.assertEqual((view['owner_title'], view['own_revision']), ('Ancestor', None))
        # A local override from the child scope: the ancestor's policy is unchanged.
        local = manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['codex'],
                                                   models={'codex': ['gpt-fast']}, preferences=['codex/gpt-fast'],
                                                   profile={'testing': {'preferred': ['codex/gpt-fast@low']}},
                                                   expected_revision=None)
        self.assertFalse(local['inherited'])
        self.assertEqual(local['policy']['models'], {'codex': ['gpt-fast']})
        self.assertEqual(manager.auto_allocation_view(ancestor['task_id'])['policy']['providers'], ['codex', 'claude'])
        # A stale editor (another change happened since it loaded) is refused; nothing is written.
        with self.assertRaisesRegex(ValueError, 'changed since it was opened'):
            manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[], expected_revision=None)
        with self.assertRaisesRegex(ValueError, 'changed since it was opened'):
            manager.set_auto_allocation_policy(ancestor['task_id'], enabled=False, allowed=[],
                                               expected_revision=root['own_revision'] + 1000)
        self.assertTrue(manager.auto_allocation_view(self.task['task_id'])['policy']['enabled'])
        # Removing the local policy returns to the ancestor's (an explicit, versioned action).
        cleared = manager.clear_auto_allocation_policy(self.task['task_id'], expected_revision=local['own_revision'])
        self.assertTrue(cleared['inherited'])
        self.assertEqual(cleared['policy']['providers'], ['codex', 'claude'])
        with self.assertRaisesRegex(ValueError, 'no policy of its own'):
            manager.clear_auto_allocation_policy(self.task['task_id'])
        self.assertIsNotNone(root['own_revision'])

    def test_policy_revisions_reject_stale_writes_even_within_one_millisecond(self):
        import threading
        first = manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['codex'], expected_revision=None)
        loaded = first['own_revision']
        second = manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['codex', 'claude'],
                                                    expected_revision=loaded)
        self.assertGreater(second['own_revision'], loaded)
        with self.store._connection() as db:  # Identical wall-clock timestamps change nothing.
            db.execute("UPDATE auto_allocation_policies SET updated_at='2026-01-01T00:00:00.000Z'")
            db.commit()
        for stale in (lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[],
                                                                 expected_revision=loaded),
                      lambda: manager.clear_auto_allocation_policy(self.task['task_id'], expected_revision=loaded)):
            with self.assertRaisesRegex(ValueError, 'changed since it was opened'):
                stale()
        self.assertEqual(manager.auto_allocation_view(self.task['task_id'])['policy']['providers'], ['codex', 'claude'])
        # Two concurrent editors that loaded the same revision: exactly one wins.
        current = second['own_revision']
        outcomes = []

        def edit(allowed):
            try:
                manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=allowed, expected_revision=current)
                outcomes.append('saved')
            except ValueError:
                outcomes.append('refused')
        threads = [threading.Thread(target=edit, args=(allowed,)) for allowed in (['codex'], ['claude'])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes), ['refused', 'saved'])
        # A removed and recreated policy never reuses a revision.
        latest = manager.auto_allocation_view(self.task['task_id'])['own_revision']
        manager.clear_auto_allocation_policy(self.task['task_id'], expected_revision=latest)
        again = manager.set_auto_allocation_policy(self.task['task_id'], enabled=True, allowed=['codex'], expected_revision=None)
        self.assertGreater(again['own_revision'], latest)
        with self.assertRaisesRegex(ValueError, 'changed since it was opened'):
            manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[], expected_revision=latest)

    def test_api_policy_writes_require_a_revision_precondition(self):
        import threading
        from fastapi import HTTPException
        from app import main
        task_id = self.task['task_id']

        def put(**fields):
            return main.set_auto_allocation_route(task_id, main.AutoAllocationRequest(enabled=True, **fields))
        with self.assertRaises(HTTPException) as missing:
            put(providers=['codex'])  # Omitted: refused, never applied without the check.
        self.assertEqual(missing.exception.status_code, 428)
        self.assertIn('expected_revision is required', missing.exception.detail)
        self.assertIsNone(manager.auto_allocation_view(task_id)['policy'])
        created = put(providers=['codex'], expected_revision=None)  # Explicit null: creation only.
        first = created['own_revision']
        with self.assertRaises(HTTPException) as recreate:
            put(providers=['claude'], expected_revision=None)  # Null once a policy exists is stale.
        self.assertEqual(recreate.exception.status_code, 409)
        updated = put(providers=['codex', 'claude'], expected_revision=first)  # Current revision.
        with self.assertRaises(HTTPException) as stale:
            put(providers=['mock'], expected_revision=first)  # Client A's old revision after client B's write.
        self.assertEqual(stale.exception.status_code, 409)
        with self.assertRaises(HTTPException):
            put(providers=['mock'])  # A legacy client without the field cannot overwrite either.
        self.assertEqual(manager.auto_allocation_view(task_id)['policy']['providers'], ['codex', 'claude'])
        for revision, status in ((None, 428), (first, 409)):
            with self.assertRaises(HTTPException) as refused:
                main.clear_auto_allocation_route(task_id, expected_revision=revision)
            self.assertEqual(refused.exception.status_code, status)
        self.assertIsNotNone(manager.auto_allocation_view(task_id)['policy'])
        # Concurrent clients holding the same revision: exactly one write commits.
        current = updated['own_revision']
        outcomes = []

        def edit(allowed):
            try:
                put(providers=allowed, expected_revision=current)
                outcomes.append('saved')
            except HTTPException as exc:
                outcomes.append(exc.status_code)
        threads = [threading.Thread(target=edit, args=(allowed,)) for allowed in (['codex'], ['claude'])]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(sorted(outcomes, key=str), [409, 'saved'])
        # Delete with the current revision, then recreate: the old revision never matches again.
        latest = manager.auto_allocation_view(task_id)['own_revision']
        main.clear_auto_allocation_route(task_id, expected_revision=latest)
        again = put(providers=['codex'], expected_revision=None)
        self.assertGreater(again['own_revision'], latest)
        with self.assertRaises(HTTPException) as reused:
            put(providers=['claude'], expected_revision=latest)
        self.assertEqual(reused.exception.status_code, 409)

    def test_policy_validation_rejects_widening_shapes(self):
        for values in (dict(enabled=True, allowed=[]), dict(enabled=True, allowed=['codex'], models={'claude': ['opus']}),
                       dict(enabled=True, allowed=['codex'], preferences=['codex/bad model']),
                       dict(enabled=True, allowed=['codex'], profile={'unknown': {'preferred': ['codex']}})):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    manager.set_auto_allocation_policy(self.task['task_id'], **values)
        self.assertIsNone(manager.auto_allocation_view(self.task['task_id'])['policy'])

    def test_policy_and_decisions_are_inspectable(self):
        self.authorize(preferences=['codex/gpt-deep'])
        self.accepted(items=[self.item()])
        view = manager.auto_allocation_view(self.task['task_id'])
        self.assertTrue(view['policy']['enabled'])
        self.assertEqual(view['policy']['providers'], ['codex', 'mock'])
        child, = view['children']
        self.assertEqual(child['allocation']['provider'], 'codex')
        self.assertEqual(len(child['history']), 1)
        with self.store._connection() as db:
            prompt = allocation.instruction(db, self.task['task_id'])
        self.assertIn('gpt-deep', prompt)
        self.assertNotIn('gpt-locked', prompt)  # Unavailable models are not offered as options.


if __name__ == '__main__':
    unittest.main()
