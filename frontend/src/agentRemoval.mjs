// Stage 2D.2: how Agent removal, branch removal and responsibility transfer are
// presented. Pure: no fetches and no state. The backend decides availability,
// re-validates every request and is the only source of success.

export function removalState(agent) {
  return agent?.removal?.state || (agent?.removed ? 'removed' : null)
}

export function isRemoved(agent) { return removalState(agent) === 'removed' }

export const REMOVAL_LABELS = { removed: 'Removed', removing: 'Removal in progress', blocked: 'Removal blocked' }

// Removed Agents are history, not active work; a removal still in progress is active.
export function removalScope(agent) {
  const state = removalState(agent)
  return state === 'removed' ? 'history' : state ? 'active' : null
}

// Hide removed Agents without inventing lineage: their remaining descendants are
// shown as top-level entries marked `parentHidden`, never under another parent.
export function withoutRemoved(nodes) {
  const roots = []
  function keep(node, parentHidden, lifted) {
    if (isRemoved(node.agent)) {
      for (const child of node.children) {
        const kept = keep(child, true, lifted)
        if (kept) lifted.push(kept)
      }
      return null
    }
    const children = []
    for (const child of node.children) {
      const kept = keep(child, false, lifted)
      if (kept) children.push(kept)
    }
    return { ...node, children, parentHidden }
  }
  for (const node of nodes) {
    const lifted = []
    const kept = keep(node, false, lifted)
    if (kept) roots.push(kept)
    roots.push(...lifted)
  }
  return roots
}

function allRemoved(node) {
  return isRemoved(node.agent) && node.children.every(allRemoved)
}

// Topmost removed Agents whose whole branch is removed (and has descendants):
// collapsed by default so history stays visible without crowding the canvas.
export function fullyRemovedBranches(nodes) {
  const ids = []
  const pending = [...nodes]
  while (pending.length) {
    const node = pending.pop()
    if (node.children.length && allRemoved(node)) ids.push(node.agent.agent_id)
    else pending.push(...node.children)
  }
  return ids
}

export function removedDescendants(node) {
  let count = 0
  const pending = [...node.children]
  while (pending.length) {
    const next = pending.pop()
    if (isRemoved(next.agent)) count++
    pending.push(...next.children)
  }
  return count
}

export function effectiveRemovalCollapse(collapsed, autoCollapsed, expandedHistory) {
  const result = new Set(collapsed)
  for (const id of autoCollapsed) if (!expandedHistory.has(id)) result.add(id)
  return result
}

export function branchCounts(operation) {
  const members = operation?.members || []
  return {
    total: members.length,
    removed: members.filter((m) => m.phase === 'removed').length,
    pending: members.filter((m) => m.phase === 'admitted' || m.phase === 'stopping').length,
    blocked: members.filter((m) => m.phase === 'recovery_required').length,
  }
}

export const MODE_LABELS = { single: 'This agent only', retire_branch: 'Entire branch' }

const REASONS = {
  already_removed: 'This agent has already been removed.',
  removal_in_progress: 'A removal of this agent is already in progress.',
  descendant_removal_in_progress: 'An agent in this branch is already being removed. Finish that removal first.',
  launch_in_progress: 'An agent is being started inside this branch. Try again in a moment.',
  responsibility_transition_in_progress: 'A replanning transition targets this work. Resolve it first.',
  unknown_launch_ownership: 'A delegated launch for this work has an unknown outcome. Inspect it first.',
  not_supported: 'Not supported.',
}

export function reasonText(code) { return REASONS[code] || code.replaceAll('_', ' ') }

const CONSEQUENCES = {
  stop_running_worker: 'Its running work is stopped and its end verified before removal completes.',
  discard_waiting_session: 'Its waiting question is discarded; the session is not reused.',
  task_awaits_new_assignment: 'Its task is kept and waits for a decision.',
  task_awaits_responsibility_transfer: 'Its task is kept and waits until you choose a new owner.',
  task_waits_hybrid_orchestration_bound: 'Its task is kept, but its Hybrid coordination is disabled and cannot move to another agent.',
  task_waits_delegation_handoff_unsettled: 'Its task is kept, but its delegated work cannot move to another agent: reviewing and acknowledging ordinary delegation results is not implemented yet.',
  task_unchanged: 'Its task is unchanged.',
  identity_retired_permanently: 'The agent identity is retired permanently. Its history stays visible.',
  children_continue: 'Its child agents keep working on their own tasks.',
  branch_members_removed: 'Every agent in the branch is removed in one tracked operation.',
  orchestration_disabled: 'Its Hybrid coordination stops. Child results are kept but are not reviewed.',
}

export function consequenceText(code) { return CONSEQUENCES[code] || code.replaceAll('_', ' ') }

