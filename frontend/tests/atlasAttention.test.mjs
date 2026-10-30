import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace } from '../src/atlas/model/workList.mjs'
import { attentionInView, attentionItems, attentionKind, attentionSummary, bellLabel, coverageText, stepAttention } from '../src/atlas/model/attention.mjs'
import { FALLBACK, announcement, browserNotification, createNotifyTracker, deliveryView, notifyEntries } from '../src/atlas/model/notify.mjs'
import { KEY_GROUPS } from '../src/atlas/model/settings.mjs'
import { onReplyRequest, replyPending, requestReply, takeReply } from '../src/atlas/workContext.js'
import { agent, node, showcase } from '../e2e/scenarios.mjs'

const contract = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const build = (page) => {
  const store = applyPage(emptyStore(), page)
  return { store, derived: deriveWorkspace(store) }
}
const page = (tasks, extra = {}) => ({ tasks, ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null, ...extra })
const holding = (agentId) => ({ current: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: '2026-10-29T11:00:00.000Z' },
  last_ended: null, count: 1, previous_agent_ids: [] })
const asking = (id, agentId, extra = {}) => node(id, null, { title: `Task ${id}`, status: 'waiting', created_at: extra.created_at || '2026-10-29T10:00:00.000Z',
  assignment: holding(agentId), agent: agent(agentId, { status: 'waiting', asks: true, name: `Agent ${agentId}` }),
  waiting_context: { kind: 'user', label: 'Waiting for you' }, ...extra })

test('attention items are Tasks, in the List’s order, with only the current Agent named', () => {
  const { store, derived } = build(contract.all)
  const items = attentionItems(store, derived)
  assert.deepEqual(items.map((item) => [item.taskId, item.kind]), [['orphaned-work', 'owner'], ['tests', 'reply']])
  const question = items.find((item) => item.kind === 'reply')
  assert.equal(question.agentId, 'tester')
  // A Task waiting for a new owner has no current Agent to name (its last one was removed).
  assert.equal(items[0].agentId, null)
  assert.equal(items[0].agentName, null)
  assert.equal(attentionKind(derived, 'release'), null)
})

test('the showcase lists its question, its ownerless task, and nothing that is only waiting on helpers or a provider', () => {
  const { store, derived } = build(showcase())
  const kinds = Object.fromEntries(attentionItems(store, derived).map((item) => [item.taskId, item.kind]))
  assert.deepEqual(kinds, { quoted: 'reply', migrate: 'owner' })
  assert.equal(attentionKind(derived, 'limits'), null)  // A provider limit is not the person's to answer.
  assert.equal(attentionKind(derived, 'csv'), null)  // Waiting for helpers.
})

test('an integration problem needs the person even when the Task itself is complete', () => {
  const { store, derived } = build(page([node('merge', null, { status: 'completed', title: 'Merge',
    integration: { integration_id: 'i1', destination_kind: 'project', status: 'conflict', validation_status: 'not_run' } })]))
  assert.deepEqual(attentionItems(store, derived).map((item) => item.kind), ['integration'])
})

test('moving between items is stable: next after the selected one, wrapping, and the first when nothing is selected', () => {
  const ids = ['a', 'b', 'c']
  assert.equal(stepAttention(ids, 'a', 1), 'b')
  assert.equal(stepAttention(ids, 'c', 1), 'a')
  assert.equal(stepAttention(ids, 'a', -1), 'c')
  assert.equal(stepAttention(ids, 'zzz', 1), 'a')
  assert.equal(stepAttention(ids, null, -1), 'c')
  assert.equal(stepAttention(['a'], 'a', 1), 'a')
  assert.equal(stepAttention([], 'a', 1), null)
})

test('the view decides which items are reachable: project, focus, colour and the Done filter', () => {
  const tasks = [asking('one', 'ag1', { project_id: 'p1' }), asking('two', 'ag2', { project_id: 'p2', created_at: '2026-10-29T09:00:00.000Z' })]
  const { store, derived } = build(page(tasks))
  assert.deepEqual(attentionInView(store, derived), ['one', 'two'])
  assert.deepEqual(attentionInView(store, derived, { project: 'p2' }), ['two'])
  assert.deepEqual(attentionInView(store, derived, { filter: 'done' }), [])
  assert.deepEqual(attentionInView(store, derived, { focus: 'one' }), ['one'])
  assert.deepEqual(attentionInView(store, derived, { colors: new Set(['green']) }), [])
  // A collapsed branch never hides an item: the path opens when it is selected.
  const nested = build(page([node('lead', null, { child_count: 1, title: 'Lead' }), asking('deep', 'agd', { parent_task_id: 'lead' })]))
  assert.deepEqual(attentionInView(nested.store, nested.derived), ['deep'])
})

