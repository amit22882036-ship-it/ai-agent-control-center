import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import uuid4

from app import agent_manager as manager, delegation_protocol as protocol, delegation_handoff
from app.codex_events import CodexEvents, Execution, StructuredCommand
from app.persistence import AgentStore
import test_delegations


def envelope(*keys):
    return json.dumps(dict(protocol=protocol.PROTOCOL, action='request_delegations',
                           requests=[dict(request_key=k, instruction='Work on ' + k) for k in keys or ('one',)]))


def events(message, session=None, ending='turn.completed'):
    return [dict(type='thread.started', thread_id=session or str(uuid4())), dict(type='turn.started'),
            dict(type='item.completed', item=dict(id='item_1', type='agent_message', text=message)),
            dict(type=ending, usage=dict(input_tokens=10, cached_input_tokens=0, output_tokens=5))]


def feed(adapter, stream):
    return [line for event in stream for line in adapter.feed(json.dumps(event))]


class ProtocolTests(unittest.TestCase):
    def test_valid_single_and_batch(self):
        self.assertEqual(len(protocol.validate(envelope('a'))), 1)
        self.assertEqual(len(protocol.validate(envelope('a', 'b'))), 2)

    def test_invalid_schema_and_limits(self):
        valid = json.loads(envelope())
        invalid = [None, [], {}, {**valid, 'identity': 'spoof'}, {**valid, 'protocol': 'v2'},
                   {**valid, 'action': 'execute'}, {**valid, 'requests': []},
                   {**valid, 'requests': valid['requests'] * 17},
                   {**valid, 'requests': [{'request_key': 'a', 'instruction': ' '}]},
                   {**valid, 'requests': [{'request_key': 'a', 'instruction': 'x' * 32769}]},
                   {**valid, 'requests': [{'request_key': ' a', 'instruction': 'x'}]},
                   {**valid, 'requests': [{'request_key': 'a', 'instruction': 'x', 'agent_id': 'spoof'}]}]
        for value in invalid:
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                protocol.validate(json.dumps(value))
        for text in ('{', '{"protocol":1,"protocol":2}', 'NaN', 'x' * (protocol.MAX_PAYLOAD + 1),
                     envelope('a', 'a')):
            with self.subTest(text=text[:100]), self.assertRaises(ValueError):
                protocol.validate(text)

    def test_embedded_examples_are_not_envelopes(self):
        for text in ('Here is an example: ' + envelope(), '```json\n' + envelope() + '\n```', 'Done.'):
            self.assertFalse(protocol.is_envelope(text))


