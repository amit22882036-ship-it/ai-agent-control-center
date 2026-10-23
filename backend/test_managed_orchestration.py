"""Managed provider policy regressions; no real Codex processes or requests."""
import io
import json
import os
import unittest
from threading import Event
from unittest.mock import Mock, patch
from uuid import uuid4

from app import agent_manager as manager, managed_orchestration as policy
from app.codex_events import CodexEvents
import test_codex_configuration as configuration
import test_delegation_protocol as baseline
import test_promotion as promotion_tests
from test_codex_collaboration_events import collaboration
from execution_test_support import use_attested_test_doubles


class CommandTests(unittest.TestCase):
    setUp = configuration.CodexConfigurationTests.setUp

    def test_managed_initial_and_resume_override_is_fixed_and_process_local(self):
        for session in (None, str(uuid4())):
            for sandbox in ('read-only', 'workspace-write'):
                with self.subTest(session=session, sandbox=sandbox), patch.dict(
                        os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-6-astra',
                        CONTROL_CENTER_CODEX_REASONING_EFFORT='low'):
                    command = manager._codex_command(sandbox, session, structured=True)
                    self.assertEqual(command.count(policy.CLI_OVERRIDE), 1)
                    self.assertIn('--model gpt-6-astra -c model_reasoning_effort=low ', command)
                    self.assertIn(f'--sandbox {sandbox} ', command)
                    self.assertIn('--json ', command)
                    with patch.object(manager.subprocess, 'Popen') as spawn:
                        process = manager._spawn_process(command, 'codex', cwd='isolated')
                    self.assertFalse(spawn.call_args.kwargs['shell'])
                    self.assertEqual(spawn.call_args.kwargs['stdin'], manager.subprocess.PIPE)
                    self.assertTrue(manager._structured(process).events.native_collaboration_prohibited)
                    ordinary = manager._codex_command(sandbox, session)
                    self.assertNotIn('features.multi_agent', ordinary)


class ParserPolicyTests(unittest.TestCase):
    def test_actual_events_fail_policy_without_becoming_control(self):
        for kind in ('item.started', 'item.updated', 'item.completed'):
            with self.subTest(kind=kind):
                adapter = CodexEvents(native_collaboration_prohibited=True)
                stream = baseline.events(baseline.envelope())
                baseline.feed(adapter, [*stream[:2], collaboration(kind), *stream[2:]])
                self.assertTrue(adapter.native_collaboration_seen)
                self.assertEqual(adapter.error, policy.VIOLATION)
                with self.assertRaisesRegex(ValueError, 'Managed orchestration policy violation'):
                    adapter.finish(0)

    def test_tool_output_and_prose_are_not_native_collaboration_events(self):
        adapter = CodexEvents(native_collaboration_prohibited=True)
        stream = baseline.events('The term collab_tool_call is documentation.')
        tool = dict(type='item.completed', item=dict(id='tool', type='command_execution',
                    aggregated_output=json.dumps(collaboration())))
        baseline.feed(adapter, [*stream[:2], tool, *stream[2:]])
        self.assertFalse(adapter.native_collaboration_seen)
        self.assertEqual(adapter.finish(0), stream[2]['item']['text'])
        for text in ('Example: '+json.dumps(collaboration()), 'collab_tool_call', '{}', 'null'):
            self.assertFalse(policy.collaboration_event(text))


