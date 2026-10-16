import test from 'node:test'
import assert from 'node:assert/strict'
import { filterAgentTree, effectiveCollapsedIds } from '../src/agentListView.mjs'
import { readProjection } from '../src/readProjection.mjs'
import { navigatorRows, revealPath, focusedEvidence, assignmentLabel, familyContext } from '../src/hierarchyContext.mjs'
import { layoutHierarchy, topologyOf, connectorPath, directionalTarget, selectionCamera, nodeWidth, nodeHeight } from '../src/hierarchyLayout.mjs'
const row = (id, parent_id = null, extra = {}) => ({ agent_id: id, parent_id, display_name: id, status: 'running', ...extra })
const rows = [row('root'), row('child', 'root'), row('deep', 'child'), row('sibling', 'root'), row('other')]
const layout = () => layoutHierarchy(topologyOf(familyContext(rows, 'root').tree, new Set()))

test('tree-local navigator includes ancestors, siblings and descendants but excludes unrelated roots', () => {
  const family = familyContext(rows, 'deep')
  const visible = navigatorRows(family.tree, new Set())
  assert.deepEqual(visible.map(n => [n.agent.agent_id, n.depth]), [['root',0],['child',1],['deep',2],['sibling',1]])
  assert.equal(visible.some(n => n.agent.agent_id === 'other'), false)
})
test('local search reveals deep matches through collapsed ancestors and restores saved choices', () => {
  const family = familyContext(rows, 'deep'), collapsed = new Set(['root','child','unrelated'])
  const filtered = filterAgentTree(family.tree, 'deep', 'all')
  assert.deepEqual(navigatorRows(filtered,effectiveCollapsedIds(filtered,collapsed,true)).map(n=>n.agent.agent_id), ['root','child','deep'])
  assert.equal(filtered[0].contextOnly,true)
  assert.deepEqual([...collapsed],['root','child','unrelated'])
  assert.deepEqual(navigatorRows(family.tree,effectiveCollapsedIds(family.tree,collapsed,false)).map(n=>n.agent.agent_id),['root'])
  const revealed = revealPath(family.indexed,'deep',collapsed)
  assert.deepEqual([...revealed],['unrelated'])
  assert.ok(navigatorRows(family.tree,revealed).some(n=>n.agent.agent_id === 'deep'))
})
test('single and incomplete navigator trees do not invent parents or branches', () => {
  const single = navigatorRows(familyContext([row('one')],'one').tree,new Set())
  assert.equal(single.length,1); assert.equal(single[0].children.length,0); assert.equal(single[0].depth,0)
  const orphan = navigatorRows(familyContext([row('child','missing')],'child').tree,new Set())
  assert.equal(orphan.length,1); assert.equal(orphan[0].agent.parent_id,'missing'); assert.equal(orphan[0].depth,0)
})
test('canceled tasks do not turn old blockers, input or delegations into active remaining work', () => {
  const evidence = focusedEvidence({ context:{task_status:'canceled',waiting_context:{kind:'user'},waiting_question:'Old question'},events:[{kind:'transition',label:'Task canceled'}],blockers:['dependency'],delegations:[{status:'materialized',responsibility:'Old child'}] })
  assert.deepEqual(evidence.remaining,[])
  assert.deepEqual(evidence.recorded,['Task canceled'])
})
test('vertical layout balances parents, separates siblings and joins bottom to top', () => {
  const tree = layout(), [root, child, deep, sibling] = tree.nodes
  assert.ok(root.y + nodeHeight < child.y)
  assert.ok(child.y + nodeHeight < deep.y)
  assert.equal(child.y, sibling.y)
  assert.ok(sibling.x >= child.x + nodeWidth)
  assert.equal(root.x, (child.x + sibling.x) / 2)
  assert.equal(connectorPath(root, child), `M ${root.x+nodeWidth/2} ${root.y+nodeHeight} C ${root.x+nodeWidth/2} 232, ${child.x+nodeWidth/2} 232, ${child.x+nodeWidth/2} ${child.y}`)
  assert.deepEqual(layout(), tree)
})
test('single and two-agent layouts align horizontally without artificial branches', () => {
  for (const agents of [[row('one')], [row('one'), row('two','one')]]) {
    const tree = layoutHierarchy(topologyOf(familyContext(agents,'one').tree,new Set()))
    assert.ok(tree.nodes.every(n=>n.x === 0))
    assert.equal(tree.edges.length, agents.length - 1)
    assert.equal(tree.width,nodeWidth)
  }
})
test('vertical arrow navigation follows children, parents and siblings', () => {
  const nodes = layout().nodes
  assert.equal(directionalTarget(nodes,'root','ArrowDown'),'child')
  assert.equal(directionalTarget(nodes,'deep','ArrowUp'),'child')
  assert.equal(directionalTarget(nodes,'child','ArrowRight'),'sibling')
  assert.equal(directionalTarget(nodes,'sibling','ArrowLeft'),'child')
  assert.equal(directionalTarget(nodes,'deep','ArrowDown'),null)
})
test('selection camera includes nearby parent when it fits without shrinking below readable scale', () => {
  const tree = layoutHierarchy(topologyOf(familyContext([row('root'),row('child','root')], 'child').tree, new Set()))
  const camera = selectionCamera(tree.nodes[1], 650, 520, tree.nodes)
  for (const node of tree.nodes) {
    assert.ok(node.y * camera.scale + camera.y >= 0)
    assert.ok((node.y + nodeHeight) * camera.scale + camera.y <= 520)
  }
  assert.ok(camera.scale >= 0.85)
})
test('historical, canceled and finished assignments are never labeled current', () => {
  assert.equal(assignmentLabel(row('unknown')),'Assigned responsibility')
  assert.equal(assignmentLabel(row('live',null,{task_id:'task',historical_assignment:false})),'Current assignment')
  for (const extra of [{historical_assignment:true},{status:'stopped'},{status:'finished'},{task_status:'canceled'},{task_status:'completed'}]) assert.equal(assignmentLabel(row('old',null,extra)), 'Last assignment')
})
test('late hierarchy and activity responses cannot replace latest selection even if fetch ignores abort', async () => {
  for (const kind of ['hierarchy','activity']) {
    let release, state
    const old = new AbortController(), current = new AbortController()
    const publish = value => { state = typeof value === 'function' ? value(state) : value }
    const first = readProjection(`/agents/A/${kind}`, old.signal, publish, () => new Promise(resolve=>{release=resolve}))
    old.abort()
    await readProjection(`/agents/B/${kind}`, current.signal, publish, async()=>({ok:true,json:async()=>({agent_id:'B'})}))
    release({ok:true,json:async()=>({agent_id:'A'})}); await first
    assert.equal(state.data.agent_id,'B')
  }
})
test('projection errors retain only same-identity evidence and are explicitly marked stale', async () => {
  let state = {path:'/agents/A/activity',data:{agent_id:'A'},error:false}
  const publish = value => { state = typeof value === 'function' ? value(state) : value }
  const fail = async()=>({ok:false})
  await readProjection('/agents/A/activity',new AbortController().signal,publish,fail)
  assert.equal(state.data.agent_id,'A'); assert.equal(state.error,true)
  await readProjection('/agents/B/activity',new AbortController().signal,publish,fail)
  assert.equal(state.data,null); assert.equal(state.error,true)
})


test('fresh canceled task state suppresses remaining work from an older activity snapshot', () => {
  const activity = {context:{task_status:'in_progress'},events:[],blockers:['dependency_incomplete'],delegations:[]}
  assert.deepEqual(focusedEvidence(activity,'canceled').remaining,[])
  assert.deepEqual(focusedEvidence(activity).remaining,['dependency incomplete'])
})
