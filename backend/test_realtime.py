import asyncio
import io
import json
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from app.realtime import ChangeBroker, format_event
from app.main import agent_events, app
from app import agent_manager as manager
import test_persistence


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiple_subscribers_revision_and_background_thread(self):
        broker = ChangeBroker()
        broker.publish('before')
        first, second = broker.subscribe(), broker.subscribe()
        self.assertTrue(first.queue.empty())
        thread = Thread(target=lambda: broker.publish('agent-a'))
        thread.start()
        thread.join()
        for subscriber in (first, second):
            self.assertEqual(await asyncio.wait_for(subscriber.queue.get(), 1),
                             {'revision': 2, 'agent_id': 'agent-a'})
        broker.publish('agent-b')
        self.assertEqual((await asyncio.wait_for(first.queue.get(), 1))['revision'], 3)
        broker.unsubscribe(first)
        broker.unsubscribe(second)
        self.assertEqual(len(broker._subscribers), 0)

    async def test_burst_bounds_both_queue_and_scheduled_callbacks(self):
        broker = ChangeBroker()
        subscriber = broker.subscribe()
        loop = subscriber.loop
        with patch.object(loop, 'call_soon_threadsafe', wraps=loop.call_soon_threadsafe) as schedule:
            for index in range(1000):
                broker.publish(str(index))
            schedule.assert_called_once()
        await asyncio.sleep(0)
        self.assertEqual(subscriber.queue.qsize(), 1)
        broker.publish('latest')
        await asyncio.sleep(0)
        self.assertEqual(subscriber.queue.qsize(), 1)
        self.assertEqual(subscriber.queue.get_nowait(), {'revision': 1001, 'agent_id': None})
        broker.unsubscribe(subscriber)

    async def test_broken_subscriber_does_not_break_healthy_one(self):
        broker = ChangeBroker()
        broken, healthy = broker.subscribe(), broker.subscribe()
        broken.loop = Mock()
        broken.loop.call_soon_threadsafe.side_effect = RuntimeError('Loop closed')
        broker.publish('agent')
        self.assertEqual((await asyncio.wait_for(healthy.queue.get(), 1))['agent_id'], 'agent')
        self.assertNotIn(broken, broker._subscribers)
        broker.unsubscribe(healthy)

    async def test_stream_format_keepalive_and_cancellation(self):
        broker = ChangeBroker()
        stream = broker.stream(keepalive_seconds=0.001)
        self.assertEqual(await anext(stream), ': connected\n\n')
        self.assertEqual(await anext(stream), ': keepalive\n\n')
        broker.publish('uuid')
        event = await anext(stream)
        self.assertTrue(event.startswith('event: agent-change\nid: 1\n'))
        self.assertEqual(json.loads(event.split('data: ')[1]), {'revision': 1, 'agent_id': 'uuid'})
        await stream.aclose()
        stream = broker.stream()
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(len(broker._subscribers), 0)

    async def test_endpoint_type_and_cleanup(self):
        self.assertIn('/events', [route.path for route in app.routes])
        response = await agent_events()
        self.assertIn('text/event-stream', response.headers['content-type'])
        with patch('app.main.changes', ChangeBroker()) as broker:
            response = await agent_events()
            await anext(response.body_iterator)
            self.assertEqual(len(broker._subscribers), 1)
            await response.body_iterator.aclose()
            self.assertEqual(len(broker._subscribers), 0)
        self.assertIn('"agent_id": null', format_event({'revision': 5, 'agent_id': None}))


class RealtimeLifecycleTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    replacement = test_persistence.PersistenceTests.replacement
    create = test_persistence.PersistenceTests.create
    output = test_persistence.PersistenceTests.output
    recover = test_persistence.PersistenceTests.recover
    resume = test_persistence.PersistenceTests.resume

    def test_creation_output_status_and_actions_publish_after_persistence(self):
        events = []
        def observe(key):
            record = next(item for item in manager._store.load_agents() if item['agent_id'] == key)
            self.assertEqual(record['status'], manager.agent_statuses[key])
            self.assertEqual(record['output'], manager.agent_outputs[key])
            events.append((key, record['status']))

        with patch.object(manager.changes, 'publish', side_effect=observe):
            root, _ = self.create(kind='mock')
            child, _ = self.create(root)
            self.assertEqual(events, [(root, 'running'), (child, 'running')])
            self.output(child, f'session id: {child}\ncodex\nCONTROL_CENTER_WAITING: Question\n', True)
            self.assertIn((child, 'waiting'), events)
            for action in (lambda key: manager.reply_agent(key, 'Answer'), manager.decide_agent,
                           manager.decide_similar_agent, manager.decide_always_agent):
                previous = len(events)
                self.resume(child, action)
                self.assertGreater(len(events), previous)
                self.assertEqual(events[-1], (child, 'running'))
                self.output(child, 'codex\nCONTROL_CENTER_WAITING: Question\n', True)
            previous = len(events)
            manager.disable_always_agent(child)
            manager.disable_similar_agent(child)
            self.assertEqual(len(events), previous + 2)
            self.resume(child, lambda key: manager.reply_agent(key, 'Answer'))
            with patch.object(manager, '_stop_windows_tree'):
                self.resume(child, lambda key: manager.redirect_agent(key, 'Correction'))
            self.assertIn('--- Redirect ---', manager.agent_outputs[child])
            self.output(child, 'Done\n', True)
            self.assertEqual(events[-1], (child, 'finished'))
            waiting, _ = self.create(root)
            self.output(waiting, 'codex\nCONTROL_CENTER_WAITING: Question\n', True)
            manager.stop_branch(root)
            self.assertIn((waiting, 'stopped'), events)
            self.assertEqual(events[-1], (root, 'stopped'))

    def test_startup_is_silent_but_later_changes_publish(self):
        root, _ = self.create(kind='mock')
        waiting, _ = self.create(root)
        self.output(waiting, 'codex\nCONTROL_CENTER_WAITING: Question\n', True)
        with patch.object(manager.changes, 'publish') as publish:
            self.recover()
            self.assertEqual(manager.agent_statuses[root], 'stopped')
            publish.assert_not_called()
            manager.stop_agent(waiting)
            publish.assert_called_once_with(waiting)

    def test_publish_failure_does_not_break_lifecycle_or_storage(self):
        with patch.object(manager.changes, 'publish', side_effect=RuntimeError('Delivery failed')), \
             self.assertLogs(manager.logger, level='ERROR'):
            key, _ = self.create(kind='mock')
            self.output(key, 'Output\n')
            self.assertEqual(manager.stop_agent(key)['status'], 'stopped')
        self.recover()
        self.assertEqual(manager.get_agent(key)['status'], 'stopped')
        self.assertEqual(manager.agent_outputs[key], ['Output'])

    def test_stale_process_output_does_not_publish_current_agent_change(self):
        key, old = self.create()
        old.stdout = io.StringIO('codex\nCONTROL_CENTER_WAITING: Stale\n')
        with patch.dict(manager.agents, {key: object()}), patch.object(manager.changes, 'publish') as publish:
            manager._read_output(key, old)
            publish.assert_not_called()
