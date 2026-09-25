import { displayColor } from './agentIdentity.mjs'

export const statuses = ['all', 'running', 'waiting', 'finished', 'stopped']

export function matchesSearch(agent, search) {
  const query = search.trim().toLowerCase()
  return [agent.display_name, agent.task, agent.agent_id, agent.agent_type, agent.status]
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

export const viewFilters = ['active', 'waiting', 'history', 'all', 'running', 'finished', 'stopped']
// Active and All are normal scopes; only explicit narrowing reveals paths.
export function shouldExpandFilteredPaths(search, status, selectedColors = new Set()) {
  return Boolean(search.trim()) || selectedColors.size > 0 || (status !== 'all' && status !== 'active')
}

export function matchesStatus(agent, status) {
  if (status === 'active') return agent.status === 'running' || agent.status === 'waiting'
  if (status === 'history') return agent.status === 'finished' || agent.status === 'stopped'
  return status === 'all' || agent.status === status
}

export function matchesColors(agent, selectedColors = new Set()) {
  return selectedColors.size === 0 || selectedColors.has(displayColor(agent.display_color))
}

// Keep only matches and their ancestors, without changing the source tree.
export function filterAgentTree(nodes, search, status, selectedColors = new Set()) {
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
      && matchesStatus(node.agent, status)
      && matchesColors(node.agent, selectedColors)
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

export function listEmptyMessage(agents, tree, search, status, selectedColors = new Set()) {
  if (!agents.length) return 'No agents yet'
  if (tree.length) return ''
  const hasSearch = Boolean(search.trim())
  const hasColors = selectedColors.size > 0
  if (hasSearch && !hasColors) return status === 'all'
    ? 'No agents match this search'
    : 'No agents match this search and the selected status'
  if (hasColors) {
    const criteria = [hasSearch && 'search', status !== 'all' && status !== 'active' && 'status', hasColors && 'color filter'].filter(Boolean)
    return `No agents match the selected ${criteria.join(', ').replace(/, ([^,]*)$/, ' and $1')}`
  }
  if (status === 'active') return 'No active agents. Choose History or All agents to see past work.'
  return 'No agents match the selected status'
}
