import assert from 'node:assert/strict'
import test from 'node:test'
import { buildAgentTree } from '../src/buildAgentTree.mjs'
import { matchesSearch, countStatuses, filterAgentTree, parentIds, effectiveCollapsedIds, visibleAgentIds, listEmptyMessage } from '../src/agentListView.mjs'

const agents = [
  { agent_id: 'root', task: 'Review project', agent_type: 'codex', status: 'finished', parent_id: null },
  { agent_id: 'child', task: 'Inspect authentication', agent_type: 'codex', status: 'waiting', parent_id: 'root' },
  { agent_id: 'leaf', task: 'Write tests', agent_type: 'mock', status: 'running', parent_id: 'child' },
  { agent_id: 'sibling', task: 'Review docs', agent_type: 'mock', status: 'stopped', parent_id: 'root' },
  { agent_id: 'other', task: 'Review data', agent_type: 'codex', status: 'running', parent_id: null },
]
const tree = buildAgentTree(agents)
const ids = (nodes, collapsed = new Set()) => [...visibleAgentIds(nodes, collapsed)]

test('searched child under collapsed root is visible and selected visibility uses the effective state', () => {
  const filtered = filterAgentTree(tree, 'authentication', 'all')
  const effective = effectiveCollapsedIds(filtered, new Set(['root']), true)
  assert.deepEqual(ids(filtered, effective), ['root', 'child'])
  assert.equal(visibleAgentIds(filtered, effective).has('child'), true)
  assert.equal(visibleAgentIds(filtered, effective).has('leaf'), false)
})

test('status-matching child under collapsed root is visible', () => {
  const filtered = filterAgentTree(tree, '', 'waiting')
  assert.deepEqual(ids(filtered, effectiveCollapsedIds(filtered, new Set(['root']), true)), ['root', 'child'])
})

test('nested matching descendant is visible through multiple collapsed ancestors', () => {
  const filtered = filterAgentTree(tree, 'write', 'running')
  assert.deepEqual(ids(filtered, effectiveCollapsedIds(filtered, new Set(['root', 'child']), true)), ['root', 'child', 'leaf'])
})

test('clearing filters restores exactly the original collapsed state', () => {
  const collapsed = new Set(['root', 'child'])
  const filtered = filterAgentTree(tree, 'write', 'all')
  assert.deepEqual(ids(filtered, effectiveCollapsedIds(filtered, collapsed, true)), ['root', 'child', 'leaf'])
  const restored = effectiveCollapsedIds(tree, collapsed, false)
  assert.equal(restored, collapsed)
  assert.deepEqual([...collapsed], ['root', 'child'])
  assert.deepEqual(ids(tree, restored), ['root', 'other'])
})

test('only necessary paths expand; unrelated and leaf collapse state is not mutated', () => {
  const collapsed = new Set(['root', 'child', 'other'])
  const filtered = filterAgentTree(tree, 'authentication', 'all')
  const effective = effectiveCollapsedIds(filtered, collapsed, true)
  assert.deepEqual([...effective], ['child', 'other'])
  assert.deepEqual([...collapsed], ['root', 'child', 'other'])
  assert.notEqual(effective, collapsed)
})

test('search matches task, ID, type and status case-insensitively with whitespace trimmed', () => {
  for (const query of ['AUTHENTICATION', ' CHILD ', 'Codex', 'WAITING']) {
    assert.equal(matchesSearch(agents[1], query), true)
  }
  assert.equal(matchesSearch(agents[1], 'missing'), false)
  assert.equal(matchesSearch(agents[1], '  '), true)
})

test('searching a child preserves ancestors but excludes unrelated siblings', () => {
  const filtered = filterAgentTree(tree, 'write', 'all')
  assert.deepEqual(ids(filtered), ['root', 'child', 'leaf'])
  assert.equal(filtered[0].contextOnly, true)
  assert.equal(filtered[0].children[0].contextOnly, true)
  assert.equal(filtered[0].children[0].children[0].contextOnly, false)
})

