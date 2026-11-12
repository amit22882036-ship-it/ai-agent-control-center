"""Local work by default: provider integrations that act outside the workspace (publishing, connectors)
stay off and every agent is told to work locally, unless the person allowed them for the root Task.

Only fake provider CLIs run here (temporary folders, no model, nothing paid)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import agent_manager as manager
from app import external_access, providers
import provider_test_support as fake
import test_agent_access
import test_providers

SESSION = '0f9a3c5e-1d2b-4c3a-9e8f-7a6b5c4d3e2f'


class RestrictTests(unittest.TestCase):
    def codex(self, *args, **kwargs):
        with tempfile.TemporaryDirectory() as root:
            env, _ = fake.isolated_environment(root)
            with patch.dict(os.environ, env):
                return manager.ValidatedCommand(providers.CODEX.command(*args, **kwargs))

    def test_codex_apps_and_plugins_are_turned_off_per_invocation(self):
        for command in (self.codex('workspace-write'), self.codex('full-access', SESSION, structured=True)):
            restricted = external_access.restrict(command, 'codex')
            self.assertIn(' exec -c features.apps=false -c features.plugins=false -c model_provider=openai ', restricted)
            self.assertEqual(restricted.count('features.apps=false'), 1)
            self.assertEqual(external_access.restrict(restricted, 'codex'), restricted)  # Applied once.
            # Everything else is exactly the command that was validated.
            self.assertEqual(restricted.replace(external_access.CODEX_OVERRIDES, ''), str(command))

    def test_a_rewritten_command_keeps_its_launch_attributes(self):
        command = manager.ValidatedStructuredCommand('"cmd" /c ""codex" exec --sandbox read-only --json -"')
        command.policy, command.resolved = {'p': 1}, ('gpt', 'high')
        restricted = external_access.restrict(command, 'codex')
        self.assertIsInstance(restricted, manager.ValidatedStructuredCommand)
        self.assertIsInstance(restricted, manager.StructuredCommand)
        self.assertEqual((restricted.policy, restricted.resolved), ({'p': 1}, ('gpt', 'high')))

    def test_claude_full_access_loads_no_mcp_servers_and_confined_levels_are_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            env, _ = fake.isolated_environment(root)
            with patch.dict(os.environ, env), patch.object(providers.CLAUDE, 'full_access_supported', True):
                providers.invalidate()
                full = manager.ProviderCommand(providers.CLAUDE.command('full-access'))
                write = manager.ProviderCommand(providers.CLAUDE.command('workspace-write'))
        self.assertNotIn('--strict-mcp-config', full)
        self.assertIn(' -p --strict-mcp-config --output-format', external_access.restrict(full, 'claude'))
        self.assertEqual(external_access.restrict(write, 'claude'), write)  # Already strict.

    def test_unexpected_shapes_fail_closed_and_other_providers_pass_through(self):
        with self.assertRaises(ValueError):
            external_access.restrict(manager.ValidatedCommand('"codex" exec a exec b'), 'codex')
        with self.assertRaises(ValueError):
            external_access.restrict(manager.ProviderCommand('"claude" --print'), 'claude')
        gemini = manager.ProviderCommand('"gemini" --output-format stream-json')
        self.assertEqual(external_access.restrict(gemini, 'gemini'), gemini)
        # A stand-in that no provider adapter built launches no provider CLI and is left as it is.
        self.assertEqual(external_access.restrict('synthetic', 'codex'), 'synthetic')
        mock = ['python', 'mock_agent.py']
        self.assertIs(external_access.restrict(mock, 'mock'), mock)

    def test_instruction_names_the_rule_and_the_question_route(self):
        rule = external_access.instruction(False)
        for phrase in ('Do not publish, deploy, host', 'websites or sites', 'skills, plugins, apps',
                       'ask the person one clear question', 'overrides any skill or plugin instruction'):
            self.assertIn(phrase, rule)
        self.assertNotIn('Do not publish', external_access.instruction(True))


class TreeChoiceTests(unittest.TestCase):
    setUp = test_agent_access.HelperAccessTests.setUp
    tearDown = test_agent_access.HelperAccessTests.tearDown
    call = test_agent_access.HelperAccessTests.call
    project = test_agent_access.HelperAccessTests.project
    replacement = test_agent_access.HelperAccessTests.replacement
    task_for = test_agent_access.HelperAccessTests.task_for
    setUp_base = test_agent_access.HelperAccessTests.setUp_base
    tree = test_agent_access.HelperAccessTests.tree

    def test_the_root_choice_applies_to_the_whole_tree_and_defaults_to_off(self):
        root, child, grandchild = self.tree()
        with manager._store._connection() as db:
            for task in (root, child, grandchild):
                self.assertFalse(external_access.allowed(db, task['task_id']))
            self.assertFalse(external_access.allowed(db, 'missing'))
            self.assertFalse(external_access.allowed(db, None))
            external_access.set_allowed(db, root['task_id'], True)
            for task in (root, child, grandchild):
                self.assertTrue(external_access.allowed(db, task['task_id']))
            with self.assertRaises(ValueError):
                external_access.set_allowed(db, root['task_id'], 'yes')

    def test_start_stores_the_choice_only_for_a_new_root(self):
        project, _ = self.project()
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        body = {'task': 'Build and publish a site', 'project_id': project['project_id'], 'agent_type': 'codex',
                'sandbox': 'workspace-write'}
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread, \
                patch.object(manager, '_codex_command', return_value='synthetic'):
            thread.return_value.is_alive.return_value = False
            allowed = self.call('POST', '/agents/start', {**body, 'external_integrations': True})
            self.call('POST', '/agents/start', {**body, 'external_integrations': 'yes'}, 422)
            self.call('POST', f"/agents/{allowed['agent_id']}/children/start",
                      {'task': 'Helper', 'agent_type': 'codex', 'sandbox': 'workspace-write',
                       'external_integrations': True}, 422)
        task = self.task_for(allowed['agent_id'])
        with manager._store._connection() as db:
            self.assertTrue(external_access.allowed(db, task['task_id']))
        manager.agent_readers[allowed['agent_id']].is_alive.return_value = False
        with patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            manager.stop_agent(allowed['agent_id'])


class SupervisedLaunchTests(unittest.TestCase):
    """Fake provider CLIs record exactly what they were started with and the prompt they read."""
    setUp = test_providers.SupervisedFakeProviderTests.setUp
    tearDown = test_providers.SupervisedFakeProviderTests.tearDown
    rows = test_providers.SupervisedFakeProviderTests.rows
    wait_status = test_providers.SupervisedFakeProviderTests.wait_status

    def run_agent(self, provider, level, **options):
        before = len(fake.runs(self.log, provider))
        agent_id = manager.start_agent('Build FrameSeek', provider, level, **options)
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')
        runs = fake.runs(self.log, provider)
        self.assertEqual(len(runs), before + 1)
        return runs[-1]

    def test_codex_runs_without_apps_or_plugins_and_is_told_to_work_locally(self):
        run = self.run_agent('codex', 'workspace-write')
        args = ' '.join(run['args'])
        self.assertIn('features.apps=false', args)
        self.assertIn('features.plugins=false', args)
        self.assertTrue(run['prompt'].startswith('Control Center operating rule (local work only):'))
        self.assertIn('Build FrameSeek', run['prompt'])

    def test_every_provider_is_told_the_rule(self):
        for provider in ('claude', 'gemini'):
            with self.subTest(provider=provider):
                run = self.run_agent(provider, 'workspace-write')
                self.assertIn('Do not publish, deploy, host', run['prompt'])

    def test_claude_full_access_runs_with_strict_mcp(self):
        with patch.dict(os.environ, FAKE_HELP_EXTRA='bypassPermissions'),                 patch.object(providers.CLAUDE, 'full_access_supported', True):
            run = self.run_agent('claude', 'full-access')
        self.assertIn('--strict-mcp-config', run['args'])
        self.assertEqual(run['args'][run['args'].index('--permission-mode') + 1], 'bypassPermissions')

    def test_an_allowed_root_keeps_its_integrations_and_says_so(self):
        project = manager._store.ensure_project_for_root(manager._project_root)
        run = self.run_agent('codex', 'workspace-write', project_id=project['project_id'], external_integrations=True)
        self.assertNotIn('features.apps=false', ' '.join(run['args']))
        self.assertTrue(run['prompt'].startswith('Control Center setting for this Task: the person allowed external'))
        self.assertNotIn('Do not publish', run['prompt'])


if __name__ == '__main__':
    unittest.main()
