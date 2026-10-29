import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { applyPage, buildForest, emptyStore, familySummary, pathTo, taskAgents } from '../src/atlas/model/tasks.mjs'
import { helpersView, integrationView, plannedView, resultView, STATUS, taskStatus } from '../src/atlas/model/taskStatus.mjs'
import { readTasks, tasksUrl } from '../src/atlas/model/taskSource.mjs'
import { reconcile, routeParamsFor, select, selectionFromRoute, toggleExpanded } from '../src/atlas/model/selection.mjs'
import { STATES } from '../src/atlas/vocabulary.mjs'

// Recorded from the real endpoint by backend/test_task_projection.py (isolated database).
const contract = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))

const statusOf = (store, taskId) => taskStatus(store.tasks[taskId], taskAgents(store, taskId))

// A minimal valid node in the backend's shape; fields can be overridden per test.
function node(taskId, parent = null, extra = {}) {
  return {
    task_id: taskId, parent_task_id: parent, project_id: 'p1', title: `Task ${taskId}`, status: 'pending', control_intent: 'active',
    stop_required: false, created_at: '2026-01-01T00:00:00.000Z', updated_at: '2026-01-01T00:00:00.000Z',
    root_task_id: null, depth: null, ancestry: 'complete', child_count: 0, blockers: [], blockers_more: 0,
    assignment: { current: null, last_ended: null, count: 0, previous_agent_ids: [] }, agent: null, waiting_context: null,
    responsibility_awaiting: false, transfer: null, continuation: null, delegation: null,
    delegations: { requested: 0, materialized: 0, result_ready: 0, acknowledged: 0, canceled: 0 },
    planned: { unmaterialized: 0, by_disposition: {} }, plan_revision: null, rounds: 0, integration: null, ...extra,
  }
}
const agent = (agentId, status = 'running', extra = {}) => ({ agent_id: agentId, role: 'current', status, display_name: agentId,
  display_color: 'blue', agent_type: 'claude', model: 'm', effort: 'high', asks_user: false, provider_condition: null,
  autonomous: false, removed: false, removal: null, ...extra })
const current = (agentId) => ({ current: { assignment_id: 'as-' + agentId, agent_id: agentId, assigned_at: 't' }, last_ended: null, count: 1, previous_agent_ids: [] })
const page = (tasks, extra = {}) => ({ tasks, ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null, ...extra })
const storeOf = (tasks, extra) => applyPage(emptyStore(), page(tasks, extra))

// Contract with the real endpoint ---------------------------------------------------------

test('the recorded endpoint response normalizes without issues for well-formed Tasks', () => {
  const store = applyPage(emptyStore(), contract.all)
  assert.equal(Object.keys(store.tasks).length, 12)
  const unexpected = store.issues.filter((issue) => !['loop-a', 'loop-b', 'stray'].includes(issue.taskId))
  assert.deepEqual(unexpected, [])
  assert.equal(store.complete, true)
  assert.equal(store.total, 12)
})

test('every recorded node carries exactly the fields the model reads', () => {
  const fields = Object.keys(node('x')).sort()
  for (const raw of [...contract.all.tasks, ...contract.all.ancestors]) assert.deepEqual(Object.keys(raw).sort(), fields, raw.task_id)
  assert.deepEqual(Object.keys(contract.all).sort(), ['ancestors', 'ancestors_truncated', 'next_after', 'root', 'tasks', 'total'])
})

test('contract scenario: each state stays distinct', () => {
  const store = applyPage(emptyStore(), contract.all)
  const keys = Object.fromEntries(Object.keys(store.tasks).map((taskId) => [taskId, statusOf(store, taskId).key]))
  assert.deepEqual(keys, {
    release: 'reviewing', docs: 'integrated', tests: 'needs_input', flaky: 'pending', solo: 'integrated',
    'orphaned-work': 'needs_owner', limited: 'provider_wait', paused: 'paused', blocked: 'blocked',
    stray: 'pending', 'loop-a': 'pending', 'loop-b': 'pending',
  })
  assert.equal(statusOf(store, 'docs').label, 'Integrated into the lead')
  assert.equal(statusOf(store, 'solo').label, 'Applied to the project')
  assert.equal(statusOf(store, 'limited').label, 'Waiting for provider usage')
  assert.equal(statusOf(store, 'blocked').label, 'Coordination blocked')
})

