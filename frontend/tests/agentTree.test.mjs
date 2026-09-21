import assert from 'node:assert/strict'
import test from 'node:test'
import { buildAgentTree } from '../src/buildAgentTree.mjs'

test('roots and empty input', () => {
  assert.deepEqual(buildAgentTree([]), [])
  const roots = buildAgentTree([{ agent_id: 'a', parent_id: null }, { agent_id: 'b', parent_id: null }])
  assert.deepEqual(roots.map((node) => node.agent.agent_id), ['a', 'b'])
})

test('children, grandchildren and siblings retain ancestry regardless of input order', () => {
  const nodes = buildAgentTree([
    { agent_id: 'grandchild', parent_id: 'child' },
    { agent_id: 'child', parent_id: 'root' },
    { agent_id: 'sibling', parent_id: 'root' },
    { agent_id: 'root', parent_id: null },
  ])
  assert.equal(nodes.length, 1)
  assert.equal(nodes[0].agent.agent_id, 'root')
  assert.deepEqual(nodes[0].children.map((node) => node.agent.agent_id), ['child', 'sibling'])
  assert.equal(nodes[0].children[0].children[0].agent.agent_id, 'grandchild')
})

test('missing parent falls back to a root while retaining its children', () => {
  const nodes = buildAgentTree([{ agent_id: 'a', parent_id: 'missing' }, { agent_id: 'b', parent_id: 'a' }])
  assert.equal(nodes[0].agent.agent_id, 'a')
  assert.equal(nodes[0].children[0].agent.agent_id, 'b')
})

test('tree building does not mutate input or reuse children arrays across polls', () => {
  const agents = Object.freeze([Object.freeze({ agent_id: 'a', parent_id: null }), Object.freeze({ agent_id: 'b', parent_id: 'a' })])
  const before = JSON.stringify(agents)
  const first = buildAgentTree(agents)
  first[0].children.pop()
  assert.equal(buildAgentTree(agents)[0].children.length, 1)
  assert.equal(JSON.stringify(agents), before)
})
