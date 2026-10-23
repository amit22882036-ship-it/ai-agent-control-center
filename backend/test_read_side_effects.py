"""Reading agents (GET) must never start paid execution.

The process watcher alone owns finalization, Always/Similar continuation and
delegation scheduling. These tests pin that ownership and that the legitimate
watcher path still continues exactly as before.
"""
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import agent_manager as manager
from app import workspace_git
from app.codex_events import Execution
from app.main import decide_always_codex_agent, decide_similar_codex_agent
import test_persistence
import test_similar_decisions
import test_task_lifecycle
from test_api_security import GET_ROUTES, call, headers
from execution_test_support import use_attested_test_doubles

LAUNCHERS = ('_spawn_process', '_resume_agent', '_schedule_materialization', '_schedule_hybrid_for_agent',
             '_wake_delegation_siblings', 'start_agent', 'materialize_delegations', 'advance_delegations',
             'advance_replanning')


class ReadSideEffectTests(unittest.TestCase):
    from workspace_test_support import process_test_setup as setUp
    tearDown = test_similar_decisions.SimilarDecisionTests.tearDown
    fake_agent = test_similar_decisions.SimilarDecisionTests.fake_agent
    waiting_agent = test_similar_decisions.SimilarDecisionTests.waiting_agent
    replacement = test_similar_decisions.SimilarDecisionTests.replacement
    resume = test_similar_decisions.SimilarDecisionTests.resume
    finish = test_similar_decisions.SimilarDecisionTests.finish
    watch = test_similar_decisions.SimilarDecisionTests.watch

    def policy_agent(self, mode):
        """A Codex agent whose exited process asks a question an enabled policy would answer."""
        agent_id, _ = self.waiting_agent()
        self.resume(decide_always_codex_agent if mode == 'always' else decide_similar_codex_agent, agent_id)
        self.finish(agent_id, 'Which file next?')
        return agent_id

    @staticmethod
    def no_launch(stack):
        return {name: stack.enter_context(patch.object(manager, name, side_effect=AssertionError(name)))
                for name in LAUNCHERS}

    def read_everything(self, agent_id):
        manager.get_agent(agent_id)
        manager.get_agent(agent_id, include_output=False)
        manager.get_agents()
        manager.get_agent_output(agent_id)
        for path in ('/agents', f'/agents/{agent_id}', f'/agents/{agent_id}?include_output=false',
                     f'/agents/{agent_id}/output', f'/agents/{agent_id}/activity', f'/agents/{agent_id}/hierarchy',
                     f'/agents/{agent_id}/name-history', '/workspace', '/tasks', '/projects'):
            path, _, query = path.partition('?')
            status, _, _ = call('GET', path, headers(token=None))
            self.assertNotEqual(status, 500, path)

    def test_reads_never_start_always_or_similar_continuation(self):
        for mode in ('always', 'similar'):
            with self.subTest(mode=mode):
                agent_id = self.policy_agent(mode)
                always = manager.agent_always_decisions[agent_id]
                similar = manager.agent_similar_decisions[agent_id]
                before = (always.automatic_attempt, always.handled, set(similar.attempted))
                with ExitStack() as stack:
                    launched = self.no_launch(stack)
                    notify = stack.enter_context(patch.object(manager.notifications, 'transition'))
                    for _ in range(3):
                        self.read_everything(agent_id)
                    for name, mock in launched.items():
                        mock.assert_not_called()
                    notify.assert_not_called()
                # Not consumed: the episode is still pending for its owner.
                self.assertEqual(manager.agent_statuses[agent_id], 'running')
                self.assertEqual((always.automatic_attempt, always.handled, set(similar.attempted)), before)
                # The watcher still performs the legitimate automatic continuation once.
                _, process = self.resume(self.watch, agent_id)
                self.assertIn('Which file next?', process.stdin.saved)
                marker = '--- Automatic Always Decision ---' if mode == 'always' else '--- Automatic Similar Decision ---'
                self.assertEqual(manager.agent_outputs[agent_id].count(marker), 1)

    def test_reads_leave_a_watched_process_to_its_watcher(self):
        agent_id, process = self.fake_agent()
        self.finish(agent_id, 'Question?')
        release = threading.Event()
        process._control_center_watcher = threading.Thread(target=release.wait)  # created, not yet started
        with patch.object(manager, '_finalize_process') as finalize:
            self.read_everything(agent_id)
            process._control_center_watcher.start()
            self.read_everything(agent_id)
            release.set()
            process._control_center_watcher.join()
            finalize.assert_not_called()
        self.assertEqual(manager.agent_statuses[agent_id], 'running')
        # A watcher that is gone without finalizing is reported and its exit settled.
        with self.assertLogs(manager.logger, level='ERROR'):
            self.read_everything(agent_id)
        self.assertEqual(manager.agent_statuses[agent_id], 'waiting')

    def test_real_watcher_is_recorded_on_the_process(self):
        agent_id, process = self.fake_agent()
        with patch.object(manager, '_watch_process'), patch.object(manager, '_read_output'):
            manager._start_reader(agent_id, process)
        self.assertIsInstance(process._control_center_watcher, threading.Thread)
        process._control_center_watcher.join()

    def test_unwatched_read_settles_status_without_scheduling(self):
        agent_id, _ = self.fake_agent()
        self.finish(agent_id, 'Question?')
        with ExitStack() as stack:
            launched = self.no_launch(stack)
            self.assertEqual(manager.get_agent(agent_id)['status'], 'waiting')
            for mock in launched.values():
                mock.assert_not_called()
        # The watcher path keeps scheduling delegation follow-up work.
        other, _ = self.fake_agent()
        self.finish(other)
        with patch.object(manager, '_wake_delegation_siblings') as wake, \
             patch.object(manager, '_schedule_hybrid_for_agent') as hybrid:
            self.watch(other)
        wake.assert_called_once_with(other)
        hybrid.assert_called_once_with(other)

    def test_structured_execution_is_left_to_its_watcher(self):
        agent_id, process = self.fake_agent()
        self.finish(agent_id)
        process._control_center_execution = Execution()
        with patch.object(manager, '_finish_structured') as finish:
            self.read_everything(agent_id)
            finish.assert_not_called()
        self.assertEqual(manager.agent_statuses[agent_id], 'running')

    def test_every_get_route_launches_nothing(self):
        agent_id = self.policy_agent('always')
        values = {'agent_id': agent_id}
        with ExitStack() as stack:
            launched = self.no_launch(stack)
            for route in sorted(GET_ROUTES - {'/events'}):
                path = route
                for name in ('agent_id', 'task_id', 'project_id', 'integration_id', 'deadlock_id', 'delegation_id', 'removal_id',
                             'execution_id'):
                    path = path.replace('{' + name + '}', values.get(name, 'missing'))
                with self.subTest(route=route):
                    status, _, _ = call('GET', path, headers(token=None))
                    self.assertNotEqual(status, 500)
            for mock in launched.values():
                mock.assert_not_called()
        self.assertEqual(manager.agent_statuses[agent_id], 'running')