test('contract scenario: proposed work, helpers, results and integration are separate facts', () => {
  const store = applyPage(emptyStore(), contract.all)
  assert.deepEqual(plannedView(store.tasks.release), { count: 2, byDisposition: { blocked: 1, direct: 1 } })
  assert.deepEqual(helpersView(store.tasks.release), { proposed: 2, active: 2, resultsReady: 1, closed: 0, canceled: 0, rounds: 2 })
  assert.deepEqual(resultView(store.tasks.docs), { outcome: 'completed', phase: 'delivered', label: 'Reported complete' })
  assert.equal(resultView(store.tasks.flaky), null)
  const docs = integrationView(store.tasks.docs)
  assert.equal(docs.scope, 'lead')
  assert.equal(docs.verified, false)
  assert.equal(integrationView(store.tasks.solo).scope, 'project')
  assert.equal(integrationView(store.tasks.tests), null)
  // Planned work is never a Task node.
  assert.equal(store.tasks['d-plan'], undefined)
})

test('contract scenario: hierarchy, replacement and removal', () => {
  const store = applyPage(emptyStore(), contract.all)
  const forest = buildForest(store)
  assert.deepEqual(forest.children.release, ['docs', 'tests'])
  assert.deepEqual(forest.children.tests, ['flaky'])
  assert.deepEqual(pathTo(forest, 'flaky'), ['tests', 'release'])
  assert.equal(forest.depth.flaky, 2)
  assert.equal(forest.placement.stray, 'missing_parent')
  assert.equal(forest.placement['loop-a'], 'cycle')
  assert.equal(forest.placement['loop-b'], 'cycle')
  const docs = taskAgents(store, 'docs')
  assert.equal(docs.current, null)
  assert.equal(docs.last.id, 'docs-new')
  assert.equal(docs.replaced, true)
  assert.deepEqual(docs.previous.map((a) => a.id), ['docs-new', 'docs-old'])
  assert.equal(docs.previous[1].unknown, true) // the removed first owner is only known by id here
  const orphan = taskAgents(store, 'orphaned-work')
  assert.equal(orphan.last.removed, true)
  assert.equal(taskAgents(store, 'flaky').current, null)
})

test('contract scenario: paged and root-filtered recordings rebuild the same model', () => {
  const full = applyPage(emptyStore(), contract.all)
  let paged = emptyStore()
  contract.paged.forEach((response, index) => { paged = applyPage(paged, response, { append: index > 0 }) })
  assert.deepEqual(Object.keys(paged.tasks).sort(), Object.keys(full.tasks).sort())
  assert.ok(Object.values(paged.tasks).every((task) => task.source === 'page'))
  assert.deepEqual(paged.order, full.order)
  assert.deepEqual(buildForest(paged).children, buildForest(full).children)
  let family = emptyStore()
  contract.root.forEach((response, index) => { family = applyPage(family, response, { append: index > 0 }) })
  assert.deepEqual(Object.keys(family.tasks).sort(), ['docs', 'flaky', 'release', 'tests'])
  assert.equal(family.root, 'release')
})

// Hierarchy shapes --------------------------------------------------------------------------

test('a standalone Task is its own root', () => {
  const store = storeOf([node('solo', null, { root_task_id: 'solo', depth: 0 })])
  const forest = buildForest(store)
  assert.deepEqual(forest.tops, ['solo'])
  assert.equal(forest.placement.solo, 'root')
  assert.deepEqual(familySummary(store, forest, 'solo'), { tasks: 1, unmaterialized: 0, loadedChildren: 0, reportedChildren: 0, notLoaded: 0 })
})

