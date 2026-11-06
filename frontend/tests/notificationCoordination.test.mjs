import assert from 'node:assert/strict'
import test from 'node:test'
import { NOTIFICATION_LOCK, RECENT_READ_MS, deliveryEligible, heartbeatDue, locksAvailable, notificationTag,
  requestLeadership } from '../src/notificationCoordination.mjs'
import { browserNotification, createNotifyTracker, notifyEntries } from '../src/atlas/model/notify.mjs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { notificationStatus } from '../src/notificationGuidance.js'
import { agent, node } from '../e2e/scenarios.mjs'

const all = { enabled: true, granted: true, failed: false, current: true, complete: true }

test('a page may claim notifications only with every condition met, partial or stale data never', () => {
  assert.equal(deliveryEligible(all), true)
  for (const key of ['enabled', 'granted', 'current', 'complete']) assert.equal(deliveryEligible({ ...all, [key]: false }), false, key)
  assert.equal(deliveryEligible({ ...all, failed: true }), false)
  assert.equal(deliveryEligible({ ...all, complete: undefined }), false)  // Unknown coverage is not coverage.
})

test('the heartbeat needs the lock, eligibility and a read within the last few seconds', () => {
  const now = 1_000_000
  assert.equal(heartbeatDue({ leader: true, eligible: true, lastRead: now - 100, now }), true)
  assert.equal(heartbeatDue({ leader: false, eligible: true, lastRead: now - 100, now }), false)
  assert.equal(heartbeatDue({ leader: true, eligible: false, lastRead: now - 100, now }), false)
  assert.equal(heartbeatDue({ leader: true, eligible: true, lastRead: now - RECENT_READ_MS, now }), false)
  assert.equal(heartbeatDue({ leader: true, eligible: true, lastRead: null, now }), false)  // Nothing read yet.
})

test('leadership: none without Web Locks or after a failed request, and released on cleanup', async () => {
  const seen = []
  requestLeadership((value) => seen.push(value), {})
  assert.deepEqual(seen, [false])
  assert.equal(locksAvailable({}), false)

  const failing = { locks: { request: () => Promise.reject(new Error('blocked')) } }
  const failed = []
  requestLeadership((value) => failed.push(value), failing)
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.deepEqual(failed, [false])

  let released = false
  const names = []
  const granting = { locks: { request: (name, options, callback) => { names.push(name); return Promise.resolve(callback()).then(() => { released = true }) } } }
  const held = []
  const release = requestLeadership((value) => held.push(value), granting)
  assert.deepEqual(names, [NOTIFICATION_LOCK])
  assert.deepEqual(held, [true])
  release()
  await new Promise((resolve) => setTimeout(resolve, 0))
  assert.equal(released, true)
  assert.deepEqual(held, [true, false])
})

test('Classic says who shows notifications without promising Windows delivery', () => {
  const on = { supported: true, permission: 'granted', enabled: true }
  assert.equal(notificationStatus({ ...on, delivery: 'here' }), 'Control Center notifications: On')
  assert.equal(notificationStatus({ ...on, delivery: 'other-tab' }), 'Control Center notifications: On. Another Control Center tab in this browser shows them.')
  assert.equal(notificationStatus({ ...on, delivery: 'fallback' }), 'Control Center notifications: On. Windows notifications are the fallback when available.')
  assert.equal(notificationStatus({ ...on, enabled: false, delivery: 'fallback' }), 'Control Center notifications: Off')
})
