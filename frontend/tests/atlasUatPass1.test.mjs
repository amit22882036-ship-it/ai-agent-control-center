import assert from 'node:assert/strict'
import test from 'node:test'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { SECTIONS, deriveWorkspace, familyCounts, nowView } from '../src/atlas/model/workList.mjs'
import { controlState, impactView } from '../src/atlas/model/taskPage.mjs'
import { projectDirectory, projectLabel } from '../src/atlas/model/projects.mjs'
import { agent, node } from '../e2e/scenarios.mjs'

const holding = (agentId) => ({ current: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: '2026-10-29T10:00:00.000Z' },
  last_ended: null, count: 1, previous_agent_ids: [] })
const ended = (agentId, reason = 'stopped') => ({ current: null, last_ended: { assignment_id: `as-${agentId}`, agent_id: agentId,
  assigned_at: '2026-10-29T09:00:00.000Z', ended_at: '2026-10-29T09:30:00.000Z', ended_reason: reason }, count: 1, previous_agent_ids: [agentId] })

function build(tasks) {
  const store = applyPage(emptyStore(), { tasks, ancestors: [], next_after: null, total: tasks.length })
  return { store, derived: deriveWorkspace(store) }
}

test('every task state keeps the backend’s fact and is named without implying it runs', () => {
  const { store, derived } = build([
    node('new', null, { status: 'pending' }),
    node('stopped', null, { status: 'pending', assignment: ended('s1'), agent: agent('s1', { status: 'stopped', role: 'last' }) }),
    node('running', null, { status: 'in_progress', assignment: holding('r1'), agent: agent('r1', { status: 'running' }) }),
    node('asking', null, { status: 'waiting', assignment: holding('w1'), agent: agent('w1', { status: 'waiting', asks: true }), waiting_context: { kind: 'user', label: 'Waiting for you' } }),
    node('blocked', null, { status: 'blocked', blockers: ['dependency_pending'] }),
    node('paused', null, { status: 'paused', control_intent: 'paused' }),
    node('unassigned', null, { status: 'in_progress' }),
    node('done', null, { status: 'completed', assignment: ended('d1', 'completed'), agent: agent('d1', { status: 'finished', role: 'last' }) }),
    node('canceled', null, { status: 'canceled', control_intent: 'canceled' }),
  ])
  const label = (id) => derived.status[id].label
  assert.equal(label('new'), 'Not started')
  assert.equal(label('stopped'), 'Not running')  // Pending again after its agent stopped: not "Not started".
  assert.match(derived.status.stopped.detail, /Worked on before/)
  assert.equal(derived.status.stopped.key, 'pending')  // The backend's state is unchanged.
  assert.equal(label('running'), 'Working')
  assert.equal(label('asking'), 'Waiting for you')
  assert.equal(label('blocked'), 'Blocked')
  assert.equal(label('paused'), 'Paused')
  assert.equal(label('unassigned'), 'No agent working')
  assert.equal(label('done'), 'Completed')
  assert.equal(label('canceled'), 'Canceled')
  assert.equal(nowView(store, derived, 'stopped').line, 'Not running')
  // Unfinished families are "Open", never "In progress": nothing claims they run.
  assert.deepEqual(SECTIONS.map((section) => section.label), ['Needs you', 'Open', 'History'])
  const counts = familyCounts(derived)
  // Open: not started, not running, working, waiting for you, blocked and paused. Completed,
  // canceled and "no agent working" (in progress with nothing on it) are under history.
  assert.equal(counts.active, 6)
  assert.equal(derived.byTop.new.section, 'active')
  assert.equal(derived.byTop.canceled.section, 'done')
})

test('a not-started task with a stopped agent may be paused or canceled, and the preview says nothing will stop', () => {
  const state = controlState({ task: { lifecycle: 'pending', controlIntent: 'active' }, detail: { status: 'pending', control_intent: 'active' } })
  assert.equal(state.pause.available, true)
  assert.equal(state.cancel.available, true)
  const view = impactView({ action: 'pause', affected_task_ids: ['t1'], workers_to_stop: [], completed_descendants: [], external_dependent_task_ids: [],
    replanning_task_ids: [] }, { tasks: { t1: { title: 'T' } }, agents: {} })
  assert.deepEqual(view.lines, ['1 task will be paused: this one.', 'No agent is running here now, so none is stopped.'])
  const running = impactView({ action: 'cancel', affected_task_ids: ['t1'], workers_to_stop: [{ task_id: 't1', agent_id: 'a1' }] },
    { tasks: { t1: { title: 'T' } }, agents: { a1: { name: 'Builder' } } })
  assert.equal(running.lines[1], '1 running agent will be stopped.')
})

test('project labels: loading only while loading, honest when unknown, never "No project" for a task that has one', () => {
  const loading = projectDirectory({ data: null, error: null, loading: true })
  assert.equal(projectLabel(loading, 'p1'), 'Loading project…')
  const failed = projectDirectory({ data: null, error: 503 })
  assert.equal(projectLabel(failed, 'p1'), 'Project name unavailable')
  const ready = projectDirectory({ data: { projects: [{ project_id: 'p1', name: 'Release planning', root_path: 'x' }] }, error: null })
  assert.equal(projectLabel(ready, 'p1'), 'Release planning')
  assert.equal(projectLabel(ready, 'p9'), 'Unregistered project')
  // A later failed read keeps the names it had.
  const stale = projectDirectory({ data: { projects: [{ project_id: 'p1', name: 'Release planning', root_path: 'x' }] }, error: 'unreachable' })
  assert.equal(projectLabel(stale, 'p1'), 'Release planning')
  assert.equal(projectLabel(ready, null), 'No project')
})
