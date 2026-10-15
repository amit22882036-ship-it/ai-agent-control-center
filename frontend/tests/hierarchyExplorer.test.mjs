import test from 'node:test'
import assert from 'node:assert/strict'
import { familyContext, revealPath, focusedEvidence } from '../src/hierarchyContext.mjs'
import { layoutHierarchy, topologyOf, selectionCamera, nodeWidth, nodeHeight } from '../src/hierarchyLayout.mjs'

const agent = (id, parent = null, extra = {}) => ({ agent_id: id, parent_id: parent, task: id, status: 'running', ...extra })
const rows = [agent('root'), agent('child', 'root'), agent('grandchild', 'child'), agent('sibling', 'root'), agent('unrelated'), agent('other-child', 'unrelated')]

test('root, child and grandchild open the same family, excluding unrelated trees', () => {
  for (const id of ['root', 'child', 'grandchild']) {
    const family = familyContext(rows, id)
    assert.equal(family.tree[0].agent.agent_id, 'root')
    assert.deepEqual([...family.indexed.keys()], ['root', 'child', 'grandchild', 'sibling'])
    assert.equal(family.indexed.get(id).agent.agent_id, id)
  }
})
test('orphans, a single agent and historical replacements keep their own identities', () => {
  const family = familyContext([agent('orphan', 'missing'), agent('old', null, { task_id: 'same', historical_assignment: true }), agent('replacement', null, { task_id: 'same' })], 'orphan')
  assert.deepEqual([...family.indexed.keys()], ['orphan'])
  assert.equal(familyContext([agent('single')], 'single').indexed.get('single').descendants, 0)
  assert.equal(familyContext(rows, 'missing').tree.length, 0)
})
test('cyclic history is finite, marked malformed and never connects an unrelated root', () => {
  const family = familyContext([agent('a', 'b'), agent('b', 'a'), agent('other')], 'a')
  assert.equal(family.indexed.size, 2)
  assert.equal(family.malformed, true)
  assert.equal(layoutHierarchy(topologyOf(family.tree, new Set())).edges.length, 1)
})
test('revealing graph focus expands only its ancestors and preserves saved independent branches', () => {
  const family = familyContext(rows, 'root'), saved = new Set(['root', 'child', 'sibling'])
  assert.deepEqual([...revealPath(family.indexed, 'grandchild', saved)], ['sibling'])
  assert.deepEqual([...saved], ['root', 'child', 'sibling'])
})
test('selection targets exact node at readable bounded scale with no previous camera dependency', () => {
  const family = familyContext(rows, 'grandchild')
  const layout = layoutHierarchy(topologyOf(family.tree, new Set()))
  for (const id of ['root', 'sibling', 'child', 'grandchild', 'root']) {
    const node = layout.nodes.find((n) => n.id === id)
    for (const width of [344, 700, 1300]) {
      const camera = selectionCamera(node, width, 520)
      assert.ok(camera.scale >= 0.85 && camera.scale <= 1)
      assert.equal(camera.x + (node.x + nodeWidth / 2) * camera.scale, width / 2)
      assert.equal(camera.y + (node.y + nodeHeight / 2) * camera.scale, 260)
    }
  }
})
test('status-only SSE snapshots preserve geometry and focused Agent ID', () => {
  const before = familyContext(rows, 'child')
  const after = familyContext(rows.map((r) => ({ ...r, status: 'finished' })), 'child')
  assert.equal(topologyOf(before.tree, new Set()), topologyOf(after.tree, new Set()))
  assert.equal(after.indexed.get('child').agent.agent_id, 'child')
})
test('focused summary distinguishes run results and integration, and excludes closed responsibilities', () => {
  const activity = { events: [{ kind: 'end', label: 'Assignment ended: stopped' }, { kind: 'plan', label: 'Plan revision 2 accepted' }],
    blockers: ['dependency_incomplete'], result: { outcome: 'completed', integration_status: null },
    delegations: [{ status: 'acknowledged', responsibility: 'Closed' }, { status: 'canceled', responsibility: 'Retired' }, { status: 'result_ready', responsibility: 'Review parser' }] }
  const evidence = focusedEvidence(activity)
  assert.deepEqual(evidence.recorded, ['Child result: completed. Integration: not recorded.', 'Plan revision 2 accepted'])
  assert.deepEqual(evidence.remaining, ['dependency incomplete', 'Review parser — result awaiting review'])
})
test('missing or stopped-only evidence never invents completion or remaining work', () => {
  assert.deepEqual(focusedEvidence(null), { recorded: [], remaining: [] })
  assert.deepEqual(focusedEvidence({ events: [{ kind: 'end', label: 'Assignment ended: stopped' }], blockers: [], delegations: [] }), { recorded: [], remaining: [] })
})

test('only authoritative user waiting context adds an input request to remaining work', () => {
  const activity = { events: [], blockers: [], delegations: [], context: { waiting_question: 'Which format?', waiting_context: { kind: 'user' } } }
  assert.deepEqual(focusedEvidence(activity).remaining, ['Your input: Which format?'])
  activity.context.waiting_context.kind = 'dependency'
  assert.deepEqual(focusedEvidence(activity).remaining, [])
})
