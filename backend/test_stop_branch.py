import asyncio
import io
import json
import unittest
from threading import Event, Thread
from unittest.mock import patch

from app import agent_manager as manager
from app.main import app
import test_agent_hierarchy


class StopBranchTests(unittest.TestCase):
    replacement = test_agent_hierarchy.HierarchyTests.replacement
    resume = test_agent_hierarchy.HierarchyTests.resume
    finish = test_agent_hierarchy.HierarchyTests.finish
    tearDown = test_agent_hierarchy.HierarchyTests.tearDown

    def create(self, parent_id=None, kind='mock'):
        key, process = test_agent_hierarchy.HierarchyTests.create(self, parent_id, kind=kind)
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        return key, process

    def test_leaf_and_subtree_order_excludes_ancestors_siblings_other_roots(self):
        root, _ = self.create()
        a, _ = self.create(root)
        b, _ = self.create(root)
        a1, _ = self.create(a)
        a2, _ = self.create(a)
        a11, _ = self.create(a1)
        other, _ = self.create()
        relationships = manager.agent_parents.copy()
        with patch.object(manager, 'stop_agent', wraps=manager.stop_agent) as stop:
            result = manager.stop_branch(a)
        expected = [a11, a1, a2, a]
        self.assertEqual([call.args[0] for call in stop.call_args_list], expected)
        self.assertEqual(result, {'root_agent_id': a, 'ok': True,
                                 'results': [{'agent_id': key, 'status': 'stopped'} for key in expected], 'failures': []})
        for key in (root, b, other):
            self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(manager.agent_parents, relationships)
        self.assertEqual(manager.get_agent(a)['child_ids'], [a1, a2])
        self.assertEqual(manager.stop_branch(a), result)
        self.assertEqual(manager.stop_branch(b)['results'], [{'agent_id': b, 'status': 'stopped'}])

    def test_mixed_statuses_and_inactive_root_with_running_child(self):
        for root_status in ('running', 'stopped', 'finished'):
            root, root_process = self.create()
            manager.agent_statuses[root] = root_status
            if root_status != 'running':
                root_process.poll.return_value = 0
            keys = []
            for status in ('running', 'waiting', 'stopped', 'finished'):
                key, process = self.create(root)
                keys.append(key)
                manager.agent_statuses[key] = status
                if status != 'running':
                    process.poll.return_value = 0
            result = manager.stop_branch(root)
            self.assertTrue(result['ok'])
            self.assertEqual([item['status'] for item in result['results']],
                             ['stopped', 'stopped', 'stopped', 'finished',
                              'finished' if root_status == 'finished' else 'stopped'])
            self.assertEqual(manager.get_agent(root)['child_ids'], keys)

    def test_arbitrary_depth_and_corrupt_cycles_visit_each_member_once(self):
        for parents in (
            {str(i): str(i - 1) if i else None for i in range(1500)},
            {'0': '0'},
            {'0': '2', '1': '0', '2': '1'},
        ):
            with patch.dict(manager.agents, dict.fromkeys(parents, object()), clear=True), \
                 patch.dict(manager.agent_parents, parents, clear=True), \
                 patch.object(manager, 'stop_agent', side_effect=lambda key: {'agent_id': key, 'status': 'stopped'}) as stop:
                result = manager.stop_branch('0')
                self.assertEqual(stop.call_count, len(parents))
                self.assertEqual([item['agent_id'] for item in result['results']], list(reversed(parents)))

    def test_partial_failure_continues_and_preserves_failed_process_status(self):
        root, _ = self.create()
        failed, process = self.create(root, kind='codex')
        sibling, _ = self.create(root)
        with patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('Could not terminate process tree')):
            result = manager.stop_branch(root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failures'], [{'agent_id': failed, 'error': 'Could not terminate process tree'}])
        self.assertEqual([item['agent_id'] for item in result['results']], [sibling, root])
        self.assertEqual(manager.agent_statuses[failed], 'running')
        self.assertIs(manager.agents[failed], process)

    def test_buffered_waiting_autonomy_never_spawns_and_notifications_cancel(self):
        root, _ = self.create()
        children = []
        for mode in ('similar', 'always'):
            key, process = self.create(root, kind='codex')
            children.append(key)
            manager.agent_sessions[key] = key
            if mode == 'similar':
                manager.agent_similar_decisions[key].enabled = True
                manager.agent_similar_decisions[key].examples.append('Question')
            else:
                manager.agent_always_decisions[key].enabled = True
            process.stdout = io.StringIO('CONTROL_CENTER_WAITING: Question\n')
            process.poll.return_value = 0
            manager.agent_readers[key].join.side_effect = lambda timeout=None, k=key, p=process: manager._read_output(k, p)
        with patch.object(manager, '_spawn_process') as spawn, \
             patch.object(manager.notifications, 'cancel') as cancel:
            result = manager.stop_branch(root)
            spawn.assert_not_called()
            self.assertTrue(result['ok'])
            for key in children + [root]:
                self.assertEqual(manager.agent_statuses[key], 'stopped')
                cancel.assert_any_call(key)

    def test_waiting_stop_cancels_without_new_notifications(self):
        root, process = self.create()
        process.poll.return_value = 0
        manager.agent_statuses[root] = 'waiting'
        with patch.object(manager.notifications, 'transition') as notify, \
             patch.object(manager.notifications, 'cancel') as cancel:
            manager.stop_branch(root)
            cancel.assert_called_once_with(root)
            notify.assert_not_called()

    def test_current_replacements_are_stopped_after_redirect_and_autonomy(self):
        root, _ = self.create()
        child, old = self.create(root, kind='codex')
        manager.agent_sessions[child] = child
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(lambda key: manager.redirect_agent(key, 'Correction'), child)
        self.finish(child, 'Question')
        manager.agent_statuses[child] = 'waiting'
        self.resume(lambda key: manager.reply_agent(key, 'Answer'), child)
        self.finish(child, 'Next question')
        manager.agent_statuses[child] = 'waiting'
        self.resume(manager.decide_always_agent, child)
        self.finish(child, 'Choice')
        _, current = self.resume(manager.get_agent, child)
        with patch.object(manager, '_stop_windows_tree') as stop:
            result = manager.stop_branch(root)
            stop.assert_called_once_with(current)
        self.assertIsNot(old, current)
        self.assertTrue(result['ok'])
        self.assertEqual(manager.agent_parents[child], root)

    def test_lock_prevents_child_creation_during_branch_stop(self):
        root, _ = self.create()
        entered = Event()
        release = Event()
        creation_attempted = Event()
        created = Event()
        results = []
        errors = []
        normal_stop = manager.stop_agent

        def slow_stop(key):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('Test release timeout')
            return normal_stop(key)

        def create_child():
            creation_attempted.set()
            try:
                results.append(manager.start_agent('Child', parent_id=root))
            except Exception as exc:
                errors.append(exc)
            finally:
                created.set()

        process = self.replacement()
        with patch.object(manager, 'stop_agent', side_effect=slow_stop), \
             patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_start_reader'):
            branch = Thread(target=lambda: manager.stop_branch(root))
            child = Thread(target=create_child)
            branch.start()
            try:
                self.assertTrue(entered.wait(2))
                child.start()
                self.assertTrue(creation_attempted.wait(2))
                self.assertFalse(created.wait(0.05))
            finally:
                release.set()
                branch.join(3)
                if child.ident is not None:
                    child.join(3)
        self.assertFalse(errors)
        self.assertTrue(created.is_set())
        self.assertEqual(manager.agent_parents[results[0]], root)
        process.poll.return_value = 0

    def test_http_endpoint_missing_success_and_partial_failure(self):
        async def request(key):
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            path = f'/agents/{key}/stop-branch'
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'method': 'POST', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                       'query_string': b'', 'headers': [], 'client': ('127.0.0.1', 1234),
                       'server': ('localhost', 8000), 'root_path': ''}, receive, send)
            return messages[0]['status'], json.loads(b''.join(item.get('body', b'') for item in messages))

        self.assertEqual(asyncio.run(request('missing'))[0], 404)
        root, _ = self.create()
        status, result = asyncio.run(request(root))
        self.assertEqual(status, 200)
        self.assertTrue(result['ok'])
        child, _ = self.create(root, kind='codex')
        with patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('Cannot stop tree')):
            status, result = asyncio.run(request(root))
        self.assertEqual(status, 200)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failures'], [{'agent_id': child, 'error': 'Cannot stop tree'}])