class FakeStdout:
    """Line source that blocks after its lines until the fake process exits."""
    def __init__(self, lines, exited):
        self.lines, self.exited, self.closed = list(lines), exited, False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def __iter__(self):
        yield from self.lines
        self.exited.wait(10)

    def close(self):
        self.closed = True


class FakeProcess:
    """A worker double driven by the real reader and watcher threads; no OS process."""
    def __init__(self, *lines, wait_gate=None, wait_error=None):
        self.pid = 4242
        self.stdin = test_similar_decisions.SavedInput()
        self.exited = threading.Event()
        self.stdout = FakeStdout(lines, self.exited)
        self.wait_gate, self.wait_error = wait_gate, wait_error

    def exit(self):
        self.exited.set()

    def poll(self):
        return 0 if self.exited.is_set() else None

    def wait(self, timeout=None):
        self.exited.wait(10)
        if self.wait_gate is not None:
            self.wait_gate.wait(10)
        if self.wait_error is not None:
            raise self.wait_error
        return 0

    def terminate(self):
        self.exit()

    kill = terminate


class RealWatcherTests(unittest.TestCase):
    """Watcher ownership exercised with the real reader/watcher threads."""
    fake_agent = test_similar_decisions.SimilarDecisionTests.fake_agent

    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        from workspace_test_support import process_test_setup
        process_test_setup(self)
        # Fakes have no OS process: never let a stop reach taskkill.
        stop = patch.object(manager, '_stop_windows_tree', side_effect=lambda process: process.terminate())
        stop.start()
        self.addCleanup(stop.stop)

    def tearDown(self):
        for process in list(manager.agents.values()):
            if isinstance(process, FakeProcess):
                process.exit()
                if isinstance(getattr(process, '_control_center_watcher', None), threading.Thread):
                    process._control_center_watcher.join(10)
        test_similar_decisions.SimilarDecisionTests.tearDown(self)
    waiting_agent = test_similar_decisions.SimilarDecisionTests.waiting_agent

    MARKERS = {'always': '--- Automatic Always Decision ---', 'similar': '--- Automatic Similar Decision ---'}

    def policy_episode(self, mode, first):
        """Reply to a waiting agent; the resumed fake worker asks a question its policy answers."""
        agent_id, _ = self.waiting_agent()
        if mode == 'always':
            manager.agent_always_decisions[agent_id].enabled = True
            manager.agent_always_decisions[agent_id].configured = True
        else:
            manager.agent_similar_decisions[agent_id].enabled = True
            manager.agent_similar_decisions[agent_id].examples.append('Which local variable name?')
        # The reply's worker, then the automatic continuation's worker (held running).
        queue, self.spawned = [first, FakeProcess()], []
        self.addCleanup(lambda: [p.exit() for p in queue + self.spawned])

        def spawn(*_args, **_kwargs):
            self.spawned.append(queue.pop(0))
            return self.spawned[-1]
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.spawn = stack.enter_context(patch.object(manager, '_spawn_process', side_effect=spawn))
        stack.enter_context(patch.object(manager, '_codex_command', return_value='fixed command'))
        self.notify = stack.enter_context(patch.object(manager.notifications, 'transition'))
        manager.reply_agent(agent_id, 'Use x')
        self.assertIsInstance(first._control_center_watcher, threading.Thread)
        return agent_id

    def until(self, condition, message):
        deadline = threading.Event()
        for _ in range(500):
            if condition():
                return
            deadline.wait(0.01)
        self.fail(message)

    def join_threads(self, process):
        process._control_center_watcher.join(10)
        self.assertFalse(process._control_center_watcher.is_alive())

    def test_watcher_continues_always_and_similar_exactly_once(self):
        for mode in ('always', 'similar'):
            with self.subTest(mode=mode):
                first = FakeProcess('codex\n', 'CONTROL_CENTER_WAITING: Which file next?\n')
                agent_id = self.policy_episode(mode, first)
                first.exit()
                self.join_threads(first)
                self.assertEqual(self.spawn.call_count, 2)  # The reply, then one automatic continuation.
                follower = self.spawned[1]
                self.assertIs(manager.agents[agent_id], follower)
                self.assertEqual(manager.agent_statuses[agent_id], 'running')
                self.assertIn('Which file next?', follower.stdin.saved)
                self.assertEqual(manager.agent_outputs[agent_id].count(self.MARKERS[mode]), 1)
                self.notify.assert_not_called()
                for _ in range(3):
                    manager.get_agent(agent_id)
                    manager.get_agents()
                self.assertEqual(self.spawn.call_count, 2)

    def test_concurrent_reads_cannot_start_or_duplicate_continuation(self):
        for mode in ('always', 'similar'):
            with self.subTest(mode=mode):
                gate = threading.Event()
                first = FakeProcess('codex\n', 'CONTROL_CENTER_WAITING: Which file next?\n', wait_gate=gate)
                agent_id = self.policy_episode(mode, first)
                first.exit()
                # Exited and drained, while the watcher is still inside process.wait().
                self.until(lambda: first.stdout.closed, 'reader did not drain')
                self.assertTrue(first._control_center_watcher.is_alive())
                errors, stop = [], threading.Event()

                def reader():
                    try:
                        while not stop.is_set():
                            manager.get_agent(agent_id)
                            manager.get_agents()
                            manager.get_agent_output(agent_id)
                            call('GET', f'/agents/{agent_id}', headers(token=None))
                    except Exception as error:  # surfaced below
                        errors.append(error)
                readers = [threading.Thread(target=reader) for _ in range(6)]
                for thread in readers:
                    thread.start()
                threading.Event().wait(0.3)
                self.assertEqual(self.spawn.call_count, 1)  # Reads alone launched nothing.
                self.assertEqual(manager.agent_statuses[agent_id], 'running')
                gate.set()  # Watcher finalizes while the reads continue.
                self.join_threads(first)
                threading.Event().wait(0.2)
                stop.set()
                for thread in readers:
                    thread.join(10)
                self.assertEqual(errors, [])
                self.assertEqual(self.spawn.call_count, 2)
                self.assertEqual(manager.agent_outputs[agent_id].count(self.MARKERS[mode]), 1)
                self.assertIs(manager.agents[agent_id], self.spawned[1])

    def test_failed_watcher_is_reported_and_settled_without_continuation(self):
        for mode in ('always', 'similar'):
            with self.subTest(mode=mode):
                first = FakeProcess('codex\n', 'CONTROL_CENTER_WAITING: Which file next?\n',
                                    wait_error=OSError('wait failed'))
                failures = []
                with patch.object(threading, 'excepthook', side_effect=failures.append):
                    agent_id = self.policy_episode(mode, first)
                    first.exit()
                    self.join_threads(first)
                self.assertEqual([type(f.exc_value) for f in failures], [OSError])
                # Exited, unfinalized and unowned: without a read it would look running forever.
                self.assertEqual(manager.agent_statuses[agent_id], 'running')
                with self.assertLogs(manager.logger, level='ERROR') as logs:
                    detail = manager.get_agent(agent_id)
                self.assertIn('ended without finalizing', '\n'.join(logs.output))
                self.assertEqual(detail['status'], 'waiting')
                self.assertEqual(detail['waiting_question'], 'Which file next?')
                self.assertEqual(self.spawn.call_count, 1)  # No automatic continuation from a read.
                self.assertEqual(manager.agent_outputs[agent_id].count(self.MARKERS[mode]), 0)
                self.notify.assert_called_once_with(agent_id, 'waiting', 'Original task')
                # Reported once; further reads are quiet and still launch nothing.
                with self.assertNoLogs(manager.logger, level='ERROR'):
                    manager.get_agents()
                    manager.get_agent(agent_id)
                self.assertEqual(self.spawn.call_count, 1)

    def test_failed_structured_watcher_is_reported_but_left_for_recovery(self):
        agent_id, _ = self.fake_agent()
        process = FakeProcess()
        process._control_center_execution = Execution()
        process.exit()
        watcher = threading.Thread(target=lambda: None)
        watcher.start()
        watcher.join()
        process._control_center_watcher = watcher
        process.stdout.closed = True
        manager.agents[agent_id] = process
        with patch.object(manager, '_finish_structured') as finish, \
             self.assertLogs(manager.logger, level='ERROR') as logs:
            manager.get_agent(agent_id)
        finish.assert_not_called()
        self.assertIn('ended without finalizing', '\n'.join(logs.output))
        # Durable structured handoff after a lost watcher is Phase 2 recovery work.
        self.assertEqual(manager.agent_statuses[agent_id], 'running')


