import assert from 'node:assert/strict'
import test from 'node:test'
import { createAgentNotifications } from '../src/agentNotifications.js'

function fixture() {
  const sent = []
  const selected = []
  const errors = []
  class NotificationStub {
    static permission = 'granted'
    constructor(title, options) {
      this.title = title
      this.options = options
      sent.push(this)
    }
    close() { this.closed = true; this.onclose?.() }
  }
  const tracker = createAgentNotifications((id) => selected.push(id), (error) => errors.push(error))
  const observe = (agents, enabled = true) => tracker.observe(agents, enabled, NotificationStub)
  return { tracker, observe, sent, selected, errors, NotificationStub }
}

const agent = (status, id = 'opaque-uuid') => ({ agent_id: id, status, task: 'Review authentication' })

test('first successful snapshot is silent, including historical waiting/finished agents', () => {
  const f = fixture()
  const baseline = [agent('waiting'), agent('finished', 'second')]
  f.observe(baseline)
  f.observe(baseline)
  assert.equal(f.sent.length, 0)
})

test('notifies once per transition, including repeated wait/reply cycles', () => {
  const f = fixture()
  f.observe([agent('running')])
  f.observe([agent('waiting')])
  f.observe([agent('waiting')])
  f.observe([agent('running')])
  f.observe([agent('waiting')])
  f.observe([agent('running')])
  f.observe([agent('finished')])
  f.observe([agent('finished')])
  f.observe([agent('stopped')])
  assert.deepEqual(f.sent.map((n) => n.title), ['Agent needs your input', 'Agent needs your input', 'Agent finished'])
})

test('empty initial baseline permits newly discovered terminal agents to notify once', () => {
  const f = fixture()
  f.observe([])
  const discovered = [agent('waiting'), agent('finished', 'second'), agent('stopped', 'third')]
  f.observe(discovered)
  f.observe(discovered)
  assert.equal(f.sent.length, 2)
})

test('disabled or denied notifications still advance baseline without replay', () => {
  const f = fixture()
  f.observe([agent('running')], false)
  f.observe([agent('waiting')], false)
  f.observe([agent('waiting')], true)
  f.NotificationStub.permission = 'denied'
  f.observe([agent('finished')])
  f.NotificationStub.permission = 'granted'
  f.observe([agent('finished')])
  assert.equal(f.sent.length, 0)
})

test('delivery failure is contained and does not retry every poll', () => {
  const f = fixture()
  class BrokenNotification {
    static permission = 'granted'
    constructor() { throw new Error('Browser rejected notification') }
  }
  f.observe([agent('running')])
  assert.doesNotThrow(() => f.tracker.observe([agent('finished')], true, BrokenNotification))
  f.observe([agent('finished')])
  assert.equal(f.errors.length, 1)
  assert.equal(f.sent.length, 0)
})

test('unsupported API is harmless', () => {
  const f = fixture()
  f.tracker.observe([], true, null)
  assert.doesNotThrow(() => f.tracker.observe([agent('finished')], true, null))
  assert.equal(f.sent.length, 0)
})

test('notification click selects the UUID, closes notification, and body stays short', () => {
  const f = fixture()
  f.observe([])
  f.observe([{ ...agent('waiting'), task: 'x'.repeat(300) }])
  assert.equal(f.sent[0].options.body.length, 180)
  f.sent[0].onclick()
  assert.deepEqual(f.selected, ['opaque-uuid'])
  assert.equal(f.sent[0].closed, true)
})

test('effect cleanup closes notifications without resetting transition baseline', () => {
  const f = fixture()
  f.observe([agent('running')])
  f.tracker.close()
  f.observe([agent('running')])
  f.observe([agent('finished')])
  f.tracker.close()
  f.observe([agent('finished')])
  assert.equal(f.sent.length, 1)
  assert.equal(f.sent[0].closed, true)
})
