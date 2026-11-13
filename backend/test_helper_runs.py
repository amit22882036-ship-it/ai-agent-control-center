"""Helpers of a lead: their model, a provider failure's reason, and delivery after every helper ended.

Synthetic provider events and fake processes only (mock CLI); no model runs."""
import io
import json
import sqlite3
import unittest
from uuid import uuid4

from app import agent_manager as manager, codex_events, delegation_orchestration, model_catalog, providers
import test_orchestration_decision as decisions

# The exact events a helper received in a real run (Codex with the CLI's global default model).
UNSUPPORTED = ("{\"type\":\"error\",\"status\":400,\"error\":{\"type\":\"invalid_request_error\",\"message\":\"The "
               "'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.\",\"code\":\"model_not_found\"}}")
FAILED_TURN = [
    dict(type='item.completed', item=dict(id='item_0', type='error', message='Model metadata for `gpt-6.1-sol` not found.')),
    dict(type='error', message=UNSUPPORTED),
    dict(type='turn.failed', error=dict(message=UNSUPPORTED)),
]


class HelperModelTests(unittest.TestCase):
    LEAD = {'agent_type': 'codex', 'model': 'gpt-5.6-sol', 'effort': 'high'}

    def test_a_helper_of_the_leads_provider_inherits_its_model_and_effort(self):
        self.assertEqual(manager._helper_model({}, self.LEAD, 'codex'), ('gpt-5.6-sol', 'high', 'inherited from the lead'))
        # Naming the lead's provider without a model (or after an unknown model was dropped) inherits too.
        self.assertEqual(manager._helper_model({'child_provider': 'codex'}, self.LEAD, 'codex'),
                         ('gpt-5.6-sol', 'high', 'inherited from the lead'))
        self.assertEqual(manager._helper_model({'child_provider': 'codex', 'child_effort': 'low'}, self.LEAD, 'codex'),
                         ('gpt-5.6-sol', 'low', 'inherited from the lead'))

    def test_a_requested_model_wins_and_another_provider_uses_its_default(self):
        self.assertEqual(manager._helper_model({'child_provider': 'codex', 'child_model': 'gpt-5.6-terra',
                                                'child_effort': 'medium'}, self.LEAD, 'codex'),
                         ('gpt-5.6-terra', 'medium', 'requested by the lead'))
        self.assertEqual(manager._helper_model({'child_provider': 'claude'}, self.LEAD, 'claude'),
                         (None, None, 'Claude Code default model'))

    def test_a_lead_on_the_provider_default_leaves_the_helper_on_it(self):
        self.assertEqual(manager._helper_model({}, {'agent_type': 'codex', 'model': None, 'effort': None}, 'codex'),
                         (None, None, 'Codex default model'))


class FailureTextTests(unittest.TestCase):
    def test_the_providers_own_reason_is_unwrapped(self):
        self.assertEqual(codex_events.failure_text(FAILED_TURN[2]),
                         "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account.")
        self.assertEqual(codex_events.failure_text({'type': 'error', 'message': 'plain text'}), 'plain text')
        self.assertIsNone(codex_events.failure_text({'type': 'error'}))

    def test_the_adapter_keeps_the_first_reason(self):
        events = codex_events.CodexEvents()
        for event in [dict(type='thread.started', thread_id=str(uuid4())), dict(type='turn.started'), *FAILED_TURN]:
            events.feed(json.dumps(event))
        self.assertEqual(events.error, 'Codex turn failed')
        self.assertIn("'gpt-6.1-sol' model is not supported", events.failure_detail)


