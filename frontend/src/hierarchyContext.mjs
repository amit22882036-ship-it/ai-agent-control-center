import { buildAgentTree } from './buildAgentTree.mjs'
import { indexHierarchy } from './hierarchyLayout.mjs'
// Flatten only the authoritative family tree, preserving real depth and collapse choices.
export function navigatorRows(tree, collapsed) {
  const rows = [], pending = tree.map(node => ({ node, depth: 0 })).reverse()
  while (pending.length) {
    const { node, depth } = pending.pop()
    rows.push({ ...node, depth })
    if (!collapsed.has(node.agent.agent_id)) {
      for (let i = node.children.length - 1; i >= 0; i--) pending.push({ node: node.children[i], depth: depth + 1 })
    }
  }
  return rows
}

export function assignmentLabel(agent) {
  if (agent.historical_assignment || ['finished', 'stopped'].includes(agent.status) || ['completed', 'canceled'].includes(agent.task_status)) return 'Last assignment'
  return agent.task_id && agent.historical_assignment === false ? 'Current assignment' : 'Assigned responsibility'
}

export function familyContext(agents, selectedId) {
  const forest = buildAgentTree(agents)
  for (const root of forest) {
    const indexed = indexHierarchy([root])
    if (indexed.has(selectedId)) {
      const byId = new Map(agents.map((agent) => [agent.agent_id, agent]))
      let malformed = false
      for (const id of indexed.keys()) {
        const seen = new Set()
        let next = id
        while (byId.has(next)) {
          if (seen.has(next)) { malformed = true; break }
          seen.add(next); next = byId.get(next).parent_id
        }
      }
      return { tree: [root], indexed, malformed }
    }
  }
  return { tree: [], indexed: new Map(), malformed: false }
}

export function revealPath(indexed, id, collapsed) {
  const parents = new Map()
  for (const [parent, entry] of indexed) for (const child of entry.children) parents.set(child.agent.agent_id, parent)
  const result = new Set(collapsed), seen = new Set()
  let parent = parents.get(id)
  while (parent && !seen.has(parent)) { seen.add(parent); result.delete(parent); parent = parents.get(parent) }
  return result
}

export function focusedEvidence(activity, currentTaskStatus = activity?.context?.task_status) {
  if (!activity) return { recorded: [], remaining: [] }
  const recorded = activity.events.filter((event) => ['plan', 'promotion', 'replan', 'result', 'transition'].includes(event.kind)).slice(0, 3).map((event) => event.label)
  if (activity.result) recorded.unshift(`Child result: ${activity.result.outcome}. Integration: ${activity.result.integration_status || 'not recorded'}.`)
  if (currentTaskStatus === 'canceled') return { recorded: recorded.slice(0, 3), remaining: [] }
  const remaining = activity.blockers.map((code) => code.replaceAll('_', ' '))
  if (activity.context?.waiting_context?.kind === 'user' && activity.context.waiting_question) {
    remaining.unshift(`Your input: ${activity.context.waiting_question}`)
  }
  for (const item of activity.delegations) {
    if (['requested', 'materialized', 'result_ready'].includes(item.status)) {
      remaining.push(`${item.responsibility} — ${item.status === 'result_ready' ? 'result awaiting review' : item.status === 'requested' ? 'requested' : 'awaiting result'}`)
    }
  }
  return { recorded: recorded.slice(0, 3), remaining: remaining.slice(0, 3) }
}
