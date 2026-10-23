export const statusLabels = { running: 'Working', waiting: 'Waiting', finished: 'Finished', stopped: 'Stopped' }
export function executionLabel(agent) {
  return agent.waiting_context?.label || (agent.status === 'waiting' ? 'Waiting' : statusLabels[agent.status] || agent.status)
}
export function agentName(agent) { return agent?.display_name || 'Untitled agent' }
export function attentionAgents(agents) { return agents.filter((agent) => agent.waiting_context
  ? ['user', 'recovery', 'provider'].includes(agent.waiting_context.kind)
  : !('waiting_context' in agent) && agent.status === 'waiting') }

export function agentContext(agent) {
  const text = (agent.task || '').replace(/\s+/g, ' ').trim()
  if (text === agentName(agent)) return ''
  return text.length > 64 ? text.slice(0, 61).trimEnd() + '…' : text
}

export function workspaceSummary(agents) {
  const working = agents.filter((agent) => agent.status === 'running').length
  const waiting = attentionAgents(agents).length
  return `${working} working · ${waiting} need you`
}
export function attentionContext(agent) {
  const text = agentContext(agent) || 'Waiting for your guidance'
  return text.length > 60 ? text.slice(0, 57).trimEnd() + '…' : text
}