class RuntimePolicyTests(unittest.TestCase):
    patch = baseline.RuntimeTests.patch
    read = baseline.RuntimeTests.read
    finish = baseline.RuntimeTests.finish

    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        baseline.RuntimeTests.setUp(self)
        self.task = self.store.create_task('Fix the CSV parser and verify its behavior.',
                                          project_id=self.project['project_id'])
        spawn = manager._spawn_process.side_effect

        class Prompt(io.StringIO):
            def close(self):
                pass

        def capture(*args, **kwargs):
            process = spawn(*args, **kwargs)
            process.stdin = Prompt()
            return process

        self.patch(patch.object(manager, '_spawn_process', side_effect=capture))

    def launch(self, autonomous=True, kind='codex'):
        capabilities = {k: True for k in ('planning_enabled', 'delegation_protocol_enabled',
            'child_materialization_enabled', 'hybrid_delegation_enabled',
            'recursive_delegation_enabled')} if autonomous else {}
        key = manager.start_agent('Fix the CSV parser and verify its behavior.', kind,
                                  task_id=self.task['task_id'], **capabilities)
        return key, manager.agents[key]

    def test_natural_language_receives_policy_and_simple_work_can_finish(self):
        key, process = self.launch()
        prompt = process.stdin.getvalue()
        self.assertIn(policy.instruction(True), prompt)
        self.assertIn('control-center.planning.v1', prompt)
        self.assertIn('control-center.promotion.v1', prompt)
        self.assertIn('Fix the CSV parser and verify its behavior.', prompt)
        self.assertTrue(manager._structured(process).events.native_collaboration_prohibited)
        self.read(key, process, 'Fixed and verified independently.')
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertEqual(len(self.commands), 1)

    def test_ordinary_codex_prompt_and_mock_are_unchanged(self):
        key, process = self.launch(False)
        self.assertEqual(process.stdin.getvalue(), manager._codex_prompt(manager.agent_tasks[key]))
        self.assertIsNone(manager._structured(process))
        manager.stop_agent(key)
        other = self.store.create_task('Mock task', project_id=self.project['project_id'])
        mock = manager.start_agent('Mock task', 'mock', task_id=other['task_id'])
        self.assertEqual(manager.agents[mock].stdin.getvalue(), '')
        self.assertIsNone(manager._structured(manager.agents[mock]))

    def test_reply_and_redirect_keep_managed_policy(self):
        key, process = self.launch()
        self.read(key, process, 'CONTROL_CENTER_WAITING: Which format?')
        self.finish(key, process)
        with patch.object(manager, '_resume_cwd', return_value=self.root):
            manager.reply_agent(key, 'CSV')
        replacement = manager.agents[key]
        self.assertIn(policy.instruction(True), replacement.stdin.getvalue())
        self.assertTrue(manager._structured(replacement).events.native_collaboration_prohibited)
        with patch.object(manager, '_resume_cwd', return_value=self.root), \
                patch.object(manager, 'evaluate_workspace_freshness', return_value=None), \
                patch.object(manager, 'reject_divergence'):
            manager.redirect_agent(key, 'Preserve whitespace')
        self.assertIn(policy.instruction(True), manager.agents[key].stdin.getvalue())
        self.assertTrue(manager._structured(manager.agents[key]).events.native_collaboration_prohibited)

    def test_native_event_schedules_one_stop_and_no_control_or_completion(self):
        key, process = self.launch()
        stream = baseline.events(baseline.envelope())
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in
            [*stream[:2], collaboration('item.started'), collaboration(), *stream[2:]]))
        seen = Event()
        with patch.object(manager, '_stop_policy_violation', side_effect=lambda *args: seen.set()) as stop:
            manager._read_output(key, process)
            self.assertTrue(seen.wait(2))
            self.assertEqual(stop.call_count, 1)
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertIn(policy.VIOLATION, self.store.full_output(key))
        self.assertEqual(len(self.commands), 1)

    def test_policy_stop_checks_process_and_assignment_authority(self):
        key, process = self.launch()
        manager._structured(process).events.native_collaboration_seen = True
        with patch.object(manager, 'stop_agent') as stop:
            with patch.object(self.store, 'execution_is_current', return_value=False):
                manager._stop_policy_violation(key, process)
            stop.assert_not_called()
            manager.agents[key] = Mock()
            manager._stop_policy_violation(key, process)
            stop.assert_not_called()
            manager.agents[key] = process
            manager._stop_policy_violation(key, process)
            stop.assert_called_once_with(key)

    def test_policy_violation_uses_existing_tree_stop_without_success(self):
        key, process = self.launch()
        events = manager._structured(process).events
        baseline.feed(events, [*baseline.events('unused')[:2], collaboration()])
        manager._stop_policy_violation(key, process)
        manager._stop_windows_tree.assert_called_once_with(process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'pending')
        self.assertFalse(process._control_center_completion['validated'])
        self.assertEqual(len(self.commands), 1)

    def test_legacy_native_session_is_refused_before_spawn_or_redirect_stop(self):
        key, process = self.launch()
        self.read(key, process, 'Work continues')
        manager.agent_outputs[key].append(json.dumps(collaboration()))
        manager._save_agent(key)
        with patch.object(manager, '_spawn_process') as spawn, \
                patch.object(manager, '_stop_windows_tree') as stop:
            with self.assertRaisesRegex(ValueError, 'prior native Codex collaboration'):
                manager.redirect_agent(key, 'Continue')
            with self.assertRaisesRegex(ValueError, 'prior native Codex collaboration'):
                manager._resume_agent(key, 'unused', 'Continue', 'Reply', cwd=self.root)
            spawn.assert_not_called()
            stop.assert_not_called()
        self.assertEqual(manager.agent_statuses[key], 'running')


class ManagedIntegrationTests(unittest.TestCase):
    setUp = promotion_tests.PromotionIntegrationTests.setUp
    patch = promotion_tests.PromotionIntegrationTests.patch
    stream = promotion_tests.PromotionIntegrationTests.stream
    finish = promotion_tests.PromotionIntegrationTests.finish
    message = promotion_tests.PromotionIntegrationTests.message
    plan = promotion_tests.PromotionIntegrationTests.plan
    read = promotion_tests.PromotionIntegrationTests.read
    launch = promotion_tests.PromotionIntegrationTests.launch
    accepted = promotion_tests.PromotionIntegrationTests.accepted

    def test_managed_policy_across_promotion_child_review_and_completion(self):
        prompts = []
        original = manager._execution_prompt

        def capture(key, text, process):
            prompt = original(key, text, process)
            prompts.append(prompt)
            self.assertTrue(manager._structured(process).events.native_collaboration_prohibited)
            return prompt

        with patch.object(manager, '_execution_prompt', side_effect=capture):
            promotion_tests.PromotionIntegrationTests.test_real_workspaces_promoted_child_integration_ack_and_completion(self)
        self.assertEqual(len(prompts), 4)
        self.assertTrue(all('Control Center managed orchestration policy:' in p for p in prompts))
        self.assertIn('Do only your assigned responsibility.', prompts[1])
        self.assertIn('acknowledge_results', prompts[2])


if __name__ == '__main__':
    unittest.main()