test('counts are honest: the backend’s workspace count of questions, and what the loaded pages show', () => {
  const { store, derived } = build(page([asking('one', 'ag1')], { next_after: 'cursor', total: 40 }))
  const items = attentionItems(store, derived)
  const partial = attentionSummary(items, { needs_input: 3 }, store)
  assert.equal(partial.reported, 3)
  assert.equal(partial.loadedReply, 1)
  assert.equal(partial.unlisted, 2)
  assert.equal(partial.complete, false)
  assert.match(coverageText(partial), /2 may be on older pages that aren’t loaded/)
  assert.equal(bellLabel(partial), 'Notifications: 3 waiting for your reply')

  const complete = attentionSummary(items, { needs_input: 1 }, build(page([asking('one', 'ag1')])).store)
  assert.equal(complete.unlisted, 0)
  assert.equal(coverageText(complete), null)

  const unknown = attentionSummary(items, null, store)
  assert.equal(unknown.reported, null)
  assert.equal(unknown.unlisted, null)
  assert.match(bellLabel(unknown), /1 waiting for your reply among loaded tasks/)
  assert.match(coverageText(unknown), /loaded so far/)
  // A malformed count is "not available", never zero.
  assert.equal(attentionSummary(items, { needs_input: -1 }, store).reported, null)
  assert.equal(bellLabel(attentionSummary([], { needs_input: 0 }, store)), 'Notifications: nothing needs you')
  // Before the task list has been read, nothing is claimed either way.
  assert.equal(bellLabel({ ...attentionSummary([], null, null), known: false }), 'Notifications: what needs you isn’t known yet')
  assert.match(bellLabel(complete, { updates: 2, tools: 1 }), /1 tool needs attention, 2 tool updates available/)
})

test('notifications: the first reading is a baseline, and each transition is announced once', () => {
  const tracker = createNotifyTracker({ startedAt: Date.parse('2026-10-29T12:00:00.000Z') })
  const running = (status = 'running', extra = {}) => node('t1', null, { title: 'Write the importer', status: 'in_progress', created_at: '2026-10-29T10:00:00.000Z',
    assignment: holding('a1'), agent: agent('a1', { status, name: 'Importer' }), ...extra })
  const read = (tasks) => tracker.observe(notifyEntries(applyPage(emptyStore(), page(tasks))))
  assert.deepEqual(read([asking('old', 'a0')]), [])  // Already waiting before the page opened: not replayed.
  assert.deepEqual(read([asking('old', 'a0'), running()]), [])
  const asked = read([asking('old', 'a0'), running('waiting', { waiting_context: { kind: 'user', label: 'Waiting for you' } })])
  assert.equal(asked.length, 1)
  assert.equal(asked[0].key, 'a1:waiting:user')
  assert.equal(asked[0].taskId, 't1')
  // The same state read again (a reconnect, a repeated event) is not announced again.
  assert.deepEqual(read([asking('old', 'a0'), running('waiting', { waiting_context: { kind: 'user', label: 'Waiting for you' } })]), [])
  // Waiting for a provider or for helpers is not the person's to answer.
  assert.deepEqual(read([running('waiting', { waiting_context: { kind: 'provider', label: 'Waiting' } })]), [])
  const done = read([node('t1', null, { title: 'Write the importer', status: 'completed', created_at: '2026-10-29T10:00:00.000Z',
    assignment: { current: null, last_ended: { assignment_id: 'as-a1', agent_id: 'a1', assigned_at: 'x', ended_at: 'y', ended_reason: 'completed' }, count: 1, previous_agent_ids: ['a1'] },
    agent: agent('a1', { status: 'finished', role: 'last', name: 'Importer' }) })])
  assert.deepEqual(done.map((event) => event.heading), ['Agent finished'])
})

