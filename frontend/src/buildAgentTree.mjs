export function buildAgentTree(agents) {
  const nodes = new Map(agents.map((agent) => [agent.agent_id, { agent, children: [] }]))
  const roots = []
  const parents = new Map(agents.map((agent) => [agent.agent_id, agent.parent_id]))
  const checked = new Set()
  for (const id of nodes.keys()) {
    const path = new Set()
    let current = id
    while (nodes.has(current) && !checked.has(current)) {
      path.add(current)
      const parent = parents.get(current)
      if (path.has(parent)) {
        parents.set(current, null) // Break only the display edge, preserving records.
        break
      }
      current = parent
    }
    for (const visited of path) checked.add(visited)
  }
  for (const node of nodes.values()) {
    const parent = nodes.get(parents.get(node.agent.agent_id))
    if (parent && parent !== node) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}