test('a Task with several children orders them by creation, then id', () => {
  const store = storeOf([
    node('lead', null, { child_count: 3 }),
    node('c', 'lead', { created_at: '2026-01-01T00:00:02.000Z' }),
    node('b', 'lead', { created_at: '2026-01-01T00:00:01.000Z' }),
    node('a', 'lead', { created_at: '2026-01-01T00:00:02.000Z' }),
  ])
  assert.deepEqual(buildForest(store).children.lead, ['b', 'a', 'c'])
})

test('Agent parent ids and names never create Task edges', () => {
  const store = storeOf([node('one', null, { assignment: current('x'), agent: agent('x', 'running', { parent_id: 'y', display_name: 'two' }) }),
    node('two', null, { assignment: current('y'), agent: agent('y') })])
  assert.deepEqual(buildForest(store).tops.sort(), ['one', 'two'])
})

test('a deep delegation tree of depth 60 resolves without recursion limits', () => {
  const tasks = [node('t0')]
  for (let i = 1; i <= 60; i += 1) tasks.push(node(`t${i}`, `t${i - 1}`))
  const forest = buildForest(storeOf(tasks))
  assert.equal(forest.depth.t60, 60)
  assert.equal(pathTo(forest, 't60').length, 60)
  assert.equal(forest.rootOf.t60, 't0')
})

test('a parent outside the loaded pages is reported, not dropped', () => {
  const store = storeOf([node('child', 'elsewhere', { ancestry: 'complete' })], { next_after: 'cursor' })
  const forest = buildForest(store)
  assert.deepEqual(forest.tops, ['child'])
  assert.equal(forest.placement.child, 'parent_not_loaded')
  assert.equal(store.complete, false)
})

test('a parent the backend could not include is unresolved, not missing and not invented', () => {
  const store = storeOf([node('leaf', 'far-away', { ancestry: 'truncated' })], { ancestors_truncated: true })
  const forest = buildForest(store)
  assert.equal(store.complete, true)
  assert.equal(forest.placement.leaf, 'parent_unresolved')
  assert.deepEqual(forest.tops, ['leaf'])
  assert.equal(forest.parent.leaf, undefined)
  const missing = buildForest(storeOf([node('stray', 'nowhere', { ancestry: 'missing_parent' })]))
  assert.equal(missing.placement.stray, 'missing_parent')
})

test('ancestors delivered as context place a child across a page boundary', () => {
  const first = applyPage(emptyStore(), page([node('grandchild', 'child', { created_at: '2026-01-01T00:00:03.000Z' })], {
    ancestors: [node('child', 'lead', { created_at: '2026-01-01T00:00:02.000Z' }), node('lead', null)], next_after: 'c1' }))
  assert.equal(first.tasks.child.source, 'context')
  assert.deepEqual(buildForest(first).children.child, ['grandchild'])
  const second = applyPage(first, page([node('child', 'lead', { title: 'Fresh' })], { next_after: null }), { append: true })
  assert.equal(second.tasks.child.source, 'page')
  assert.equal(second.tasks.child.title, 'Fresh')
  // A later context copy never replaces a Task already loaded as a page row.
  const third = applyPage(second, page([], { ancestors: [node('child', 'lead', { title: 'Stale' })] }), { append: true })
  assert.equal(third.tasks.child.title, 'Fresh')
  assert.deepEqual(third.order, ['grandchild', 'child'])
})

test('a refresh without append replaces the previous snapshot', () => {
  const before = storeOf([node('gone'), node('kept')])
  const after = applyPage(before, page([node('kept')]))
  assert.deepEqual(Object.keys(after.tasks), ['kept'])
})

test('cycles and malformed references are isolated, never looped over', () => {
  const store = storeOf([node('a', 'b', { ancestry: 'complete' }), node('b', 'c', { ancestry: 'complete' }), node('c', 'a', { ancestry: 'complete' }),
    node('d', 'a'), node('bad', 42), { nonsense: true }, null])
  const forest = buildForest(store)
  assert.deepEqual(['a', 'b', 'c'].map((taskId) => forest.placement[taskId]), ['cycle', 'cycle', 'cycle'])
  assert.deepEqual(forest.children.a, ['d'])
  assert.equal(forest.placement.bad, 'root')
  assert.ok(store.issues.some((issue) => issue.kind === 'malformed_reference' && issue.taskId === 'bad'))
  assert.equal(store.issues.filter((issue) => issue.kind === 'malformed_task').length, 2)
})