test('notifications: a replacement agent on a watched Task is new; an older page loaded later replays nothing', () => {
  const tracker = createNotifyTracker({ startedAt: Date.parse('2026-10-29T12:00:00.000Z') })
  const read = (tasks) => tracker.observe(notifyEntries(applyPage(emptyStore(), page(tasks))))
  read([node('t1', null, { status: 'in_progress', created_at: '2026-10-29T10:00:00.000Z', assignment: holding('first'), agent: agent('first') })])
  // An older page brings a Task that was already waiting: recorded, not announced.
  assert.deepEqual(read([node('t1', null, { status: 'in_progress', created_at: '2026-10-29T10:00:00.000Z', assignment: holding('first'), agent: agent('first') }),
    asking('older', 'a-old', { created_at: '2026-10-20T10:00:00.000Z' })]), [])
  // The Task's Agent was replaced and the new one asks: that is announced, for the new Agent.
  const replaced = read([asking('t1', 'second', { created_at: '2026-10-29T10:00:00.000Z' })])
  assert.deepEqual(replaced.map((event) => event.agentId), ['second'])
  // A Task created after watching began is announced even on its first reading.
  const fresh = read([asking('new', 'a-new', { created_at: '2026-10-29T12:05:00.000Z' })])
  assert.deepEqual(fresh.map((event) => event.taskId), ['new'])
})

test('a browser notification is worded like Classic’s and tagged by event, so tabs replace rather than stack', () => {
  const [event] = createNotifyTracker({ startedAt: 0 }).observe([])
  assert.equal(event, undefined)
  const shown = browserNotification({ heading: 'Agent needs your input', task: 'x'.repeat(300), key: 'a1:waiting:user' })
  assert.equal(shown.title, 'Agent needs your input')
  assert.equal(shown.options.body.length, 180)
  assert.equal(shown.options.tag, 'control-center:a1:waiting:user')
  assert.equal(announcement([{ state: 'finished', task: 'T' }]), '')
  assert.equal(announcement([{ state: 'waiting:user', task: 'לתרגם', name: 'מתרגם' }]), 'מתרגם on “לתרגם” needs your reply.')
  assert.equal(announcement([{ state: 'waiting:user', task: 'a' }, { state: 'waiting:user', task: 'b' }]), '2 agents need your reply.')
})

test('delivery wording never promises a Windows notification and names the reason this page shows none', () => {
  const on = { supported: true, permission: 'granted', enabled: true }
  assert.equal(deliveryView(on, { delivering: true }).short, 'On: shown by this browser.')
  assert.equal(deliveryView({ ...on, enabled: false }, {}).short, 'Off.')
  assert.equal(deliveryView({ ...on, permission: 'denied' }, {}).short, 'Blocked by the browser.')
  assert.equal(deliveryView({ ...on, supported: false }, {}).short, 'This browser can’t show notifications.')
  assert.equal(FALLBACK, 'Windows notifications are the fallback when available.')
  for (const cause of ['failed', 'no-locks', 'stale', 'partial', null]) {
    const view = deliveryView(on, { delivering: false, cause })
    assert.match(view.short, /fallback when available/, String(cause))
    assert.match(view.detail, /can’t open a task/)
  }
  // Disconnected: nothing can be sent, and no Windows notification is implied.
  const offline = deliveryView(on, { delivering: false, cause: 'offline' })
  assert.equal(offline.short, 'On. The backend isn’t answering, so nothing can be sent until it returns.')
  assert.doesNotMatch(offline.short, /Windows/)
  assert.equal(deliveryView(on, { delivering: false, cause: 'follower' }).short, 'On. Another Atlas tab shows them.')
  for (const cause of ['failed', 'no-locks', 'stale', 'partial', 'offline', 'follower', null]) {
    const { short, detail } = deliveryView(on, { delivering: false, cause })
    assert.doesNotMatch(`${short} ${detail}`, /(are|is) (used|shown)|will (be shown|appear)/, String(cause))
  }
})

test('a reply request is taken once, by the Task it names, and listeners hear it', () => {
  const heard = []
  const stop = onReplyRequest((id) => heard.push(id))
  requestReply('t1')
  assert.deepEqual(heard, ['t1'])
  assert.equal(replyPending('t2'), false)
  assert.equal(takeReply('t2'), false)
  assert.equal(replyPending('t1'), true)
  assert.equal(takeReply('t1'), true)
  assert.equal(takeReply('t1'), false)
  stop()
  requestReply('t3')
  assert.deepEqual(heard, ['t1'])
  requestReply(null)
  assert.equal(replyPending('t3'), false)
})

test('the keyboard reference lists the attention keys', () => {
  const work = Object.fromEntries(KEY_GROUPS.find(([group]) => group === 'Work')[1])
  assert.equal(work['Next or previous task that needs you'], 'A · Shift A')
  assert.equal(work['Reply to the selected task’s question'], 'R')
})
