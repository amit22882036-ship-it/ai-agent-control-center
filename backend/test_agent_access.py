"""Access levels (schema v39): each provider's own control for Read only, Workspace and Full Access,
and the rule that helpers never inherit Full Access unless the person allowed it.

No provider CLI runs here; commands are built and inspected, and launches use fake processes."""
import os
import tempfile
import unittest
from unittest.mock import patch

from app import agent_manager as manager
from app import delegation_safety, providers
import provider_test_support as fake
import test_project_lifecycle

SESSION = '0f9a3c5e-1d2b-4c3a-9e8f-7a6b5c4d3e2f'


class ProviderAccessMappingTests(unittest.TestCase):
    def test_codex_maps_each_level_to_its_documented_sandbox(self):
        self.assertEqual(providers.CODEX.sandbox_arguments('read-only'), ['--sandbox', 'read-only'])
        self.assertEqual(providers.CODEX.sandbox_arguments('workspace-write'), ['--sandbox', 'workspace-write'])
        self.assertEqual(providers.CODEX.sandbox_arguments('full-access'), ['--sandbox', 'danger-full-access'])
        with tempfile.TemporaryDirectory() as root:
            env, _ = fake.isolated_environment(root)
            with patch.dict(os.environ, env):
                command = providers.CODEX.command('full-access', SESSION)
        self.assertIn('--sandbox danger-full-access --color never', command)
        self.assertNotIn('dangerously-bypass', command)  # The sandbox value only; approvals stay Codex's own.
        with self.assertRaises(ValueError):
            providers.CODEX.sandbox_arguments('danger-full-access')

    def test_claude_full_access_needs_a_cli_that_documents_it(self):
        with patch.object(providers.CLAUDE, 'full_access_supported', False):
            with self.assertRaises(providers.ProviderRefused):
                providers.CLAUDE.sandbox_arguments('full-access')
        with patch.object(providers.CLAUDE, 'full_access_supported', True):
            plain = providers.CLAUDE.sandbox_arguments('full-access')
            managed = providers.CLAUDE.sandbox_arguments('full-access', managed=True)
        self.assertEqual(plain, ['--setting-sources', 'user,project,local', '--permission-mode', 'bypassPermissions'])
        # Native tools all run; only provider-internal sub-agents stay off when helpers go through admission.
        self.assertEqual(managed[managed.index('--disallowedTools') + 1].split(','), ['Agent', 'Task'])
        for arguments in (plain, managed):
            self.assertNotIn('--strict-mcp-config', arguments)
            self.assertNotIn('Bash', ','.join(arguments))
        # The confined levels are unchanged: no commands or web tools.
        write = providers.CLAUDE.sandbox_arguments('workspace-write')
        self.assertTrue({'Bash', 'PowerShell', 'WebFetch', 'WebSearch'} <= set(write[write.index('--disallowedTools') + 1].split(',')))

    def test_gemini_full_access_uses_yolo_only_when_documented(self):
        with patch.object(providers.GEMINI, 'yolo_supported', False):
            with self.assertRaises(providers.ProviderRefused):
                providers.GEMINI.sandbox_arguments('full-access')
        with patch.object(providers.GEMINI, 'yolo_supported', True):
            self.assertEqual(providers.GEMINI.sandbox_arguments('full-access'), ['--approval-mode', 'yolo'])
            with self.assertRaises(providers.ProviderRefused):
                providers.GEMINI.sandbox_arguments('full-access', managed=True)

    def test_every_real_provider_describes_every_level_honestly(self):
        for provider in providers.REAL:
            modes = providers.adapter(provider).describe()['access_modes']
            self.assertEqual(set(modes), set(providers.SANDBOXES), provider)
            self.assertFalse(modes['full-access']['sandboxed'], provider)
            self.assertIn('anything your Windows account can', modes['full-access']['summary'])
            self.assertFalse(modes['read-only']['edits'], provider)
        self.assertFalse(providers.CLAUDE.access_modes['workspace-write']['commands'])
        self.assertTrue(providers.CODEX.access_modes['workspace-write']['commands'])
        self.assertEqual(providers.adapter('mock').describe()['access_modes'], {})

    def test_ranks_never_widen(self):
        self.assertLess(providers.ACCESS_RANK['read-only'], providers.ACCESS_RANK['workspace-write'])
        self.assertLess(providers.ACCESS_RANK['workspace-write'], providers.ACCESS_RANK['full-access'])


