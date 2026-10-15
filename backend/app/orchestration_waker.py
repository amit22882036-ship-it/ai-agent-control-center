"""Coalescing, bounded wake-ups for Parent orchestration passes (Phase 2.1).

A wake asks for one bounded pass over a Parent Task's existing orchestration
steps. Wakes for the same Task merge; at most one pass per Task runs at a time;
a wake that arrives during a pass schedules exactly one follow-up pass. A pass
that only failed because a Task lock was busy is retried with bounded backoff,
and exhaustion is reported, never silently dropped. Nothing here scans for new
work, runs at startup, or retries domain refusals or unexpected errors.
"""
import logging
from threading import Lock

logger = logging.getLogger(__name__)

STEPS = ('replanning', 'materialize', 'advance')
BUSY_DELAYS = (1, 2, 5, 10, 30)  # seconds; 6 attempts in total
PENDING_DELAYS = (2, 10)  # existing follow-up for prepared-but-unlaunched Children


class PassOutcome:
    """What a pass learned: 'busy' retries; 'pending' uses the existing follow-up."""
    def __init__(self, busy=False, pending=False):
        self.busy, self.pending = busy, pending


class OrchestrationWaker:
    def __init__(self, run_pass, *, thread_factory, timer_factory, on_exhausted=None, is_active=lambda: True):
        self._run_pass = run_pass
        self._thread = thread_factory
        self._timer = timer_factory
        self._on_exhausted = on_exhausted or (lambda task_id, reason: None)
        self._is_active = is_active
        self._lock = Lock()
        self._entries = {}
        self._queue = []
        self._draining = False

    def wake(self, task_id, steps=STEPS, reason='event'):
        """Request a pass. Event wakes reset the busy backoff for that Task."""
        with self._lock:
            entry = self._entries.setdefault(task_id, dict(state='idle', steps=set(), busy_attempts=0,
                                                           pending_attempts=0, rerun=False, reason=reason))
            entry['steps'] |= set(steps)
            entry['reason'] = reason
            if reason != 'retry':
                entry['busy_attempts'] = 0
                if reason != 'pending':
                    entry['pending_attempts'] = 0
            if entry['state'] == 'running':
                entry['rerun'] = True
                return self.status(task_id, locked=True)
            if entry['state'] != 'queued':
                entry['state'] = 'queued'
                self._queue.append(task_id)
            start = not self._draining
            self._draining = True
        if start:
            self._thread(self._drain)
        return self.status(task_id)

    def status(self, task_id, locked=False):
        def read():
            entry = self._entries.get(task_id)
            if entry is None:
                return {'state': 'idle'}
            return {key: (sorted(value) if isinstance(value, set) else value) for key, value in entry.items()}
        if locked:
            return read()
        with self._lock:
            return read()

    def _drain(self):
        while True:
            with self._lock:
                if not self._queue or not self._is_active():
                    self._draining = False
                    return
                task_id = self._queue.pop(0)
                entry = self._entries[task_id]
                steps = [step for step in STEPS if step in entry['steps']]
                entry['steps'] = set()
                entry['state'], entry['rerun'] = 'running', False
            try:
                outcome = self._run_pass(task_id, steps) or PassOutcome()
            except Exception:
                logger.exception('Orchestration pass failed unexpectedly for Task %s; not retried', task_id)
                outcome = PassOutcome()
            self._settle(task_id, steps, outcome)

    def _settle(self, task_id, steps, outcome):
        exhausted = None
        delay = None
        with self._lock:
            entry = self._entries[task_id]
            if entry['rerun']:
                # A wake arrived mid-pass: run once more (keeping busy steps unfinished).
                if outcome.busy or not entry['steps']:
                    entry['steps'] |= set(steps)
                entry['state'] = 'queued'
                self._queue.append(task_id)
                return
            if outcome.busy:
                if entry['busy_attempts'] < len(BUSY_DELAYS):
                    delay = BUSY_DELAYS[entry['busy_attempts']]
                    entry['busy_attempts'] += 1
                    entry['state'] = 'retry_wait'
                    entry['steps'] |= set(steps)
                else:
                    entry['state'] = 'exhausted'
                    exhausted = f'Orchestration wake exhausted after {len(BUSY_DELAYS) + 1} attempts: Task busy'
            elif outcome.pending and entry['pending_attempts'] < len(PENDING_DELAYS):
                delay = PENDING_DELAYS[entry['pending_attempts']]
                entry['pending_attempts'] += 1
                entry['state'] = 'retry_wait'
                entry['steps'] |= set(steps)
            else:
                entry['state'] = 'idle'
            reason = 'retry' if outcome.busy else 'pending'
        if delay is not None:
            self._timer(delay, lambda: self._is_active() and self.wake(task_id, (), reason=reason))
        if exhausted:
            logger.error('%s (Task %s)', exhausted, task_id)
            try:
                self._on_exhausted(task_id, exhausted)
            except Exception:
                logger.exception('Could not record exhausted orchestration wake for Task %s', task_id)
