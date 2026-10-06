from contextlib import closing
import asyncio
import json
import io
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Thread
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from app import agent_manager as manager
from app.main import app
from app.persistence import AgentStore
import test_similar_decisions


class PersistenceTests(unittest.TestCase):
    replacement = test_similar_decisions.SimilarDecisionTests.replacement
    make_launcher = test_similar_decisions.SimilarDecisionTests.make_launcher
    wait_until_waiting = test_similar_decisions.SimilarDecisionTests.wait_until_waiting

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'data' / 'test.sqlite3'
        self.assertIsNone(manager._store)
        from workspace_test_support import make_repository
        root = make_repository(Path(self.temp.name) / 'canonical')
        patch.object(manager, '_project_root', root).start()
        patch('app.persistence.DEFAULT_ROOT', root).start()
        patch.dict(os.environ, {'CONTROL_CENTER_WORKSPACE_ROOT': str(Path(self.temp.name) / 'workspaces')}).start()
        manager.initialize_persistence(self.path)
        self.notify = patch.object(manager.notifications, 'transition').start()
        self.cancel = patch.object(manager.notifications, 'cancel').start()

    def tearDown(self):
        for process in manager.agents.values():
            if process is not None:
                process.stdout.close()
                if not process.stdin.closed:
                    process.stdin.close()
        manager._store = None
        manager._shutting_down = False
        for registry in (manager.agents, manager.agent_parents, manager.agent_statuses,
                         manager.agent_outputs, manager.agent_tasks, manager.agent_types,
                         manager.agent_sandboxes, manager.agent_sessions, manager.agent_waiting_questions,
                         manager.agent_readers, manager.agent_similar_decisions, manager.agent_always_decisions):
            registry.clear()
        patch.stopall()
        self.temp.cleanup()

    def create(self, parent=None, task='Task', kind='codex', sandbox='read-only'):
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        with patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, 'Thread'):
            key = manager.start_agent(task, kind, sandbox, parent_id=parent)
        manager.agent_readers[key].is_alive.return_value = False
        return key, process

    def output(self, key, text, finish=False):
        process = manager.agents[key]
        process.stdout = io.StringIO(text.replace('CONTROL_CENTER_WAITING:', 'codex\nCONTROL_CENTER_WAITING:'))
        manager._read_output(key, process)
        if finish:
            process.poll.return_value = 0
            manager._finalize_process(key, process, allow_auto=False)

    def recover(self):
        # Simulate loss of process ownership. Tests use mocks, never orphan real AI work.
        for key in manager.agents:
            manager.agents[key] = None
        manager.initialize_persistence(self.path)

    def resume(self, key, action):
        replacement = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed command') as command, \
             patch.object(manager.subprocess, 'Popen', return_value=replacement) as popen, \
             patch.object(manager, 'Thread'):
            result = action(key)
        manager.agent_readers[key].is_alive.return_value = False
        return result, replacement, command, popen

    def test_schema_override_and_safe_round_trip(self):
        self.assertTrue(self.path.is_file())
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 12)
            self.assertEqual({row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")},
                             {'agents', 'output', 'agent_name_history', 'sqlite_sequence', 'tasks', 'task_assignments', 'projects', 'task_workspaces', 'agent_source_context', 'integrations', 'task_dependencies', 'task_blockers', 'task_replan_reasons', 'task_control_operations', 'task_control_members'})
        with patch.dict(os.environ, {'CONTROL_CENTER_DB_PATH': str(self.path)}):
            self.assertEqual(AgentStore().path, self.path)
        text = "quotes '\"; DROP TABLE agents; --\nUnicode שלום 🐍"
        key, _ = self.create(task=text, sandbox='workspace-write')
        lines = ["'\"; SELECT * FROM agents", '', 'שלום 🐍', 'same', 'same']
        self.output(key, '\n'.join(lines) + '\n', finish=True)
        self.recover()
        detail = manager.get_agent(key)
        self.assertEqual(detail['task'], text)
        self.assertEqual(detail['output'], lines)
        self.assertEqual(detail['sandbox'], 'workspace-write')
        self.assertEqual(detail['agent_type'], 'codex')
        self.assertIsNone(detail['parent_id'])
        manager.initialize_persistence(self.path)
        self.assertEqual(manager.get_agent(key), detail)

    def test_future_and_incompatible_versions_do_not_destroy_data(self):
        for version in (2, 1, 0):
            path = Path(self.temp.name) / f'unsupported-{version}.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE important (value TEXT)')
                db.execute('INSERT INTO important VALUES (?)', ('keep me',))
                # Test-only numeric schema constant, never user data.
                db.execute(f'PRAGMA user_version={version}')
            with self.assertRaisesRegex(RuntimeError, 'schema|Unversioned'):
                AgentStore(path)
            with closing(sqlite3.connect(path)) as db, db:
                self.assertEqual(db.execute('SELECT value FROM important').fetchone()[0], 'keep me')

    def test_status_recovery_and_running_marker_once_without_notifications(self):
        keys = {}
        for status in ('running', 'waiting', 'finished', 'stopped'):
            key, _ = self.create()
            keys[status] = key
            if status == 'waiting':
                self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which file?\n', True)
            elif status == 'finished':
                self.output(key, 'Done\n', True)
            elif status == 'stopped':
                with patch.object(manager, '_stop_windows_tree'):
                    manager.stop_agent(key)
        self.notify.reset_mock()
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            first = manager.get_agent(keys['running'])['output']
            self.assertEqual(first.count('--- Backend Restart ---'), 1)
            self.recover()
            self.assertEqual(manager.get_agent(keys['running'])['output'], first)
            for status, key in keys.items():
                self.assertEqual(manager.get_agent(key)['status'], 'stopped' if status == 'running' else status)
                self.assertIsNone(manager.agents[key])
            spawn.assert_not_called()
            self.notify.assert_not_called()
        waiting = manager.get_agent(keys['waiting'])
        self.assertEqual(waiting['session_id'], keys['waiting'])
        self.assertEqual(waiting['waiting_question'], 'Which file?')

    def test_hierarchy_recovery_children_and_processless_stop_branch(self):
        root, _ = self.create(kind='mock')
        child, _ = self.create(root)
        grandchild, _ = self.create(child)
        unrelated, _ = self.create()
        self.output(child, f'session id: {child}\nCONTROL_CENTER_WAITING: Question\n', True)
        self.output(grandchild, 'Done\n', True)
        self.output(unrelated, 'CONTROL_CENTER_WAITING: Other question\n', True)
        self.recover()
        self.assertEqual([item['agent_id'] for item in manager.get_agents()], [root, child, grandchild, unrelated])
        self.assertEqual(manager.get_agent(root)['child_ids'], [child])
        self.assertEqual(manager.get_agent(child)['child_ids'], [grandchild])
        relationships = manager.agent_parents.copy()
        # Include a new live child in the same branch.
        fresh, _ = self.create(root, kind='mock')
        self.assertNotIn(fresh, relationships)
        result = manager.stop_branch(root)
        self.assertTrue(result['ok'])
        self.assertEqual([item['status'] for item in result['results']], ['finished', 'stopped', 'stopped', 'stopped'])
        self.assertEqual(manager.get_agent(unrelated)['status'], 'waiting')
        self.recover()
        self.assertEqual(manager.get_agent(child)['status'], 'stopped')
        self.assertEqual(manager.get_agent(grandchild)['status'], 'finished')
        for parent in (child, unrelated):
            new, _ = self.create(parent)
            self.assertEqual(manager.agent_parents[new], parent)
        # Stage 2F.1 forbids creating new work from completed parent work.
        with patch.object(manager, '_spawn_process') as spawn:
            with self.assertRaises(ValueError):
                manager.start_agent('Child', 'mock', parent_id=grandchild)
            spawn.assert_not_called()

    def test_recovered_waiting_actions_reuse_session_stdin_cwd_and_history(self):
        for action, marker in ((lambda key: manager.reply_agent(key, 'Reply & "literal"'), '--- User Reply ---'),
                               (manager.decide_agent, '--- Delegated Decision ---'),
                               (manager.decide_similar_agent, '--- Similar Decisions Enabled ---'),
                               (manager.decide_always_agent, '--- Always Decide Enabled ---')):
            key, _ = self.create(sandbox='workspace-write')
            self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which file?\n', True)
            history = manager.agent_outputs[key].copy()
            self.recover()
            with self.assertRaises(ValueError):
                manager.redirect_agent(key, 'Cannot redirect without a process')
            result, process, command, popen = self.resume(key, action)
            self.assertEqual(result, {'agent_id': key, 'status': 'running'})
            command.assert_called_once_with('workspace-write', key)
            self.assertEqual(popen.call_args.args, ('fixed command',))
            self.assertEqual(popen.call_args.kwargs['cwd'], manager._resume_cwd(key))
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertTrue(process.stdin.closed)
            self.assertIn('CONTROL_CENTER_WAITING', process.stdin.saved)
            self.assertEqual(manager.agent_outputs[key][:len(history)], history)
            self.assertEqual(manager.agent_outputs[key][len(history)], marker)
            self.output(key, 'Done\n', True)
            self.recover()
            self.assertEqual(manager.agent_outputs[key][len(history)], marker)
            self.assertEqual(manager.agent_sessions[key], key)

    def test_policies_survive_flags_reset_and_future_auto_wait_after_reply(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Example\n', True)
        self.resume(key, manager.decide_similar_agent)
        self.output(key, 'CONTROL_CENTER_WAITING: Missing fact\n', True)
        self.resume(key, manager.decide_always_agent)
        similar = manager.agent_similar_decisions[key]
        similar.attempted.add('Previous decline')
        similar.automatic_question = 'Transient'
        similar.handled = True
        always = manager.agent_always_decisions[key]
        always.automatic_attempt = True
        always.handled = True
        self.output(key, 'CONTROL_CENTER_WAITING: Unavailable fact\n', True)
        self.notify.reset_mock()
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            manager.get_agents()
            self.assertEqual(manager.get_agent(key)['status'], 'waiting')
            spawn.assert_not_called()
            self.notify.assert_not_called()
        similar = manager.agent_similar_decisions[key]
        always = manager.agent_always_decisions[key]
        self.assertTrue(similar.enabled)
        self.assertEqual(similar.examples, ['Example'])
        self.assertEqual(similar.attempted, set())
        self.assertIsNone(similar.automatic_question)
        self.assertFalse(similar.handled)
        self.assertTrue(always.enabled)
        self.assertTrue(always.configured)
        self.assertFalse(always.automatic_attempt)
        self.assertFalse(always.handled)
        self.resume(key, lambda key: manager.reply_agent(key, 'Answer'))
        self.output(key, 'CONTROL_CENTER_WAITING: New choice\n')
        manager.agents[key].poll.return_value = 0
        self.resume(key, manager.get_agent)
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertIn('--- Automatic Always Decision ---', manager.agent_outputs[key])
        manager.disable_always_agent(key)
        manager.disable_similar_agent(key)
        self.recover()
        self.assertFalse(manager.agent_always_decisions[key].enabled)
        self.assertTrue(manager.agent_always_decisions[key].configured)
        self.assertFalse(manager.agent_similar_decisions[key].enabled)
        self.assertEqual(manager.agent_similar_decisions[key].examples, [])

    def test_shutdown_stops_running_preserves_waits_and_persists(self):
        running, _ = self.create(kind='mock')
        waiting, _ = self.create()
        finished, _ = self.create()
        stopped, _ = self.create(kind='mock')
        buffered, process = self.create()
        self.output(waiting, 'CONTROL_CENTER_WAITING: Question\n', True)
        self.output(finished, 'Done\n', True)
        manager.stop_agent(stopped)
        process.stdout = io.StringIO('codex\nCONTROL_CENTER_WAITING: Buffered question\n')
        process.poll.return_value = 0
        manager.agent_readers[buffered].join.side_effect = lambda timeout=None: (
            manager._read_output(buffered, process) if not process.stdout.closed else None)
        with patch.object(manager, 'stop_agent', wraps=manager.stop_agent) as stop, \
             patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.shutdown_agents(), [])
            stop.assert_called_once_with(running)
            spawn.assert_not_called()
        self.recover()
        self.assertEqual([manager.agent_statuses[key] for key in (running, waiting, finished, stopped, buffered)],
                         ['stopped', 'waiting', 'finished', 'stopped', 'waiting'])
        self.assertNotIn('--- Backend Restart ---', manager.agent_outputs[running])

    def test_concurrent_reader_output_is_ordered_and_committed_before_exit(self):
        keys = [self.create()[0] for _ in range(3)]
        threads = []
        for key in keys:
            manager.agents[key].stdout = io.StringIO('\n'.join(f'{key}:{i}' for i in range(30)) + '\n')
            threads.append(Thread(target=manager._read_output, args=(key, manager.agents[key])))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        for record in AgentStore(self.path).load_agents():
            self.assertEqual(record['output'], [f"{record['agent_id']}:{i}" for i in range(30)])

    def test_failed_recovered_always_resume_rolls_back_persisted_history(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which file?\n', True)
        self.recover()
        before = manager.get_agent(key)
        process = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed command'), \
             patch.object(manager, '_spawn_process', return_value=process), \
             patch.object(manager, 'Thread') as thread, \
             patch.object(process.stdin, 'write', side_effect=BrokenPipeError('closed')), \
             patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            thread.return_value.is_alive.return_value = False
            with self.assertRaises(BrokenPipeError):
                manager.decide_always_agent(key)
        self.assertIsNone(manager.agents[key])
        self.assertEqual(manager.get_agent(key), before)
        self.recover()
        self.assertEqual(manager.get_agent(key), before)

    def test_redirect_marker_and_processless_stop_statuses_persist(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\n')
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(key, lambda key: manager.redirect_agent(key, 'Correct the task'))
        self.output(key, 'CONTROL_CENTER_WAITING: Question\n', True)
        self.recover()
        self.assertIn('--- Redirect ---', manager.get_agent(key)['output'])
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        self.recover()
        self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        finished, _ = self.create()
        self.output(finished, 'Done\n', True)
        self.recover()
        with patch.object(manager, '_spawn_process') as spawn:
            self.assertEqual(manager.stop_agent(finished)['status'], 'finished')
            spawn.assert_not_called()

    def test_echoed_waiting_marker_is_not_persisted_as_a_wait(self):
        key, process = self.create()
        transcript = (f'session id: {key}\nuser\nInternal control-center instruction:\n'
                      'CONTROL_CENTER_WAITING: <your question>\n'
                      'CONTROL_CENTER_WAITING: Which database should I use?\n'
                      'codex\nCompleted successfully.\ntokens used\n100\n')
        # Feed actual CLI sections directly, without the response-fixture helper.
        process.stdout = io.StringIO(transcript)
        manager._read_output(key, process)
        process.poll.return_value = 0
        manager._finalize_process(key, process)
        self.recover()
        detail = manager.get_agent(key)
        self.assertEqual(detail['status'], 'finished')
        self.assertIsNone(detail['waiting_question'])
        self.assertEqual(detail['session_id'], key)
        self.assertEqual(detail['output'], transcript.splitlines())

    def test_lifespan_uses_override_and_never_loads_on_import(self):
        # Existing direct tests have no store until explicit initialization/lifespan.
        with patch.dict(os.environ, {'CONTROL_CENTER_DB_PATH': str(self.path)}):
            async def run():
                async with app.router.lifespan_context(app):
                    self.assertEqual(manager._store.path, self.path)
                    self.assertEqual(manager.agents, {})
            asyncio.run(run())

    @unittest.skipUnless(os.name == 'nt', 'Windows npm CLI shim')
    def test_windows_waiting_child_recovery_resumes_same_session(self):
        session = str(uuid4())
        self.make_launcher(self.temp.name,
            'import json, os, sys\n'
            'sys.stdin.reconfigure(encoding="utf-8")\n'
            'prompt = sys.stdin.read()\n'
            f'print("session id: {session}", flush=True)\n'
            'print(json.dumps({"args": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd()}), flush=True)\n'
            'print("codex", flush=True)\n'
            'print("CONTROL_CENTER_WAITING: Which file?", flush=True)\n')
        with patch.dict(os.environ, {'APPDATA': self.temp.name}):
            root, _ = self.create(kind='mock')
            child = manager.start_agent('Child task', 'codex', 'workspace-write', parent_id=root)
            self.wait_until_waiting(child)
            before = manager.get_agent(child)
            self.assertEqual(manager.shutdown_agents(), [])
            self.recover()
            self.assertEqual(manager.get_agent(child), before)
            self.assertEqual(manager.get_agent(root)['child_ids'], [child])
            answer = 'Use "main.py" & preserve Unicode שלום'
            manager.reply_agent(child, answer)
            self.wait_until_waiting(child)
            after = manager.get_agent(child)
            payload = [json.loads(line) for line in after['output'] if line.startswith('{')][-1]
            self.assertEqual(payload['args'], ['exec', '--sandbox', 'workspace-write', '--color', 'never',
                                              '--skip-git-repo-check', 'resume', session, '-'])
            self.assertEqual(payload['prompt'], manager._codex_prompt(answer))
            self.assertEqual(Path(payload['cwd']), manager._resume_cwd(child))
            self.assertEqual(after['session_id'], session)
            self.assertEqual(after['parent_id'], root)
            self.assertEqual(after['output'][:len(before['output'])], before['output'])
            self.recover()
            self.assertEqual(manager.get_agent(child), after)
