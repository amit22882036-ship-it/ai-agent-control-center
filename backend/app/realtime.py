"""Bounded, thread-safe SSE invalidations. REST remains authoritative."""
import asyncio
from dataclasses import dataclass, field
import json
from threading import Lock


@dataclass(eq=False)
class Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    pending: dict | None = None
    scheduled: bool = False


class ChangeBroker:
    def __init__(self):
        self._lock = Lock()
        self._subscribers = set()
        self.revision = 0

    def subscribe(self):
        subscriber = Subscriber(asyncio.get_running_loop())
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)
            subscriber.pending = None

    def publish(self, agent_id=None):
        with self._lock:
            self.revision += 1
            for subscriber in tuple(self._subscribers):
                # Coalescing distinct agents becomes a global invalidation.
                target = agent_id
                if subscriber.pending and subscriber.pending['agent_id'] != target:
                    target = None
                subscriber.pending = {'revision': self.revision, 'agent_id': target}
                if not subscriber.scheduled:
                    subscriber.scheduled = True
                    try:
                        subscriber.loop.call_soon_threadsafe(self._deliver, subscriber)
                    except Exception:
                        self._subscribers.discard(subscriber)
                        subscriber.pending = None

    def _deliver(self, subscriber):
        with self._lock:
            subscriber.scheduled = False
            if subscriber not in self._subscribers:
                return
            event = subscriber.pending
            subscriber.pending = None
            if subscriber.queue.full():
                previous = subscriber.queue.get_nowait()
                if previous['agent_id'] != event['agent_id']:
                    event['agent_id'] = None
            subscriber.queue.put_nowait(event)

    async def stream(self, keepalive_seconds=15):
        subscriber = self.subscribe()
        try:
            # Flush headers without replaying historical changes.
            yield ': connected\n\n'
            while True:
                try:
                    event = await asyncio.wait_for(subscriber.queue.get(), keepalive_seconds)
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
                else:
                    yield format_event(event)
        finally:
            self.unsubscribe(subscriber)


def format_event(event):
    return f"event: agent-change\nid: {event['revision']}\ndata: {json.dumps(event)}\n\n"


changes = ChangeBroker()
