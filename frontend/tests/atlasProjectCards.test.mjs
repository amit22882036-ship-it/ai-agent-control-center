import test from 'node:test'
import assert from 'node:assert/strict'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace, projectStats } from '../src/atlas/model/workList.mjs'
import { PREVIEW, projectPreview } from '../src/atlas/model/projectPreview.mjs'
import { agent, node } from '../e2e/scenarios.mjs'

const holding = (agentId) => ({ current: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: '2026-10-29T11:00:00Z' }, last_ended: null, count: 1, previous_agent_ids: [] })
const ended = (agentId) => ({ current: null, last_ended: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: '2026-10-29T10:00:00Z',
  ended_at: '2026-10-29T10:30:00Z', ended_reason: 'completed' }, count: 1, previous_agent_ids: [agentId] })
const page = (tasks) => ({ tasks, ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null })
const at = (minute) => `2026-10-29T10:${String(minute % 60).padStart(2, '0')}:${String(Math.floor(minute / 60) % 60).padStart(2, '0')}Z`

// One lead and seven helpers in mixed states, plus a task in another project that must not count.
function mixedProject() {
  const kid = (id, extra) => node(id, 'lead', { created_at: at(Number(id.slice(1))), ...extra })
  return page([
    node('lead', null, { status: 'in_progress', child_count: 7, assignment: holding('a-lead'), agent: agent('a-lead', { status: 'running' }) }),
    kid('c1', { status: 'in_progress', assignment: holding('a1'), agent: agent('a1', { status: 'running' }) }),
    kid('c2', { status: 'waiting', assignment: holding('a2'), agent: agent('a2', { status: 'waiting', asks: true }), waiting_context: { kind: 'user', label: 'Waiting for you' } }),
    kid('c3', { status: 'completed', assignment: ended('a3'), agent: agent('a3', { status: 'finished', role: 'last' }) }),
    kid('c4', { status: 'blocked', blockers: ['resource_busy'] }),
    kid('c5', { status: 'in_progress', assignment: holding('a5'), agent: agent('a5', { status: 'waiting' }),
      waiting_context: { kind: 'provider', label: 'Waiting for provider usage', condition: 'usage_limited' } }),
    kid('c6', { status: 'pending' }),
    kid('c7', { status: 'in_progress', assignment: holding('a7'), agent: agent('a7', { status: 'waiting' }) }),
    node('other', null, { project_id: 'p2', status: 'in_progress', assignment: holding('a-o'), agent: agent('a-o', { status: 'running' }) }),
  ])
}

test('project counts are of individual tasks, and only running agents count as working', () => {
  const store = applyPage(emptyStore(), mixedProject())
  const derived = deriveWorkspace(store)
  assert.deepEqual(projectStats(store, derived, 'p1'), {
    tasks: 8, // the lead and its seven helpers, not one "family"
    families: 1,
    open: 7, // everything but the completed helper
    needsYou: 1, // the helper with a question
    working: 2, // the lead and c1; waiting agents with a current assignment are not "working"
    waiting: 2, // the provider wait and an agent waiting without a reported reason
    blocked: 1,
  })
  assert.deepEqual(projectStats(store, derived, 'p2'), { tasks: 1, families: 1, open: 1, needsYou: 0, working: 1, waiting: 0, blocked: 0 })
  assert.deepEqual(projectStats(store, derived, 'none'), { tasks: 0, families: 0, open: 0, needsYou: 0, working: 0, waiting: 0, blocked: 0 })
})

test('a project preview stays bounded with a family of a thousand helpers, and says what it leaves out', () => {
  const tasks = [node('lead', null, { status: 'in_progress', child_count: 1000, assignment: holding('a-lead'), agent: agent('a-lead') })]
  for (let i = 0; i < 1000; i += 1) {
    tasks.push(node(`c${i}`, 'lead', { status: 'completed', created_at: at(i), child_count: i === 0 ? 20 : 0 }))
  }
  for (let i = 0; i < 20; i += 1) tasks.push(node(`g${i}`, 'c0', { created_at: at(2000 + i) }))
  const store = applyPage(emptyStore(), page(tasks))
  const derived = deriveWorkspace(store)
  const started = performance.now()
  const preview = projectPreview(derived, 'p1')
  const elapsed = performance.now() - started
  assert.ok(elapsed < 50, `the preview took ${elapsed.toFixed(1)} ms`)
  assert.equal(preview.loaded, 1021)
  assert.ok(preview.shown <= PREVIEW.tasks)
  assert.equal(Object.keys(preview.layout.nodes).length, preview.shown)
  assert.equal(preview.truncated, true)
  // Under the lead: six helpers drawn, 994 counted; under the first helper: six of its twenty.
  assert.equal(preview.layout.children.lead.length, PREVIEW.children)
  assert.equal(preview.more.lead, 994)
  assert.equal(preview.more.c0, 14)
  // Every drawn link is a real parent_task_id link.
  for (const [child, parent] of Object.entries(preview.layout.parent)) assert.equal(derived.forest.parent[child], parent)
})

test('a deep chain is cut at the preview depth and the cut is counted', () => {
  const tasks = [node('t0', null, { child_count: 1 })]
  for (let i = 1; i < 50; i += 1) tasks.push(node(`t${i}`, `t${i - 1}`, { created_at: at(i), child_count: i < 49 ? 1 : 0 }))
  const store = applyPage(emptyStore(), page(tasks))
  const preview = projectPreview(deriveWorkspace(store), 'p1')
  assert.equal(preview.shown, PREVIEW.depth)
  assert.equal(preview.more[`t${PREVIEW.depth - 1}`], 1)
  assert.equal(preview.truncated, true)
})

test('small projects are drawn whole, and an empty project draws nothing', () => {
  const store = applyPage(emptyStore(), mixedProject())
  const derived = deriveWorkspace(store)
  const preview = projectPreview(derived, 'p1')
  assert.equal(preview.shown, 7) // the lead and six of its seven helpers…
  assert.equal(preview.more.lead, 1) // …and the seventh is counted, not hidden
  assert.equal(projectPreview(derived, 'p2').truncated, false)
  assert.equal(projectPreview(derived, 'none').layout, null)
})
