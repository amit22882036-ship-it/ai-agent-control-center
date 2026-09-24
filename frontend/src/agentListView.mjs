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
  const result = []
  const pending = nodes.map((node) => ({ node, target: result, expanded: false })).reverse()
  while (pending.length) {
    const entry = pending.pop()
    const { node, target } = entry
    if (!entry.expanded) {
      entry.children = []
      entry.expanded = true
      pending.push(entry)
      for (let i = node.children.length - 1; i >= 0; i--) {
        pending.push({ node: node.children[i], target: entry.children, expanded: false })
      }
      continue
    }
    const children = entry.children
    const matches = matchesSearch(node.agent, search)
      && (status === 'all' || node.agent.status === status)
    if (matches || children.length) target.push({ ...node, children, contextOnly: !matches })
  }
  return result
}

export function parentIds(nodes) {
  const ids = []
  const pending = [...nodes].reverse()
  while (pending.length) {
    const node = pending.pop()
    if (node.children.length) ids.push(node.agent.agent_id)
    for (let i = node.children.length - 1; i >= 0; i--) pending.push(node.children[i])
  }
  return ids
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
  const pending = [...nodes].reverse()
  while (pending.length) {
    const node = pending.pop()
    ids.add(node.agent.agent_id)
    if (!collapsed.has(node.agent.agent_id)) {
      for (let i = node.children.length - 1; i >= 0; i--) pending.push(node.children[i])
    }
  }
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
