"""Phase 2.2A: the lost-watcher sweep only detects and marks; recovery is an explicit action."""
import threading
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from app import agent_manager as manager
from app.codex_events import Execution
import test_read_side_effects
import test_replanning
from test_api_security import call, headers
from test_read_side_effects import FakeProcess, LAUNCHERS

WAKES = ('wake_orchestration', '_schedule_materialization', '_schedule_hybrid_for_agent', '_wake_delegation_siblings')


def forbid(stack, names):
    return {name: stack.enter_context(patch.object(manager, name, side_effect=AssertionError(name))) for name in names}


def dead_watcher():
    watcher = threading.Thread(target=lambda: None)
    watcher.start()
    watcher.join()
    return watcher


class RegularLostWatcherTests(unittest.TestCase):
    setUp = test_read_side_effects.RealWatcherTests.setUp
    tearDown = test_read_side_effects.RealWatcherTests.tearDown
    fake_agent = test_read_side_effects.RealWatcherTests.fake_agent
    waiting_agent = test_read_side_effects.RealWatcherTests.waiting_agent
    policy_episode = test_read_side_effects.RealWatcherTests.policy_episode
    join_threads = test_read_side_effects.RealWatcherTests.join_threads

    def lost(self, mode='always'):
        first = FakeProcess('codex\n', 'CONTROL_CENTER_WAITING: Which file next?\n', wait_error=OSError('wait failed'))
        with patch.object(threading, 'excepthook'):
            agent_id = self.policy_episode(mode, first)
            first.exit()
            self.join_threads(first)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')
        return agent_id

    def test_sweep_only_marks_and_never_launches_or_wakes(self):
        agent_id = self.lost()
        spawned = self.spawn.call_count
        with ExitStack() as stack:
            forbidden = forbid(stack, set(LAUNCHERS) | set(WAKES) | {'_finalize_process', '_finish_structured'})
            with self.assertLogs(manager.logger, level='ERROR'):
                self.assertEqual(manager.sweep_lost_watchers(), [agent_id])
            self.assertEqual(manager.sweep_lost_watchers(), [])  # marked once
            for mock in forbidden.values():
                mock.assert_not_called()
        self.assertEqual(self.spawn.call_count, spawned)
        self.assertEqual(manager.agent_statuses[agent_id], 'running')  # not finalized by the sweep
        self.assertEqual(manager.agent_watcher_lost[agent_id], manager.WATCHER_LOST_REASON)
        self.assertEqual(manager.agent_outputs[agent_id].count(manager.WATCHER_LOST_MARKER), 1)
        self.notify.assert_not_called()

    def test_watcher_lost_is_visible_in_agent_detail(self):
        agent_id = self.lost()
        manager.sweep_lost_watchers()
        status, _, payload = call('GET', f'/agents/{agent_id}', headers(token=None))
        self.assertEqual(status, 200)
        self.assertIn(manager.WATCHER_LOST_REASON.encode(), payload)

    def test_explicit_handoff_settles_once_without_continuation_or_wake(self):
        for mode in ('always', 'similar'):
            with self.subTest(mode=mode):
                agent_id = self.lost(mode)
                manager.sweep_lost_watchers()
                spawned = self.spawn.call_count
                with ExitStack() as stack:
                    waker = stack.enter_context(patch.object(manager._waker, 'wake', side_effect=AssertionError('wake')))
                    status, _, payload = call('POST', f'/agents/{agent_id}/recovery/handoff', headers())
                    self.assertEqual(status, 200, payload)
                    waker.assert_not_called()
                self.assertEqual(manager.agent_statuses[agent_id], 'waiting')  # ordinary wait, user decides
                self.assertEqual(self.spawn.call_count, spawned)  # no paid continuation
                self.assertNotIn(agent_id, manager.agent_watcher_lost)
                status, _, _ = call('POST', f'/agents/{agent_id}/recovery/handoff', headers())
                self.assertEqual(status, 409)  # once only

    def test_handoff_requires_csrf_and_a_lost_watcher(self):
        agent_id = self.lost()
        self.assertEqual(call('POST', f'/agents/{agent_id}/recovery/handoff', headers(token=None))[0], 403)
        healthy, _ = self.fake_agent()
        self.assertEqual(call('POST', f'/agents/{healthy}/recovery/handoff', headers())[0], 409)
        self.assertEqual(call('POST', '/agents/missing/recovery/handoff', headers())[0], 404)

    def test_failed_handoff_fails_closed_and_is_not_repeated(self):
        agent_id = self.lost()
        with patch.object(manager, '_finalize_process', side_effect=RuntimeError('handoff defect')), \
             self.assertLogs(manager.logger, level='ERROR'):
            status, _, payload = call('POST', f'/agents/{agent_id}/recovery/handoff', headers())
        self.assertEqual(status, 503)
        self.assertEqual(manager.agent_statuses[agent_id], 'stopped')
        self.assertIn('Recovery failed', manager.agent_watcher_lost[agent_id])
        self.assertIn('--- Explicit recovery failed; execution stopped for inspection ---', manager.agent_outputs[agent_id])

    def test_sweep_waits_for_output_to_drain_before_any_action(self):
        agent_id, process = self.fake_agent()
        process._control_center_watcher = dead_watcher()
        process.poll.return_value = 0
        process.stdout = type('Open', (), {'closed': False})()
        try:
            manager.sweep_lost_watchers()  # marks the visible condition only
            with self.assertRaisesRegex(ValueError, 'still being read'):
                manager.recover_lost_watcher(agent_id)
        finally:
            process.stdout.closed = True  # let the shared teardown drain

    def test_recovery_sweep_timer_stops_with_its_store(self):
        timers = []
        with patch.object(manager, '_start_timer', side_effect=lambda delay, fn: timers.append((delay, fn))), \
             patch.object(manager, '_store', object()), patch.object(manager, 'sweep_lost_watchers') as sweep:
            manager.start_recovery_sweep()
            delay, tick = timers.pop()
            self.assertEqual(delay, manager.SWEEP_INTERVAL_SECONDS)
            tick()
            sweep.assert_called_once()
            self.assertEqual(len(timers), 1)  # re-armed
            with patch.object(manager, '_store', object()):
                timers.pop()[1]()  # a different persistence lifetime: stop
            self.assertEqual(sweep.call_count, 1)
            self.assertEqual(timers, [])


