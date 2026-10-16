"""Phase 2.1: coalesced, bounded orchestration wake-ups that are never silently lost."""
import threading
import unittest
from unittest.mock import patch

from app import agent_manager as manager, orchestration_waker as waking, replanning
from app.task_workspaces import TaskBusy, task_lock
import test_delegation_materialization
import test_replanning
from test_api_security import call, headers


class Clock:
    """Deterministic thread/timer factories: threads run inline, timers fire on demand."""
    def __init__(self):
        self.timers = []

    def thread(self, target):
        target()

    def timer(self, delay, function):
        self.timers.append((delay, function))

    def fire(self):
        delay, function = self.timers.pop(0)
        function()
        return delay


def waker(run_pass, clock, **options):
    return waking.OrchestrationWaker(run_pass, thread_factory=clock.thread, timer_factory=clock.timer, **options)


class WakerUnitTests(unittest.TestCase):
    def test_wakes_coalesce_into_one_pass_with_merged_steps(self):
        passes, clock, gate = [], Clock(), []
        deferred = waking.OrchestrationWaker(lambda task, steps: passes.append((task, steps)),
                                             thread_factory=gate.append, timer_factory=clock.timer)
        deferred.wake('parent', ('advance',))
        deferred.wake('parent', ('materialize',))
        deferred.wake('parent', ('advance',))
        self.assertEqual(len(gate), 1)  # one drainer
        gate.pop()()
        self.assertEqual(passes, [('parent', ['materialize', 'advance'])])
        self.assertEqual(deferred.status('parent')['state'], 'idle')

    def test_wake_during_a_pass_schedules_exactly_one_follow_up(self):
        clock, passes = Clock(), []
        holder = {}

        def run(task, steps):
            passes.append(steps)
            if len(passes) == 1:
                holder['waker'].wake(task, ('advance',))
                holder['waker'].wake(task, ('advance',))
        holder['waker'] = waker(run, clock)
        holder['waker'].wake('parent')
        self.assertEqual(passes, [list(waking.STEPS), ['advance']])

    def test_busy_pass_retries_with_backoff_then_reports_exhaustion(self):
        clock, passes, exhausted = Clock(), [], []
        subject = waker(lambda task, steps: passes.append(steps) or waking.PassOutcome(busy=True), clock,
                        on_exhausted=lambda task, reason: exhausted.append((task, reason)))
        subject.wake('parent')
        delays = []
        while clock.timers:
            delays.append(clock.fire())
        self.assertEqual(delays, list(waking.BUSY_DELAYS))
        self.assertEqual(len(passes), len(waking.BUSY_DELAYS) + 1)
        self.assertTrue(all(steps == list(waking.STEPS) for steps in passes))  # busy work is never dropped
        self.assertEqual(exhausted, [('parent', 'Orchestration wake exhausted after 6 attempts: Task busy')])
        self.assertEqual(subject.status('parent')['state'], 'exhausted')

    def test_new_event_resets_busy_backoff(self):
        clock, outcomes = Clock(), [waking.PassOutcome(busy=True), waking.PassOutcome(busy=True), None]
        subject = waker(lambda task, steps: outcomes.pop(0), clock)
        subject.wake('parent')
        clock.fire()
        self.assertEqual(subject.status('parent')['busy_attempts'], 2)
        subject.wake('parent', reason='event')  # fresh evidence restarts the bounded budget
        self.assertEqual(subject.status('parent')['state'], 'idle')
        self.assertEqual(subject.status('parent')['busy_attempts'], 0)

    def test_pending_children_use_the_existing_bounded_follow_up(self):
        clock, passes = Clock(), []
        subject = waker(lambda task, steps: passes.append(steps) or waking.PassOutcome(pending=True), clock)
        subject.wake('parent')
        delays = []
        while clock.timers:
            delays.append(clock.fire())
        self.assertEqual(delays, list(waking.PENDING_DELAYS))
        self.assertEqual(subject.status('parent')['state'], 'idle')

    def test_unexpected_error_is_logged_and_not_retried(self):
        clock = Clock()
        subject = waker(lambda task, steps: (_ for _ in ()).throw(RuntimeError('defect')), clock)
        with self.assertLogs(waking.logger, level='ERROR'):
            subject.wake('parent')
        self.assertEqual(clock.timers, [])
        self.assertEqual(subject.status('parent')['state'], 'idle')

    def test_inactive_waker_runs_nothing(self):
        passes = []
        subject = waker(lambda task, steps: passes.append(task), Clock(), is_active=lambda: False)
        subject.wake('parent')
        self.assertEqual(passes, [])