test('wide sibling groups keep every child', () => {
  const tasks = [node('lead', null, { child_count: 3000 })]
  for (let i = 0; i < 3000; i += 1) tasks.push(node(`c${String(i).padStart(4, '0')}`, 'lead'))
  const store = storeOf(tasks)
  const forest = buildForest(store)
  assert.equal(forest.children.lead.length, 3000)
  assert.equal(forest.children.lead[0], 'c0000')
  assert.equal(familySummary(store, forest, 'lead').notLoaded, 0)
})

test('children the backend reports but which are not loaded are counted, not hidden', () => {
  const store = storeOf([node('lead', null, { child_count: 5 }), node('one', 'lead')], { next_after: 'more' })
  const forest = buildForest(store)
  assert.equal(familySummary(store, forest, 'lead').notLoaded, 4)
})

test('1,000 Tasks derive a model quickly (data derivation only, no rendering)', () => {
  const tasks = []
  for (let r = 0; r < 50; r += 1) {
    tasks.push(node(`r${r}`, null, { child_count: 19, assignment: current(`a${r}`), agent: agent(`a${r}`) }))
    for (let c = 0; c < 19; c += 1) tasks.push(node(`r${r}-c${c}`, `r${r}`))
  }
  assert.equal(tasks.length, 1000)
  const started = performance.now()
  const store = storeOf(tasks)
  const forest = buildForest(store)
  const statuses = Object.keys(store.tasks).map((taskId) => statusOf(store, taskId).key)
  const elapsed = performance.now() - started
  assert.equal(forest.tops.length, 50)
  assert.equal(statuses.filter((key) => key === 'running').length, 50)
  assert.ok(elapsed < 1000, `derivation took ${elapsed.toFixed(1)} ms`)
  console.log(`1,000-Task derivation: ${elapsed.toFixed(1)} ms`)
})

// Agents and assignments ---------------------------------------------------------------------

test('Agent replacement is read from assignment history', () => {
  const store = storeOf([node('t', null, {
    assignment: { current: { assignment_id: 'as2', agent_id: 'new', assigned_at: 't2' },
      last_ended: { assignment_id: 'as1', agent_id: 'old', assigned_at: 't0', ended_at: 't1', ended_reason: 'reassigned' },
      count: 2, previous_agent_ids: ['old'] },
    agent: agent('new'),
  })])
  const agents = taskAgents(store, 't')
  assert.equal(agents.current.id, 'new')
  assert.equal(agents.replaced, true)
  assert.deepEqual(agents.previous.map((a) => a.id), ['old'])
  assert.equal(statusOf(store, 't').key, 'running')
})

test('a Task without an Agent is not invented an owner', () => {
  const store = storeOf([node('free', null, { status: 'in_progress' })])
  const agents = taskAgents(store, 'free')
  assert.deepEqual([agents.current, agents.last, agents.replaced], [null, null, false])
  const status = statusOf(store, 'free')
  assert.equal(status.key, 'stopped')
  assert.equal(status.certainty, 'unknown')
})

test('removed Agents and historical assignments stay visible as history', () => {
  const store = storeOf([node('t', null, {
    status: 'completed',
    assignment: { current: null, last_ended: { assignment_id: 'as', agent_id: 'gone', assigned_at: 't0', ended_at: 't1', ended_reason: 'removed' }, count: 1, previous_agent_ids: ['gone'] },
    agent: agent('gone', 'stopped', { role: 'last', removed: true, removal: { state: 'removed', mode: 'single', failure: null, removed_at: 't1' } }),
  })])
  const agents = taskAgents(store, 't')
  assert.equal(agents.last.removed, true)
  assert.equal(statusOf(store, 't').key, 'completed') // removal of the Agent does not change the Task's outcome
})