class StructuredLostWatcherTests(unittest.TestCase):
    setUp = test_replanning.ReplanningTests.setUp
    patch = test_replanning.ReplanningTests.patch
    stream = test_replanning.ReplanningTests.stream
    finish = test_replanning.ReplanningTests.finish
    launch = test_replanning.ReplanningTests.launch
    plan = test_replanning.ReplanningTests.plan
    message = test_replanning.ReplanningTests.message
    rows = test_replanning.ReplanningTests.rows
    workspace_fixture = test_replanning.ReplanningTests.workspace_fixture
    accepted = test_replanning.ReplanningTests.accepted

    def lose(self):
        process = self.process
        self.assertIsInstance(manager._structured(process), Execution)
        process._control_center_watcher = dead_watcher()
        process.poll.return_value = 0
        process.stdout.close()
        process.stderr.close()
        self.assertEqual(manager.agent_statuses[self.key], 'running')

    def continuation(self):
        with self.store._connection() as db:
            return manager.delegation_orchestration.state(db, self.task['task_id'])

    def test_sweep_marks_continuation_recovery_required_without_handoff(self):
        self.lose()
        with ExitStack() as stack:
            forbidden = forbid(stack, set(LAUNCHERS) | set(WAKES) | {'_finish_structured', '_finalize_process'})
            with self.assertLogs(manager.logger, level='ERROR'):
                manager.sweep_lost_watchers()
            for mock in forbidden.values():
                mock.assert_not_called()
        current = self.continuation()
        self.assertEqual(current['phase'], 'recovery_required')
        self.assertEqual(current['last_error'], manager.WATCHER_LOST_REASON)
        self.assertEqual(manager.agent_statuses[self.key], 'running')

    def test_explicit_handoff_runs_structured_finish_once_and_suppresses_wakes(self):
        self.lose()
        manager.sweep_lost_watchers()
        calls = []
        real = manager._finish_structured

        def finish(agent_id, process):
            calls.append(agent_id)
            return real(agent_id, process)
        with patch.object(manager, '_finish_structured', side_effect=finish), \
             patch.object(manager._waker, 'wake', side_effect=AssertionError('no implicit wake')):
            try:
                manager.recover_lost_watcher(self.key)
            except RuntimeError:
                pass  # an unfinished synthetic turn fails closed; the guard is what matters here
            with self.assertRaisesRegex(ValueError, 'already attempted|only to an exited'):
                manager.recover_lost_watcher(self.key)
        self.assertEqual(calls, [self.key])
        self.assertTrue(manager._structured(self.process).processed)
        self.assertNotEqual(manager.agent_statuses[self.key], 'running')  # never left appearing to run


if __name__ == '__main__':
    unittest.main()
