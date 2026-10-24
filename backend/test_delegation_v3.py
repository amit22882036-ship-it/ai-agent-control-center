"""control-center.delegation.v3: ordinary delegation with an optional Child allocation.

Isolated databases, test doubles for provider processes and seeded catalogs only.
"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, allocation, delegation_continuation, delegation_protocol, delegation_v3
from app import delegations, model_catalog, providers
from app.codex_events import StructuredCommand
from app.persistence import AgentStore
import provider_test_support as fake
import test_allocation as alloc
import test_delegation_protocol as protocol_tests


def request(key, allocation_value=None, instruction=None):
    value = dict(request_key=key, instruction=instruction or 'Work on ' + key)
    if allocation_value is not None:
        value['allocation'] = allocation_value
    return value


def message(*requests, continuation=None, **changes):
    return json.dumps(dict(protocol=delegation_v3.PROTOCOL, action='request_delegations', requests=list(requests),
                           continuation=continuation) | changes)


def auto(*candidates, **options):
    return alloc.request(*candidates, **options)


class ProtocolTests(unittest.TestCase):
    """Strict schema; v1 and v2 keep their exact meanings."""

    def test_valid_explicit_auto_and_legacy_requests(self):
        payload = delegation_v3.validate(message(
            request('a', dict(mode='explicit', provider='codex', model='gpt-fast', effort='low')),
            request('b', auto(alloc.candidate('codex', model='gpt-deep'))),
            request('c')))
        self.assertEqual([r['request_key'] for r in payload['requests']], ['a', 'b', 'c'])

    def test_invalid_shapes_are_rejected(self):
        cases = {
            'mixed': request('a', dict(mode='auto', provider='codex', candidates=[])),
            'explicit with candidates': request('a', dict(mode='explicit', provider='codex', candidates=[])),
            'unknown mode': request('a', dict(mode='best')),
            'unknown provider': request('a', dict(mode='explicit', provider='auto')),
            'invalid model': request('a', dict(mode='explicit', provider='codex', model='bad model')),
            'invalid effort': request('a', dict(mode='explicit', provider='codex', effort='bad effort')),
            'mock effort': request('a', dict(mode='explicit', provider='mock', effort='low')),
            'bad candidate': request('a', dict(mode='auto', candidates=[dict(provider='codex')])),
            'worker policy': request('a', dict(mode='auto', providers=['claude'])),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    delegation_v3.validate(message(value))
        with self.assertRaises(ValueError):
            delegation_v3.validate(message(dict(request_key='a', instruction='x', extra=1)))
        with self.assertRaises(ValueError):
            delegation_v3.validate(json.dumps(dict(protocol=delegation_v3.PROTOCOL, action='request_delegations',
                                                   requests=[request('a')])))  # continuation is required.
        with self.assertRaisesRegex(ValueError, 'Malformed delegation JSON'):
            delegation_v3.validate('{"protocol":"control-center.delegation.v3","protocol":"x","action":"request_delegations",'
                                   '"requests":[],"continuation":null}')
        with self.assertRaisesRegex(ValueError, '256 KiB'):
            delegation_v3.validate(message(request('a', instruction='x' * 270000)))
        with self.assertRaises(ValueError):
            delegation_v3.validate(message(*[request(f'k{i}') for i in range(17)]))
        with self.assertRaises(ValueError):
            delegation_v3.validate(message(request('a'), request('a')))
        with self.assertRaises(ValueError):
            delegation_v3.validate(message(request('a'), continuation=dict(instruction='go')))

    def test_v1_and_v2_keep_their_closed_schemas(self):
        with self.assertRaises(ValueError):
            delegation_protocol.validate(json.dumps(dict(protocol=delegation_protocol.PROTOCOL, action='request_delegations',
                                                         requests=[request('a', dict(mode='explicit', provider='codex'))])))
        with self.assertRaises(ValueError):
            delegation_continuation.validate(json.dumps(dict(protocol=delegation_continuation.PROTOCOL,
                action='request_delegations', requests=[request('a', dict(mode='explicit', provider='codex'))],
                continuation=None)))
        with self.assertRaises(ValueError):
            delegation_v3.validate(protocol_tests.envelope('a'))  # A v1 envelope is not v3.
        self.assertEqual(delegation_protocol.validate(protocol_tests.envelope('a'))[0]['request_key'], 'a')


class OrdinaryAutoTests(unittest.TestCase):
    """v3 requests through the existing handoff, materialization and launch path."""
    setUp_base = protocol_tests.RuntimeTests.setUp
    patch = protocol_tests.RuntimeTests.patch
    read = protocol_tests.RuntimeTests.read
    finish = protocol_tests.RuntimeTests.finish

    def setUp(self):
        self.setUp_base()
        self.launched = []

        def codex_command(sandbox='read-only', session_id=None, structured=False, model=None, effort=None):
            self.launched.append((model, effort))
            return StructuredCommand('exec --json resume ' + str(session_id)) if structured else f'exec {model} {effort}'
        self.patch(patch.object(manager, '_codex_command', side_effect=codex_command))
        self.patch(patch.object(providers, 'account_evidence', return_value='account-a'))
        self.patch(patch.object(manager, '_schedule_materialization'))
        model_catalog._last_attempt.clear()
        self.addCleanup(model_catalog._last_attempt.clear)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'codex', billing_confirmed=True)
        with patch.object(providers.CODEX, 'discover_models', return_value=(alloc.CODEX, 'codex-cli 9')):
            model_catalog.refresh(self.store._connection, 'codex', force=True)

        def workspace(task_id, origin='legacy_project_snapshot'):
            task = self.store.get_task(task_id)
            if self.store.get_task_workspace(task_id) is None:
                key = str(uuid4())
                self.store.save_task_workspace(dict(workspace_id=key, task_id=task_id, project_id=task['project_id'],
                    workspace_path=str(self.root.parent / key), workspace_path_key=key,
                    base_snapshot='fixture', origin_kind=origin, source_task_id=task['parent_task_id']))
            return self.root
        self.patch(patch.object(manager, '_execution_workspace', side_effect=workspace))

    def authorize(self, allowed=('codex', 'mock'), task_id=None, **values):
        return manager.set_auto_allocation_policy(task_id or self.task['task_id'], enabled=True, allowed=list(allowed),
                                                  **values)

    def accept(self, *requests, continuation=None):
        self.parent = manager.start_agent('Parent work', 'codex', 'read-only', task_id=self.task['task_id'],
                                          delegation_protocol_enabled=True, child_materialization_enabled=True)
        self.read(self.parent, manager.agents[self.parent], message(*requests, continuation=continuation))
        self.finish(self.parent, manager.agents[self.parent])
        return self.store.list_delegations(self.task['task_id'])

    def record(self, key='a'):
        return next(d for d in self.store.list_delegations(self.task['task_id']) if d['request_key'] == key)

    def decisions(self):
        with self.store._connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM allocation_decisions ORDER BY decision_id')]

    def children(self):
        return [t for t in self.store.list_tasks() if t['parent_task_id'] == self.task['task_id']]

    def child_agents(self):
        return [a for a in manager.agents if manager.agent_parents.get(a) == self.parent]

    def test_auto_without_the_users_authorization_is_held(self):
        self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep'))))
        result, = manager.materialize_delegations(self.task['task_id'])
        self.assertIn('Auto allocation held', result['last_error'])
        self.assertIn('not authorized', self.record()['admission_error'])
        self.assertEqual(self.children(), [])  # No Child Task while held.
        decision, = self.decisions()
        self.assertEqual((decision['status'], decision['origin']), ('blocked', 'delegation'))
        self.authorize()  # The user's authorization lets the next pass decide.
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.children()), 1)
        self.assertEqual(self.launched[-1], ('gpt-deep', None))

    def test_parent_assessment_selects_model_and_effort_durably(self):
        self.authorize()
        self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep', rationale='Cross-module work'),
                                      alloc.candidate('codex', 'acceptable', model='gpt-fast'), complexity='high')))
        result, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'], 'started', result)
        record = self.record()
        self.assertEqual((record['child_provider'], record['child_model'], record['child_effort']), ('codex', 'gpt-deep', 'high'))
        decision, = self.decisions()
        self.assertEqual((decision['origin'], decision['proposal_id'], decision['plan_revision']), ('delegation', None, None))
        self.assertIn('Cross-module work', decision['explanation'])
        child, = self.child_agents()
        self.assertEqual((manager.agent_models[child], manager.agent_efforts[child]), ('gpt-deep', 'high'))
        self.assertIsNone(manager.agent_models[self.parent])  # Parent defaults are not touched.
        self.assertEqual(manager.get_agent(child, include_output=False)['allocation']['origin'], 'delegation')

    def test_profile_fallback_missing_evidence_and_unverified_models(self):
        self.authorize(profile={'testing': {'preferred': ['codex/gpt-fast']}})
        self.accept(request('a', auto(category='testing')), request('b', auto(category='review')),
                    request('c', auto(alloc.candidate('codex', model='gpt-unlisted'))))
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(self.record('a')['child_model'], 'gpt-fast')  # The user's profile.
        self.assertIn('No suitability evidence', self.record('b')['admission_error'] or self.latest('b'))
        self.assertIn('Auto allocation held', self.record('c')['admission_error'])  # Never an unverified model.
        self.assertEqual(len(self.child_agents()), 1)

    def latest(self, key):
        with self.store._connection() as db:
            return allocation.current(db, self.record(key)['delegation_id'])['explanation']

    def test_quota_low_remains_eligible_and_confirmed_exhaustion_excludes(self):
        self.authorize()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, 'codex', [dict(window_id='weekly', label='Weekly', status='available', used_percent=97)],
                                     source='claude_usage_command')
        self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep'))))
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(self.record('a')['child_model'], 'gpt-deep')  # 3% left still starts.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, 'codex', [dict(window_id='weekly', label='Weekly', status='exhausted', used_percent=100)],
                                     source='claude_usage_command')
        self.assertIn('confirmed usage limit', self.evaluate(auto(alloc.candidate('codex', model='gpt-fast')))['explanation'])

    def evaluate(self, value):
        with self.store._connection() as db:
            policy = allocation.policy_for(db, self.task['task_id'])
            return allocation.evaluate(db, policy, dict(agent_type='codex', sandbox='read-only'), value)

    def test_explicit_and_legacy_requests_in_one_batch(self):
        self.authorize()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'claude', enabled=False)
            # Stage 2G: allowed but disabled, so the launch reaches the readiness refusal under test.
            fake.allow_providers(db, ('codex', 'claude'))
        self.accept(request('explicit', dict(mode='explicit', provider='codex', model='gpt-fast', effort='low')),
                    request('auto', auto(alloc.candidate('mock'))),
                    request('legacy'),
                    request('refused', dict(mode='explicit', provider='claude', model='sonnet')))
        manager.materialize_delegations(self.task['task_id'])
        self.assertIn(('gpt-fast', 'low'), self.launched)  # Exactly as requested, no ranking.
        self.assertEqual(self.record('auto')['child_provider'], 'mock')  # A mixed-provider Child.
        self.assertIsNone(self.record('legacy')['child_provider'])  # The historical default: the Parent's.
        refused = self.record('refused')
        self.assertEqual((refused['child_provider'], refused['child_model']), ('claude', 'sonnet'))  # Never substituted.
        self.assertEqual([d['delegation_id'] for d in self.decisions()], [self.record('auto')['delegation_id']])
        # Existing gates stay: overlapping ordinary Children wait for declared scopes.
        self.assertEqual([manager.agent_types[a] for a in self.child_agents()], ['codex'])
        with self.store._connection() as db:
            error = db.execute('SELECT last_error FROM delegation_materializations m JOIN delegations d USING(delegation_id) '
                               "WHERE d.request_key='refused'").fetchone()[0]
        self.assertIn('ProviderRefused: Claude Code', error)  # The actual refusal reason, never another provider.

    def test_explicit_request_outside_the_permissions_is_held_without_a_child(self):
        # Stage 2G: enabled is not enough; the request is held with the permission reason, never substituted.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
        self.accept(request('held', dict(mode='explicit', provider='claude', model='sonnet')), request('legacy'))
        manager.materialize_delegations(self.task['task_id'])
        held = self.record('held')
        self.assertEqual((held['child_provider'], held['child_task_id']), ('claude', None))
        self.assertIn('Child provider not permitted: Claude Code is not in your allowed providers', held['admission_error'])
        self.assertEqual([manager.agent_types[a] for a in self.child_agents()], ['codex'])  # The permitted one runs.

    def test_explicit_request_under_a_deep_ancestor_restriction_is_held(self):
        # Review finding: an ancestor 70 levels up still bounds an explicit allocation. The delegation
        # depth limit (4 by default) is raised here so that only the permission can stop the Child.
        self.patch(patch.dict(os.environ, CONTROL_CENTER_DELEGATION_DEPTH='100'))
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
            fake.allow_providers(db, ('codex', 'claude'))
        top = parent = self.store.create_task('Top work', title='Top', project_id=self.task['project_id'])['task_id']
        for index in range(70):
            parent = self.store.create_task(f'Level {index}', title=f'Level {index}', parent_task_id=parent,
                                            project_id=self.task['project_id'])['task_id']
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (parent, self.task['task_id']))
        manager.set_task_provider_restrictions(top, ['codex'], expected_revision=None)
        self.accept(request('held', dict(mode='explicit', provider='claude', model='sonnet')))
        manager.materialize_delegations(self.task['task_id'])
        held = self.record('held')
        self.assertIsNone(held['child_task_id'])
        self.assertIn('provider restriction on Task "Top" excludes it', held['admission_error'])

    def test_request_identity_includes_the_allocation(self):
        self.parent = manager.start_agent('Parent work', 'codex', 'read-only', task_id=self.task['task_id'],
                                          delegation_protocol_enabled=True, child_materialization_enabled=True)
        assignment = self.store.get_active_assignment_for_task(self.task['task_id'])['assignment_id']
        options = dict(project_id=self.project['project_id'], requested_by_agent_id=self.parent,
                       requested_by_assignment_id=assignment, request_key='k', instruction='Work on k')
        first = self.store.create_delegation(self.task['task_id'], allocation=auto(alloc.candidate('codex')), **options)
        self.assertEqual(self.store.create_delegation(self.task['task_id'], allocation=auto(alloc.candidate('codex')),
                                                      **options)['delegation_id'], first['delegation_id'])
        for other in (auto(alloc.candidate('codex', model='gpt-fast')), dict(mode='explicit', provider='codex'), None):
            with self.assertRaisesRegex(ValueError, 'different Child allocation'):
                self.store.create_delegation(self.task['task_id'], allocation=other, **options)

    def test_replays_are_idempotent_and_conflicting_allocations_are_refused(self):
        self.authorize()
        value = request('a', auto(alloc.candidate('codex', model='gpt-deep')))
        self.accept(value)
        process = manager.agents[self.parent]
        context = process._control_center_execution
        receipt = self.store.accept_delegation_message(context.origin, message(value), session_id=context.events.session_id,
                                                       message_id=context.events.message_id)
        self.assertEqual(receipt['requests'], [value])
        with self.assertRaisesRegex(ValueError, 'Conflicting replay'):
            self.store.accept_delegation_message(context.origin, message(request('a', auto(alloc.candidate('codex', model='gpt-fast')))),
                                                 session_id=context.events.session_id, message_id=context.events.message_id)
        self.assertEqual(len(self.store.list_delegations(self.task['task_id'])), 1)

    def revoke_before_final_admission(self, change):
        original = manager._spawn_with_resources

        def spawn(*args, **kwargs):
            change()
            return original(*args, **kwargs)
        return patch.object(manager, '_spawn_with_resources', side_effect=spawn)

    def test_policy_changes_before_launch_are_refused_without_a_process(self):
        changes = {
            'disabled': lambda: manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[]),
            'provider': lambda: self.authorize(allowed=('mock',)),
            'model': lambda: self.authorize(allowed=('codex',), models={'codex': ['gpt-fast']}),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                self.doCleanups()
                self.setUp()
                self.authorize()
                self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep'))))
                before = len(self.commands)
                with self.revoke_before_final_admission(change):
                    manager.materialize_delegations(self.task['task_id'])
                self.assertEqual(len(self.commands), before)  # No process.
                with self.store._connection() as db:
                    progress = db.execute('SELECT phase,last_error FROM delegation_materializations').fetchone()
                self.assertIn(progress['phase'], ('attached', 'workspace_ready'))
                self.assertIn('AllocationRevoked', progress['last_error'])
                self.assertEqual(self.record()['child_provider'], 'codex')  # Nothing substituted at the boundary.
                self.assertEqual(len(self.children()), 1)

    def test_ancestor_policy_and_recovery_after_refusal(self):
        ancestor = self.store.create_task('Ancestor', project_id=self.project['project_id'])
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (ancestor['task_id'], self.task['task_id']))
            db.commit()
        self.authorize(task_id=ancestor['task_id'])
        self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep'))))
        before = len(self.commands)
        with self.revoke_before_final_admission(
                lambda: manager.set_auto_allocation_policy(ancestor['task_id'], enabled=False, allowed=[])):
            manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before)
        self.authorize(task_id=ancestor['task_id'])
        manager.materialize_delegations(self.task['task_id'])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), before + 1)
        self.assertEqual(len(self.children()), 1)

    def test_started_child_is_unaffected_and_restart_does_not_duplicate(self):
        self.authorize()
        self.accept(request('a', auto(alloc.candidate('codex', model='gpt-deep'))))
        manager.materialize_delegations(self.task['task_id'])
        count = len(self.commands)
        manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), count)
        self.assertEqual(len(self.decisions()), 1)
        self.assertEqual(self.record()['child_model'], 'gpt-deep')
        restarted = AgentStore(self.store.path)
        with restarted._connection() as db:
            self.assertEqual(allocation.current(db, self.record()['delegation_id'])['model'], 'gpt-deep')
        self.assertEqual(len(self.children()), 1)

    def test_explicit_v3_is_advertised_without_auto_and_auto_only_when_authorized(self):
        with self.store._connection() as db:
            text = delegation_v3.instruction(db, self.task['task_id'])
        self.assertIn(delegation_v3.PROTOCOL, text)
        self.assertIn('"mode":"explicit"', text)
        self.assertNotIn('"mode":"auto"', text)  # Never an invitation without the user's policy.
        self.authorize()
        with self.store._connection() as db:
            self.assertIn('"mode":"auto"', delegation_v3.instruction(db, self.task['task_id']))
        manager.set_auto_allocation_policy(self.task['task_id'], enabled=False, allowed=[])
        enabled = manager.start_agent('Parent work', 'codex', 'read-only', task_id=self.task['task_id'],
                                      delegation_protocol_enabled=True, child_materialization_enabled=True)
        prompt = manager._execution_prompt(enabled, 'x', manager.agents[enabled])
        self.assertIn(delegation_v3.PROTOCOL, prompt)
        self.assertNotIn('"mode":"auto"', prompt)
        manager.stop_agent(enabled)
        other = self.store.create_task('Other', project_id=self.project['project_id'])
        plain = manager.start_agent('Other', 'codex', 'read-only', task_id=other['task_id'])
        self.assertNotIn(delegation_v3.PROTOCOL, manager._execution_prompt(plain, 'x', manager.agents[plain]))

    def test_explicit_v3_is_accepted_without_any_auto_policy(self):
        self.accept(request('a', dict(mode='explicit', provider='codex', model='gpt-fast', effort='low')),
                    request('b', auto(alloc.candidate('codex', model='gpt-deep'))))
        manager.materialize_delegations(self.task['task_id'])
        self.assertIn(('gpt-fast', 'low'), self.launched)
        self.assertIn('not authorized', self.record('b')['admission_error'])  # Unauthorized Auto stays held.

    def test_v1_unchanged_and_v3_continuation_requires_hybrid(self):
        self.authorize()
        self.parent = manager.start_agent('Parent work', 'codex', 'read-only', task_id=self.task['task_id'],
                                          delegation_protocol_enabled=True, child_materialization_enabled=True)
        self.read(self.parent, manager.agents[self.parent], protocol_tests.envelope('legacy'))
        self.finish(self.parent, manager.agents[self.parent])
        manager.materialize_delegations(self.task['task_id'])
        self.assertIsNone(self.record('legacy')['child_provider'])
        self.assertEqual(self.decisions(), [])  # An Auto policy never applies to v1 requests.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            agent = db.execute('SELECT * FROM agents WHERE agent_id=?', (self.parent,)).fetchone()
        self.assertFalse(agent['hybrid_delegation_enabled'])


if __name__ == '__main__':
    unittest.main()