class FailureVisibilityTests(unittest.TestCase):
    setUp = decisions.DecisionTests.setUp
    patch = decisions.DecisionTests.patch
    launch = decisions.DecisionTests.launch

    def test_a_failed_turn_records_the_providers_reason_for_the_task(self):
        key, process = self.launch()
        lines = [dict(type='thread.started', thread_id=str(uuid4())), dict(type='turn.started'), *FAILED_TURN]
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in lines))
        process.stderr = io.StringIO('')
        manager._read_output(key, process)
        process.poll.return_value = 1
        manager._finalize_process(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        rejection, = self.store.control_rejections(self.task['task_id'])
        self.assertEqual(rejection['reason'], "Provider error: The 'gpt-6.1-sol' model is not supported when using "
                                              'Codex with a ChatGPT account.')
        self.assertIn("--- Provider error: The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT "
                      'account. ---', self.store.full_output(key))


class DeliveryGateTests(unittest.TestCase):
    """children_may_run decides whether the parallel-scope gate applies to a Parent continuation.
    The query is checked against the columns it reads, in an in-memory database."""

    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE tasks (task_id TEXT PRIMARY KEY, status TEXT);
            CREATE TABLE delegations (delegation_id TEXT PRIMARY KEY, parent_task_id TEXT, child_task_id TEXT, status TEXT);
            CREATE TABLE agents (agent_id TEXT PRIMARY KEY, status TEXT);
            INSERT INTO agents VALUES ('helper', 'stopped');
            CREATE TABLE task_assignments (assignment_id TEXT PRIMARY KEY, task_id TEXT, agent_id TEXT, ended_at TEXT);
            CREATE TABLE delegation_materializations (delegation_id TEXT, phase TEXT);
            CREATE TABLE delegation_results (result_id TEXT, delegation_id TEXT, child_task_id TEXT);
            INSERT INTO tasks VALUES ('lead', 'blocked');''')
        self.addCleanup(self.db.close)

    def child(self, status='pending', *, assignment=None, result=False, phase='started', materialized=True, delegation='materialized'):
        child, delegation_id = str(uuid4()), str(uuid4())
        self.db.execute('INSERT INTO tasks VALUES (?,?)', (child, status))
        self.db.execute('INSERT INTO delegations VALUES (?,?,?,?)', (delegation_id, 'lead', child if materialized else None, delegation))
        if materialized:
            self.db.execute('INSERT INTO delegation_materializations VALUES (?,?)', (delegation_id, phase))
        if assignment:
            self.db.execute('INSERT INTO task_assignments VALUES (?,?,?,?)', (str(uuid4()), child, 'helper',
                                                                            None if assignment == 'active' else '2026-11-13'))
        if result:
            self.db.execute('INSERT INTO delegation_results VALUES (?,?,?)', (str(uuid4()), delegation_id, child))

    def may_run(self):
        return delegation_orchestration.children_may_run(self.db, 'lead')

    def test_helpers_that_all_ended_with_results_let_the_lead_run_alone(self):
        # The real run: four helpers each stopped with a recorded result (failures included).
        for _ in range(4):
            self.child(assignment='ended', result=True)
        self.assertFalse(self.may_run())

    def test_a_working_helper_keeps_the_gate(self):
        self.child(assignment='ended', result=True)
        self.child(status='in_progress', assignment='active')
        self.assertTrue(self.may_run())

    def test_a_helper_not_started_yet_or_still_launching_keeps_the_gate(self):
        self.child(assignment='ended', result=True)
        self.assertFalse(self.may_run())
        self.child()  # Materialized, never started: no result yet.
        self.assertTrue(self.may_run())

    def test_launching_or_unmaterialized_requests_keep_the_gate(self):
        self.child(assignment='ended', result=True, phase='launching')
        self.assertTrue(self.may_run())
        other = DeliveryGateTests('test_launching_or_unmaterialized_requests_keep_the_gate')
        other.setUp()
        other.child(materialized=False, delegation='requested')
        self.assertTrue(other.may_run())

    def test_finished_accepted_or_canceled_helpers_never_hold_the_lead(self):
        self.child(status='completed', assignment='ended', result=True)
        self.child(status='canceled')
        self.child(delegation='acknowledged')
        self.assertFalse(self.may_run())


class ConfiguredDefaultTests(unittest.TestCase):
    """Codex's config.toml default may come from a newer Codex (the Codex app) than the CLI the Control
    Center runs: such a default is reported as not listed and never used for a helper."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / 'config.toml'
        patcher = patch.object(providers.CODEX, 'config_path', return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_configured_model_is_read_without_changing_anything(self):
        self.assertIsNone(providers.CODEX.configured_model())
        self.config.write_text('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n', encoding='utf-8')
        self.assertEqual(providers.CODEX.configured_model(), 'gpt-6.1-sol')
        self.assertEqual(self.config.read_text(encoding='utf-8'), 'model = "gpt-6.1-sol"\nmodel_reasoning_effort = "high"\n')
        self.config.write_text('not toml [', encoding='utf-8')
        self.assertIsNone(providers.CODEX.configured_model())

    def test_an_unlisted_default_is_reported_and_replaced_for_helpers(self):
        from unittest.mock import patch
        self.config.write_text('model = "gpt-6.1-sol"\n', encoding='utf-8')
        listed = {'gpt-6-astra': {'listing': 'listed'}, 'gpt-5.6-sol': {'listing': 'listed'}}
        with patch.object(model_catalog, 'current_scope', return_value={'scope_key': 'k'}), \
                patch.object(model_catalog, 'lookup', side_effect=lambda db, provider, model, scope=None: listed.get(model)), \
                patch.object(model_catalog, '_entries', return_value=[
                    {'model_id': 'gpt-6-astra', 'listing': 'listed', 'removed_at': None},
                    {'model_id': 'gpt-5.6-sol', 'listing': 'listed', 'removed_at': None}]):
            self.assertEqual(model_catalog.configured_default(None, 'codex'),
                             {'model': 'gpt-6.1-sol', 'listed': False, 'substitute': 'gpt-5.6-sol'})
            self.assertIsNone(model_catalog.configured_default(None, 'claude'))
            with patch.object(manager, '_store') as store:
                store._connection.return_value.__enter__.return_value = None
                model, why = manager._offered_default('codex', 'Codex default model')
            self.assertEqual(model, 'gpt-5.6-sol')
            self.assertIn("configured default gpt-6.1-sol is not offered by the installed Codex CLI; using gpt-5.6-sol", why)
            self.config.write_text('model = "gpt-5.6-sol"\n', encoding='utf-8')
            self.assertEqual(model_catalog.configured_default(None, 'codex'),
                             {'model': 'gpt-5.6-sol', 'listed': True, 'substitute': None})
            with patch.object(manager, '_store') as store:
                store._connection.return_value.__enter__.return_value = None
                self.assertEqual(manager._offered_default('codex', 'Codex default model'), (None, 'Codex default model'))



class LeadDefaultFallbackTests(unittest.TestCase):
    """A lead started on "Provider default" whose default the installed CLI does not offer runs on the CLI's
    first listed model and says so in its output; a chosen model or an offered default is left alone."""
    REASON = "Codex's configured default gpt-6.1-sol is not offered by the installed Codex CLI; using gpt-5.6-sol"

    def test_only_an_unoffered_default_is_replaced(self):
        from unittest.mock import patch
        with patch.object(manager, '_offered_default', return_value=('gpt-5.6-sol', self.REASON)) as offered:
            self.assertEqual(manager._unoffered_default_fallback('codex', None, None), ('gpt-5.6-sol', self.REASON))
            self.assertEqual(manager._unoffered_default_fallback('codex', 'gpt-5.6-sol', None), ('gpt-5.6-sol', None))
            # A helper's model is resolved at its launch, never here.
            self.assertEqual(manager._unoffered_default_fallback('codex', None, ('id', {})), (None, None))
        offered.assert_called_once_with('codex', None)
        with patch.object(manager, '_offered_default', return_value=(None, None)):
            self.assertEqual(manager._unoffered_default_fallback('codex', None, None), (None, None))

    def test_the_lead_starts_on_the_substitute_and_says_why(self):
        from unittest.mock import patch
        seen = {}

        def fake_start(task, agent_type, sandbox, parent_id, **options):
            seen.update(options)
            manager.agent_outputs['lead-x'] = []
            return 'lead-x'
        self.addCleanup(manager.agent_outputs.pop, 'lead-x', None)
        with patch.object(manager, '_offered_default', return_value=('gpt-5.6-sol', self.REASON)),                 patch.object(manager, '_start_agent', side_effect=fake_start):
            self.assertEqual(manager.start_agent('t', 'codex', 'read-only', effort='low'), 'lead-x')
        self.assertEqual(seen['model'], 'gpt-5.6-sol')
        self.assertEqual(manager.agent_outputs['lead-x'], [f'--- Model: gpt-5.6-sol, low effort ({self.REASON}) ---'])



class ClosestListedTests(unittest.TestCase):
    """The substitute for an unoffered default: the newest listed model of the same tier, else the first listed."""

    def closest(self, model, listed):
        from unittest.mock import patch
        entries = [{'model_id': m, 'listing': 'listed', 'removed_at': None} for m in listed]
        entries.append({'model_id': 'gpt-9-sol', 'listing': 'hidden', 'removed_at': None})
        entries.append({'model_id': 'gpt-8-sol', 'listing': 'listed', 'removed_at': '2026-11-01'})
        with patch.object(model_catalog, 'current_scope', return_value={'scope_key': 'k'}),                 patch.object(model_catalog, '_entries', return_value=entries):
            return model_catalog.closest_listed(None, 'codex', model)

    def test_the_same_tier_newest_version_wins(self):
        listed = ['gpt-6-astra', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'gpt-5.4-sol']
        self.assertEqual(self.closest('gpt-6.1-sol', listed), 'gpt-5.6-sol')
        self.assertEqual(self.closest('gpt-6.1-terra', listed), 'gpt-5.6-terra')
        self.assertEqual(self.closest('GPT-6-Luna', listed), 'gpt-5.6-luna')

    def test_without_a_same_tier_model_the_first_listed_is_used(self):
        self.assertEqual(self.closest('gpt-6-nova', ['gpt-6-astra', 'gpt-5.6-sol']), 'gpt-6-astra')
        self.assertEqual(self.closest('o9', ['gpt-6-astra']), 'gpt-6-astra')
        self.assertIsNone(self.closest('gpt-6.1-sol', []))



class StartupWakeTests(unittest.TestCase):
    def test_startup_wakes_only_idle_leads_for_one_delivery_pass(self):
        from unittest.mock import MagicMock, patch
        store = MagicMock()
        with patch.object(manager, '_store', store), patch.object(manager, '_shutting_down', False),                 patch.object(delegation_orchestration, 'idle_parents', return_value=['lead-a', 'lead-b']),                 patch.object(manager, 'wake_orchestration') as wake:
            self.assertEqual(manager.wake_idle_parents(), ['lead-a', 'lead-b'])
        self.assertEqual([c.args for c in wake.call_args_list], [('lead-a', ('advance',)), ('lead-b', ('advance',))])
        self.assertTrue(all(c.kwargs == {'reason': 'startup'} for c in wake.call_args_list))
        with patch.object(manager, '_store', None):
            self.assertEqual(manager.wake_idle_parents(), [])


if __name__ == '__main__':
    unittest.main()