test('removal states: in progress, blocked', () => {
  const removing = storeOf([node('t', null, { status: 'in_progress', assignment: current('a'),
    agent: agent('a', 'running', { removal: { state: 'removing', mode: 'single', failure: null, removed_at: null } }),
    waiting_context: { kind: 'removal', label: 'Removal in progress' } })])
  assert.equal(statusOf(removing, 't').key, 'removing')
  const blocked = storeOf([node('t', null, { status: 'in_progress', assignment: current('a'),
    agent: agent('a', 'stopped', { removal: { state: 'blocked', mode: 'single', failure: 'x', removed_at: null } }),
    waiting_context: { kind: 'recovery', label: 'Removal blocked' } })])
  const status = statusOf(blocked, 't')
  assert.deepEqual([status.key, status.label, status.attention], ['recovery', 'Removal blocked', true])
})

// Status rules --------------------------------------------------------------------------------

const withAgent = (status, extra = {}, agentExtra = {}) => storeOf([node('t', null, { status, assignment: current('a'), agent: agent('a', agentExtra.status || 'running', agentExtra), ...extra })])

test('each status in the list is distinct and reachable', () => {
  const cases = {
    needs_input: withAgent('waiting', { waiting_context: { kind: 'user', label: 'Waiting for you' } }, { status: 'waiting', asks_user: true }),
    provider_wait: withAgent('waiting', { waiting_context: { kind: 'provider', label: 'Waiting for provider usage', condition: 'usage_limited' } }, { status: 'waiting' }),
    paused: storeOf([node('t', null, { status: 'paused', control_intent: 'paused' })]),
    blocked: withAgent('blocked', { blockers: ['dependency_x'], waiting_context: { kind: 'dependency', label: 'Waiting for dependencies' } }, { status: 'waiting' }),
    waiting_helpers: withAgent('waiting', { waiting_context: { kind: 'delegation', label: 'Waiting for delegated results' } }, { status: 'waiting' }),
    reviewing: withAgent('in_progress', { continuation: { phase: 'reviewing', mode: 'reviewing', current_assignment: true } }),
    running: withAgent('in_progress'),
    needs_owner: storeOf([node('t', null, { status: 'in_progress', responsibility_awaiting: true })]),
    completed: storeOf([node('t', null, { status: 'completed', integration: { integration_id: 'i', destination_kind: 'project', status: 'noop', validation_status: 'not_run', created_at: 't', applied_at: null } })]),
    integrated: storeOf([node('t', null, { status: 'completed', integration: { integration_id: 'i', destination_kind: 'project', status: 'applied', validation_status: 'not_run', created_at: 't', applied_at: 't' } })]),
    canceled: storeOf([node('t', null, { status: 'canceled', control_intent: 'canceled' })]),
    stopped: withAgent('in_progress', {}, { status: 'stopped' }),
    recovery: withAgent('in_progress', { waiting_context: { kind: 'recovery', label: 'Recovery required' } }, { status: 'stopped' }),
  }
  const seen = new Set()
  for (const [expected, store] of Object.entries(cases)) {
    const status = statusOf(store, 't')
    assert.equal(status.key, expected, expected)
    assert.ok(STATES[status.bead], `${expected} uses a known bead`)
    seen.add(status.key)
  }
  assert.equal(seen.size, 13)
  assert.ok(Object.keys(STATUS).every((key) => typeof STATUS[key].label === 'string' && !/%/.test(STATUS[key].label)))
})

test('stopped is never reported as failed, and finished is not success', () => {
  const stopped = statusOf(withAgent('in_progress', {}, { status: 'stopped' }), 't')
  assert.equal(stopped.key, 'stopped')
  assert.doesNotMatch(stopped.label, /fail/i)
  const finished = statusOf(withAgent('in_progress', {}, { status: 'finished' }), 't')
  assert.equal(finished.key, 'stopped')
  assert.equal(finished.certainty, 'unknown')
  // A recorded failure is shown as the result, not as the Task's status.
  const failedChild = storeOf([node('c', 'p', { status: 'in_progress', delegation: { delegation_id: 'd', parent_task_id: 'p', status: 'result_ready',
    result: { outcome: 'failed', phase: 'delivered', integration: null } } })])
  assert.equal(resultView(failedChild.tasks.c).label, 'Reported failure')
  assert.notEqual(statusOf(failedChild, 'c').key, 'completed')
})

