import unittest
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException
from app import agent_manager as manager
from app.main import StartAgentRequest, start_mock_agent, start_child_agent
import test_similar_decisions


class HierarchyTests(unittest.TestCase):
    replacement = test_similar_decisions.SimilarDecisionTests.replacement
    resume = test_similar_decisions.SimilarDecisionTests.resume
    finish = test_similar_decisions.SimilarDecisionTests.finish
    tearDown = test_similar_decisions.SimilarDecisionTests.tearDown

    def create(self, parent_id=None, task='Task', kind='mock', sandbox='read-only'):
        process = self.replacement()
        request = StartAgentRequest(task=task, agent_type=kind, sandbox=sandbox)
        with patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, 'Thread'):
            result = start_mock_agent(request) if parent_id is None else start_child_agent(parent_id, request)
        agent_id = result['agent_id']
        manager.agent_readers[agent_id].is_alive.return_value = False
        return agent_id, process

    def test_missing_parent_rejected_before_spawn(self):
        with patch.object(manager, '_spawn_process') as spawn:
            with self.assertRaises(HTTPException) as error:
                start_child_agent('missing', StartAgentRequest(task='Child'))
            self.assertEqual(error.exception.status_code, 404)
            with self.assertRaises(LookupError):
                manager.start_agent('Child', parent_id='missing')
            spawn.assert_not_called()
        self.assertEqual(manager.agent_parents, {})

    def test_roots_siblings_grandchildren_and_api_relationships(self):
        root, _ = self.create(task='Root')
        first, _ = self.create(root, 'First')
        second, _ = self.create(root, 'Second')
        grandchild, _ = self.create(first, 'Grandchild')
        expected = {root: None, first: root, second: root, grandchild: first}
        self.assertEqual({agent['agent_id']: agent['parent_id'] for agent in manager.get_agents()}, expected)
        for key, parent in expected.items():
            self.assertEqual(str(UUID(key)), key)
            self.assertEqual(manager.get_agent(key)['parent_id'], parent)
        self.assertEqual(manager.get_agent(root)['child_ids'], [first, second])
        self.assertEqual(manager.get_agent(first)['child_ids'], [grandchild])
        self.assertEqual(manager.get_agent(grandchild)['child_ids'], [])
        self.assertEqual(len(set(expected)), 4)

    def test_child_state_and_sandbox_are_independent(self):
        root, parent_process = self.create(kind='codex', sandbox='workspace-write')
        manager.agent_sessions[root] = root
        manager.agent_outputs[root].append('Parent output')
        manager.agent_similar_decisions[root].enabled = True
        manager.agent_similar_decisions[root].examples.append('Parent example')
        manager.agent_always_decisions[root].enabled = True
        for kind in ('mock', 'codex'):
            child, child_process = self.create(root, task='Independent child', kind=kind)
            detail = manager.get_agent(child)
            self.assertEqual(detail['task'], 'Independent child')
            self.assertEqual(detail['agent_type'], kind)
            self.assertEqual(detail['sandbox'], 'read-only' if kind == 'codex' else None)
            self.assertIsNone(detail['session_id'])
            self.assertEqual(detail['output'], [])
            self.assertFalse(detail['similar_decisions_enabled'])
            self.assertFalse(detail['always_decide_enabled'])
            self.assertEqual(manager.agent_similar_decisions[child].examples, [])
            self.assertIsNot(child_process, parent_process)
            self.assertIsNot(manager.agent_outputs[child], manager.agent_outputs[root])
            if kind == 'codex':
                self.assertEqual(child_process.stdin.saved, manager._codex_prompt('Independent child'))
                self.assertNotIn(root, child_process.stdin.saved)

    def test_all_parent_statuses_allow_children_without_status_changes(self):
        for status in ('running', 'waiting', 'finished', 'stopped'):
            parent, process = self.create()
            manager.agent_statuses[parent] = status
            process.poll.return_value = None if status == 'running' else 0
            child, _ = self.create(parent)
            self.assertEqual(manager.agent_statuses[parent], status)
            self.assertEqual(manager.agent_statuses[child], 'running')

    def test_stop_and_finish_are_not_cascading(self):
        parent, parent_process = self.create()
        child, child_process = self.create(parent)
        manager.stop_agent(child)
        parent_process.terminate.assert_not_called()
        self.assertEqual(manager.agent_statuses[parent], 'running')
        child_process.terminate.assert_called_once()
        sibling, sibling_process = self.create(parent)
        manager.stop_agent(parent)
        sibling_process.terminate.assert_not_called()
        self.assertEqual(manager.agent_statuses[sibling], 'running')
        sibling_process.poll.return_value = 0
        self.assertEqual(manager.get_agent(sibling)['status'], 'finished')
        self.assertEqual(manager.get_agent(parent)['status'], 'stopped')

    def test_relationship_survives_redirect_reply_decide_and_autonomy(self):
        parent, _ = self.create()
        child, _ = self.create(parent, kind='codex')
        manager.agent_sessions[child] = child
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(lambda key: manager.redirect_agent(key, 'Correction'), child)
        for action in (lambda key: manager.reply_agent(key, 'Answer'), manager.decide_agent,
                       manager.decide_similar_agent, manager.decide_always_agent):
            self.finish(child, 'Question')
            manager.agent_statuses[child] = 'waiting'
            self.resume(action, child)
            self.assertEqual(manager.get_agent(child)['parent_id'], parent)
            self.assertEqual(manager.get_agent(parent)['child_ids'], [child])
        self.finish(child, 'Next question')
        self.resume(manager.get_agent, child)  # Automatic Always resume.
        manager.disable_always_agent(child)
        self.finish(child, 'Different question')
        self.resume(manager.get_agent, child)  # Automatic Similar resume.
        self.assertEqual(manager.get_agent(child)['parent_id'], parent)
        self.assertEqual(manager.get_agent(child)['session_id'], child)

    def test_relationship_does_not_enter_codex_command_or_change_spawn_options(self):
        parent, _ = self.create()
        process = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed command') as command, \
             patch.object(manager.subprocess, 'Popen', return_value=process) as popen, \
             patch.object(manager, 'Thread'):
            child = manager.start_agent('Task & literal text', 'codex', 'workspace-write', parent_id=parent)
        manager.agent_readers[child].is_alive.return_value = False
        command.assert_called_once_with('workspace-write')
        self.assertEqual(popen.call_args.args, ('fixed command',))
        self.assertEqual(popen.call_args.kwargs['cwd'], manager._project_root)
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertEqual(process.stdin.saved, manager._codex_prompt('Task & literal text'))
        self.assertNotIn(parent, str(popen.call_args))

    def test_child_completion_uses_normal_notifications_only(self):
        parent, _ = self.create()
        with patch.object(manager.notifications, 'transition') as notify:
            child, _ = self.create(parent, kind='codex')
            notify.assert_not_called()
            self.finish(child, 'Need information')
            self.assertEqual(manager.get_agent(child)['status'], 'waiting')
            notify.assert_called_once_with(child, 'waiting', 'Task')
            self.assertEqual(manager.agent_statuses[parent], 'running')
