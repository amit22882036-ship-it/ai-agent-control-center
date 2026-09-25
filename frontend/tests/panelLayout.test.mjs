import test from 'node:test'
import assert from 'node:assert/strict'
import { panelLimits, readLayout, clampWidth, dragWidth, keyboardWidth } from '../src/panelLayout.mjs'
import { agentContext } from '../src/agentPresentation.mjs'
import { buildAgentTree } from '../src/buildAgentTree.mjs'
import { shouldExpandFilteredPaths, parentIds, filterAgentTree, visibleAgentIds, effectiveCollapsedIds, viewFilters, listEmptyMessage } from '../src/agentListView.mjs'

test('panel preferences preserve inspector width and discard obsolete sidebar settings', () => {
  assert.deepEqual(readLayout({ getItem: () => null }), { right: 380 })
  assert.deepEqual(readLayout({ getItem: () => JSON.stringify({ left: 180, right: 450, collapsed: true }) }), { right: 450 })
  assert.deepEqual(readLayout({ getItem: () => '{broken' }), readLayout({ getItem() { throw Error() } }))
  assert.equal('left' in readLayout({ getItem: () => '{"left":"160","right":9999}' }), false)
  assert.equal(readLayout({ getItem: () => '{"right":9999}' }).right, 600)
})
test('drag directions, bounds and keyboard reset match both panel edges', () => {
  assert.equal(dragWidth('left', 144, 100, 120), 164)
  assert.equal(dragWidth('right', 380, 100, 120), 360)
  for (const side of ['left', 'right']) {
    assert.equal(clampWidth(side, -999), panelLimits[side].min)
    assert.equal(clampWidth(side, Infinity), panelLimits[side].default)
    assert.equal(clampWidth(side, 9999), panelLimits[side].max)
    assert.equal(keyboardWidth(side, 200, 'Home'), panelLimits[side].default)
    assert.equal(keyboardWidth(side, 200, 'End'), panelLimits[side].max)
    assert.equal(keyboardWidth(side, 200, 'Escape'), null)
  }
  assert.equal(keyboardWidth('right', 380, 'ArrowLeft'), 396)
  assert.equal(keyboardWidth('left', 144, 'ArrowLeft'), 128)
})
test('compact context omits duplicate names, normalizes whitespace and bounds long assignments', () => {
  assert.equal(agentContext({ display_name: 'Review API', task: 'Review API' }), '')
  assert.equal(agentContext({ display_name: 'Engineer', task: 'Build\n the API' }), 'Build the API')
  assert.ok(agentContext({ task: 'x'.repeat(500) }).length <= 64)
})
test('active view retains inactive ancestors but excludes unrelated history; all statuses remain accessible', () => {
  const agents = [
    { agent_id: 'parent', status: 'finished' },
    { agent_id: 'child', parent_id: 'parent', status: 'running', task: 'API' },
    { agent_id: 'waiting', parent_id: 'child', status: 'waiting' },
    { agent_id: 'old', status: 'stopped' },
  ]
  const tree = buildAgentTree(agents)
  const filtered = filterAgentTree(tree, '', 'active')
  const collapsed = new Set(['parent', 'child', 'old'])
  assert.deepEqual([...visibleAgentIds(filtered, effectiveCollapsedIds(filtered, collapsed, shouldExpandFilteredPaths('', 'active')))], ['parent'])
  assert.equal(filtered[0].contextOnly, true)
  assert.deepEqual([...collapsed], ['parent', 'child', 'old'])
  assert.deepEqual([...visibleAgentIds(filterAgentTree(tree, '', 'history'), new Set())], ['parent', 'old'])
  assert.deepEqual([...visibleAgentIds(filterAgentTree(tree, 'API', 'active'), new Set())], ['parent', 'child'])
  assert.equal(viewFilters.length, new Set(viewFilters).size)
  for (const value of ['all', 'running', 'waiting', 'finished', 'stopped']) assert.ok(viewFilters.includes(value))
  assert.match(listEmptyMessage(agents, [], '', 'active'), /History or All/)
  assert.equal(effectiveCollapsedIds(tree, collapsed, false), collapsed)
})


test('Active permits manual toggles, collapse all and expand all using saved choices', () => {
  const tree = buildAgentTree([
    { agent_id: 'root', status: 'finished' },
    { agent_id: 'child', parent_id: 'root', status: 'running' },
    { agent_id: 'leaf', parent_id: 'child', status: 'waiting' },
  ])
  const active = filterAgentTree(tree, '', 'active')
  const forced = shouldExpandFilteredPaths('', 'active')
  assert.equal(forced, false) // Same flag gates all three controls in AgentList.
  assert.equal(shouldExpandFilteredPaths('   ', 'active'), false)
  const collapsed = new Set(['child'])
  assert.equal(effectiveCollapsedIds(active, collapsed, forced), collapsed)
  assert.deepEqual([...visibleAgentIds(active, collapsed)], ['root', 'child'])
  const toggled = new Set(collapsed)
  toggled.delete('child')
  assert.deepEqual([...visibleAgentIds(active, effectiveCollapsedIds(active, toggled, forced))], ['root', 'child', 'leaf'])
  const allCollapsed = new Set(parentIds(tree))
  assert.deepEqual([...visibleAgentIds(active, effectiveCollapsedIds(active, allCollapsed, forced))], ['root'])
  assert.deepEqual([...visibleAgentIds(active, effectiveCollapsedIds(active, new Set(), forced))], ['root', 'child', 'leaf'])
})

test('search and focused filters reveal paths temporarily then restore Active collapse and hidden selection', () => {
  const tree = buildAgentTree([
    { agent_id: 'root', status: 'running' },
    { agent_id: 'child', parent_id: 'root', status: 'waiting', task: 'authentication' },
    { agent_id: 'old', parent_id: 'root', status: 'finished' },
  ])
  const saved = new Set(['root', 'unrelated'])
  for (const [search, status, match] of [
    ['authentication', 'active', 'child'], ['authentication', 'all', 'child'],
    ['', 'waiting', 'child'], ['', 'history', 'old'], ['', 'finished', 'old'],
  ]) {
    const filtered = filterAgentTree(tree, search, status)
    const forced = shouldExpandFilteredPaths(search, status)
    assert.equal(forced, true)
    const effective = effectiveCollapsedIds(filtered, saved, forced)
    assert.equal(visibleAgentIds(filtered, effective).has(match), true)
    assert.equal(effective.has('unrelated'), true)
    assert.deepEqual([...saved], ['root', 'unrelated'])
    const restored = effectiveCollapsedIds(filterAgentTree(tree, '', 'active'), saved, shouldExpandFilteredPaths('', 'active'))
    assert.equal(restored, saved)
    assert.equal(visibleAgentIds(filterAgentTree(tree, '', 'active'), restored).has('child'), false)
  }
  for (const status of ['running', 'waiting', 'history', 'finished', 'stopped']) {
    assert.equal(shouldExpandFilteredPaths('', status), true)
  }
  assert.equal(shouldExpandFilteredPaths('', 'all'), false)
})