class HelperAccessTests(unittest.TestCase):
    setUp = test_project_lifecycle.ProjectLifecycleTests.setUp
    tearDown = test_project_lifecycle.ProjectLifecycleTests.tearDown
    call = test_project_lifecycle.ProjectLifecycleTests.call
    project = test_project_lifecycle.ProjectLifecycleTests.project
    replacement = test_project_lifecycle.ProjectLifecycleTests.replacement
    task_for = test_project_lifecycle.ProjectLifecycleTests.task_for
    setUp_base = test_project_lifecycle.ProjectLifecycleTests.setUp_base

    def tree(self):
        store = manager._store
        project, _ = self.project()
        root = store.create_task('Lead work', project_id=project['project_id'])
        child = store.create_task('Helper work', parent_task_id=root['task_id'])
        grandchild = store.create_task('Nested helper work', parent_task_id=child['task_id'])
        return root, child, grandchild

    def test_helpers_of_full_access_work_with_workspace_access_by_default(self):
        root, child, grandchild = self.tree()
        with manager._store._connection() as db:
            for task in (root, child, grandchild):
                self.assertEqual(delegation_safety.helper_sandbox(db, task['task_id'], 'full-access'), 'workspace-write')
                self.assertEqual(delegation_safety.helper_sandbox(db, task['task_id'], 'workspace-write'), 'workspace-write')
                self.assertEqual(delegation_safety.helper_sandbox(db, task['task_id'], 'read-only'), 'read-only')
            delegation_safety.set_helper_access(db, root['task_id'], 'full-access')
            for task in (root, child, grandchild):
                self.assertEqual(delegation_safety.helper_sandbox(db, task['task_id'], 'full-access'), 'full-access')
            with self.assertRaises(ValueError):
                delegation_safety.set_helper_access(db, root['task_id'], 'read-only')

    def test_inherited_launches_are_checked_against_the_helper_access(self):
        parent = {'agent_type': 'codex', 'sandbox': 'full-access', 'recursive_delegation_enabled': 0,
                  'delegation_protocol_enabled': 0, 'child_materialization_enabled': 0, 'hybrid_delegation_enabled': 0,
                  'planning_enabled': 0}
        delegation_safety.require_inherited(parent, 'codex', 'workspace-write', {}, expected_sandbox='workspace-write')
        with self.assertRaisesRegex(ValueError, 'exceed'):
            delegation_safety.require_inherited(parent, 'codex', 'full-access', {}, expected_sandbox='workspace-write')

    def test_start_stores_the_choice_and_refuses_it_without_full_access(self):
        project, _ = self.project()
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        body = {'task': 'Lead work', 'project_id': project['project_id'], 'agent_type': 'codex', 'sandbox': 'full-access',
                'helper_access': 'full-access'}
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread, \
                patch.object(manager, '_codex_command', return_value='synthetic'):
            thread.return_value.is_alive.return_value = False
            started = self.call('POST', '/agents/start', body)
            self.call('POST', '/agents/start', {**body, 'sandbox': 'workspace-write'}, 422)
            self.call('POST', '/agents/start', {**body, 'helper_access': 'read-only'}, 422)
        self.assertEqual(started['sandbox'], 'full-access')
        task = self.task_for(started['agent_id'])
        with manager._store._connection() as db:
            row = db.execute('SELECT helper_access FROM task_access WHERE task_id=?', (task['task_id'],)).fetchone()
        self.assertEqual(row['helper_access'], 'full-access')
        manager.agent_readers[started['agent_id']].is_alive.return_value = False
        with patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            manager.stop_agent(started['agent_id'])

    def test_existing_agents_never_widen_their_access(self):
        project, _ = self.project()
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread, \
                patch.object(manager, '_codex_command', return_value='synthetic'):
            thread.return_value.is_alive.return_value = False
            started = self.call('POST', '/agents/start', {'task': 'Work', 'project_id': project['project_id'],
                                                          'agent_type': 'codex', 'sandbox': 'workspace-write'})
        agent = started['agent_id']
        manager.agent_statuses[agent] = 'waiting'
        manager.agents[agent] = None
        with self.assertRaisesRegex(ValueError, 'cannot be widened'):
            manager.change_agent_provider(agent, provider='codex', sandbox='full-access')


if __name__ == '__main__':
    unittest.main()