class LostWakeIntegrationTests(unittest.TestCase):
    """Real orchestration passes against a temporary store; workers are recorded, never real."""
    setUp = test_delegation_materialization.RuntimeMaterializationTests.setUp
    patch = test_delegation_materialization.RuntimeMaterializationTests.patch
    read = test_delegation_materialization.RuntimeMaterializationTests.read
    finish = test_delegation_materialization.RuntimeMaterializationTests.finish
    accepted = test_delegation_materialization.RuntimeMaterializationTests.accepted

    def install(self):
        self.clock = Clock()
        self.patch(patch.object(manager, '_waker', manager.orchestration_waker.OrchestrationWaker(
            manager._orchestration_pass, thread_factory=self.clock.thread, timer_factory=self.clock.timer,
            on_exhausted=manager._record_wake_exhausted)))

    def test_busy_parent_lock_no_longer_loses_the_child_launch(self):
        self.accepted()
        self.install()
        base = len(self.commands)  # the Parent's own start
        parent = self.task['task_id']
        held, release = threading.Event(), threading.Event()

        def hold():
            with task_lock(self.store, parent):
                held.set()
                release.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(5)
        try:
            manager._schedule_materialization(parent)  # live event while another operation holds the Task
            self.assertEqual(len(self.commands), base)
            self.assertEqual(manager._waker.status(parent)['state'], 'retry_wait')
        finally:
            release.set()
            holder.join(5)
        self.assertEqual(self.clock.fire(), 1)
        self.assertEqual(len(self.commands), base + 1)  # the Child launch happened once, after the retry
        self.assertEqual(manager._waker.status(parent)['state'], 'idle')

    def test_failing_replanning_step_does_not_skip_materialization(self):
        self.accepted()
        self.install()
        base = len(self.commands)
        with patch.object(manager, 'advance_replanning', side_effect=RuntimeError('replanning defect')), \
             self.assertLogs(manager.logger, level='ERROR'):
            manager._schedule_materialization(self.task['task_id'])
        self.assertEqual(len(self.commands), base + 1)

    def test_retries_never_relaunch_a_started_child(self):
        self.accepted()
        self.install()
        base = len(self.commands)
        manager._schedule_materialization(self.task['task_id'])
        manager._schedule_materialization(self.task['task_id'])
        manager.wake_orchestration(self.task['task_id'], reason='operator')
        self.assertEqual(len(self.commands), base + 1)


class ReplanningBusyTests(unittest.TestCase):
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
    change = test_replanning.ReplanningTests.change
    decision = test_replanning.ReplanningTests.decision
    submit = test_replanning.ReplanningTests.submit
    transitions = test_replanning.ReplanningTests.transitions

    def test_busy_target_leaves_transition_eligible_instead_of_blocked(self):
        self.submit(self.decision(changes=[self.change(action='retain')]))
        target = self.targets[0]['child_task_id']
        held, release = threading.Event(), threading.Event()

        def hold():
            with task_lock(self.store, target):
                held.set()
                release.wait(10)
        holder = threading.Thread(target=hold)
        holder.start()
        held.wait(5)
        try:
            with self.assertRaises(TaskBusy):
                manager.advance_replanning(self.task['task_id'], report_busy=True)
            row, = self.transitions()
            self.assertEqual(row['phase'], 'validated')  # before: permanently 'blocked'
        finally:
            release.set()
            holder.join(5)
        row, = manager.advance_replanning(self.task['task_id'])
        self.assertEqual(row['phase'], 'applied')

    def test_exhausted_wake_is_recorded_on_the_continuation(self):
        with self.store._connection() as db:
            db.execute("UPDATE delegation_continuations SET phase='waiting',last_error=NULL WHERE task_id=?", (self.task['task_id'],))
        manager._record_wake_exhausted(self.task['task_id'], 'Orchestration wake exhausted after 6 attempts: Task busy')
        status = manager.orchestration_status(self.task['task_id'])
        self.assertEqual(status['continuation']['last_error'], 'Orchestration wake exhausted after 6 attempts: Task busy')


class OrchestrationApiTests(unittest.TestCase):
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

    def test_operator_wake_queues_and_status_is_read_only(self):
        task = self.task['task_id']
        with patch.object(manager, 'wake_orchestration', return_value={'state': 'queued'}) as wake:
            status, _, payload = call('POST', f'/tasks/{task}/orchestration/wake', headers())
            self.assertEqual(status, 202)
            wake.assert_called_once_with(task, reason='operator')
            status, _, _ = call('POST', f'/tasks/{task}/orchestration/wake', headers(token=None))
            self.assertEqual(status, 403)  # CSRF still applies
            self.assertEqual(call('POST', '/tasks/missing/orchestration/wake', headers())[0], 404)
        with patch.object(manager, '_orchestration_pass', side_effect=AssertionError('GET must not wake')):
            status, _, payload = call('GET', f'/tasks/{task}/orchestration', headers(token=None))
        self.assertEqual(status, 200)
        self.assertIn(b'"continuation"', payload)
        self.assertIn(b'"wake"', payload)
        self.assertEqual(call('GET', '/tasks/missing/orchestration', headers(token=None))[0], 404)

    def test_startup_queues_no_orchestration_work(self):
        with patch.dict(manager.agents, {key: None for key in manager.agents}), \
             patch.object(manager, '_orchestration_pass', side_effect=AssertionError('no startup pass')):
            manager.initialize_persistence(self.store.path)
            self.assertEqual(manager._waker.status(self.task['task_id']), {'state': 'idle'})


if __name__ == '__main__':
    unittest.main()
