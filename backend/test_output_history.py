import asyncio
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager
from app.main import app
from app.output_history import RECENT_OUTPUT_LIMIT, OUTPUT_PAGE_SIZE, OUTPUT_MAX_LIMIT
from app.persistence import AgentStore
import test_persistence


class OutputHistoryTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    create = test_persistence.PersistenceTests.create
    replacement = test_persistence.PersistenceTests.replacement
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    resume = test_persistence.PersistenceTests.resume

    def append(self, key, lines):
        with manager._data_lock:
            manager.agent_outputs[key].extend(lines)
            manager._save_agent(key)

    def request(self, path, query='', expected=200):
        async def run():
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                       'method': 'GET', 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                       'query_string': query.encode(), 'headers': [], 'root_path': '',
                       'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000)}, receive, send)
            self.assertEqual(messages[0]['status'], expected)
            return json.loads(b''.join(item.get('body', b'') for item in messages))
        return asyncio.run(run())

    def test_tail_after_before_are_bounded_ascending_and_exclusive(self):
        key, _ = self.create()
        self.append(key, [str(i) for i in range(900)])
        tail = self.request(f'/agents/{key}/output')
        self.assertEqual([item['seq'] for item in tail['items']], list(range(600, 900)))
        self.assertEqual(len(tail['items']), OUTPUT_PAGE_SIZE)
        self.assertTrue(tail['has_older'])
        self.assertFalse(tail['has_newer'])
        after = self.request(f'/agents/{key}/output', 'after=100&limit=3')
        self.assertEqual(after['items'], [{'seq': i, 'text': str(i)} for i in range(101, 104)])
        self.assertTrue(after['has_newer'])
        before = self.request(f'/agents/{key}/output', 'before=4&limit=3')
        self.assertEqual([item['seq'] for item in before['items']], [1, 2, 3])
        self.assertTrue(before['has_older'])
        first = self.request(f'/agents/{key}/output', 'before=2&limit=3')
        self.assertFalse(first['has_older'])
        self.assertEqual([item['seq'] for item in first['items']], [0, 1])
        self.assertEqual(self.request(f'/agents/{key}/output', 'after=899')['items'], [])

    def test_api_validation_unknown_and_empty(self):
        key, _ = self.create()
        self.assertEqual(self.request(f'/agents/{key}/output')['items'], [])
        for query in ('after=1&before=4', 'limit=0', 'limit=-1', f'limit={OUTPUT_MAX_LIMIT+1}',
                      'after=-1', 'before=-1', 'after=bad', 'limit=bad'):
            self.request(f'/agents/{key}/output', query, 422)
        self.request('/agents/missing/output', expected=404)
        self.request(f'/agents/{key}/output', f'limit={OUTPUT_MAX_LIMIT}')

    def test_large_history_survives_with_bounded_startup_cache_and_incremental_reads(self):
        key, _ = self.create()
        lines = [f'line {i}' for i in range(6000)]
        self.append(key, lines)
        self.assertEqual(manager.agent_outputs[key], lines[-RECENT_OUTPUT_LIMIT:])
        manager.agent_statuses[key] = 'finished'
        manager._save_agent(key)
        with patch.object(AgentStore, 'load_agents', autospec=True, wraps=None,
                          side_effect=AgentStore.load_agents) as load:
            self.recover()
            self.assertEqual(load.call_args.kwargs, {'output_limit': RECENT_OUTPUT_LIMIT})
        self.assertEqual(manager.agent_outputs[key], lines[-RECENT_OUTPUT_LIMIT:])
        self.assertEqual(manager.agent_outputs[key].next_sequence, 6000)
        self.assertEqual(manager.get_agent(key)['output'], lines)
        with patch.object(manager._store, 'full_output', side_effect=AssertionError('Unbounded read')):
            self.assertNotIn('output', self.request(f'/agents/{key}', 'include_output=false'))
            manager.get_agents()
            page = self.request(f'/agents/{key}/output', 'after=123&limit=10')
            self.assertEqual(len(page['items']), 10)
        self.assertEqual(self.request(f'/agents/{key}')['output'], lines)

    def test_existing_v1_storage_reopens_idempotently_without_rewriting_history(self):
        key, _ = self.create()
        self.append(key, ['', 'raw \x1b[31mtext', 'שלום', 'duplicate', 'duplicate'])
        with manager._store._connection() as db:
            before = list(map(tuple, db.execute('SELECT * FROM output ORDER BY sequence')))
            # Guard against destructive migration or rewrite of existing entries.
            db.execute("CREATE TRIGGER no_output_update BEFORE UPDATE ON output BEGIN SELECT RAISE(ABORT, 'rewrite'); END")
            db.execute("CREATE TRIGGER no_output_delete BEFORE DELETE ON output BEGIN SELECT RAISE(ABORT, 'delete'); END")
        for _ in range(2):
            store = AgentStore(self.path)
            self.assertEqual(store.full_output(key), [row[2] for row in before])
        self.append(key, ['new'])
        with manager._store._connection() as db:
            rows = list(map(tuple, db.execute('SELECT * FROM output ORDER BY sequence')))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 14)
        self.assertEqual(rows[:-1], before)
        self.assertEqual(rows[-1], (key, 5, 'new'))

    def test_stream_persists_before_sse_and_final_wait_marker(self):
        key, process = self.create()
        transcript = f'session id: {key}\nuser\nCONTROL_CENTER_WAITING: fake\ncodex\nCONTROL_CENTER_WAITING: Actual?\n'
        process.stdout = io.StringIO(transcript)
        observed = []
        def publish(agent_id):
            page = manager._store.read_output(agent_id)
            observed.append(page['items'][-1]['text'])
            self.assertEqual(page['items'][-1]['seq'], len(observed)-1)
        with patch.object(manager.changes, 'publish', side_effect=publish):
            manager._read_output(key, process)
        process.poll.return_value = 0
        manager._finalize_process(key, process)
        self.assertEqual(observed, transcript.splitlines())
        self.assertEqual(manager.get_agent(key)['status'], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Actual?')

    def test_redirect_reply_restart_markers_and_recovered_reply_keep_sequence(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\n')
        with patch.object(manager, '_stop_windows_tree'):
            self.resume(key, lambda key: manager.redirect_agent(key, 'Correct'))
        self.output(key, 'codex\nCONTROL_CENTER_WAITING: Question\n', True)
        old = manager.get_agent(key)['output']
        self.recover()
        self.assertEqual(manager.agent_sessions[key], key)
        self.resume(key, lambda key: manager.reply_agent(key, 'Answer'))
        self.output(key, 'New output\n')
        self.recover()
        lines = manager.get_agent(key)['output']
        self.assertEqual(lines[:len(old)], old)
        self.assertEqual(lines[len(old):len(old)+3], ['--- User Reply ---', 'Answer', 'New output'])
        self.assertEqual(lines[-2], '--- Backend Restart ---')
        page = manager.get_agent_output(key)
        self.assertEqual([item['seq'] for item in page['items']], list(range(len(lines))))
        self.recover()
        self.assertEqual(manager.get_agent_output(key), page)

    def test_stale_reader_cannot_append_or_publish_after_replacement(self):
        key, old = self.create()
        self.output(key, 'Old valid output\n')
        replacement = self.replacement()
        manager.agents[key] = replacement
        old.stdout = io.StringIO('codex\nCONTROL_CENTER_WAITING: Stale\n')
        with patch.object(manager.changes, 'publish') as publish:
            manager._read_output(key, old)
            publish.assert_not_called()
        self.assertEqual(manager._store.full_output(key), ['Old valid output'])
        self.assertEqual(manager.agent_outputs[key].next_sequence, 1)
        self.assertIsNone(manager.agent_waiting_questions[key])

    def test_stop_does_not_duplicate_output(self):
        key, _ = self.create(kind='mock')
        self.output(key, 'one\ntwo\n')
        manager.stop_agent(key)
        manager.stop_agent(key)
        self.assertEqual(manager.get_agent_output(key)['items'], [{'seq': 0, 'text': 'one'}, {'seq': 1, 'text': 'two'}])

    def test_storage_failure_retains_backlog_and_retries_without_crashing_reader(self):
        key, _ = self.create(kind='mock')
        with patch.object(manager._store, 'save_agent', side_effect=sqlite3.OperationalError('disk unavailable')), \
             patch.object(manager.changes, 'publish') as publish, self.assertLogs(manager.logger, level='ERROR'):
            self.output(key, 'one\ntwo\n', True)
            self.assertEqual(manager.agent_statuses[key], 'finished')
            self.assertEqual(manager.agent_outputs[key].pending, [(0, 'one'), (1, 'two')])
            self.request(f'/agents/{key}/output', expected=503)
            publish.assert_not_called()
        self.assertEqual(manager.get_agent_output(key)['items'], [{'seq': 0, 'text': 'one'}, {'seq': 1, 'text': 'two'}])
        self.assertEqual(manager.agent_outputs[key].pending, [])

    def test_uncertain_commit_retry_and_sse_failure_never_duplicate_or_lose_entries(self):
        key, _ = self.create()
        save = manager._store.save_agent
        def uncertain(*args, **kwargs):
            save(*args, **kwargs)
            raise sqlite3.OperationalError('commit acknowledgement lost')
        with patch.object(manager._store, 'save_agent', side_effect=uncertain), self.assertLogs(manager.logger, level='ERROR'):
            self.append(key, ['one'])
        with patch.object(manager.changes, 'publish', side_effect=RuntimeError('SSE failed')), self.assertLogs(manager.logger, level='ERROR'):
            self.append(key, ['two'])
        self.assertEqual(manager._store.full_output(key), ['one', 'two'])
        self.recover()
        self.assertEqual(manager.get_agent(key)['output'][:2], ['one', 'two'])

    def test_sequence_collision_is_reported_without_overwriting_or_discarding_pending_text(self):
        key, _ = self.create()
        self.append(key, ['original'])
        manager.agent_outputs[key].pending.append((0, 'conflicting'))
        with self.assertLogs(manager.logger, level='ERROR'):
            self.assertFalse(manager._save_agent(key))
        self.assertEqual(manager._store.full_output(key), ['original'])
        self.assertEqual(manager.agent_outputs[key].pending, [(0, 'conflicting')])

    def test_failed_reply_decide_similar_and_redirect_keep_durable_order(self):
        actions = [lambda key: manager.reply_agent(key, 'Answer'), manager.decide_agent,
                   manager.decide_similar_agent, lambda key: manager.redirect_agent(key, 'Correction')]
        for index, action in enumerate(actions):
            with self.subTest(action=index):
                key, _ = self.create()
                self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Choose?\n', True)
                if index == 3:
                    manager.agent_statuses[key] = 'running'
                    manager.agents[key].poll.return_value = None
                previous = manager._store.full_output(key)
                replacement = self.replacement()
                with patch.object(manager, '_codex_command', return_value='fixed'), \
                     patch.object(manager, '_spawn_process', return_value=replacement), \
                     patch.object(manager, 'Thread') as thread, \
                     patch.object(replacement.stdin, 'write', side_effect=BrokenPipeError('closed')), \
                     patch.object(manager, '_stop_windows_tree'):
                    thread.return_value.is_alive.return_value = False
                    with self.assertRaises(BrokenPipeError):
                        action(key)
                self.assertEqual(manager.agent_statuses[key], 'stopped')
                self.assertFalse(manager.agent_similar_decisions[key].enabled)
                durable = manager._store.full_output(key)
                self.assertEqual(durable[:len(previous)], previous)
                self.assertEqual(len(durable), len(previous) + 2)
                self.assertEqual([item['seq'] for item in manager.get_agent_output(key)['items']], list(range(len(durable))))

    def test_failed_always_delivery_never_publishes_or_deletes_tentative_output(self):
        key, _ = self.create()
        self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Choose?\n', True)
        before = manager.get_agent_output(key)
        replacement = self.replacement()
        with patch.object(manager, '_codex_command', return_value='fixed'), \
             patch.object(manager, '_spawn_process', return_value=replacement), \
             patch.object(manager, 'Thread') as thread, \
             patch.object(replacement.stdin, 'write', side_effect=BrokenPipeError('closed')), \
             patch.object(manager, '_stop_windows_tree'), patch.object(manager.changes, 'publish') as publish:
            thread.return_value.is_alive.return_value = False
            with self.assertRaises(BrokenPipeError):
                manager.decide_always_agent(key)
            publish.assert_not_called()
        self.assertEqual(manager.get_agent_output(key), before)
        self.resume(key, lambda key: manager.reply_agent(key, 'Answer'))
        self.assertEqual(manager.get_agent_output(key)['items'][-2]['seq'], len(before['items']))