test('verification is never inferred from Agent text or integration', () => {
  const store = withAgent('completed', { title: 'All tests verified and passing', integration: { integration_id: 'i', destination_kind: 'project',
    status: 'applied', validation_status: 'not_run', created_at: 't', applied_at: 't' } }, { status: 'finished' })
  assert.equal(integrationView(store.tasks.t).verified, false)
  const weird = withAgent('completed', { integration: { integration_id: 'i', destination_kind: 'project', status: 'applied', validation_status: 'passed', created_at: 't', applied_at: 't' } })
  assert.equal(weird.tasks.t.integration.validationStatus, 'unknown')
  assert.equal(integrationView(weird.tasks.t).verified, false)
})

test('integration pending and complete are distinct from execution', () => {
  const pending = storeOf([node('c', 'p', { status: 'completed', delegation: { delegation_id: 'd', parent_task_id: 'p', status: 'result_ready',
    result: { outcome: 'completed', phase: 'ready', integration: null } } })])
  assert.equal(statusOf(pending, 'c').key, 'completed')
  assert.deepEqual([integrationView(pending.tasks.c).status, integrationView(pending.tasks.c).scope], ['not_started', 'lead'])
  const conflict = storeOf([node('c', 'p', { status: 'completed', delegation: { delegation_id: 'd', parent_task_id: 'p', status: 'result_ready',
    result: { outcome: 'completed', phase: 'blocked', integration: { integration_id: 'i', status: 'conflict', applied_at: null } } } })])
  assert.equal(statusOf(conflict, 'c').key, 'completed')
  assert.equal(integrationView(conflict.tasks.c).attention, true)
  const applied = storeOf([node('c', 'p', { status: 'completed', delegation: { delegation_id: 'd', parent_task_id: 'p', status: 'acknowledged',
    result: { outcome: 'completed', phase: 'accepted', integration: { integration_id: 'i', status: 'applied', applied_at: 't' } } } })])
  assert.equal(statusOf(applied, 'c').key, 'integrated')
  const running = withAgent('in_progress', { integration: { integration_id: 'i', destination_kind: 'project', status: 'applying', validation_status: 'not_run', created_at: 't', applied_at: null } })
  assert.equal(statusOf(running, 't').key, 'running')
  assert.equal(integrationView(running.tasks.t).label, 'Combining')
})

test('repeated orchestration rounds and planned work are counts, not Tasks', () => {
  const store = withAgent('in_progress', { rounds: 3, plan_revision: 4,
    delegations: { requested: 2, materialized: 1, result_ready: 0, acknowledged: 5, canceled: 1 },
    planned: { unmaterialized: 2, by_disposition: { pending: 1, conflict: 1 } } })
  assert.deepEqual(helpersView(store.tasks.t), { proposed: 2, active: 1, resultsReady: 0, closed: 5, canceled: 1, rounds: 3 })
  assert.deepEqual(plannedView(store.tasks.t), { count: 2, byDisposition: { pending: 1, conflict: 1 } })
  assert.equal(store.tasks.t.planRevision, 4)
  assert.equal(Object.keys(store.tasks).length, 1)
  assert.equal(plannedView(storeOf([node('x')]).tasks.x), null)
})

