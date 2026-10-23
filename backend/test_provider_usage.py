"""Stage 2F corrections: billing routes, persistence-failure blocks, usage windows and manual resume.

Only disposable fake CLIs and databases are used. No model is called and no
billable usage check is made.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from app import agent_manager as manager, providers
from app.persistence import AgentStore
import provider_test_support as fake
import test_providers as base
from test_providers import until


def iso(delta_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat(timespec='seconds').replace('+00:00', 'Z')


def window(window_id, status, **extra):
    return dict(window_id=window_id, label=extra.pop('label', window_id), status=status, **extra)


class CodexRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'codex'
        self.home.mkdir()
        self.enterContext(patch.dict(os.environ, {'CODEX_HOME': str(self.home)}))

    def config(self, text, path=None):
        (path or self.home / 'config.toml').write_text(text, encoding='utf-8')

    def test_default_configuration_routes_to_the_built_in_openai_provider(self):
        self.assertEqual(providers.CODEX.route_problems(), [])
        self.config('model = "gpt-5"\nmodel_provider = "openai"\n')
        self.assertEqual(providers.CODEX.route_problems(), [])

    def test_chatgpt_login_with_a_custom_provider_or_endpoint_is_refused(self):
        for text, fragment in (('model_provider = "azure"\n[model_providers.azure]\nbase_url = "https://x"\n', 'model_provider'),
                               ('openai_base_url = "https://proxy.example"\n', 'openai_base_url'),
                               ('chatgpt_base_url = "https://proxy.example"\n', 'chatgpt_base_url'),
                               ('forced_login_method = "api"\n', 'login method'),
                               ('profile = "work"\n[profiles.work]\nmodel_provider = "custom"\n', 'default profile'),
                               ('model_provider = [unparseable', 'parsed')):
            with self.subTest(fragment=fragment):
                self.config(text)
                problems = providers.CODEX.route_problems()
                self.assertTrue(problems and any(fragment in p for p in problems), problems)

    def test_workspace_project_files_are_checked_conservatively(self):
        workspace = Path(self.temp.name) / 'repo' / 'sub'
        (workspace / '.codex').mkdir(parents=True)
        (workspace.parent / '.git').mkdir()
        self.config('openai_base_url = "https://proxy.example"\n', workspace / '.codex' / 'config.toml')
        self.assertEqual(providers.CODEX.route_problems(), [])
        self.assertTrue(providers.CODEX.route_problems(workspace))

    def test_the_command_pins_the_built_in_provider(self):
        with patch.object(providers.CodexAdapter, 'locate', return_value=Path(r'C:\npm\codex.cmd')), \
             patch.dict(os.environ, {'SystemRoot': r'C:\Windows'}), patch.object(providers.os, 'name', 'nt'):
            self.assertIn(' exec -c model_provider=openai ', providers.CODEX.command())
        self.assertIn('OPENAI_BASE_URL', providers.CODEX.scrubbed_env)


class GeminiLayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home, self.system = root / 'home', root / 'programdata'
        self.workspace = root / 'workspace'
        (self.workspace / '.git').mkdir(parents=True)
        self.enterContext(patch.dict(os.environ, {'GEMINI_CLI_HOME': str(self.home), 'USERPROFILE': str(self.home),
                                                  'ProgramData': str(self.system)}))
        fake.gemini_settings(self.home)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_workspace_settings_override_user_settings(self):
        self.assertIsNone(providers.GEMINI.route_refusal(None))
        self.write(self.workspace / '.gemini' / 'settings.json', {'security': {'auth': {'selectedType': 'gemini-api-key'}}})
        self.assertIsNone(providers.GEMINI.route_refusal(None))  # Another workspace's result is not reused.
        state, reason = providers.GEMINI.route_refusal(self.workspace)
        self.assertEqual(state, providers.BILLING_BLOCKED)
        self.assertIn('project settings select gemini-api-key', reason)
        self.write(self.workspace / '.gemini' / 'settings.json', {'billing': {'overageStrategy': 'always'}})
        self.assertIn('always', providers.GEMINI.route_refusal(self.workspace)[1])

    def test_system_settings_rank_above_project_and_user(self):
        self.write(self.system / 'gemini-cli' / 'settings.json', {'security': {'auth': {'selectedType': 'vertex-ai'}}})
        self.assertIn('system settings select vertex-ai', providers.GEMINI.route_refusal(None)[1])
        self.write(self.system / 'gemini-cli' / 'system-defaults.json', {'billing': {'overageStrategy': 'always'}})
        (self.system / 'gemini-cli' / 'settings.json').unlink()
        self.assertIsNone(providers.GEMINI.route_refusal(None))  # Defaults rank below the user's "never".

    def test_unknown_and_unapproved_authentication_types_are_refused(self):
        for value, fragment in (('something-new', 'unknown authentication type'), ('cloud-shell', 'not an approved'),
                                ('gateway', 'not an approved')):
            self.write(self.home / '.gemini' / 'settings.json',
                       {'security': {'auth': {'selectedType': value}}, 'billing': {'overageStrategy': 'never'}})
            self.assertIn(fragment, providers.GEMINI.route_refusal(None)[1])
        (self.home / '.gemini' / 'settings.json').write_text('{not json', encoding='utf-8')
        self.assertIn('could not be read', providers.GEMINI.route_refusal(None)[1])


class UsageModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'usage.sqlite3')
        providers.invalidate()
        self.addCleanup(providers.invalidate)
        self.addCleanup(lambda: providers._unpersisted.clear())

    def record(self, *windows, source='claude_usage_command'):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.record_windows(db, 'claude', list(windows), source=source)

    def limits(self, model=None):
        with self.store._connection() as db:
            return providers.constraints(db, 'claude', model)

    def test_five_hour_and_weekly_windows_are_independent(self):
        session = dict(label='Session limit', kind='session', group_name='session', used_percent=100.0, reset_at=iso(3600))
        weekly = dict(label='Weekly limit (all models)', kind='weekly_all', group_name='weekly', used_percent=45.0, reset_at=iso(86400))
        cases = {'A': ('exhausted', 'available', ['session']), 'B': ('available', 'exhausted', ['weekly_all']),
                 'C': ('exhausted', 'exhausted', ['session', 'weekly_all'])}
        for case, (first, second, expected) in cases.items():
            with self.subTest(case=case):
                self.record(window('session', first, **{**session, 'used_percent': 100.0 if first == 'exhausted' else 20.0}),
                            window('weekly_all', second, **{**weekly, 'used_percent': 100.0 if second == 'exhausted' else 45.0}))
                found = self.limits()
                self.assertEqual(sorted(c['window_id'] for c in found), expected)
                self.assertTrue(all(c['hard'] for c in found))  # Fresh and reset in the future: no bypass.
        # Case E: one window refreshed, the other still exhausted: no resume.
        self.record(window('session', 'available', **{**session, 'used_percent': 10.0}))
        self.assertEqual([c['window_id'] for c in self.limits()], ['weekly_all'])

    def test_unknown_stale_and_missing_reset(self):
        stale = iso(-3 * providers.USAGE_FRESH_SECONDS)
        self.record(window('session', 'exhausted', used_percent=100.0, observed_at=stale))  # No reset reported.
        limit, = self.limits()
        self.assertFalse(limit['hard'])  # Stale and no future reset: an explicit retry may try it.
        self.assertIn('reset time is unknown', providers.describe_constraints([limit]))
        self.record(window('session', 'unknown', observed_at=stale))
        with self.store._connection() as db:
            view = providers.usage_view(db, 'claude')
        self.assertEqual(view['overall'], 'unknown')
        self.assertFalse(view['windows'][0]['fresh'])
        self.assertIsNone(view['windows'][0]['used_percent'])
        self.assertIsNone(view['remaining'])

    def test_case_d_unknown_window_does_not_lift_a_confirmed_exhaustion(self):
        self.record(window('session', 'exhausted', used_percent=100.0), window('weekly_all', 'unknown'))
        self.assertEqual([c['window_id'] for c in self.limits()], ['session'])

    def test_model_specific_and_shared_limits(self):
        self.record(window('weekly_scoped', 'exhausted', scope_key='Opus', model_scope='Opus', used_percent=100.0),
                    window('weekly_all', 'available', used_percent=30.0))
        self.assertEqual(len(self.limits('opus')), 1)
        self.assertEqual(len(self.limits('claude-opus-9')), 1)
        self.assertEqual(self.limits('sonnet'), [])  # Separate, known-different scope: not blocked.
        self.assertEqual(len(self.limits(None)), 1)  # The default model's scope is uncertain: blocked.
        self.record(window('weekly_all', 'exhausted', used_percent=100.0))
        self.assertEqual(len(self.limits('sonnet')), 1)  # A shared limit applies to every model.

    def test_report_rows_are_taken_verbatim_with_provenance(self):
        report = {'rate_limits': {'limits': [
            {'kind': 'session', 'group': 'session', 'percent': 80, 'resets_at': '2026-10-23T15:00:00+00:00',
             'scope': None, 'severity': 'warning', 'is_active': True},
            {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 100, 'resets_at': None,
             'scope': {'model': {'display_name': 'Opus'}}, 'severity': 'critical', 'is_active': False}]}}
        windows = providers.windows_from_claude_report(report)
        self.assertEqual([(w['window_id'], w['used_percent'], w['status']) for w in windows],
                         [('session', 80.0, 'available'), ('weekly_scoped', 100.0, 'exhausted')])
        self.assertNotIn('remaining_percent', windows[0])  # Used is never turned into remaining.
        self.record(*windows)
        with self.store._connection() as db:
            stored = providers.usage_windows(db, 'claude')
        self.assertEqual({w['source'] for w in stored}, {'claude_usage_command'})
        self.assertEqual(stored[0]['reset_at'], '2026-10-23T15:00:00Z')
        self.assertEqual(stored[0]['reset_source'], 'provider_reported')
        self.assertIsNone(stored[1]['reset_at'])  # Never derived from "five hours" or "seven days".
        self.assertIsNone(providers.windows_from_claude_report({'rate_limits': None}))
        for bad in (150, -1, '80', True):
            with self.assertRaises(ValueError):
                providers.windows_from_claude_report({'rate_limits': {'limits': [{'kind': 'session', 'percent': bad}]}})

    def test_failed_block_persistence_stays_enforced_until_written(self):
        connect = self.store._connection
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
        with patch.object(providers, 'record_block', side_effect=sqlite3.OperationalError('disk I/O error')):
            providers.note_block(connect, 'claude', providers.QUOTA_EXHAUSTED, 'Usage limit reached')
            self.assertEqual(len(providers.unpersisted('claude')), 1)
            with self.store._connection() as db:
                db.execute('SELECT 1').fetchone()  # A successful read changes nothing.
                with self.assertRaisesRegex(providers.ProviderRefused, 'Usage limit reached'):
                    providers.admission_check(db, 'claude')
                self.assertTrue(providers.constraints(db, 'claude')[0]['hard'])
            self.assertEqual(providers.reconcile(connect), 0)
            self.assertEqual(len(providers.unpersisted('claude')), 1)
        self.assertEqual(providers.reconcile(connect), 1)  # Persistence recovered: written durably first.
        self.assertEqual(providers.unpersisted('claude'), [])
        with self.store._connection() as db:
            block, = providers.active_blocks(db, 'claude')
            self.assertEqual(block['reason'], 'Usage limit reached')
            with self.assertRaises(providers.ProviderRefused):
                providers.admission_check(db, 'claude')  # Still blocked, now by the durable row.

    def test_concurrent_admissions_during_a_failed_persistence_are_all_refused(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
        with patch.object(providers, 'record_block', side_effect=sqlite3.OperationalError('locked')):
            providers.note_block(self.store._connection, 'claude', providers.RATE_LIMITED, 'Rate limited')

            def admit(_):
                with self.store._connection() as db:
                    db.execute('BEGIN IMMEDIATE')
                    try:
                        providers.admission_check(db, 'claude')
                        return 'admitted'
                    except providers.ProviderRefused:
                        return 'refused'
            with ThreadPoolExecutor(8) as pool:
                outcomes = list(pool.map(admit, range(16)))
        self.assertEqual(set(outcomes), {'refused'})

    def test_no_billable_usage_probe_exists_for_codex_or_gemini(self):
        with patch.object(providers.subprocess, 'Popen', side_effect=AssertionError('no process')):
            for provider in ('codex', 'gemini'):
                self.assertFalse(providers.usage_view(None, provider)['refresh_supported'])
            self.assertEqual(providers.USAGE_SOURCES['codex'][0], 'exhaustion_signal')
            self.assertEqual(providers.USAGE_SOURCES['gemini'][0], 'none')


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class ManualResumeTests(unittest.TestCase):
    """Real supervised fake-CLI processes, with the fixture of test_providers (not its tests)."""
    tearDown = base.SupervisedFakeProviderTests.tearDown
    rows = base.SupervisedFakeProviderTests.rows
    wait_status = base.SupervisedFakeProviderTests.wait_status

    def setUp(self):
        # A Claude Code version whose /usage carries the documented usage_report.
        self.enterContext(patch.dict(os.environ, FAKE_CLAUDE_VERSION='2.1.280'))
        base.SupervisedFakeProviderTests.setUp(self)
        self.addCleanup(lambda: providers._unpersisted.clear())

    def limited(self, reset_in=None, task='Long work', scenario='limit'):
        env = {'FAKE_CLAUDE_SCENARIO': scenario}
        if reset_in is not None:
            env['FAKE_RESET_AT'] = str(int(time.time() + reset_in))
        with patch.dict(os.environ, env):
            agent_id = manager.start_agent(task, 'claude', 'read-only', model='sonnet')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        return agent_id

    def test_quota_exhaustion_waits_with_task_session_and_provider_reset(self):
        agent_id = self.limited(reset_in=3600)
        view = manager.get_agent(agent_id, include_output=False)
        condition = view['provider_condition']
        self.assertEqual((condition['kind'], condition['provider'], condition['model']), ('usage_limited', 'claude', 'sonnet'))
        self.assertIsNotNone(condition['reset_at'])
        self.assertTrue(condition['session_preserved'])
        self.assertTrue(condition['blocking'])
        self.assertEqual(view['status'], 'waiting')  # Never described as running.
        self.assertIsNotNone(manager._store.get_active_assignment_for_task(manager._agent_task_id(agent_id)))
        self.assertFalse(view['resume']['available'])  # The provider-reported reset is still in the future.
        with self.assertRaisesRegex(providers.ProviderRefused, 'reset'):
            manager.continue_agent(agent_id, explicit_retry=True)
        with self.assertRaises(providers.ProviderRefused):
            manager.allow_provider_work('claude')  # Known exhaustion cannot be cleared without evidence.
        with self.assertRaises(providers.ProviderRefused):
            manager.start_agent('Another task', 'claude', 'read-only')  # Account-wide for this provider.
        windows = self.rows("SELECT * FROM provider_usage_windows WHERE provider='claude'")
        self.assertEqual([(w['window_id'], w['status'], w['source']) for w in windows],
                         [('active_limit', 'exhausted', 'claude_rate_limit_event')])

    def test_no_automatic_resume_at_reset_and_explicit_retry_after_it(self):
        agent_id = self.limited(reset_in=2)
        runs = len(fake.runs(self.log, 'claude'))
        time.sleep(3)  # The provider-reported reset passes.
        self.assertEqual(manager.agent_statuses[agent_id], 'waiting')
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs)  # Nothing resumed by itself.
        view = manager.get_agent(agent_id, include_output=False)['resume']
        self.assertEqual((view['available'], view['mode']), (True, 'explicit_retry'))
        with self.assertRaisesRegex(providers.ProviderRefused, 'explicit retry'):
            manager.continue_agent(agent_id)  # A reset time is not proof of availability.
        session = manager.agent_sessions[agent_id]
        manager.continue_agent(agent_id, explicit_retry=True)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        run = fake.runs(self.log, 'claude')[-1]
        self.assertEqual(run['args'][run['args'].index('--resume') + 1], session)
        self.assertEqual(run['args'][run['args'].index('--model') + 1], 'sonnet')  # No model fallback.
        request, = self.rows('SELECT * FROM agent_resume_requests')
        self.assertEqual((request['mode'], request['outcome']), ('explicit_retry', 'admitted'))
        cleared = self.rows('SELECT clear_reason FROM provider_usage_blocks')
        self.assertTrue(cleared[0]['clear_reason'].startswith('explicit_retry:'))

    def test_resume_rejected_while_execution_ownership_is_uncertain(self):
        agent_id = self.limited(scenario='rate_limit')  # A provider error signal without a reported window.
        manager.allow_provider_work('claude')
        with patch.object(manager, '_execution_live', return_value=True):
            self.assertEqual(manager.get_agent(agent_id, include_output=False)['resume']['reason'],
                             'Previous execution not safely settled.')
            with self.assertRaisesRegex(ValueError, 'not safely settled'):
                manager.continue_agent(agent_id)

    def test_billing_route_or_acknowledgment_change_blocks_resume(self):
        agent_id = self.limited(scenario='rate_limit')  # A provider error signal without a reported window.
        manager.allow_provider_work('claude')
        with patch.dict(os.environ, FAKE_CLAUDE_AUTH='apikey'):
            with self.assertRaises(providers.ProviderRefused):
                manager.continue_agent(agent_id)
        providers.invalidate()
        manager.update_provider_settings('claude', billing_confirmed=False)
        self.assertFalse(manager.get_agent(agent_id, include_output=False)['resume']['available'])
        with self.assertRaises(providers.ProviderRefused):
            manager.continue_agent(agent_id)
        refused = self.rows("SELECT outcome FROM agent_resume_requests")
        self.assertTrue(all(r['outcome'] == 'refused' for r in refused))
        self.assertEqual(manager.agent_statuses[agent_id], 'waiting')  # Never marked resumed.

    def test_independent_tasks_on_other_providers_continue(self):
        self.limited()
        other = manager.start_agent('Independent work', 'gemini', 'read-only')
        third = manager.start_agent('More independent work', 'codex', 'read-only')
        self.assertEqual(self.wait_status(other, 'finished'), 'finished')
        self.assertEqual(self.wait_status(third, 'finished'), 'finished')

    def test_usage_refresh_is_evidence_and_never_runs_work(self):
        agent_id = self.limited()
        report = {'rate_limits': {'limits': [
            {'kind': 'session', 'group': 'session', 'percent': 100, 'resets_at': iso(7200), 'scope': None,
             'severity': 'critical', 'is_active': True},
            {'kind': 'weekly_all', 'group': 'weekly', 'percent': 45, 'resets_at': iso(86400), 'scope': None,
             'severity': 'normal', 'is_active': False}]}}
        runs = len(fake.runs(self.log, 'claude'))
        with patch.dict(os.environ, FAKE_CLAUDE_USAGE=json.dumps(report)):
            status = manager.refresh_provider_usage('claude')
        self.assertTrue(status['reported'])
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs)  # A refresh starts no execution.
        self.assertEqual({w['window_id']: w['status'] for w in status['usage']['windows']},
                         {'active_limit': 'exhausted', 'session': 'exhausted', 'weekly_all': 'available'})
        self.assertFalse(manager.get_agent(agent_id, include_output=False)['resume']['available'])
        report['rate_limits']['limits'][0]['percent'] = 12
        with patch.dict(os.environ, FAKE_CLAUDE_USAGE=json.dumps(report)):
            status = manager.refresh_provider_usage('claude')
        windows = {w['window_id']: w['status'] for w in status['usage']['windows']}
        self.assertEqual((windows['session'], windows['weekly_all']), ('available', 'available'))
        # Finding 1: the run's limit was not identified with any reported meter, so a
        # report showing capacity elsewhere cannot clear it.
        self.assertEqual(windows['active_limit'], 'exhausted')
        self.assertEqual(status['usage']['overall'], 'limited')
        self.assertEqual(manager.agent_statuses[agent_id], 'waiting')  # Refresh is not permission to run.
        self.assertFalse(manager.get_agent(agent_id, include_output=False)['resume']['available'])

    def test_partial_usage_report_never_clears_omitted_or_unrelated_limits(self):
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            weekly = providers.record_block(db, 'claude', providers.QUOTA_EXHAUSTED, 'Weekly limit', window_id='weekly_all')
            generic = providers.record_block(db, 'claude', providers.RATE_LIMITED, 'Rate limited (no window)')
            providers.record_windows(db, 'claude', [window('weekly_all', 'exhausted', used_percent=100.0)],
                                     source='claude_usage_command')
        partial = {'rate_limits': {'limits': [{'kind': 'session', 'group': 'session', 'percent': 10, 'resets_at': None,
                                               'scope': None, 'severity': 'normal', 'is_active': True}]}}
        with patch.object(providers.CLAUDE, 'usage_report', return_value=partial):
            manager.refresh_provider_usage('claude')
        active = {b['block_id'] for b in self.rows('SELECT block_id FROM provider_usage_blocks WHERE cleared_at IS NULL')}
        self.assertEqual(active, {weekly, generic})  # The omitted weekly window is not "available".
        windows = {w['window_id']: w['status'] for w in self.rows('SELECT window_id,status FROM provider_usage_windows')}
        self.assertEqual(windows['weekly_all'], 'exhausted')
        complete = {'rate_limits': {'limits': partial['rate_limits']['limits'] + [
            {'kind': 'weekly_all', 'group': 'weekly', 'percent': 40, 'resets_at': None, 'scope': None,
             'severity': 'normal', 'is_active': False}]}}
        with patch.object(providers.CLAUDE, 'usage_report', return_value=complete):
            manager.refresh_provider_usage('claude')
        active = {b['block_id'] for b in self.rows('SELECT block_id FROM provider_usage_blocks WHERE cleared_at IS NULL')}
        self.assertEqual(active, {generic})  # Positively covered: cleared; unidentified: kept.

    def test_usage_refresh_requires_a_compatible_claude_code_version(self):
        with patch.dict(os.environ, FAKE_CLAUDE_VERSION='2.1.198'):
            providers.invalidate()
            with self.assertRaisesRegex(providers.ProviderRefused, '2.1.273'):
                manager.refresh_provider_usage('claude')
        self.assertEqual([e for e in fake.read_log(self.log) if e['kind'] == 'usage'], [])  # Never sent.

    def test_a_usage_command_without_a_report_disables_refresh(self):
        providers.CLAUDE.usage_refresh_disabled_for = None
        self.addCleanup(setattr, providers.CLAUDE, 'usage_refresh_disabled_for', None)
        with self.assertRaisesRegex(providers.ProviderRefused, 'did not return a usage report'):
            manager.refresh_provider_usage('claude')
        with self.assertRaisesRegex(providers.ProviderRefused, 'disabled'):
            manager.refresh_provider_usage('claude')
        self.assertEqual(len([e for e in fake.read_log(self.log) if e['kind'] == 'usage']), 1)
        with self.assertRaisesRegex(providers.ProviderRefused, 'No usage refresh'):
            manager.refresh_provider_usage('codex')

    def test_codex_needs_the_acknowledgment_and_a_safe_route(self):
        manager.update_provider_settings('codex', billing_confirmed=False)
        with self.assertRaisesRegex(providers.ProviderRefused, 'acknowledgment'):
            manager.start_agent('Work', 'codex', 'read-only')
        manager.update_provider_settings('codex', billing_confirmed=True)
        status = next(p for p in manager.provider_statuses() if p['provider'] == 'codex')
        self.assertIn('credits', status['confirmation_text'])
        self.assertTrue(status['settings']['billing_confirmed'])
        codex_home = Path(os.environ['CODEX_HOME'])
        codex_home.mkdir(parents=True, exist_ok=True)
        (codex_home / 'config.toml').write_text('model_provider = "custom"\n', encoding='utf-8')
        providers.invalidate()
        with self.assertRaisesRegex(providers.ProviderRefused, 'model route'):
            manager.start_agent('Work', 'codex', 'read-only')  # ChatGPT login, custom provider.
        (codex_home / 'config.toml').unlink()
        agent_id = manager.start_agent('Work', 'codex', 'read-only')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertIn('model_provider=openai', fake.runs(self.log, 'codex')[-1]['args'])

    def test_configuration_changes_before_admission_are_refused(self):
        real = manager._execution_workspace
        codex_home = Path(os.environ['CODEX_HOME'])

        def change_codex(*args, **kwargs):
            codex_home.mkdir(parents=True, exist_ok=True)
            (codex_home / 'config.toml').write_text('openai_base_url = "https://proxy.example"\n', encoding='utf-8')
            return real(*args, **kwargs)
        with patch.object(manager, '_execution_workspace', side_effect=change_codex):
            with self.assertRaisesRegex(providers.ProviderRefused, 'configuration changed'):
                manager.start_agent('Work', 'codex', 'read-only')
        (codex_home / 'config.toml').unlink()

        def change_gemini_workspace(*args, **kwargs):
            workspace = real(*args, **kwargs)
            (Path(workspace) / '.gemini').mkdir(exist_ok=True)
            (Path(workspace) / '.gemini' / 'settings.json').write_text(
                json.dumps({'security': {'auth': {'selectedType': 'gemini-api-key'}}}), encoding='utf-8')
            return workspace
        with patch.object(manager, '_execution_workspace', side_effect=change_gemini_workspace):
            with self.assertRaisesRegex(providers.ProviderRefused, 'execution workspace'):
                manager.start_agent('Work', 'gemini', 'read-only')
        self.assertEqual([r['state'] for r in self.rows('SELECT state FROM executions')], ['launch_aborted', 'launch_aborted'])
        self.assertEqual(fake.runs(self.log), [])

    def test_failed_block_persistence_during_a_run_blocks_concurrent_launches(self):
        with patch.object(providers, 'record_block', side_effect=sqlite3.OperationalError('disk I/O error')):
            agent_id = self.limited()
            self.assertEqual(len(providers.unpersisted('claude')), 1)
            with ThreadPoolExecutor(4) as pool:
                outcomes = list(pool.map(lambda i: self._try_start(f'Concurrent {i}'), range(4)))
            self.assertEqual(set(outcomes), {'refused'})
            with self.assertRaises(providers.ProviderRefused):
                manager.continue_agent(agent_id, explicit_retry=True)  # Not durable yet: never overridden.
        manager.provider_statuses()  # Persistence available again: reconciled durably.
        self.assertEqual(providers.unpersisted('claude'), [])
        self.assertEqual(len(self.rows('SELECT * FROM provider_usage_blocks WHERE cleared_at IS NULL')), 1)
        with self.assertRaises(providers.ProviderRefused):
            manager.start_agent('After recovery', 'claude', 'read-only')  # Not unblocked by the recovery.

    def _try_start(self, task):
        try:
            manager.start_agent(task, 'claude', 'read-only')
            return 'started'
        except providers.ProviderRefused:
            return 'refused'


if __name__ == '__main__':
    unittest.main()