class AdapterTests(unittest.TestCase):
    def test_windows_command_keeps_json_resume_and_stdin_options(self):
        session = str(uuid4())
        with patch.object(manager.os, 'name', 'nt'), patch.object(manager.Path, 'is_file', return_value=True), \
             patch.dict(manager.os.environ, APPDATA='C:\\npm-fixture', SystemRoot='C:\\Windows'):
            command = manager._codex_command('workspace-write', session, structured=True)
        self.assertIsInstance(command, StructuredCommand)
        self.assertIn('--sandbox workspace-write --color never --skip-git-repo-check --json resume ' + session + ' -', command)
        self.assertIn('npm\\codex.cmd', command)
        with patch.object(manager.subprocess, 'Popen') as spawn:
            process = manager._spawn_process(command, 'codex', cwd='fixture')
        self.assertIsNotNone(manager._structured(process))
        self.assertIs(spawn.call_args.kwargs['shell'], False)
        self.assertEqual(spawn.call_args.kwargs['stderr'], manager.subprocess.PIPE)
        self.assertEqual(spawn.call_args.kwargs['stdin'], manager.subprocess.PIPE)

    def test_message_and_session_require_successful_turn(self):
        stream = events(envelope())
        adapter = CodexEvents()
        readable = feed(adapter, stream)
        self.assertEqual(adapter.finish(0), envelope())
        self.assertEqual(adapter.session_id, stream[0]['thread_id'])
        self.assertIn('codex', readable)
        self.assertIn(envelope(), readable)

    def test_duplicate_completed_events_are_not_rehandled(self):
        stream = events(envelope())
        adapter = CodexEvents()
        feed(adapter, stream)
        self.assertEqual(feed(adapter, [stream[2], stream[3]]), [])
        self.assertEqual(adapter.finish(0), envelope())

    def test_tool_reasoning_and_file_output_are_never_messages(self):
        for kind in ('command_execution', 'reasoning', 'file_change', 'mcp_tool_call'):
            with self.subTest(kind=kind):
                adapter = CodexEvents()
                stream = events('Normal response')
                stream.insert(2, dict(type='item.completed', item=dict(id='tool', type=kind,
                                     text=envelope(), aggregated_output=envelope())))
                feed(adapter, stream)
                self.assertFalse(adapter.saw_request)
                self.assertEqual(adapter.finish(0), 'Normal response')

    def test_incomplete_failed_and_malformed_streams_fail_closed(self):
        good = events(envelope())
        bad = [good[:-1], good[1:], good[:2], events(envelope(), ending='turn.failed'),
               [*good, {'type': 'error', 'message': 'interrupted'}],
               [*good[:2], {'type': 'unexpected'}, *good[2:]],
               [*good[:2], {'type': 'item.completed', 'item': {'id': 'x', 'type': 'agent_message', 'text': 2}}, good[-1]],
               [*good, {'type': 'turn.started'}],
               [*good[:2], {'type': 'item.updated', 'item': good[2]['item']}, good[-1]],
               [*good[:3], {'type': 'turn.completed'}]]
        for stream in bad:
            with self.subTest(stream=stream):
                adapter = CodexEvents()
                feed(adapter, stream)
                with self.assertRaises(ValueError):
                    adapter.finish(0)
        for line in ('not JSON', '{"type":"error","type":"turn.completed"}', '[]',
                     '{"type":"thread.started","thread_id":42}'):
            adapter = CodexEvents()
            feed(adapter, good)
            adapter.feed(line)
            with self.assertRaises(ValueError):
                adapter.finish(0)

    def test_failed_exit_or_mismatched_session_rejected(self):
        adapter = CodexEvents(expected_session=str(uuid4()))
        feed(adapter, events(envelope()))
        with self.assertRaises(ValueError):
            adapter.finish(0)
        adapter = CodexEvents()
        feed(adapter, events(envelope()))
        with self.assertRaises(ValueError):
            adapter.finish(1)

    def test_designated_message_is_final_and_conflicting_item_rejected(self):
        stream = events(envelope())
        other = dict(type='item.completed', item=dict(id='other', type='agent_message', text='Done'))
        for last in (other, {**other, 'item': {**other['item'], 'id': 'item_1'}}):
            adapter = CodexEvents()
            feed(adapter, [*stream[:-1], last, stream[-1]])
            with self.assertRaises(ValueError):
                adapter.finish(0)


