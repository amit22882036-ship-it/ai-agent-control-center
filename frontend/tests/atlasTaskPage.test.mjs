import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace } from '../src/atlas/model/workList.mjs'
import { assignmentsView, blockerText, controlState, impactView, pauseHeld, progressView, resultView, sameImpact } from '../src/atlas/model/taskPage.mjs'

// Both recordings come from the same synthetic scenario (backend/task_projection_scenarios.py):
// the projection pages and the real task-detail endpoints for the same Tasks.
const projection = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const details = JSON.parse(readFileSync(new URL('./fixtures/task-detail.contract.json', import.meta.url), 'utf8'))
const store = applyPage(emptyStore(), projection.all)
const derived = deriveWorkspace(store)
const read = (id, key) => details[id][key].body
const view = (id) => progressView({ task: store.tasks[id], status: derived.status[id], detail: read(id, 'task'), orchestration: read(id, '/orchestration') })

test('the recorded detail endpoints answer for every scenario task, and terminal work refuses control previews', () => {
  for (const [id, entry] of Object.entries(details)) {
    for (const key of ['task', '/assignments', '/orchestration', '/integrations', '/delegations']) assert.equal(entry[key].status, 200, `${id} ${key}`)
  }
  assert.equal(details.docs['impact:pause'].status, 409)
  assert.equal(details.solo['impact:cancel'].status, 409)
})

test('how the work is going: plan, rounds, continuation and blockers in plain words', () => {
  const release = view('release')
  assert.equal(release.plan.state, 'promoted')
  assert.equal(release.plan.text, 'The lead turned its plan into helper tasks.')
  assert.match(release.plan.rationale, /Docs and tests can proceed in parallel/) // the agent's words, shown as such
  assert.deepEqual(release.plan.items.map((item) => [item.id, item.text]), [['prop-1', 'Held']])
  assert.equal(release.continuation.text, 'Reviewing its helpers’ results.')
  assert.equal(release.rounds, 2) // from the projection's accepted rounds
  assert.equal(release.planned, 2)
  const blocked = view('blocked')
  assert.deepEqual(blocked.blockers.map((item) => item.text), ['Another task holds a resource it needs'])
  assert.equal(blockerText('something_new'), 'Something new')
  assert.equal(view('orphaned-work').plan.text, 'This task’s agent works without planning helpers.')
  assert.ok(view('paused').facts.some((fact) => fact.text.startsWith('Paused.')))
})

test('assignment history keeps the task identity: replaced and removed agents are history, newest first', () => {
  const docs = assignmentsView(read('docs', '/assignments').assignments, store)
  assert.deepEqual(docs.map((item) => [item.agentId, item.current, item.text, item.removed]), [
    ['docs-new', false, 'finished its work', false],
    ['docs-old', false, 'was removed', true],
  ])
  const tests = assignmentsView(read('tests', '/assignments').assignments, store)
  assert.deepEqual(tests.map((item) => [item.agentId, item.current]), [['tester', true]])
})

test('results stay five separate statements: reported, recorded, verified, combined into the lead, applied to the project', () => {
  const docs = resultView({ task: store.tasks.docs, detail: read('docs', 'task'), integrations: read('docs', '/integrations').integrations,
    parentDelegations: read('release', '/delegations').delegations, taskId: 'docs' })
  assert.equal(docs.isChild, true)
  assert.equal(docs.reported.available, true)
  assert.equal(docs.reported.outcome, 'completed')
  assert.equal(docs.recorded.text, 'The task is marked completed.')
  assert.match(docs.verified.text, /^Not independently verified/)
  assert.equal(docs.integrated.status, 'applied')
  assert.equal(docs.integrated.text, 'Applied')
  assert.equal(docs.integrated.validation, 'not_run')
  assert.equal(docs.applied, null) // a helper's work goes to its lead, never straight to the project
  const solo = resultView({ task: store.tasks.solo, detail: read('solo', 'task'), integrations: read('solo', '/integrations').integrations, taskId: 'solo' })
  assert.equal(solo.isChild, false)
  assert.equal(solo.reported.available, false)
  assert.equal(solo.applied.status, 'applied')
  assert.equal(solo.integrated, null)
  const tests = resultView({ task: store.tasks.tests, detail: read('tests', 'task'), integrations: [], parentDelegations: read('release', '/delegations').delegations, taskId: 'tests' })
  assert.equal(tests.recorded.done, false)
  assert.equal(tests.integrated, null)
})

test('lifecycle controls follow the backend rules, including who owns a pause', () => {
  const release = controlState({ task: store.tasks.release, detail: read('release', 'task'), operations: [] })
  assert.equal(release.pause.available, true)
  assert.equal(release.cancel.available, true)
  assert.equal(release.resume.available, false)
  const docs = controlState({ task: store.tasks.docs, detail: read('docs', 'task') })
  assert.ok(!docs.pause.available && !docs.cancel.available && !docs.resume.available)
  // Paused without an operation of its own (and not an older single-task pause): resume elsewhere.
  const paused = controlState({ task: store.tasks.paused, detail: read('paused', 'task'), operations: read('paused', '/control-operations').operations })
  assert.equal(paused.resume.available, false)
  assert.match(paused.resume.reason, /task above it/)
  const owning = controlState({ task: store.tasks.paused, detail: read('paused', 'task'),
    operations: [{ action: 'pause', released: 0, status: 'completed', impact: { affected_task_ids: ['paused'] }, failures: [] }] })
  assert.equal(owning.resume.available, true)
  assert.deepEqual(pauseHeld([{ action: 'pause', released: 1 }]), null)
  const canceling = controlState({ task: store.tasks.release, detail: { ...read('release', 'task'), control_intent: 'canceled' } })
  assert.equal(canceling.cancel.retry, true)
  assert.equal(canceling.pause.available, false)
})

test('an impact preview reads as consequences, and a changed impact is noticed', () => {
  const pause = impactView(read('release', 'impact:pause'), store)
  assert.deepEqual(pause.lines, ['3 tasks will be paused: this one and 2 below it.', '1 running agent will be stopped.', '1 finished task stays as it is.'])
  assert.equal(pause.workers[0].name, 'Lead')
  assert.ok(sameImpact(pause, impactView(read('release', 'impact:pause'), store)))
  const grown = impactView({ ...read('release', 'impact:pause'), affected_task_ids: ['flaky', 'release', 'tests', 'docs'] }, store)
  assert.ok(!sameImpact(pause, grown))
  const cancel = impactView(read('release', 'impact:cancel'), store)
  assert.match(cancel.lines[0], /will be canceled/)
})

test('the lead’s early planning decision reads the same on the task page and in the side panel', async () => {
  const { planText } = await import('../src/atlas/model/taskPage.mjs')
  assert.equal(planText(null), null)
  assert.equal(planText({ state: 'independent' }), 'The lead decided to do the work itself.')
  assert.match(planText({ state: 'not_recorded', plan_missing: false }), /^No plan has been recorded for the current agent yet\. With helpers on/)
  assert.equal(planText({ state: 'not_recorded', plan_missing: true }), 'The lead ended its turn without recording a plan decision.')
  const progress = progressView({ task: null, status: null, detail: null,
    orchestration: { decision: { state: 'not_recorded', plan_missing: true, promotions: [] } } })
  assert.equal(progress.plan.missing, true)
  assert.equal(progress.plan.text, 'The lead ended its turn without recording a plan decision.')
})
