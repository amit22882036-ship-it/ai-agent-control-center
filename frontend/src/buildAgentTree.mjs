export function buildAgentTree(agents) {
  const nodes = new Map(agents.map((agent) => [agent.agent_id, { agent, children: [] }]))
  const roots = []
  for (const node of nodes.values()) {
    const parent = nodes.get(node.agent.parent_id)
    if (parent && parent !== node) parent.children.push(node)
    else roots.push(node)
  }
  return roots
}