test('status filters retain only matches and necessary ancestor context', () => {
  assert.deepEqual(ids(filterAgentTree(tree, '', 'running')), ['root', 'child', 'leaf', 'other'])
  assert.deepEqual(ids(filterAgentTree(tree, '', 'waiting')), ['root', 'child'])
  assert.deepEqual(ids(filterAgentTree(tree, '', 'finished')), ['root'])
  assert.deepEqual(ids(filterAgentTree(tree, '', 'stopped')), ['root', 'sibling'])
})

test('search and status must match the same agent', () => {
  assert.deepEqual(ids(filterAgentTree(tree, 'review', 'running')), ['other'])
  assert.deepEqual(ids(filterAgentTree(tree, 'authentication', 'running')), [])
})

test('counts derive from full snapshot including empty input', () => {
  assert.deepEqual(countStatuses(agents), { all: 5, running: 2, waiting: 1, finished: 1, stopped: 1 })
  assert.deepEqual(countStatuses([]), { all: 0, running: 0, waiting: 0, finished: 0, stopped: 0 })
})

test('collapsing a root hides descendants without modifying state or tree', () => {
  const before = JSON.stringify({ agents, tree })
  assert.deepEqual(ids(tree, new Set(['root'])), ['root', 'other'])
  filterAgentTree(tree, 'tests', 'all')
  assert.equal(JSON.stringify({ agents, tree }), before)
})

test('nested parents collapse independently and retain state after ancestor expands', () => {
  const collapsed = new Set(['root', 'child'])
  assert.deepEqual(ids(tree, collapsed), ['root', 'other'])
  collapsed.delete('root')
  assert.deepEqual(ids(tree, collapsed), ['root', 'child', 'sibling', 'other'])
  collapsed.delete('child')
  assert.deepEqual(ids(tree, collapsed), agents.map((agent) => agent.agent_id))
})

test('collapse all uses parents; expand all restores every descendant', () => {
  assert.deepEqual(parentIds(tree), ['root', 'child'])
  assert.deepEqual(ids(tree, new Set(parentIds(tree))), ['root', 'other'])
  assert.deepEqual(ids(tree, new Set()), agents.map((agent) => agent.agent_id))
})

test('hidden selection is not replaced or removed by filtering or collapse', () => {
  const selection = { agent_id: 'leaf' }
  const selected = agents.find((agent) => agent.agent_id === selection.agent_id)
  assert.equal(visibleAgentIds(filterAgentTree(tree, '', 'waiting'), new Set()).has(selection.agent_id), false)
  assert.equal(visibleAgentIds(tree, new Set(['root'])).has(selection.agent_id), false)
  assert.equal(selection.agent_id, 'leaf')
  assert.equal(selected, agents[2])
  assert.equal(visibleAgentIds(tree, new Set()).has(selection.agent_id), true)
})

test('empty states distinguish no agents, search, status, and combined filters', () => {
  assert.equal(listEmptyMessage([], [], '', 'all'), 'No agents yet')
  assert.equal(listEmptyMessage(agents, [], 'missing', 'all'), 'No agents match this search')
  assert.equal(listEmptyMessage(agents, [], '', 'waiting'), 'No agents match the selected status')
  assert.equal(listEmptyMessage(agents, [], 'missing', 'waiting'), 'No agents match this search and the selected status')
  assert.equal(listEmptyMessage(agents, tree, '', 'all'), '')
})

test('new snapshots recompute matches for status changes, new children and reconnect', () => {
  const view = (snapshot) => filterAgentTree(buildAgentTree(snapshot), 'authentication', 'waiting')
  assert.deepEqual(ids(view(agents)), ['root', 'child'])
  const changed = agents.map((agent) => ({ ...agent, status: 'stopped' }))
  assert.deepEqual(ids(view(changed)), [])
  const newChild = { ...agents[1], agent_id: 'new-child', parent_id: 'other' }
  assert.deepEqual(ids(view([...changed, newChild])), ['other', 'new-child'])
  assert.deepEqual(ids(view(agents)), ['root', 'child'])
})

test('new branches default to expanded while existing collapsed choices persist', () => {
  const collapsed = new Set(['root'])
  const snapshot = [...agents, { ...agents[2], agent_id: 'new-child', parent_id: 'other' }]
  assert.deepEqual(ids(buildAgentTree(snapshot), collapsed), ['root', 'other', 'new-child'])
})
