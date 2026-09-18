import assert from 'node:assert/strict'
import test from 'node:test'
import { syncSystemNotifications } from '../src/systemNotificationSync.js'

test('enabled and capable dashboard sends preference then heartbeat', async () => {
  const calls = []
  await syncSystemNotifications({ enabled: true, browserActive: true }, async (...args) => {
    calls.push(args)
    return { ok: true }
  })
  assert.equal(calls.length, 2)
  assert.equal(JSON.parse(calls[0][1].body).enabled, true)
  assert.ok(calls[1][0].endsWith('/heartbeat'))
})

test('disabled or non-capable dashboards never renew the lease', async () => {
  for (const enabled of [true, false]) {
    const calls = []
    await syncSystemNotifications({ enabled, browserActive: false }, async (...args) => {
      calls.push(args)
      return { ok: true }
    })
    assert.equal(calls.length, 1)
    assert.equal(JSON.parse(calls[0][1].body).enabled, enabled)
  }
})

test('failed preference sync never claims browser presence', async () => {
  let calls = 0
  await assert.rejects(syncSystemNotifications({ enabled: true, browserActive: true }, async () => {
    calls += 1
    return { ok: false }
  }))
  assert.equal(calls, 1)
})

test('cleanup abort prevents a late heartbeat', async () => {
  const controller = new AbortController()
  let calls = 0
  await syncSystemNotifications({ enabled: true, browserActive: true, signal: controller.signal }, async () => {
    calls += 1
    controller.abort()
    return { ok: true }
  })
  assert.equal(calls, 1)
})