test('missing or stale fields lower certainty instead of guessing', () => {
  const raw = node('t')
  delete raw.status
  delete raw.assignment
  raw.waiting_context = { kind: 'new_kind_from_a_future_backend', label: 'Something' }
  raw.delegations = null
  const store = storeOf([raw, node('u', null, { status: 'exploded', child_count: -3, depth: 'deep' })])
  assert.equal(store.tasks.t.lifecycle, null)
  assert.deepEqual(store.tasks.t.delegations, { requested: 0, materialized: 0, result_ready: 0, acknowledged: 0, canceled: 0 })
  assert.equal(statusOf(store, 't').certainty, 'unknown')
  assert.equal(store.tasks.u.lifecycle, null)
  assert.equal(store.tasks.u.childCount, null)
  assert.equal(store.tasks.u.depth, null)
  assert.ok(store.issues.some((issue) => issue.kind === 'missing_field' && issue.field === 'status'))
  assert.ok(store.issues.some((issue) => issue.kind === 'unknown_value' && issue.field === 'status'))
  assert.ok(store.issues.some((issue) => issue.kind === 'unknown_value' && issue.field === 'waiting_context.kind'))
  // A stale count smaller than the visible history is corrected upward.
  const stale = storeOf([node('s', null, { assignment: { ...current('a'), count: 0 }, agent: agent('a') })])
  assert.equal(stale.assignments.s.count, 1)
  assert.equal(statusOf(stale, 's').key, 'running')
})

test('a malformed page keeps nothing it cannot read', () => {
  const store = applyPage(emptyStore(), { tasks: 'nope' })
  assert.deepEqual(Object.keys(store.tasks), [])
  assert.equal(store.issues[0].kind, 'malformed_page')
})

test('text fields are bounded', () => {
  const store = storeOf([node('t', null, { title: 'x'.repeat(5000), blockers: Array.from({ length: 100 }, (_, i) => `code_${i}`) })])
  assert.equal(store.tasks.t.title.length, 200)
  assert.equal(store.tasks.t.blockers.length, 32)
})

// Reading pages and selection ------------------------------------------------------------------

test('the reader follows cursors with one request per page and stops on the last page', async () => {
  const urls = []
  const fetcher = async (url) => {
    urls.push(url)
    const index = urls.length - 1
    return { ok: true, json: async () => contract.paged[index] }
  }
  const store = await readTasks({ fetcher, base: 'http://127.0.0.1:8000', limit: 3 })
  assert.equal(urls.length, contract.paged.length)
  assert.equal(urls[0], 'http://127.0.0.1:8000/workspace/tasks?limit=3')
  assert.ok(urls[1].includes('after='))
  assert.equal(Object.keys(store.tasks).length, 12)
  assert.equal(store.complete, true)
  assert.equal(tasksUrl('b', { root: 'release', limit: 2 }), 'b/workspace/tasks?limit=2&root=release')
})

test('the reader stops at the page limit and marks the store incomplete', async () => {
  const fetcher = async () => ({ ok: true, json: async () => page([node(`t${Math.random()}`)], { next_after: 'more' }) })
  const store = await readTasks({ fetcher, base: 'b', maxPages: 3 })
  assert.equal(store.pages, 3)
  assert.equal(store.complete, false)
  await assert.rejects(readTasks({ fetcher: async () => ({ ok: false, status: 503 }), base: 'b' }), /unavailable \(503\)/)
})

test('selection is a Task id that survives refreshes and Agent replacement', () => {
  const store = applyPage(emptyStore(), contract.all)
  const forest = buildForest(store)
  const selected = select(selectionFromRoute({ params: {} }), forest, 'flaky')
  assert.deepEqual(selected, { taskId: 'flaky', focusId: 'flaky', expanded: ['release', 'tests'] })
  assert.deepEqual(routeParamsFor(selected), { task: 'flaky' })
  assert.deepEqual(selectionFromRoute({ params: { task: 'flaky' } }).taskId, 'flaky')
  assert.deepEqual(toggleExpanded(selected, 'tests').expanded, ['release'])
  assert.equal(reconcile(selected, store).status, 'present')
  const partial = applyPage(emptyStore(), page([node('other')], { next_after: 'x' }))
  assert.equal(reconcile(selected, partial).status, 'not_loaded')
  const gone = applyPage(emptyStore(), page([node('other')]))
  const result = reconcile(selected, gone)
  assert.equal(result.status, 'missing')
  assert.deepEqual(result.selection.expanded, [])
  assert.equal(result.selection.taskId, 'flaky') // kept, so the UI can say what happened
})