const FAILURES = {
  stop_not_confirmed: 'Its process could not be confirmed stopped. Its task and resources stay held.',
  execution_unresolved: 'Its process end is not established. Review Process ownership, then retry.',
  watcher_lost: 'Its process watcher was lost. Use the watcher recovery first.',
  output_draining: 'Its final output is still being read. Retry shortly.',
  worker_not_settled: 'Its worker is still recorded as running. Retry shortly.',
  output_not_persisted: 'Its final output could not be saved. Retry when storage is available.',
  no_process_handle: 'Its process is not attached to this backend. Review Process ownership.',
  assignment_not_settled: 'Its assignment could not be settled. Retry.',
}

export function failureText(failure) {
  const code = (failure || '').split(':', 1)[0]
  return FAILURES[code] || (failure ? failure : 'Needs attention.')
}

export function previewSummary(preview) {
  if (!preview) return null
  const mode = preview.mode
  const members = mode === 'retire_branch' ? preview.members || [] : [preview.agent]
  const continuing = mode === 'single' ? (preview.descendants || []).filter((d) => d.active && !d.removed) : []
  return {
    mode,
    affected: members.length,
    running: members.filter((m) => m.status === 'running' || m.status === 'waiting').length,
    unresolved: (preview.members || []).reduce((sum, m) => sum + (m.unresolved_executions || 0), 0),
    continuing: continuing.length,
    resultsWait: continuing.filter((d) => d.continuation === 'continues_result_waits').length,
    orchestration: Boolean(preview.orchestration?.coordinates_delegated_work),
    available: Boolean(preview.options?.[mode]?.available),
    reasons: preview.options?.[mode]?.reasons || [],
    // The generic Task note is superseded by the explicit responsibility consequences.
    consequences: (preview.consequences || []).filter((code) => code !== 'task_awaits_new_assignment'
      || !(preview.consequences || []).some((other) => other.startsWith('task_awaits_responsibility') || other.startsWith('task_waits_'))),
  }
}

// Only the fields the backend accepts; confirmation is a deliberate client step,
// the plan_token binds the request to the reviewed impact.
export function removalRequest(preview, { confirmed, requestKey, reason }) {
  if (!preview || confirmed !== true || !requestKey || !preview.options?.[preview.mode]?.available) return null
  const note = (reason || '').trim()
  return { request_key: requestKey, plan_token: preview.plan_token, mode: preview.mode, task_disposition: 'preserve',
    reason: note ? note.slice(0, 2000) : null }
}

const TRANSFER_PROVIDERS = ['mock', 'codex', 'claude', 'gemini']

export function transferRequest(state, { confirmed, requestKey, agentType, sandbox, reason }) {
  if (!state?.transferable || confirmed !== true || !requestKey) return null
  const note = (reason || '').trim()
  return { request_key: requestKey, expected_owner_assignment_id: state.removed_owner?.assignment_id || null,
    agent_type: TRANSFER_PROVIDERS.includes(agentType) ? agentType : 'mock',
    sandbox: TRANSFER_PROVIDERS.includes(agentType) && agentType !== 'mock' ? (sandbox || 'read-only') : null,
    reason: note ? note.slice(0, 2000) : null }
}

export function responsibilitySummary(state) {
  if (!state) return { tone: 'neutral', text: 'Responsibility is loading.' }
  if (state.owner) return { tone: 'clear', text: 'The task has an active owner.' }
  if (state.awaiting_transfer && state.transferable) return { tone: 'waiting', text: 'The task waits for you to choose a new owner.' }
  if (state.awaiting_transfer) return { tone: 'blocked', text: 'The task waits for a new owner, but one cannot be assigned yet.' }
  if (['completed', 'canceled'].includes(state.task_status)) return { tone: 'clear', text: `The task is ${state.task_status}.` }
  return { tone: 'neutral', text: 'No owner change is pending.' }
}

// Guidance for ordinary Delegations only: the existing explicit Task integration may
// bring a completed Child's workspace into this Task. It is never a Delegation
// acknowledgement and never clears the delegation_requested blocker.
export function integrationGuidance(state) {
  const candidates = state?.orchestration_kind === 'delegation_handoff' ? state.integration_candidates || [] : []
  return {
    available: candidates.filter((c) => c.integration_available),
    unavailable: candidates.filter((c) => !c.integration_available),
    note: 'Integration copies a completed child\'s workspace changes into this task\'s workspace. It does not acknowledge the delegation, does not unblock this task and does not complete it.',
  }
}

export function readRemovalError(status, data) {
  const detail = data?.detail
  if (detail && typeof detail === 'object') {
    return { code: detail.code || 'error', message: detail.message || 'The request was refused.',
      preview: detail.preview || null, removal: detail.removal || null, reasons: detail.reasons || [],
      transfer: detail.transfer || null, responsibility: detail.responsibility || null }
  }
  if (status === 503) return { code: 'unavailable', message: 'The backend could not complete this. Please retry.', reasons: [] }
  return { code: 'error', message: typeof detail === 'string' ? detail : 'The request could not be completed.', reasons: [] }
}

export function newRequestKey(random = globalThis.crypto) {
  return random?.randomUUID ? random.randomUUID() : `request-${Date.now()}-${Math.random().toString(16).slice(2)}`
}
