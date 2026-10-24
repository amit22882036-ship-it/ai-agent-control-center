"""Stage 2F: provider contract, readiness, policy, parsers and real supervised fake-CLI runs.

Only disposable fake CLIs run here (provider_test_support). No real provider is
installed, signed in, configured or called; no model request is made.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager
from app import delegation_safety, managed_orchestration, promotion, provider_events, providers, responsibility
from app.persistence import AgentStore
import provider_test_support as fake
from workspace_test_support import make_repository, worker_output


def until(condition, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def stream(parser, events):
    output = []
    for event in events:
        output += parser.feed(json.dumps(event))
    return output


SESSION = str(uuid4())


class AdapterContractTests(unittest.TestCase):
    def test_capabilities_are_explicit_and_core_is_universal(self):
        for provider in providers.IDS:
            item = providers.adapter(provider)
            for name in providers.CORE_CAPABILITIES:
                self.assertTrue(item.supports(name), (provider, name))
            for name in providers.ADVANCED_CAPABILITIES:
                reason = item.capability(name)
                self.assertTrue(reason is None or (isinstance(reason, str) and reason), (provider, name))
        self.assertTrue(providers.managed_capable('codex'))
        self.assertTrue(providers.managed_capable('claude'))
        self.assertFalse(providers.managed_capable('gemini'))
        self.assertIn('sub-agents', providers.GEMINI.capability('delegation'))
        self.assertFalse(providers.managed_capable('mock'))
        with self.assertRaisesRegex(ValueError, 'Unknown agent type'):
            providers.adapter('antigravity')

    def test_claude_access_mapping_never_widens_and_uses_only_listed_flags(self):
        read = providers.CLAUDE.sandbox_arguments('read-only')
        write = providers.CLAUDE.sandbox_arguments('workspace-write', managed=True)
        self.assertEqual(read[read.index('--permission-mode') + 1], 'dontAsk')
        self.assertEqual(write[write.index('--permission-mode') + 1], 'acceptEdits')
        for arguments in (read, write):
            denied = arguments[arguments.index('--disallowedTools') + 1].split(',')
            self.assertTrue({'Bash', 'PowerShell', 'WebFetch', 'WebSearch'} <= set(denied))
            self.assertEqual(arguments[:4], ['--setting-sources', 'user', '--strict-mcp-config', '--permission-mode'])
            for flag in arguments:
                if flag.startswith('--'):
                    self.assertIn(flag, providers.CLAUDE_REQUIRED_HELP)
        self.assertTrue({'Edit', 'Write', 'NotebookEdit'} <= set(read[read.index('--disallowedTools') + 1].split(',')))
        self.assertTrue({'Agent', 'Task'} <= set(write[write.index('--disallowedTools') + 1].split(',')))
        with self.assertRaises(ValueError):
            providers.CLAUDE.sandbox_arguments('danger-full-access')

    def test_gemini_access_mapping_and_managed_refusal(self):
        self.assertEqual(providers.GEMINI.sandbox_arguments('read-only'), ['--approval-mode', 'default'])
        self.assertEqual(providers.GEMINI.sandbox_arguments('workspace-write'), ['--approval-mode', 'auto_edit'])
        with self.assertRaises(providers.ProviderRefused):
            providers.GEMINI.sandbox_arguments('read-only', managed=True)

    def test_commands_contain_no_dangerous_flags_and_validate_tokens(self):
        with tempfile.TemporaryDirectory() as root:
            env, _ = fake.isolated_environment(root)
            with patch.dict(os.environ, env):
                for provider in ('claude', 'gemini', 'codex'):
                    for sandbox in providers.SANDBOXES:
                        for session in (None, SESSION):
                            command = providers.adapter(provider).command(sandbox, session, model='fable-5' if provider != 'codex' else None)
                            for forbidden in ('dangerously', 'bypassPermissions', 'yolo', 'danger-full-access',
                                              '--skip-trust', 'auto_edit' if sandbox == 'read-only' else '\0'):
                                self.assertNotIn(forbidden, command, (provider, sandbox))
                            self.assertEqual(session is not None, SESSION in command)
                for bad in ('x & del', 'a b', '%PATH%', '"x', '--other', ''):
                    with self.assertRaises(ValueError):
                        providers.validate_model(bad)

    def test_environment_scrubs_paid_routes(self):
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'k', 'ANTHROPIC_AUTH_TOKEN': 't', 'CLAUDE_CODE_USE_BEDROCK': '1',
                                     'GEMINI_API_KEY': 'g', 'GOOGLE_API_KEY': 'g', 'CODEX_API_KEY': 'c', 'KEEP': '1'}):
            self.assertNotIn('ANTHROPIC_API_KEY', providers.CLAUDE.environment())
            self.assertNotIn('ANTHROPIC_AUTH_TOKEN', providers.CLAUDE.environment())
            self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', providers.CLAUDE.environment())
            self.assertNotIn('GEMINI_API_KEY', providers.GEMINI.environment())
            self.assertNotIn('GOOGLE_API_KEY', providers.GEMINI.environment())
            self.assertNotIn('CODEX_API_KEY', providers.CODEX.environment())
            self.assertEqual(providers.CLAUDE.environment()['KEEP'], '1')

    def test_model_catalog_distinguishes_known_unknown_and_mock(self):
        self.assertEqual(providers.CLAUDE.classify_model(None), 'default')
        self.assertEqual(providers.CLAUDE.classify_model('sonnet'), 'known')
        self.assertEqual(providers.CLAUDE.classify_model('claude-new-9'), 'unknown')
        self.assertEqual(providers.CLAUDE.classify_model('claude-new-9', ['claude-new-9']), 'known')
        self.assertEqual(providers.GEMINI.classify_model('flash'), 'known')
        with self.assertRaises(ValueError):
            providers.adapter('mock').classify_model('gpt')


class ParserTests(unittest.TestCase):
    def test_claude_success_waiting_and_session(self):
        parser = provider_events.ClaudeEvents()
        output = stream(parser, [
            {'type': 'system', 'subtype': 'init', 'session_id': SESSION, 'model': 'm'},
            {'type': 'assistant', 'uuid': 'u1', 'parent_tool_use_id': None,
             'message': {'id': 'a', 'content': [{'type': 'text', 'text': 'Hi\nCONTROL_CENTER_WAITING: Which?'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Hi\nCONTROL_CENTER_WAITING: Which?',
             'session_id': SESSION}])
        self.assertIn('session id: ' + SESSION, output)
        self.assertEqual(parser.outcome(0), {'success': True, 'message': 'Hi\nCONTROL_CENTER_WAITING: Which?',
                                             'condition': None, 'detail': None})

    def test_claude_documented_retry_categories_are_classified(self):
        for category, condition in (('rate_limit', providers.RATE_LIMITED), ('authentication_failed', providers.AUTH_FAILED),
                                    ('billing_error', providers.BILLING_ERROR), ('overloaded', providers.PROVIDER_UNAVAILABLE),
                                    ('model_not_found', providers.MODEL_UNAVAILABLE), ('invalid_request', providers.UNKNOWN_FAILURE)):
            parser = provider_events.ClaudeEvents()
            stream(parser, [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                            {'type': 'system', 'subtype': 'api_retry', 'error': category, 'attempt': 1},
                            {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True}])
            self.assertEqual(parser.outcome(1)['condition'], condition, category)

    def test_claude_prose_never_classifies_billing(self):
        parser = provider_events.ClaudeEvents()
        stream(parser, [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                        {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
                         'result': 'usage limit reached, add credits'}])
        self.assertEqual(parser.outcome(1)['condition'], providers.UNKNOWN_FAILURE)

    def test_resume_reporting_another_session_is_invalid(self):
        parser = provider_events.ClaudeEvents(expected_session=SESSION)
        stream(parser, [{'type': 'system', 'subtype': 'init', 'session_id': str(uuid4())}])
        self.assertEqual(parser.outcome(0)['condition'], providers.SESSION_INVALID)

    def test_claude_native_collaboration_and_subagent_text(self):
        parser = provider_events.ClaudeEvents(native_collaboration_prohibited=True)
        output = stream(parser, [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                                 {'type': 'assistant', 'parent_tool_use_id': None, 'message': {'content': [
                                     {'type': 'tool_use', 'name': 'Agent', 'input': {}}]}},
                                 {'type': 'assistant', 'parent_tool_use_id': 'toolu_1', 'message': {'content': [
                                     {'type': 'text', 'text': 'CONTROL_CENTER_WAITING: from a sub-agent'}]}}])
        self.assertTrue(parser.native_collaboration_seen)
        self.assertEqual(parser.error, managed_orchestration.VIOLATION)
        self.assertTrue(any(provider_events.collaboration_event(line) for line in output))
        self.assertIsNone(parser.message)  # Sub-agent text is never control input.

    def test_claude_final_result_is_validated_as_final_message(self):
        parser = provider_events.ClaudeEvents()
        stream(parser, [{'type': 'system', 'subtype': 'init', 'session_id': SESSION},
                        {'type': 'assistant', 'uuid': 'a', 'parent_tool_use_id': None, 'message': {'content': [
                            {'type': 'text', 'text': 'first'}]}},
                        {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'final'}])
        self.assertEqual(parser.finish(0), 'final')

    def test_gemini_delta_messages_and_unclassified_errors(self):
        parser = provider_events.GeminiEvents()
        stream(parser, [{'type': 'init', 'session_id': SESSION, 'model': 'g'},
                        {'type': 'message', 'role': 'user', 'content': 'p'},
                        {'type': 'message', 'role': 'assistant', 'content': 'Hel', 'delta': True},
                        {'type': 'tool_use', 'tool_name': 'read_file', 'tool_id': '1', 'parameters': {}},
                        {'type': 'message', 'role': 'assistant', 'content': 'Done', 'delta': True},
                        {'type': 'result', 'status': 'success', 'stats': {'models': {'g': {}}}}])
        self.assertEqual(parser.finish(0), 'Done')
        self.assertTrue(parser.tool_activity)
        failed = provider_events.GeminiEvents()
        stream(failed, [{'type': 'init', 'session_id': SESSION}, {'type': 'result', 'status': 'error',
                                                                     'error': {'type': 'quota', 'message': 'quota'}}])
        self.assertEqual(failed.outcome(1)['condition'], providers.UNKNOWN_FAILURE)
        with self.assertRaises(ValueError):
            provider_events.GeminiEvents().finish(0)


class PolicyStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AgentStore(Path(self.temp.name) / 'policy.sqlite3')
        providers.invalidate()
        self.addCleanup(providers.invalidate)

    def db(self):
        return self.store._connection()

    def test_defaults_preserve_codex_and_mock_and_disable_new_providers(self):
        with self.db() as db:
            self.assertTrue(providers.settings(db, 'mock')['enabled'])
            self.assertTrue(providers.settings(db, 'codex')['enabled'])
            self.assertEqual(providers.settings(db, 'codex')['enabled_basis'], 'pre_2f_default')
            self.assertFalse(providers.settings(db, 'claude')['enabled'])
            self.assertFalse(providers.settings(db, 'gemini')['enabled'])

    def test_enabling_requires_billing_confirmation_and_withdrawal_disables(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaises(providers.ProviderRefused):
                providers.update_settings(db, 'claude', enabled=True)
            result = providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
            self.assertTrue(result['enabled'] and result['billing_confirmed'])
            result = providers.update_settings(db, 'claude', billing_confirmed=False)
            self.assertFalse(result['enabled'])
            with self.assertRaises(ValueError):
                providers.update_settings(db, 'mock', enabled=False)

    def test_usage_blocks_store_only_provider_reported_resets(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            first = providers.record_block(db, 'claude', providers.RATE_LIMITED, 'limited')
            self.assertEqual(first, providers.record_block(db, 'claude', providers.RATE_LIMITED, 'again'))
            self.assertIsNone(providers.record_block(db, 'claude', providers.UNKNOWN_FAILURE, 'x'))
            with self.assertRaises(ValueError):
                providers.record_block(db, 'gemini', providers.QUOTA_EXHAUSTED, 'x', reset_at='tomorrow')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO provider_usage_blocks(provider,condition,reason,reset_at) VALUES "
                           "('claude','quota_exhausted','x','2030-01-01T00:00:00Z')")
            block = providers.active_blocks(db, 'claude')[0]
            self.assertIsNone(block['reset_at'])
            self.assertEqual(providers.clear_blocks(db, 'claude'), 1)
            self.assertEqual(providers.active_blocks(db, 'claude'), [])

    def test_admission_check_rechecks_policy_without_probing(self):
        with self.db() as db, patch.object(providers, 'run_probe') as probe:
            db.execute('BEGIN IMMEDIATE')
            with self.assertRaisesRegex(providers.ProviderRefused, 'disabled'):
                providers.admission_check(db, 'claude')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
            providers.admission_check(db, 'claude')
            providers.record_block(db, 'claude', providers.QUOTA_EXHAUSTED, 'Usage limit reported')
            with self.assertRaisesRegex(providers.ProviderRefused, 'Usage limit reported'):
                providers.admission_check(db, 'claude')
            providers.clear_blocks(db, 'claude')
            with providers._cache_lock:
                providers._cache['claude'] = (providers.Readiness('claude', providers.NOT_AUTHENTICATED, 'signed out'),
                                              time.monotonic(), providers._fingerprint('claude'))
            with self.assertRaisesRegex(providers.ProviderRefused, 'signed out'):
                providers.admission_check(db, 'claude')
            providers.admission_check(db, 'mock')
            probe.assert_not_called()

    def test_migration_is_idempotent_and_widens_responsibility_transfers(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            providers.migrate(db)
            providers.migrate(db)
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='responsibility_transfers'").fetchone()[0]
            self.assertIn("'claude','gemini'", sql)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)

    def test_v30_responsibility_table_is_rebuilt_with_rows(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='responsibility_transfers'").fetchone()[0]
            old = sql.replace("('mock','codex','claude','gemini')", "('mock','codex')")
            db.execute('ALTER TABLE responsibility_transfers RENAME TO rt_current')
            db.execute('DROP TABLE rt_current')
            db.execute(old)
            responsibility.migrate(db)  # The v30 indexes on the old table.
            providers.migrate(db)
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='responsibility_transfers'").fetchone()[0]
            self.assertIn("'claude','gemini'", sql)
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='responsibility_transfers' AND type='index'")}
            self.assertIn('responsibility_transfer_live', names)


class ReadinessProbeTests(unittest.TestCase):
    """Fake installations behind the real launcher locations and probe paths."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='provider probe ')
        self.addCleanup(self.temp.cleanup)
        env, self.log = fake.isolated_environment(self.temp.name)
        self.enterContext(patch.dict(os.environ, env))
        self.root = Path(self.temp.name)
        providers.invalidate()
        self.addCleanup(providers.invalidate)

    def probe(self, provider, **env):
        with patch.dict(os.environ, env):
            providers.invalidate(provider)
            return providers.probe(provider, force=True)

    @unittest.skipUnless(os.name == 'nt', 'Windows launchers')
    def test_ready_routes(self):
        self.assertEqual(self.probe('claude').state, providers.READY)
        self.assertEqual(self.probe('claude').billing_route, 'subscription')
        self.assertEqual(self.probe('codex').state, providers.READY)
        gemini = self.probe('gemini')
        self.assertEqual(gemini.state, providers.READY)
        self.assertEqual(gemini.billing_route, 'subscription_unverified')
        # The probes received none of their own provider's paid-route credentials.
        for entry in fake.read_log(self.log):
            self.assertFalse(set(entry['env']) & set(providers.adapter(entry['provider']).scrubbed_env), entry)

    @unittest.skipUnless(os.name == 'nt', 'Windows launchers')
    def test_distinguishable_unavailability(self):
        self.assertEqual(self.probe('claude', FAKE_CLAUDE_AUTH='none').state, providers.NOT_AUTHENTICATED)
        self.assertEqual(self.probe('claude', FAKE_CLAUDE_AUTH='apikey').state, providers.BILLING_BLOCKED)
        self.assertEqual(self.probe('codex', FAKE_CODEX_LOGIN='apikey').state, providers.BILLING_BLOCKED)
        self.assertEqual(self.probe('codex', FAKE_CODEX_LOGIN='none').state, providers.NOT_AUTHENTICATED)
        self.assertEqual(self.probe('claude', FAKE_CLAUDE_BROKEN='1').state, providers.INVALID_INSTALLATION)
        self.assertEqual(self.probe('claude', FAKE_CLAUDE_OLD='1').state, providers.UNSUPPORTED_VERSION)
        fake.gemini_settings(self.root / 'home', selected=None)
        self.assertEqual(self.probe('gemini').state, providers.NOT_AUTHENTICATED)
        fake.gemini_settings(self.root / 'home', selected='gemini-api-key')
        self.assertEqual(self.probe('gemini').state, providers.BILLING_BLOCKED)
        fake.gemini_settings(self.root / 'home', overage=None)
        self.assertIn('overageStrategy', self.probe('gemini').reason)
        fake.gemini_settings(self.root / 'home')
        (self.root / 'home' / '.env').write_text('GEMINI_API_KEY=abc\n', encoding='utf-8')
        self.assertIn('.env', self.probe('gemini').reason)
        (self.root / 'home' / '.env').unlink()
        (self.root / 'appdata' / 'npm' / 'gemini.cmd').unlink()
        self.assertEqual(self.probe('gemini').state, providers.NOT_INSTALLED)

    def test_disabled_provider_is_reported_before_readiness(self):
        state = providers.evaluate(None, 'claude', probe_installation=False)
        self.assertEqual(state.state, providers.DISABLED)
        self.assertEqual(providers.evaluate(None, 'gemini', probe_installation=False).state, providers.DISABLED)