class ReadAuditTests(unittest.TestCase):
    """Every GET route against a real temporary SQLite store and Git repositories."""
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    replacement = test_persistence.PersistenceTests.replacement
    create = test_persistence.PersistenceTests.create
    output = test_persistence.PersistenceTests.output
    resume = test_persistence.PersistenceTests.resume
    task_for = test_task_lifecycle.TaskLifecycleTests.task_for

    def database(self):
        return Path(self.path).read_bytes()

    def test_get_routes_write_no_database_rows_and_launch_no_agents(self):
        idle, _ = self.create()
        self.output(idle, f'session id: {idle}\nCONTROL_CENTER_WAITING: Question\n', True)
        policy, _ = self.create()
        self.output(policy, f'session id: {policy}\nCONTROL_CENTER_WAITING: Example\n', True)
        self.resume(policy, manager.decide_always_agent)
        # Exited, unwatched, with Always Decide due: only its watcher may continue it.
        self.output(policy, 'CONTROL_CENTER_WAITING: Next\n')
        manager.agents[policy].poll.return_value = 0
        task = self.task_for(idle)
        values = {'agent_id': policy, 'task_id': task['task_id'], 'project_id': task['project_id']}
        before = self.database()
        git_commands = []
        real_popen = workspace_git.Popen

        def record_git(args, **options):
            command = args[args.index('-C') + 2:]
            git_commands.append(' '.join(command[:2]) if command[0] == 'worktree' else command[0])
            return real_popen(args, **options)
        with ExitStack() as stack:
            launched = ReadSideEffectTests.no_launch(stack)
            agent_popen = stack.enter_context(patch.object(manager.subprocess, 'Popen', side_effect=AssertionError))
            stack.enter_context(patch.object(workspace_git, 'Popen', side_effect=record_git))
            for agent_id in (idle, policy):
                values['agent_id'] = agent_id
                for route in sorted(GET_ROUTES - {'/events'}):
                    path = route
                    for name in ('agent_id', 'task_id', 'project_id', 'integration_id', 'deadlock_id', 'delegation_id', 'removal_id',
                             'execution_id'):
                        path = path.replace('{' + name + '}', values.get(name, 'missing'))
                    with self.subTest(route=route, agent=agent_id):
                        status, _, _ = call('GET', path, headers(token=None))
                        self.assertNotEqual(status, 500)
            for mock in launched.values():
                mock.assert_not_called()
            agent_popen.assert_not_called()
        self.assertEqual(self.database(), before)
        self.assertEqual(manager.agent_statuses[policy], 'running')
        # Only /tasks/{task_id}/workspace runs Git: read-only queries plus
        # content-addressed object hashing into a temporary index.
        self.assertTrue(set(git_commands) <= {'rev-parse', 'worktree list', 'ls-files', 'read-tree', 'hash-object',
                                              'update-index', 'write-tree', 'diff-tree'}, git_commands)


if __name__ == '__main__':
    unittest.main()
