import assert from 'node:assert/strict'
import test from 'node:test'
import { connectAgentEvents, createRefreshQueue } from '../src/agentEventStream.mjs'
import { createAgentNotifications } from '../src/agentNotifications.js'

class Timers {
  now = 0
  next = 0
  jobs = new Map()
  setTimeout = (fn, delay) => this.add(fn, delay, false)
  setInterval = (fn, delay) => this.add(fn, delay, true)
  clearTimeout = (id) => this.jobs.delete(id)
  clearInterval = this.clearTimeout
  add(fn, delay, repeat) {
    const id = ++this.next
    this.jobs.set(id, { fn, at: this.now + delay, delay, repeat })
    return id
  }
  tick(ms) {
    const end = this.now + ms
    while (true) {
      const next = [...this.jobs].sort((a, b) => a[1].at - b[1].at)[0]
      if (!next || next[1].at > end) break
      const [id, job] = next
      this.now = job.at
      if (job.repeat) job.at += job.delay
      else this.jobs.delete(id)
      job.fn()
    }
    this.now = end
  }
}

test('failed REST refresh retries without another SSE event and disposal cancels retries', async () => {
  const timers = new Timers()
  let calls = 0
  const queue = createRefreshQueue(async () => ++calls > 1, timers)
  queue.request(true)
  timers.tick(0)
  await Promise.resolve()
  timers.tick(1999)
  assert.equal(calls, 1)
  timers.tick(1)
  await Promise.resolve()
  assert.equal(calls, 2)
  assert.equal(timers.jobs.size, 0)
  queue.dispose()
  const failing = createRefreshQueue(async () => false, timers)
  failing.request(true)
  timers.tick(0)
  await Promise.resolve()
  failing.dispose()
  assert.equal(timers.jobs.size, 0)
})

function fixture(refresh, onStatus) {
  const timers = new Timers()
  let calls = 0
  let source
  class FakeEventSource {
    listeners = new Map()
    constructor(url) { this.url = url; source = this }
    addEventListener(name, fn) { this.listeners.set(name, fn) }
    emit(name) { this.listeners.get(name)?.() }
    close() { this.closed = true }
  }
  const disconnect = connectAgentEvents({
    timers, EventSourceClass: FakeEventSource, onStatus,
    refresh: () => { calls++; refresh?.() },
  })
  return { timers, source, disconnect, calls: () => calls }
}

test('transport reports fallback, live, error fallback, and reconnect without callbacks after cleanup', () => {
  const states = []
  const f = fixture(undefined, (status) => states.push(status))
  assert.deepEqual(states, ['polling'])
  f.source.onopen()
  f.source.onerror()
  f.source.onerror()
  f.source.onopen()
  assert.deepEqual(states, ['polling', 'live', 'polling', 'live'])
  f.disconnect()
  f.source.onerror()
  f.source.onopen()
  assert.equal(states.length, 4)
  assert.equal(f.source.closed, true)
  assert.equal(f.timers.jobs.size, 0)
})

test('initial refresh, open disables polling, events coalesce and keepalive is ignored', () => {
  const f = fixture()
  assert.equal(f.calls(), 1)
  assert.equal(f.source.url, 'http://127.0.0.1:8000/events')
  f.source.onopen()
  assert.equal(f.calls(), 2)
  f.timers.tick(4000)
  assert.equal(f.calls(), 2)
  f.source.emit('keepalive')
  f.source.emit('message')
  for (let i = 0; i < 1000; i++) f.source.emit('agent-change')
  assert.equal(f.timers.jobs.size, 1)
  f.timers.tick(74)
  assert.equal(f.calls(), 2)
  f.timers.tick(1)
  assert.equal(f.calls(), 3)
  f.disconnect()
})

test('errors enable one fallback interval; reconnect refreshes immediately and stops it', () => {
  const f = fixture()
  f.source.onopen()
  f.source.onerror()
  f.source.onerror()
  assert.equal(f.timers.jobs.size, 1)
  f.timers.tick(4000)
  assert.equal(f.calls(), 4)
  f.source.onopen()
  assert.equal(f.calls(), 5)
  f.timers.tick(4000)
  assert.equal(f.calls(), 5)
  f.disconnect()
})

test('unsupported or failed EventSource uses polling and cleanup cancels it', () => {
  for (const EventSourceClass of [null, class { constructor() { throw new Error('Unavailable') } }]) {
    const timers = new Timers()
    let calls = 0
    const states = []
    const disconnect = connectAgentEvents({ timers, EventSourceClass, refresh: () => calls++, onStatus: (status) => states.push(status) })
    assert.deepEqual(states, ['polling'])
    assert.equal(calls, 1)
    timers.tick(2000)
    assert.equal(calls, 2)
    disconnect()
    timers.tick(4000)
    assert.equal(calls, 2)
    assert.equal(timers.jobs.size, 0)
  }
})

test('cleanup closes stream, cancels debounce/polling and ignores late callbacks', () => {
  const f = fixture()
  f.source.emit('agent-change')
  f.disconnect()
  assert.equal(f.source.closed, true)
  assert.equal(f.timers.jobs.size, 0)
  f.source.onopen()
  f.source.onerror()
  f.source.emit('agent-change')
  f.timers.tick(4000)
  assert.equal(f.calls(), 1)
})

test('refresh queue prevents overlap and retains one trailing refresh during a request', async () => {
  const timers = new Timers()
  let calls = 0
  let finish
  const queue = createRefreshQueue(() => {
    calls++
    return new Promise((resolve) => { finish = resolve })
  }, timers)
  queue.request(true)
  timers.tick(0)
  for (let i = 0; i < 100; i++) queue.request()
  timers.tick(2000)
  assert.equal(calls, 1)
  finish()
  await Promise.resolve()
  timers.tick(75)
  assert.equal(calls, 2)
  queue.request()
  queue.dispose()
  finish()
  await Promise.resolve()
  timers.tick(2000)
  assert.equal(calls, 2)
  assert.equal(timers.jobs.size, 0)
})

test('refreshed REST snapshots feed notification observer and selected details without changing selection', () => {
  const sent = []
  class Notification {
    static permission = 'granted'
    constructor(title) { sent.push(title) }
    close() {}
  }
  const tracker = createAgentNotifications(() => {}, () => {})
  const selectedId = 'stable-uuid'
  let snapshot = [{ agent_id: selectedId, status: 'finished', task: 'Historical' }]
  let details
  let detailFetches = 0
  const f = fixture(() => {
    tracker.observe(snapshot, true, Notification)
    details = snapshot.find((agent) => agent.agent_id === selectedId)
    detailFetches++
  })
  f.source.onopen()
  assert.deepEqual(sent, [])
  snapshot = [{ agent_id: selectedId, status: 'running', task: 'Review' }]
  f.source.emit('agent-change'); f.timers.tick(75)
  snapshot = [{ ...snapshot[0], status: 'waiting', output: ['Question'] }]
  f.source.emit('agent-change'); f.timers.tick(75)
  f.source.emit('agent-change'); f.timers.tick(75)
  f.source.onerror(); f.timers.tick(2000)
  f.source.onopen()
  assert.equal(details.agent_id, selectedId)
  assert.deepEqual(details.output, ['Question'])
  assert.equal(detailFetches, 7)
  assert.deepEqual(sent, ['Agent needs your input'])
  f.disconnect()
  tracker.close()
})