class HandoffTests(unittest.TestCase):
    setUp = test_delegations.DelegationTests.setUp
    agent = test_delegations.DelegationTests.agent
    task = test_delegations.DelegationTests.task
    worker = test_delegations.DelegationTests.worker
    snapshot = test_delegations.DelegationTests.snapshot

    def origin_context(self):
        return self.store.begin_delegation_execution(self.origin['agent_id'], str(uuid4()))

    def accept(self, origin=None, message=None):
        origin = origin or self.origin_context()
        session = next(r for r in self.store.load_agents() if r['agent_id'] == origin.agent_id)['session_id']
        return self.store.accept_delegation_message(origin, message or envelope(), session_id=session, message_id='item_1')

    def test_handoff_atomically_preserves_assignment_and_incomplete_parent(self):
        before_tasks = self.store.list_tasks()
        receipt = self.accept(message=envelope('a', 'b'))
        task = self.store.get_task(self.parent['task_id'])
        self.assertEqual(task['status'], 'blocked')
        self.assertEqual(task['orchestration_handoff'], receipt)
        self.assertEqual(len(receipt['delegation_ids']), 2)
        self.assertEqual(self.store.get_active_assignment_for_task(task['task_id']), self.origin)
        self.assertEqual(len(self.store.list_tasks()), len(before_tasks))
        row = self.store.load_agents()[0]
        self.assertEqual(row['status'], 'stopped')
        self.assertIsNone(row['waiting_question'])

    def test_idempotent_receipt_and_original_record_provenance(self):
        original = self.store.create_delegation(self.parent['task_id'], project_id=self.project['project_id'],
                   requested_by_agent_id=self.origin['agent_id'], requested_by_assignment_id=self.origin['assignment_id'],
                   request_key='one', instruction='Work on one')
        context = self.origin_context()
        receipt = self.accept(context)
        before = self.snapshot()
        self.assertEqual(self.accept(context), receipt)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.list_delegations(self.parent['task_id']), [original])

    def test_conflicting_batch_rolls_back_first_insert(self):
        self.store.create_delegation(self.parent['task_id'], project_id=self.project['project_id'],
                   requested_by_agent_id=self.origin['agent_id'], requested_by_assignment_id=self.origin['assignment_id'],
                   request_key='b', instruction='Existing')
        context = self.origin_context()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.accept(context, envelope('a', 'b'))
        self.assertEqual(self.snapshot(), before)

    def test_mixed_invalid_batch_has_no_mutation(self):
        context = self.origin_context()
        before = self.snapshot()
        payload = json.loads(envelope('a', 'b'))
        payload['requests'][1]['instruction'] = ''
        with self.assertRaises(ValueError):
            self.accept(context, json.dumps(payload))
        self.assertEqual(self.snapshot(), before)

    def test_replacement_generation_and_assignment_reject_stale_origin(self):
        context = self.origin_context()
        self.origin_context()
        with self.assertRaises(ValueError):
            self.accept(context)
        context = self.origin_context()
        self.store.end_assignment(self.origin['assignment_id'], 'stopped')
        self.worker(self.parent)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.accept(context)
        self.assertEqual(self.snapshot(), before)

    def test_pause_and_cancel_reject_before_mutation(self):
        context = self.origin_context()
        for intent in ('paused', 'canceled'):
            self.store.request_work_control(self.parent['task_id'], intent)
            before = self.snapshot()
            with self.assertRaises(ValueError):
                self.accept(context)
            self.assertEqual(self.snapshot(), before)

    def test_session_and_project_identity_cannot_be_forged(self):
        from dataclasses import replace
        context = self.origin_context()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.store.accept_delegation_message(context, envelope(), session_id=str(uuid4()), message_id='item_1')
        with self.assertRaises(ValueError):
            self.accept(replace(context, project_id=str(uuid4())))
        self.assertEqual(self.snapshot(), before)

    def test_canceled_request_retry_keeps_original_provenance(self):
        row = self.store.create_delegation(self.parent['task_id'], project_id=self.project['project_id'],
                   requested_by_agent_id=self.origin['agent_id'], requested_by_assignment_id=self.origin['assignment_id'],
                   request_key='one', instruction='Work on one')
        row = self.store.transition_delegation(row['delegation_id'], 'canceled')
        self.store.end_assignment(self.origin['assignment_id'], 'stopped')
        self.origin = self.worker(self.parent)
        self.accept()
        self.assertEqual(self.store.get_delegation(row['delegation_id']), row)

    def test_concurrent_receipts_use_one_atomic_batch(self):
        context = self.origin_context()
        barrier = Barrier(2)
        session = self.store.load_agents()[0]['session_id']
        def submit(_):
            store = AgentStore(self.path)
            barrier.wait(timeout=5)
            return store.accept_delegation_message(context, envelope('a', 'b'), session_id=session, message_id='item_1')
        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(submit, range(2)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(len(self.store.list_delegations(self.parent['task_id'])), 2)

    def test_restart_and_pause_resume_preserve_handoff_without_workers(self):
        receipt = self.accept()
        self.store = AgentStore(self.path)
        self.store.reconcile_task_recovery()
        self.assertEqual(self.store.get_task(self.parent['task_id'])['orchestration_handoff'], receipt)
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']), self.origin)
        self.store.request_work_control(self.parent['task_id'], 'paused')
        self.store.finalize_work_control(self.parent['task_id'])
        self.store.request_work_control(self.parent['task_id'], 'active')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'], 'blocked')
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']), self.origin)
        self.store.request_work_control(self.parent['task_id'], 'canceled')
        self.store.finalize_work_control(self.parent['task_id'])
        self.assertIsNone(self.store.get_active_assignment_for_task(self.parent['task_id']))
        self.assertEqual(self.store.get_task(self.parent['task_id'])['orchestration_handoff'], receipt)

    def test_failure_between_records_and_handoff_rolls_back(self):
        context = self.origin_context()
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_handoff BEFORE UPDATE OF orchestration_handoff ON tasks BEGIN SELECT RAISE(ABORT,'failure'); END")
        before = self.snapshot()
        with self.assertRaises(sqlite3.Error):
            self.accept(context, envelope('a', 'b'))
        self.assertEqual(self.snapshot(), before)

    def test_real_v18_migration_and_interruption_rollback(self):
        with self.store._connection() as db:
            for table, column in [('agents', 'execution_generation'), ('agents', 'delegation_protocol_enabled'), ('tasks', 'orchestration_handoff')]:
                db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
            db.execute('PRAGMA user_version=18')
        before = self.snapshot()
        original = delegation_handoff.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('migration interrupted')
        with patch.object(delegation_handoff, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        self.assertEqual(self.snapshot(), before)
        self.store = AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 20)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        self.assertIsNone(self.store.get_task(self.parent['task_id'])['orchestration_handoff'])
        self.assertFalse(self.store.execution_settings(self.origin['agent_id']))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AgentStore(self.root / 'test.sqlite3')
        self.project = self.store.create_project('Fixture', self.root)
        self.task = self.store.create_task('Parent work', project_id=self.project['project_id'])
        for name, value in vars(manager).items():
            if isinstance(value, dict) and (name == 'agents' or name.startswith('agent_')):
                self.patch(patch.dict(value, {}, clear=True))
        self.patch(patch.object(manager, '_store', self.store))
        self.patch(patch.object(manager, '_shutting_down', False))
        self.patch(patch.object(manager, '_execution_workspace', return_value=self.root))
        self.patch(patch.object(manager, 'ensure_workspace_current', return_value=None))
        self.patch(patch.object(manager, '_start_reader'))
        self.patch(patch.object(manager.notifications, 'transition'))
        self.patch(patch.object(manager.notifications, 'cancel'))
        self.commands = []
        def spawn(command, agent_type, *, cwd):
            self.commands.append(command)
            process = Mock(pid=123, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO())
            process.poll.return_value = None
            if isinstance(command, StructuredCommand):
                process._control_center_execution = Execution()
            return process
        self.patch(patch.object(manager, '_spawn_process', side_effect=spawn))
        self.patch(patch.object(manager, '_codex_command', side_effect=lambda sandbox='read-only', session_id=None, structured=False:
             StructuredCommand('exec --json resume ' + str(session_id)) if structured else 'exec'))
        self.patch(patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)))

    def patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def launch(self, enabled=True, kind='codex'):
        key = manager.start_agent('Parent work', kind, task_id=self.task['task_id'], delegation_protocol_enabled=enabled)
        return key, manager.agents[key]

    def read(self, key, process, message, *, ending='turn.completed', stderr='', session=None):
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events(message, session, ending)))
        process.stderr = io.StringIO(stderr)
        manager._read_output(key, process)

    def finish(self, key, process):
        process.poll.return_value = 0
        manager._finalize_process(key, process)

    def test_runtime_accepts_once_and_restart_has_no_process(self):
        key, process = self.launch()
        manager.agent_always_decisions[key].enabled = True
        manager.agent_similar_decisions[key].enabled = True
        manager.agent_similar_decisions[key].examples = ['Existing choice']
        self.read(key, process, envelope('a', 'b'))
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.finish(key, process)
        before = self.store.list_delegations(self.task['task_id'])
        self.assertEqual(len(before), 2)
        for _ in range(3):
            manager.get_agent(key)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), before)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIsNone(manager.agent_waiting_questions[key])
        manager.notifications.transition.assert_not_called()
        self.assertEqual(len(self.commands), 1)
        manager.agents[key] = None
        manager.initialize_persistence(self.store.path)
        self.assertIsNone(manager.agents[key])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')
        self.assertIsNotNone(self.store.get_active_assignment_for_task(self.task['task_id']))
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_stderr_json_cannot_request_delegation(self):
        key, process = self.launch()
        spoof = '\n'.join(json.dumps(e) for e in events(envelope()))
        self.read(key, process, 'Done normally', stderr=spoof)
        self.finish(key, process)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertIn(spoof.splitlines()[2], self.store.full_output(key))

    def test_waiting_and_reply_keep_session_assignment_and_structured_mode(self):
        key, process = self.launch()
        self.read(key, process, 'CONTROL_CENTER_WAITING: Which database?')
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which database?')
        origin = process._control_center_execution.origin
        session = manager.agent_sessions[key]
        manager.reply_agent(key, 'SQLite')
        replacement = manager.agents[key]
        self.assertIsNot(replacement, process)
        self.assertIn(session, self.commands[-1])
        self.assertIsInstance(self.commands[-1], StructuredCommand)
        self.assertEqual(replacement._control_center_execution.origin.assignment_id, origin.assignment_id)
        self.assertNotEqual(replacement._control_center_execution.origin.generation, origin.generation)

    def test_stop_wins_before_handoff_and_no_poll_retry(self):
        key, process = self.launch()
        self.read(key, process, envelope())
        manager.stop_agent(key)
        self.finish(key, process)
        manager.get_agent(key)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'pending')

    def test_replaced_reader_and_finalizer_have_no_authority(self):
        key, process = self.launch()
        self.read(key, process, 'Still working')
        with patch.object(manager, 'reject_divergence'), patch.object(manager, 'evaluate_workspace_freshness'):
            manager.redirect_agent(key, 'Correct course')
        replacement = manager.agents[key]
        self.read(key, process, envelope())
        self.finish(key, process)
        self.assertIs(manager.agents[key], replacement)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertEqual(manager.agent_statuses[key], 'running')

    def test_db_failure_before_commit_never_completes_parent(self):
        key, process = self.launch()
        self.read(key, process, envelope())
        with patch.object(self.store, 'accept_delegation_message', side_effect=sqlite3.OperationalError('failure')):
            self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'pending')
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_failure_after_commit_preserves_handoff(self):
        key, process = self.launch()
        self.read(key, process, envelope())
        original = self.store.accept_delegation_message
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('lost acknowledgement')
        with patch.object(self.store, 'accept_delegation_message', side_effect=lost_ack):
            self.finish(key, process)
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')
        self.assertEqual(len(self.store.list_delegations(self.task['task_id'])), 1)
        self.assertIsNotNone(self.store.get_active_assignment_for_task(self.task['task_id']))

    def test_unsuccessful_turn_never_completes_or_asks_user(self):
        key, process = self.launch()
        self.read(key, process, 'CONTROL_CENTER_WAITING: question', ending='turn.failed')
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIsNone(manager.agent_waiting_questions[key])
        manager.notifications.transition.assert_not_called()

    def test_pipe_failure_after_turn_completion_is_not_accepted(self):
        class BrokenStream(io.StringIO):
            def __next__(self):
                try:
                    return super().__next__()
                except StopIteration:
                    raise OSError('pipe failure')
        for pipe in ('stdout', 'stderr'):
            self.task = self.store.create_task('Parent work', project_id=self.project['project_id'])
            key, process = self.launch()
            process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events(envelope())))
            content = process.stdout.getvalue() if pipe == 'stdout' else ''
            setattr(process, pipe, BrokenStream(content))
            manager._read_output(key, process)
            self.finish(key, process)
            self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
            self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_decide_similar_and_always_preserve_structured_resumes(self):
        for action in (manager.decide_agent, manager.decide_similar_agent, manager.decide_always_agent):
            with self.subTest(action=action.__name__):
                # Independent pending Task for each real manager flow.
                self.task = self.store.create_task('Parent work', project_id=self.project['project_id'])
                key, process = self.launch()
                self.read(key, process, 'CONTROL_CENTER_WAITING: Which approach?')
                self.finish(key, process)
                assignment = self.store.get_active_assignment_for_agent(key)
                action(key)
                self.assertIsInstance(self.commands[-1], StructuredCommand)
                self.assertEqual(self.store.get_active_assignment_for_agent(key), assignment)
                self.assertEqual(manager.agent_statuses[key], 'running')

    def test_genuine_handled_markers_and_tool_spoofs(self):
        key, process = self.launch()
        manager.agent_similar_decisions[key].automatic_question = 'Question'
        manager.agent_always_decisions[key].automatic_attempt = True
        text = 'CONTROL_CENTER_SIMILAR_HANDLED\nCONTROL_CENTER_ALWAYS_HANDLED'
        stream = events('Done')
        stream.insert(2, dict(type='item.completed', item=dict(id='tool', type='command_execution', aggregated_output=text)))
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in stream))
        manager._read_output(key, process)
        self.finish(key, process)
        self.assertFalse(manager.agent_similar_decisions[key].handled)
        self.assertFalse(manager.agent_always_decisions[key].handled)
        self.task = self.store.create_task('Parent work', project_id=self.project['project_id'])
        key, process = self.launch()
        manager.agent_similar_decisions[key].automatic_question = 'Question'
        manager.agent_always_decisions[key].automatic_attempt = True
        self.read(key, process, text)
        self.finish(key, process)
        self.assertTrue(manager.agent_similar_decisions[key].handled)
        self.assertTrue(manager.agent_always_decisions[key].handled)

    def test_changed_generation_cannot_write_or_publish_old_output(self):
        key, process = self.launch()
        self.store.begin_delegation_execution(key, str(uuid4()))
        before = self.store.full_output(key)
        with patch.object(manager, '_emit_agent_change') as emit:
            self.read(key, process, envelope())
            self.finish(key, process)
        self.assertEqual(self.store.full_output(key), before)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        emit.assert_not_called()
        self.assertEqual(self.store.load_agents()[0]['status'], 'running')

    def test_stale_waiting_worker_cannot_resume_over_new_generation(self):
        key, process = self.launch()
        self.read(key, process, 'CONTROL_CENTER_WAITING: Choice?')
        self.finish(key, process)
        newer = self.store.begin_delegation_execution(key, str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'stale execution'):
            manager.reply_agent(key, 'Choose this')
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(self.store.execution_is_current(newer))

    def test_pause_cancel_before_finalizer_cannot_delegate(self):
        for intent in ('paused', 'canceled'):
            self.task = self.store.create_task('Parent work', project_id=self.project['project_id'])
            key, process = self.launch()
            self.read(key, process, envelope())
            manager.control_task(self.task['task_id'], intent)
            self.finish(key, process)
            self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
            self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')

    def test_commit_precedes_sse_and_publication_failure_retains_handoff(self):
        key, process = self.launch()
        self.read(key, process, envelope())
        def publish(*_):
            task = self.store.get_task(self.task['task_id'])
            self.assertIsNotNone(task['orchestration_handoff'])
            self.assertEqual(len(self.store.list_delegations(task['task_id'])), 1)
            raise RuntimeError('SSE unavailable')
        with patch.object(manager.changes, 'publish', side_effect=publish):
            self.finish(key, process)
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')

    def test_start_bind_failure_compensates_without_prompt_delivery(self):
        with patch.object(self.store, 'begin_delegation_execution', side_effect=sqlite3.OperationalError('failure')):
            with self.assertRaises(sqlite3.Error):
                self.launch()
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'pending')
        self.assertIsNone(self.store.get_active_assignment_for_task(self.task['task_id']))
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_stop_branch_revokes_ready_handoff(self):
        key, process = self.launch()
        self.read(key, process, envelope())
        result = manager.stop_branch(key)
        self.assertTrue(result['ok'])
        self.finish(key, process)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_concurrent_stop_and_finalizer_have_one_consistent_outcome(self):
        key, process = self.launch()
        self.read(key, process, envelope('a', 'b'))
        process.poll.return_value = 0
        barrier = Barrier(2)
        def act(stop):
            barrier.wait(timeout=5)
            return manager.stop_agent(key) if stop else manager._finalize_process(key, process)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(act, (True, False)))
        rows = self.store.list_delegations(self.task['task_id'])
        self.assertIn(len(rows), (0, 2))
        task = self.store.get_task(self.task['task_id'])
        self.assertEqual(task['status'], 'blocked' if rows else 'pending')
        self.assertEqual(bool(task['orchestration_handoff']), bool(rows))
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_failed_always_prompt_restores_waiting_generation_and_history(self):
        key, process = self.launch()
        self.read(key, process, 'CONTROL_CENTER_WAITING: Choice?')
        self.finish(key, process)
        previous = process._control_center_execution.origin
        history = self.store.full_output(key)
        replacement = Mock(pid=123, stdout=io.StringIO(), stderr=io.StringIO(), stdin=Mock())
        replacement.stdin.__enter__ = Mock(return_value=replacement.stdin)
        replacement.stdin.__exit__ = Mock(return_value=False)
        replacement.stdin.write.side_effect = OSError('broken pipe')
        replacement.poll.return_value = None
        replacement._control_center_execution = Execution()
        reader = Mock()
        reader.is_alive.return_value = False
        def start_reader(agent_id, child):
            manager.agent_readers[agent_id] = reader
        with patch.object(manager, '_spawn_process', return_value=replacement), \
             patch.object(manager, '_start_reader', side_effect=start_reader):
            with self.assertRaises(OSError):
                manager.decide_always_agent(key)
        self.assertIs(manager.agents[key], process)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertTrue(self.store.execution_is_current(previous))
        self.assertEqual(self.store.full_output(key), history)

    def test_mock_default_keeps_human_output_behavior(self):
        key, process = self.launch(False, 'mock')
        process.stdout = io.StringIO('Agent started\nAgent finished\n')
        manager._read_output(key, process)
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertFalse(self.store.execution_settings(key))
        self.assertEqual(self.store.full_output(key), ['Agent started', 'Agent finished'])


if __name__ == '__main__':
    unittest.main()
