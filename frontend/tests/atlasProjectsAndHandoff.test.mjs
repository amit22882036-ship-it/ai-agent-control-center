import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { projectDirectory, projectLabel, projectMenu } from '../src/atlas/model/projects.mjs'
import { CLASSIC_HANDOFF_KEY, CLASSIC_HANDOFF_MS, clearClassicHandoff, peekClassicHandoff, writeClassicHandoff } from '../src/atlas/uiMode.mjs'
import { actionTarget, classicHandoffAgent, targetStillCurrent } from '../src/atlas/model/agentTarget.mjs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'

const contract = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const store = applyPage(emptyStore(), contract.all)

function memory() {
  const values = new Map()
  return { getItem: (key) => values.get(key) ?? null, setItem: (key, value) => values.set(key, String(value)), removeItem: (key) => values.delete(key), values }
}

test('registered projects come from GET /projects, including projects without loaded tasks', () => {
  const directory = projectDirectory({ data: { projects: [{ project_id: 'p2', name: 'Docs site' }, { project_id: 'p1', name: 'Release planning' }, { bad: true }] }, error: null })
  assert.equal(directory.state, 'ready')
  assert.deepEqual(directory.projects.map((p) => p.name), ['Docs site', 'Release planning'])
  const menu = projectMenu(directory, null)
  assert.deepEqual(menu.filter((item) => !item.separator).map((item) => [item.label, Boolean(item.checked)]),
    [['All projects', true], ['Docs site', false], ['Release planning', false]])
  assert.equal(projectLabel(directory, 'p2'), 'Docs site')
  assert.equal(projectLabel(directory, 'gone'), 'Unregistered project')
  assert.equal(projectLabel(directory, null), 'No project')
})

test('loading, empty and unavailable project lists are named, never guessed from tasks', () => {
  const loading = projectDirectory({ data: null, error: null })
  assert.equal(loading.state, 'loading')
  assert.deepEqual(projectMenu(loading, null).at(-1), { value: undefined, label: 'Loading projects…', disabled: true })
  assert.equal(projectLabel(loading, 'p1'), 'Loading project…')
  const empty = projectDirectory({ data: { projects: [] }, error: null })
  assert.equal(projectMenu(empty, null).at(-1).label, 'No projects registered yet')
  const unavailable = projectDirectory({ data: null, error: 503 })
  assert.equal(unavailable.state, 'unavailable')
  assert.equal(projectMenu(unavailable, null).at(-1).label, 'Projects couldn’t be loaded')
  assert.equal(projectLabel(unavailable, 'p1'), 'Project name unavailable')
  // A failed re-read keeps the last list and says it is stale.
  const stale = projectDirectory({ data: { projects: [{ project_id: 'p1', name: 'Release planning' }] }, error: 'unreachable' })
  assert.deepEqual([stale.state, stale.stale], ['ready', true])
})

test('a selected project that is not registered stays visible in the menu', () => {
  const directory = projectDirectory({ data: { projects: [{ project_id: 'p1', name: 'Release planning' }] }, error: null })
  const menu = projectMenu(directory, 'p9')
  assert.deepEqual(menu.at(-1), { value: 'p9', label: 'Unregistered project', checked: true })
  assert.equal(menu[0].checked, false)
})

test('the Classic hand-off is short-lived, one-shot and only carries a plausible agent id', () => {
  const storage = memory()
  writeClassicHandoff('agent-1', storage, 1000)
  assert.equal(peekClassicHandoff(storage, 1000 + CLASSIC_HANDOFF_MS), 'agent-1')
  assert.equal(peekClassicHandoff(storage, 1001 + CLASSIC_HANDOFF_MS), null) // expired
  assert.equal(peekClassicHandoff(storage, 999), null) // from the future
  clearClassicHandoff(storage)
  assert.equal(storage.getItem(CLASSIC_HANDOFF_KEY), null)
  writeClassicHandoff('agent-1', storage, 1000)
  writeClassicHandoff(null, storage, 1000) // leaving without a selection clears an older record
  assert.equal(storage.getItem(CLASSIC_HANDOFF_KEY), null)
  storage.setItem(CLASSIC_HANDOFF_KEY, '{not json')
  assert.equal(peekClassicHandoff(storage, 1000), null)
  storage.setItem(CLASSIC_HANDOFF_KEY, JSON.stringify({ agentId: 'x'.repeat(201), at: 1000 }))
  assert.equal(peekClassicHandoff(storage, 1000), null)
  assert.equal(peekClassicHandoff({ getItem() { throw new Error('blocked') } }, 1000), null)
})

test('actions and the hand-off address only a task’s current, non-removed agent', () => {
  assert.deepEqual(actionTarget(store, 'tests'), { kind: 'current', agentId: 'tester', assignmentId: 'as-tests', lastAgentId: null, removal: null })
  assert.equal(classicHandoffAgent(store, 'tests'), 'tester')
  // Replaced agents and the agent that finished last are history.
  assert.deepEqual(actionTarget(store, 'docs'), { kind: 'none', agentId: null, assignmentId: null, lastAgentId: 'docs-new' })
  assert.equal(classicHandoffAgent(store, 'docs'), null)
  // The removed owner of a task waiting for a new one.
  assert.equal(actionTarget(store, 'orphaned-work').kind, 'none')
  assert.equal(classicHandoffAgent(store, 'orphaned-work'), null)
  // No agent at all, and a task that does not exist.
  assert.equal(actionTarget(store, 'flaky').kind, 'none')
  assert.equal(actionTarget(store, 'missing').kind, 'missing')
})

test('a captured target that is no longer current is detected', () => {
  const captured = actionTarget(store, 'tests')
  assert.equal(targetStillCurrent(store, 'tests', captured), true)
  const replaced = applyPage(emptyStore(), { ...contract.all, tasks: contract.all.tasks.map((node) => node.task_id !== 'tests' ? node
    : { ...node, assignment: { ...node.assignment, current: { assignment_id: 'as-new', agent_id: 'tester-2', assigned_at: 't' } },
      agent: { ...node.agent, agent_id: 'tester-2' } }) })
  assert.equal(targetStillCurrent(replaced, 'tests', captured), false)
  assert.equal(targetStillCurrent(store, 'tests', { agentId: null }), false)
})
