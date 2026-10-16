import test from 'node:test'
import assert from 'node:assert/strict'
import { initialAutonomousChoice, autonomyEligible, creationPayload } from '../src/autonomousCreation.mjs'
import { topologyOf, layoutHierarchy, indexHierarchy, zoomCamera, focusCamera, fitCamera, retainCamera, nodeWidth, nodeHeight } from '../src/hierarchyLayout.mjs'
import { buildAgentTree } from '../src/buildAgentTree.mjs'
import { filterAgentTree, effectiveCollapsedIds, matchesStatus } from '../src/agentListView.mjs'
import { executionLabel, attentionAgents } from '../src/agentPresentation.mjs'

const agent = (id, parent = null, extra = {}) => ({ agent_id: id, parent_id: parent, task: id, status: 'running', ...extra })

test('eligible new Codex defaults on; initial Mock stays inactive', () => {
  assert.equal(initialAutonomousChoice, true)
  assert.equal(creationPayload(' Work ', 'mock', 'read-only', initialAutonomousChoice).autonomous, false)
  assert.deepEqual(creationPayload(' Work ', 'codex', 'workspace-write', initialAutonomousChoice), { task: 'Work', agent_type: 'codex', sandbox: 'workspace-write', autonomous: true })
})
test('explicit off intent survives provider changes; children and unsupported providers cannot opt in', () => {
  const choice = false
  for (const provider of ['codex', 'mock', 'unknown', 'codex']) assert.equal(creationPayload('Work', provider, 'read-only', choice).autonomous, false)
  assert.equal(autonomyEligible('unknown'), false)
  assert.equal(creationPayload('Work', 'codex', 'read-only', true, 'parent').autonomous, false)
})
test('layout draws real connections and has stable IDs through status refresh', () => {
  const rows = [agent('root'), agent('child', 'root'), agent('grandchild', 'child')]
  const initial = topologyOf(buildAgentTree(rows), new Set())
  assert.equal(topologyOf(buildAgentTree(rows.map((row) => ({ ...row, status: 'finished' }))), new Set()), initial)
  const layout = layoutHierarchy(initial)
  assert.deepEqual(layout.nodes.map((n) => n.id), ['root', 'child', 'grandchild'])
  assert.equal(layout.edges.length, 2)
  assert.ok(layout.nodes[2].y > layout.nodes[1].y)
})
test('new child keeps identities and camera independent of the layout', () => {
  const camera = { x: -100, y: 18, scale: 0.7 }
  const previous = structuredClone(camera)
  layoutHierarchy(topologyOf(buildAgentTree([agent('root'), agent('new', 'root')]), new Set()))
  assert.deepEqual(camera, previous)
})
test('collapsed branches summarize working and attention descendants', () => {
  const tree = buildAgentTree([agent('root'), agent('child', 'root'), agent('deep', 'child', { status: 'waiting', waiting_context: { kind: 'user' } })])
  const root = indexHierarchy(tree).get('root')
  assert.equal(root.descendants, 2); assert.equal(root.active, 1); assert.equal(root.attention, 1)
  assert.equal(layoutHierarchy(topologyOf(tree, new Set(['root']))).nodes.length, 1)
})
test('deep search reveals necessary paths without changing saved collapse choices', () => {
  const tree = buildAgentTree([agent('root'), agent('child', 'root'), agent('deep', 'child')])
  const saved = new Set(['root', 'child', 'unrelated'])
  const filtered = filterAgentTree(tree, 'deep', 'all')
  assert.equal(layoutHierarchy(topologyOf(filtered, effectiveCollapsedIds(filtered, saved, true))).nodes.length, 3)
  assert.deepEqual([...saved], ['root', 'child', 'unrelated'])
  assert.equal(layoutHierarchy(topologyOf(tree, effectiveCollapsedIds(tree, saved, false))).nodes.length, 1)
})
test('large, deep, malformed and orphan hierarchies remain finite without fabricated links', () => {
  const rows = Array.from({ length: 500 }, (_, i) => agent(String(i), i ? String(i - 1) : null))
  const deep = layoutHierarchy(topologyOf(buildAgentTree(rows), new Set()))
  assert.equal(deep.nodes.length, 500)
  const broken = layoutHierarchy(topologyOf(buildAgentTree([agent('a', 'b'), agent('b', 'a'), agent('orphan', 'missing')]), new Set()))
  assert.equal(broken.nodes.length, 3)
  assert.equal(broken.edges.length, 1)
})
test('replaced Workers remain distinct Agent identities without inventing a second Task', () => {
  const rows = [agent('old', null, { task_id: 'task', historical_assignment: true }), agent('new', null, { task_id: 'task' })]
  assert.equal(indexHierarchy(buildAgentTree(rows)).size, 2)
  assert.equal(new Set(rows.map((r) => r.task_id)).size, 1)
})
test('waiting labels use authoritative classification; coordination stays in Active scope', () => {
  const parent = agent('parent', null, { status: 'stopped', waiting_context: { kind: 'delegation', label: 'Waiting for delegated results' } })
  const user = agent('user', null, { status: 'waiting', waiting_context: { kind: 'user', label: 'Waiting for you' } })
  assert.equal(matchesStatus(parent, 'active'), true)
  assert.equal(matchesStatus(parent, 'history'), false)
  assert.deepEqual(attentionAgents([parent, user]), [user])
  assert.equal(executionLabel(parent), 'Waiting for delegated results')
  assert.equal(executionLabel({ status: 'waiting', waiting_context: null }), 'Waiting')
  assert.equal(executionLabel({ status: 'stopped', task_status: 'canceled' }), 'Stopped')
})
test('zoom is bounded; fit and explicit focus center stable coordinates', () => {
  const camera = { x: 0, y: 0, scale: 1 }
  assert.equal(zoomCamera(camera, 100, 600, 500).scale, 1.6)
  assert.equal(zoomCamera(camera, 0.0001, 600, 500).scale, 0.005)
  const layout = layoutHierarchy(topologyOf(buildAgentTree([agent('root')]), new Set()))
  assert.equal(fitCamera(layout, 600, 500).scale, 1)
  const focus = focusCamera(layout.nodes[0], camera, 600, 500)
  assert.equal(focus.x + nodeWidth / 2, 300); assert.equal(focus.y + nodeHeight / 2, 250)
  assert.deepEqual(camera, { x: 0, y: 0, scale: 1 })
})

test('collapse keeps the visible root anchored instead of losing it outside the canvas', () => {
  const tree = buildAgentTree([agent('root'), ...Array.from({ length: 60 }, (_, i) => agent(String(i), 'root'))])
  const before = layoutHierarchy(topologyOf(tree, new Set()))
  const after = layoutHierarchy(topologyOf(tree, new Set(['root'])))
  const camera = { x: 24 - before.nodes[0].x, y: 24, scale: 1 }
  const retained = retainCamera(before, after, camera, 900, 600)
  assert.equal(after.nodes[0].y + retained.y, 24)
  assert.equal(after.nodes[0].x + retained.x, 24)
  assert.equal(camera.y, 24 - before.nodes[0].y)
  const fit = fitCamera(before, 900, 600)
  assert.ok(before.height * fit.scale <= 600)
})
