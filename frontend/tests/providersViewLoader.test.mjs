// Behavioral tests for the AI Providers view loader (review finding: automatic refreshes after an
// earlier user action bypassed the disconnect backoff). A fake clock and deferred responses
// replace the network: every read is a promise the test settles explicitly.
import test from 'node:test'
import assert from 'node:assert/strict'
import { createViewLoader } from '../src/providersViewLoader.mjs'

function harness() {
  let time = 0
  let nextId = 1
  const pending = new Map()  // timer id -> { at, fn }
  const timers = {
    setTimeout(fn, ms) { const id = nextId++; pending.set(id, { at: time + ms, fn }); return id },
    clearTimeout(id) { pending.delete(id) },
  }
  const reads = []  // One deferred response per backend read, in order.
  const seen = { data: [], errors: [] }
  const loader = createViewLoader({
    fetchView: () => new Promise((resolve, reject) => reads.push({ resolve, reject })),
    onData: (data) => seen.data.push(data),
    onError: (error) => seen.errors.push(error),
    now: () => time,
    timers,
  })
  const settle = () => new Promise((resolve) => setImmediate(resolve))
  async function advance(ms) {
    const until = time + ms
    for (;;) {
      const due = [...pending.entries()].filter(([, timer]) => timer.at <= until).sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      time = due[1].at
      pending.delete(due[0])
      due[1].fn()
      await settle()
    }
    time = until
    await settle()
  }
  const offline = () => Object.assign(new Error('not reachable'), { offline: true })
  return { loader, reads, seen, settle, advance, offline, pending, now: () => time }
}

test('initial load, then a successful user action reloads (automatic, no backoff needed)', async () => {
  const h = harness()
  h.loader.request()  // mount
  assert.equal(h.reads.length, 1)
  h.reads[0].resolve({ n: 1 })
  await h.settle()
  assert.deepEqual(h.seen.data, [{ n: 1 }])
  h.loader.request()  // reload() after a successful Save: the backend just answered
  assert.equal(h.reads.length, 2)
  h.reads[1].resolve({ n: 2 })
  await h.settle()
  assert.deepEqual(h.seen.data.at(-1), { n: 2 })
  assert.equal(h.loader.state.failures, 0)
})

test('after a user action, a disconnect makes automatic refreshes respect the backoff', async () => {
  const h = harness()
  h.loader.request()
  h.reads[0].resolve({ n: 1 })
  await h.settle()
  h.loader.request()  // a user action's reload: once this was the trigger for permanent bypass
  h.reads[1].resolve({ n: 2 })
  await h.settle()
  h.loader.request()  // backend now down
  h.reads[2].reject(h.offline())
  await h.settle()
  assert.equal(h.seen.errors.length, 1)
  // Polling fallback and live-event refreshes every ~2 s for 30 s:
  const before = h.reads.length
  for (let elapsed = 0; elapsed < 30000; elapsed += 2000) {
    h.loader.request()
    await h.advance(2000)
    while (h.reads.length > h.seen.data.length + h.seen.errors.length) {  // backend stays down
      h.reads.at(-1).reject(h.offline())
      await h.settle()
    }
  }
  // Backoff 2 s, 5 s, 10 s, 20 s: at most 4 reads in 30 s instead of 15.
  assert.ok(h.reads.length - before <= 4, `reads during the outage: ${h.reads.length - before}`)
  assert.ok(h.reads.length - before >= 2)
  assert.ok(h.loader.state.notBefore > h.now())
})

test('operation-status polling and repeated refreshes never stack timers or bypass the backoff', async () => {
  const h = harness()
  h.loader.request()
  h.reads[0].reject(h.offline())
  await h.settle()
  for (let i = 0; i < 10; i++) h.loader.request()  // operation polling + refreshVersion bumps
  assert.equal(h.reads.length, 1)  // all wait for the 2 s backoff
  assert.equal(h.pending.size, 1)  // a single scheduled retry, not ten
  await h.advance(1999)
  assert.equal(h.reads.length, 1)
  await h.advance(1)
  assert.equal(h.reads.length, 2)
})

test('an explicit Retry skips the backoff once; it does not keep doing so afterwards', async () => {
  const h = harness()
  h.loader.request()
  h.reads[0].reject(h.offline())
  await h.settle()
  h.loader.request()  // an automatic refresh schedules the backed-off retry
  assert.equal(h.pending.size, 1)
  h.loader.retry()
  assert.equal(h.reads.length, 2)  // immediate
  assert.equal(h.pending.size, 0)  // the scheduled automatic retry was superseded
  h.reads[1].reject(h.offline())
  await h.settle()
  h.loader.request()  // the next automatic refresh: back to backing off (5 s now)
  assert.equal(h.reads.length, 2)
  await h.advance(4999)
  assert.equal(h.reads.length, 2)
  await h.advance(1)
  assert.equal(h.reads.length, 3)
})

test('one read in flight; refreshes meanwhile coalesce into one follow-up, never cancelling the read', async () => {
  const h = harness()
  h.loader.request()
  for (let i = 0; i < 5; i++) h.loader.request()
  assert.equal(h.reads.length, 1)
  h.reads[0].resolve({ n: 1 })
  await h.settle()
  assert.equal(h.reads.length, 2)  // exactly one queued follow-up
  h.reads[1].resolve({ n: 2 })
  await h.settle()
  assert.equal(h.reads.length, 2)
  assert.deepEqual(h.seen.data, [{ n: 1 }, { n: 2 }])
})

test('a queued automatic refresh after a failed read waits; a Retry pressed meanwhile runs at once', async () => {
  const h = harness()
  h.loader.request()
  h.loader.request()  // automatic, queued
  h.reads[0].reject(h.offline())
  await h.settle()
  assert.equal(h.reads.length, 1)  // the queued automatic refresh respects the new backoff
  await h.advance(2000)
  assert.equal(h.reads.length, 2)
  h.loader.retry()  // Retry while that read is still in flight
  h.reads[1].reject(h.offline())
  await h.settle()
  assert.equal(h.reads.length, 3)  // the user's Retry is honored right after it
})

test('reconnection recovers, resets the backoff and later refreshes are immediate again', async () => {
  const h = harness()
  h.loader.request()
  h.reads[0].reject(h.offline())
  await h.settle()
  h.loader.request()
  await h.advance(2000)
  h.reads[1].resolve({ recovered: true })
  await h.settle()
  assert.deepEqual(h.seen.data, [{ recovered: true }])
  assert.equal(h.loader.state.failures, 0)
  h.loader.request()
  assert.equal(h.reads.length, 3)  // no residual delay
})

test('no older result overwrites a newer one, and nothing is applied or started after unmount', async () => {
  const h = harness()
  h.loader.request()
  h.loader.state.running = false  // force an overlapping read to exercise the ordering guard
  h.loader.request()
  h.reads[1].resolve({ n: 'newer' })
  await h.settle()
  h.reads[0].resolve({ n: 'older' })
  await h.settle()
  assert.deepEqual(h.seen.data, [{ n: 'newer' }])
  h.loader.dispose()
  h.loader.request()
  assert.equal(h.reads.length, 2)
  h.loader.activate()  // remount (React StrictMode) works again
  h.loader.request()
  assert.equal(h.reads.length, 3)
})