class ProtocolTests(unittest.TestCase):
    def promotion(self, **extra):
        item = dict(proposal_id='cli', scope=None, resources=[], depends_on_task_ids=[], reuse_task_id=None, **extra)
        return json.dumps(dict(protocol=promotion.PROTOCOL, task_id=str(uuid4()), revision=1, decision_key='k',
                               items=[item], continuation=None))

    def test_promotion_items_may_request_a_child_provider(self):
        promotion.validate(self.promotion())
        promotion.validate(self.promotion(provider='claude'))
        promotion.validate(self.promotion(provider='gemini', model='flash'))
        for extra in (dict(provider='antigravity'), dict(model='flash'), dict(provider='claude', model='a b'),
                      dict(provider='codex', other=1)):
            with self.assertRaises(ValueError, msg=extra):
                promotion.validate(self.promotion(**extra))

    def test_inheritance_narrows_for_children_that_cannot_coordinate(self):
        parent = dict(agent_type='claude', sandbox='read-only', recursive_delegation_enabled=1, planning_enabled=1,
                      delegation_protocol_enabled=1, child_materialization_enabled=1, hybrid_delegation_enabled=1)
        self.assertTrue(delegation_safety.inherited(parent, 'codex')['recursive_delegation_enabled'])
        narrowed = delegation_safety.inherited(parent, 'gemini')
        self.assertFalse(any(narrowed.values()))
        delegation_safety.require_inherited(parent, 'gemini', 'read-only', {}, child_provider='gemini')
        with self.assertRaises(ValueError):  # Not the requested provider: never substituted.
            delegation_safety.require_inherited(parent, 'codex', 'read-only', {}, child_provider='gemini')
        with self.assertRaises(ValueError):  # Access never widens.
            delegation_safety.require_inherited(parent, 'codex', 'workspace-write', {}, child_provider='codex')
        with self.assertRaises(ValueError):  # Recursive authority the Gemini Child cannot hold.
            delegation_safety.require_inherited(parent, 'gemini', 'read-only', {'recursive_delegation_enabled': True},
                                                child_provider='gemini')


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class SupervisedFakeProviderTests(unittest.TestCase):
    """Real supervised processes (Windows Job Objects) running fake provider CLIs."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='provider run ')
        root = Path(self.temp.name)
        env, self.log = fake.isolated_environment(root)
        env['CONTROL_CENTER_WORKSPACE_ROOT'] = str(root / 'workspaces')
        self.enterContext(patch.dict(os.environ, env))
        self.assertIsNone(manager._store)
        repo = make_repository(root / 'canonical')
        self.enterContext(patch.object(manager, '_project_root', repo))
        self.enterContext(patch('app.persistence.DEFAULT_ROOT', repo))
        self.enterContext(patch.object(manager.notifications, 'transition'))
        self.enterContext(patch.object(manager.notifications, 'cancel'))
        providers.invalidate()
        self.path = root / 'data' / 'providers.sqlite3'
        manager.initialize_persistence(self.path)
        for provider in ('claude', 'gemini'):
            manager.update_provider_settings(provider, enabled=True, billing_confirmed=True)
        manager.update_provider_settings('codex', billing_confirmed=True)
        fake.allow_providers()  # Stage 2G: global permission is separate from enablement.

    def tearDown(self):
        for agent_id, process in list(manager.agents.items()):
            if process is not None and process.poll() is None:
                try:
                    manager.stop_agent(agent_id)
                except Exception:
                    pass
        for process in list(manager.agents.values()):
            watcher = getattr(process, '_control_center_watcher', None)
            if watcher is not None:
                watcher.join(15)
        manager._store = None
        manager._shutting_down = False
        for registry in (manager.agents, manager.agent_parents, manager.agent_statuses, manager.agent_outputs,
                         manager.agent_tasks, manager.agent_types, manager.agent_sandboxes, manager.agent_sessions,
                         manager.agent_waiting_questions, manager.agent_readers, manager.agent_similar_decisions,
                         manager.agent_always_decisions, manager.agent_models, manager.agent_conditions):
            registry.clear()
        providers.invalidate()
        try:
            self.temp.cleanup()
        except OSError:
            pass  # A Windows file may still be closing; the temp root is disposable.

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql, args)]

    def wait_status(self, agent_id, *statuses, timeout=25):
        ok = until(lambda: manager.agent_statuses.get(agent_id) in statuses and (
            manager.agents[agent_id] is None or manager.agents[agent_id].poll() is not None)
            and not manager._execution_live(manager.agents[agent_id]), timeout)
        self.assertTrue(ok, (manager.agent_statuses.get(agent_id), worker_output(manager.agent_outputs[agent_id])))
        until(lambda: getattr(manager.agents[agent_id], '_control_center_watcher', None) is None
              or not manager.agents[agent_id]._control_center_watcher.is_alive(), 10)
        return manager.agent_statuses[agent_id]

    def test_claude_run_is_supervised_restricted_and_scrubbed(self):
        agent_id = manager.start_agent('Summarize the repository', 'claude', 'read-only')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        run = fake.runs(self.log, 'claude')[-1]
        self.assertNotIn('ANTHROPIC_API_KEY', run['env'])  # The paid route never reached the provider.
        self.assertIn('Summarize the repository', run['prompt'])
        args = run['args']
        self.assertEqual(args[args.index('--permission-mode') + 1], 'dontAsk')
        self.assertNotIn('--resume', args)
        execution = self.rows('SELECT * FROM executions WHERE agent_id=?', agent_id)[-1]
        self.assertEqual((execution['provider'], execution['containment'], execution['state']),
                         ('claude', 'job_kill_on_close', 'ended_verified'))
        self.assertIn('Fake Claude completed the work.', manager.agent_outputs[agent_id])
        self.assertIsNotNone(manager.agent_sessions[agent_id])

    def test_reply_resumes_the_same_provider_session(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            agent_id = manager.start_agent('Change a file', 'claude', 'workspace-write', model='sonnet')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        self.assertEqual(manager.agent_waiting_questions[agent_id], 'Which file should I change?')
        session = manager.agent_sessions[agent_id]
        manager.reply_agent(agent_id, 'source.txt')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        args = fake.runs(self.log, 'claude')[-1]['args']
        self.assertEqual(args[args.index('--resume') + 1], session)
        self.assertEqual(args[args.index('--model') + 1], 'sonnet')
        self.assertEqual(args[args.index('--permission-mode') + 1], 'acceptEdits')  # Unchanged on resume.
        self.assertEqual({r['model'] for r in self.rows('SELECT model FROM executions WHERE agent_id=?', agent_id)}, {'sonnet'})

    def test_rate_limit_waits_and_blocks_new_work_until_explicitly_allowed(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='rate_limit'):
            agent_id = manager.start_agent('Long work', 'claude', 'read-only')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        task_id = manager._agent_task_id(agent_id)
        view = manager.get_agent(agent_id, include_output=False)
        self.assertEqual(view['provider_condition']['kind'], 'usage_limited')
        self.assertIsNone(view['provider_condition']['reset_at'])  # Never invented.
        self.assertEqual(view['waiting_context']['kind'], 'provider')
        self.assertIsNotNone(manager._store.get_active_assignment_for_task(task_id))  # Work kept.
        tasks_before = len(self.rows('SELECT * FROM tasks'))
        with self.assertRaisesRegex(providers.ProviderRefused, 'rate_limit'):
            manager.start_agent('Another task', 'claude', 'read-only')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), tasks_before)  # Refused before any Task.
        with self.assertRaises(providers.ProviderRefused):
            manager.continue_agent(agent_id)  # Still blocked: no automatic or implicit renewal.
        runs = len(fake.runs(self.log, 'claude'))
        self.assertEqual(manager.allow_provider_work('claude')['cleared'], 1)
        session = manager.agent_sessions[agent_id]
        manager.continue_agent(agent_id)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs + 1)
        args = fake.runs(self.log, 'claude')[-1]['args']
        self.assertEqual(args[args.index('--resume') + 1], session)
        self.assertEqual(manager._agent_task_id(agent_id), task_id)

    def test_provider_change_starts_a_new_session_with_backend_context(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            agent_id = manager.start_agent('Document the parser', 'claude', 'workspace-write')
            self.wait_status(agent_id, 'waiting')
        old_session = manager.agent_sessions[agent_id]
        with self.assertRaisesRegex(ValueError, 'Mock agents'):
            manager.change_agent_provider(manager.start_agent('Simulated', 'mock'), provider='gemini')
        changed = manager.change_agent_provider(agent_id, provider='gemini', sandbox='read-only')
        self.assertEqual((changed['agent_type'], changed['model'], changed['sandbox']), ('gemini', None, 'read-only'))
        self.assertIsNone(manager.agent_sessions[agent_id])
        history = changed['provider_history'][-1]
        self.assertEqual((history['from_provider'], history['to_provider'], history['session_retained']), ('claude', 'gemini', 0))
        with self.assertRaisesRegex(ValueError, 'widened'):
            manager.change_agent_provider(agent_id, provider='gemini', sandbox='workspace-write')
        manager.reply_agent(agent_id, 'Use docs/parser.md')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        run = fake.runs(self.log, 'gemini')[-1]
        self.assertNotIn('--resume', run['args'])  # Never the previous provider's session.
        self.assertNotIn(old_session, ' '.join(run['args']))
        self.assertIn('provider handoff', run['prompt'])
        self.assertIn('Document the parser', run['prompt'])
        self.assertEqual(run['args'][run['args'].index('--approval-mode') + 1], 'default')
        providers_seen = [r['provider'] for r in self.rows('SELECT provider FROM executions WHERE agent_id=? ORDER BY rowid', agent_id)]
        self.assertEqual(providers_seen, ['claude', 'gemini'])  # History is not rewritten.

    def test_running_execution_rejects_provider_change(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='slow', FAKE_SLOW_SECONDS='30'):
            agent_id = manager.start_agent('Slow', 'claude', 'read-only')
        self.assertTrue(until(lambda: fake.runs(self.log, 'claude')))
        with self.assertRaisesRegex(ValueError, 'safe boundary'):
            manager.change_agent_provider(agent_id, provider='codex')
        manager.stop_agent(agent_id)

    def test_disabled_provider_is_never_launched(self):
        manager.update_provider_settings('claude', enabled=False)
        before = len(self.rows('SELECT * FROM tasks'))
        with self.assertRaisesRegex(providers.ProviderRefused, 'not enabled'):
            manager.start_agent('Work', 'claude', 'read-only')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), before)
        self.assertEqual(self.rows('SELECT * FROM executions'), [])
        self.assertEqual(fake.runs(self.log), [])

    def test_status_change_during_launch_is_refused_at_admission(self):
        real = manager._execution_workspace

        def disable_then_continue(*args, **kwargs):
            manager.update_provider_settings('claude', enabled=False)
            return real(*args, **kwargs)

        with patch.object(manager, '_execution_workspace', side_effect=disable_then_continue):
            with self.assertRaisesRegex(providers.ProviderRefused, 'disabled before the launch'):
                manager.start_agent('Work', 'claude', 'read-only')
        self.assertEqual([r['state'] for r in self.rows('SELECT state FROM executions')], ['launch_aborted'])
        self.assertEqual(fake.runs(self.log), [])

    def test_gemini_cannot_coordinate_children(self):
        with self.assertRaisesRegex(providers.ProviderRefused, 'sub-agents'):
            manager.start_agent('Plan', 'gemini', 'read-only', planning_enabled=True)
        with self.assertRaisesRegex(providers.ProviderRefused, 'sub-agents'):
            manager.start_agent('Plan', 'gemini', 'read-only', delegation_protocol_enabled=True)
        self.assertEqual(fake.runs(self.log), [])

    def test_mixed_provider_hierarchy_runs_concurrently(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='slow', FAKE_SLOW_SECONDS='4'):
            parent = manager.start_agent('Coordinate the release', 'claude', 'read-only')
        child = manager.start_agent('Update the changelog', 'codex', 'read-only', parent_id=parent)
        grandchild = manager.start_agent('Check the wording', 'gemini', 'read-only', parent_id=child)
        self.assertTrue(until(lambda: all(manager.agents[a].poll() is None or manager.agent_statuses[a] != 'running'
                                          for a in (parent, child, grandchild))))
        for agent_id in (grandchild, child, parent):
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertEqual(manager.agent_parents[child], parent)
        self.assertEqual(manager.agent_parents[grandchild], child)
        recorded = {r['agent_id']: r['provider'] for r in self.rows('SELECT agent_id,provider FROM executions')}
        self.assertEqual((recorded[parent], recorded[child], recorded[grandchild]), ('claude', 'codex', 'gemini'))
        tasks = {r['task_id']: r for r in self.rows('SELECT task_id,parent_task_id FROM tasks')}
        self.assertEqual(tasks[manager._agent_task_id(grandchild)]['parent_task_id'], manager._agent_task_id(child))
        for run in fake.runs(self.log):
            self.assertFalse(set(run['env']) & set(providers.adapter(run['provider']).scrubbed_env), run['provider'])

    def test_managed_claude_run_stops_on_native_sub_agent(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='native'):
            agent_id = manager.start_agent('Coordinate', 'claude', 'read-only', delegation_protocol_enabled=True)
            self.assertEqual(self.wait_status(agent_id, 'stopped'), 'stopped')
        args = fake.runs(self.log, 'claude')[-1]['args']
        denied = args[args.index('--disallowedTools') + 1].split(',')
        self.assertTrue({'Agent', 'Task'} <= set(denied))
        self.assertIn(managed_orchestration.VIOLATION, manager.agent_outputs[agent_id])

    def test_transient_failure_retries_are_bounded(self):
        with patch.object(manager, 'RETRY_DELAYS', (0.3, 0.3)), patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='overloaded'):
            agent_id = manager.start_agent('Work', 'claude', 'read-only')
            self.assertTrue(until(lambda: len(fake.runs(self.log, 'claude')) >= 3, 30))
            time.sleep(1.5)
            self.wait_status(agent_id, 'waiting')
        self.assertEqual(len(fake.runs(self.log, 'claude')), 3)  # One run and exactly two retries.
        view = manager.get_agent(agent_id, include_output=False)
        self.assertEqual(view['provider_condition']['kind'], 'provider_unavailable')
        self.assertEqual(self.rows('SELECT * FROM provider_usage_blocks'), [])  # Transient: no usage block.

    def test_restart_during_quota_wait_keeps_state_and_launches_nothing(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='rate_limit'):
            agent_id = manager.start_agent('Long work', 'claude', 'read-only')
            self.wait_status(agent_id, 'waiting')
        task_id, session = manager._agent_task_id(agent_id), manager.agent_sessions[agent_id]
        runs = len(fake.runs(self.log))
        self.assertEqual(manager.shutdown_agents(), [])
        for key in list(manager.agents):
            manager.agents[key] = None  # A new backend process has no attached processes.
        manager.initialize_persistence(self.path)  # Startup restores evidence only.
        view = manager.get_agent(agent_id, include_output=False)
        self.assertEqual((view['status'], view['provider_condition']['kind'], view['session_id']),
                         ('waiting', 'usage_limited', session))
        self.assertEqual(len(fake.runs(self.log)), runs)  # Nothing relaunched by recovery.
        self.assertEqual(len(self.rows('SELECT * FROM provider_usage_blocks WHERE cleared_at IS NULL')), 1)
        with self.assertRaises(providers.ProviderRefused):
            manager.continue_agent(agent_id)
        manager.allow_provider_work('claude')
        manager.continue_agent(agent_id)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertEqual(manager._agent_task_id(agent_id), task_id)
        self.assertEqual(len(fake.runs(self.log)), runs + 1)  # Exactly one continuation, no duplicate.

    def test_unsupported_capabilities_and_models_are_refused_precisely(self):
        mock = manager.start_agent('Simulated', 'mock')
        self.wait_status(mock, 'finished')
        with self.assertRaisesRegex(providers.ProviderRefused, 'Replying is not supported'):
            manager.reply_agent(mock, 'answer')
        with self.assertRaisesRegex(ValueError, 'do not accept a model'):
            manager.start_agent('Simulated', 'mock', model='gpt')
        with self.assertRaisesRegex(ValueError, 'model identifier'):
            manager.start_agent('Work', 'claude', model='x & del')
        with self.assertRaisesRegex(ValueError, 'Unknown agent type'):
            manager.start_agent('Work', 'antigravity')

    def test_gemini_parent_with_claude_child(self):
        with patch.dict(os.environ, FAKE_GEMINI_SCENARIO='slow', FAKE_SLOW_SECONDS='3'):
            parent = manager.start_agent('Review the plan', 'gemini', 'read-only')
        child = manager.start_agent('Draft the summary', 'claude', 'read-only', parent_id=parent)
        for agent_id in (child, parent):
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        self.assertEqual(manager.agent_parents[child], parent)

    def test_authentication_failure_waits_and_verified_sign_in_clears_it(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='auth'):
            agent_id = manager.start_agent('Work', 'claude', 'read-only')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        self.assertEqual(manager.get_agent(agent_id, include_output=False)['provider_condition']['kind'], 'auth_required')
        status = next(p for p in manager.provider_statuses() if p['provider'] == 'claude')
        self.assertEqual(status['state']['state'], providers.NOT_AUTHENTICATED)
        self.assertEqual(manager.check_provider('claude')['state']['state'], providers.READY)
        manager.continue_agent(agent_id)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')


if __name__ == '__main__':
    unittest.main()
