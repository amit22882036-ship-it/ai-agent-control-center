export const statuses = ['all', 'running', 'waiting', 'finished', 'stopped']

export function matchesSearch(agent, search) {
  const query = search.trim().toLowerCase()
  return [agent.task, agent.agent_id, agent.agent_type, agent.status]
    .some((value) => String(value ?? '').toLowerCase().includes(query))
}

export function countStatuses(agents) {
  const counts = Object.fromEntries(statuses.map((status) => [status, 0]))
  counts.all = agents.length
  for (const agent of agents) {
    if (agent.status !== 'all' && agent.status in counts) counts[agent.status]++
  }
  return counts
}

// Keep only matches and their ancestors, without changing the source tree.
export function filterAgentTree(nodes, search, status) {
  return nodes.flatMap((node) => {
    const children = filterAgentTree(node.children, search, status)
    const matches = matchesSearch(node.agent, search)
      && (status === 'all' || node.agent.status === status)
    return matches || children.length ? [{ ...node, children, contextOnly: !matches }] : []
  })
}

export function parentIds(nodes) {
  return nodes.flatMap((node) => node.children.length
    ? [node.agent.agent_id, ...parentIds(node.children)] : [])
}

export function effectiveCollapsedIds(filteredTree, collapsed, filtering) {
  if (!filtering) return collapsed
  const effective = new Set(collapsed)
  // Every retained parent has a matching descendant; reveal only those paths.
  for (const id of parentIds(filteredTree)) effective.delete(id)
  return effective
}

export function visibleAgentIds(nodes, collapsed) {
  const ids = new Set()
  function visit(branches) {
    for (const node of branches) {
      ids.add(node.agent.agent_id)
      if (!collapsed.has(node.agent.agent_id)) visit(node.children)
    }
  }
  visit(nodes)
  return ids
}

export function listEmptyMessage(agents, tree, search, status) {
  if (!agents.length) return 'No agents yet'
  if (tree.length) return ''
  if (search.trim()) return status === 'all'
    ? 'No agents match this search'
    : 'No agents match this search and the selected status'
  return 'No agents match the selected status'
}
