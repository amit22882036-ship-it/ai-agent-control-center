// The canonical Atlas task model: one normalized store built from GET /workspace/tasks pages, shared
// by every Atlas view (List and Map read the same store and the same derived forest).
//
// Entities are kept apart on purpose: a Task (durable work), an Agent (a worker), an assignment
// (which Agent holds which Task, now and before), a delegation (why a child Task exists), planned
// responsibility (proposed work that is not a Task yet) and integration (whether finished work was
// combined into its lead or applied to the project). Hierarchy comes only from a Task's
// parent_task_id; Agent parents, names and order never create an edge.
//
// Every function here is pure: it never mutates its arguments and never reads the network.

export const TASK_STATUSES = ['pending', 'in_progress', 'waiting', 'blocked', 'paused', 'completed', 'canceled']
export const CONTROL_INTENTS = ['active', 'paused', 'canceled']
export const ANCESTRY = ['complete', 'missing_parent', 'cycle', 'truncated']
export const AGENT_STATUSES = ['running', 'waiting', 'finished', 'stopped']
export const ENDED_REASONS = ['completed', 'stopped', 'reassigned', 'paused', 'blocked', 'resource_conflict', 'resource_deadlock',
  'external_resource_unavailable', 'removed', 'canceled']
export const DELEGATION_STATUSES = ['requested', 'materialized', 'result_ready', 'acknowledged', 'canceled']
export const RESULT_OUTCOMES = ['completed', 'stopped', 'failed', 'canceled', 'unverified']
export const RESULT_PHASES = ['recorded', 'blocked', 'ready', 'delivered', 'accepted', 'rejected', 'failure']
export const INTEGRATION_STATUSES = ['preparing', 'ready', 'applying', 'applied', 'noop', 'conflict', 'failed', 'source_changed',
  'destination_changed', 'recovery_required']
export const CONTINUATION_PHASES = ['waiting', 'launching', 'independent', 'reviewing', 'finishing', 'recovery_required', 'stopped', 'completed']
export const WAITING_KINDS = ['recovery', 'control', 'continuation', 'dependency', 'delegation', 'coordination', 'user', 'provider',
  'unknown', 'removal', 'removed']
export const REMOVAL_STATES = ['removing', 'blocked', 'removed']

const LIMITS = { title: 200, name: 80, label: 120, id: 200, list: 32 }

const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value)

function text(value, limit = LIMITS.label) {
  if (typeof value !== 'string') return null
  return value.length > limit ? value.slice(0, limit - 1) + '…' : value
}

function id(value) {
  return typeof value === 'string' && value.length > 0 && value.length <= LIMITS.id ? value : null
}

function count(value) {
  return Number.isInteger(value) && value >= 0 ? value : null
}

// A field outside its known vocabulary is reported, never guessed: it becomes null plus an issue.
function oneOf(value, allowed, field, issues, taskId, { optional = false } = {}) {
  if (value === null || value === undefined) {
    if (!optional) issues.push({ kind: 'missing_field', taskId, field })
    return null
  }
  if (allowed.includes(value)) return value
  issues.push({ kind: 'unknown_value', taskId, field })
  return null
}

function counts(value, keys) {
  const out = {}
  for (const key of keys) out[key] = isObject(value) ? count(value[key]) ?? 0 : 0
  return out
}

function integration(raw, issues, taskId) {
  if (!isObject(raw)) return null
  return {
    id: id(raw.integration_id),
    destinationKind: raw.destination_kind === undefined ? null : oneOf(raw.destination_kind, ['task', 'project'], 'integration.destination_kind', issues, taskId, { optional: true }),
    status: oneOf(raw.status, INTEGRATION_STATUSES, 'integration.status', issues, taskId),
    // The backend never runs validation yet; anything else is unknown, never "verified".
    validationStatus: raw.validation_status === 'not_run' ? 'not_run' : (raw.validation_status === undefined ? 'not_run' : 'unknown'),
    createdAt: text(raw.created_at, 40),
    appliedAt: text(raw.applied_at, 40),
  }
}

function waitingContext(raw, issues, taskId) {
  if (!isObject(raw)) return null
  const kind = oneOf(raw.kind, WAITING_KINDS, 'waiting_context.kind', issues, taskId)
  return { kind: kind ?? 'unknown', label: text(raw.label) ?? 'Waiting', condition: text(raw.condition, 40) }
}

function providerCondition(raw) {
  if (!isObject(raw)) return null
  const blocking = Array.isArray(raw.blocking)
    ? raw.blocking.slice(0, 8).filter(isObject).map((item) => ({ label: text(item.label), resetAt: text(item.reset_at, 40) }))
    : []
  return { kind: text(raw.kind, 40), label: text(raw.label), blocking }
}

function normalizeAgent(raw, issues, taskId) {
  if (!isObject(raw)) return null
  const agentId = id(raw.agent_id)
  if (!agentId) {
    issues.push({ kind: 'malformed_agent', taskId })
    return null
  }
  const removal = isObject(raw.removal) ? {
    state: oneOf(raw.removal.state, REMOVAL_STATES, 'agent.removal.state', issues, taskId),
    mode: text(raw.removal.mode, 40),
    failure: text(raw.removal.failure, 60),
    removedAt: text(raw.removal.removed_at, 40),
  } : null
  return {
    id: agentId,
    name: text(raw.display_name, LIMITS.name) || null,
    color: text(raw.display_color, 20),
    tool: text(raw.agent_type, 20),
    model: text(raw.model, LIMITS.name),
    effort: text(raw.effort, 32),
    status: oneOf(raw.status, AGENT_STATUSES, 'agent.status', issues, taskId),
    asksUser: raw.asks_user === true,
    // Its question is a plan waiting for the person's approval (Approve, Request changes, Work alone).
    awaitsPlanApproval: raw.awaits_plan_approval === true,
    providerCondition: providerCondition(raw.provider_condition),
    autonomous: raw.autonomous === true,
    removed: raw.removed === true || removal?.state === 'removed',
    removal,
  }
}

function normalizeAssignment(raw, taskId, issues) {
  const value = isObject(raw) ? raw : {}
  if (!isObject(raw)) issues.push({ kind: 'missing_field', taskId, field: 'assignment' })
  const current = isObject(value.current) && id(value.current.agent_id) ? {
    id: id(value.current.assignment_id), agentId: value.current.agent_id, assignedAt: text(value.current.assigned_at, 40),
  } : null
  const ended = value.last_ended
  const lastEnded = isObject(ended) && id(ended.agent_id) ? {
    id: id(ended.assignment_id), agentId: ended.agent_id, assignedAt: text(ended.assigned_at, 40), endedAt: text(ended.ended_at, 40),
    endedReason: oneOf(ended.ended_reason, ENDED_REASONS, 'assignment.last_ended.ended_reason', issues, taskId),
  } : null
  const previous = Array.isArray(value.previous_agent_ids) ? value.previous_agent_ids.map(id).filter(Boolean).slice(0, LIMITS.list) : []
  const total = count(value.count)
  return {
    taskId,
    current,
    lastEnded,
    // A count smaller than what is visibly known is stale; the visible minimum wins.
    count: Math.max(total ?? 0, (current ? 1 : 0) + (lastEnded ? 1 : 0)),
    previousAgentIds: [...new Set(previous)].filter((agentId) => agentId !== current?.agentId),
  }
}

function normalizeDelegation(raw, issues, taskId) {
  if (!isObject(raw)) return null
  const result = isObject(raw.result) ? {
    outcome: oneOf(raw.result.outcome, RESULT_OUTCOMES, 'delegation.result.outcome', issues, taskId),
    phase: oneOf(raw.result.phase, RESULT_PHASES, 'delegation.result.phase', issues, taskId),
    integration: isObject(raw.result.integration) ? {
      id: id(raw.result.integration.integration_id),
      status: oneOf(raw.result.integration.status, INTEGRATION_STATUSES, 'delegation.result.integration.status', issues, taskId),
      appliedAt: text(raw.result.integration.applied_at, 40),
    } : null,
  } : null
  return {
    id: id(raw.delegation_id),
    parentTaskId: id(raw.parent_task_id),
    status: oneOf(raw.status, DELEGATION_STATUSES, 'delegation.status', issues, taskId),
    result,
  }
}

// One backend node -> { task, assignment, agent }. Returns null when the node has no usable id.
export function normalizeNode(raw, source, issues) {
  if (!isObject(raw) || !id(raw.task_id)) {
    issues.push({ kind: 'malformed_task', taskId: null })
    return null
  }
  const taskId = raw.task_id
  const parentId = raw.parent_task_id === null || raw.parent_task_id === undefined ? null : id(raw.parent_task_id)
  if (raw.parent_task_id && !parentId) issues.push({ kind: 'malformed_reference', taskId, field: 'parent_task_id' })
  const continuation = isObject(raw.continuation) ? {
    phase: oneOf(raw.continuation.phase, CONTINUATION_PHASES, 'continuation.phase', issues, taskId),
    mode: text(raw.continuation.mode, 20),
    currentAssignment: raw.continuation.current_assignment === true,
  } : null
  const planned = isObject(raw.planned) ? raw.planned : {}
  const byDisposition = {}
  if (isObject(planned.by_disposition)) {
    for (const [key, value] of Object.entries(planned.by_disposition).slice(0, LIMITS.list)) {
      if (count(value) !== null) byDisposition[text(key, 40)] = value
    }
  }
  const blockers = Array.isArray(raw.blockers) ? raw.blockers.map((code) => text(code, 60)).filter(Boolean).slice(0, LIMITS.list) : []
  const task = {
    id: taskId,
    parentId,
    projectId: id(raw.project_id),
    title: text(raw.title, LIMITS.title) || null,
    lifecycle: oneOf(raw.status, TASK_STATUSES, 'status', issues, taskId),
    controlIntent: oneOf(raw.control_intent, CONTROL_INTENTS, 'control_intent', issues, taskId, { optional: true }) ?? 'active',
    stopRequired: raw.stop_required === true,
    createdAt: text(raw.created_at, 40),
    updatedAt: text(raw.updated_at, 40),
    rootId: id(raw.root_task_id),
    depth: count(raw.depth),
    ancestry: oneOf(raw.ancestry, ANCESTRY, 'ancestry', issues, taskId, { optional: true }),
    childCount: count(raw.child_count),
    blockers,
    blockersMore: count(raw.blockers_more) ?? 0,
    waitingContext: waitingContext(raw.waiting_context, issues, taskId),
    // Why its last agent stopped: a refused control message (kind and the backend's reason).
    rejection: isObject(raw.control_rejection) ? { kind: text(raw.control_rejection.kind, 20), reason: text(raw.control_rejection.reason, 500),
      at: text(raw.control_rejection.at, 40) } : null,
    responsibilityAwaiting: raw.responsibility_awaiting === true,
    transfer: isObject(raw.transfer) ? { phase: text(raw.transfer.phase, 20), toAgentId: id(raw.transfer.to_agent_id) } : null,
    continuation,
    delegation: normalizeDelegation(raw.delegation, issues, taskId),
    delegations: counts(raw.delegations, DELEGATION_STATUSES),
    planned: { unmaterialized: count(planned.unmaterialized) ?? 0, byDisposition },
    planRevision: count(raw.plan_revision),
    rounds: count(raw.rounds) ?? 0,
    integration: integration(raw.integration, issues, taskId),
    source,
  }
  if (task.delegation?.parentTaskId && task.delegation.parentTaskId !== parentId) {
    issues.push({ kind: 'delegation_parent_mismatch', taskId })
  }
  const assignment = normalizeAssignment(raw.assignment, taskId, issues)
  const agent = normalizeAgent(raw.agent, issues, taskId)
  if (agent && agent.id !== (assignment.current?.agentId ?? assignment.lastEnded?.agentId)) {
    issues.push({ kind: 'agent_assignment_mismatch', taskId })
  }
  return { task, assignment, agent }
}

export function emptyStore() {
  return {
    tasks: {}, assignments: {}, agents: {}, order: [], issues: [],
    total: null, nextAfter: null, root: null, complete: false, ancestorsTruncated: false, pages: 0,
  }
}

// Merge one response. Without `append` the page starts a fresh snapshot (a refresh from the top);
// with it, the page continues the previous one. A Task loaded as a page row always wins over the
// same Task seen only as an ancestor (context for placing another Task).
export function applyPage(store, response, { append = false } = {}) {
  const base = append ? store : emptyStore()
  const next = {
    ...base,
    tasks: { ...base.tasks }, assignments: { ...base.assignments }, agents: { ...base.agents },
    order: [...base.order], issues: [...base.issues],
  }
  if (!isObject(response) || !Array.isArray(response.tasks)) {
    next.issues.push({ kind: 'malformed_page', taskId: null })
    return next
  }
  const add = (raw, source) => {
    const node = normalizeNode(raw, source, next.issues)
    if (!node) return
    const existing = next.tasks[node.task.id]
    if (existing?.source === 'page' && source === 'context') return
    if (source === 'page' && existing?.source === 'page') next.issues.push({ kind: 'duplicate_task', taskId: node.task.id })
    if (source === 'page' && existing?.source !== 'page') next.order.push(node.task.id)
    next.tasks[node.task.id] = node.task
    next.assignments[node.task.id] = node.assignment
    if (node.agent) next.agents[node.agent.id] = node.agent
  }
  for (const raw of response.tasks) add(raw, 'page')
  for (const raw of Array.isArray(response.ancestors) ? response.ancestors : []) add(raw, 'context')
  next.total = count(response.total)
  next.nextAfter = typeof response.next_after === 'string' ? response.next_after : null
  next.complete = next.nextAfter === null
  next.root = id(response.root)
  next.ancestorsTruncated = next.ancestorsTruncated || response.ancestors_truncated === true
  next.pages = base.pages + 1
  return next
}

export function taskAgents(store, taskId) {
  const assignment = store.assignments[taskId]
  if (!assignment) return { current: null, last: null, replaced: false, previous: [] }
  const current = assignment.current ? store.agents[assignment.current.agentId] ?? { id: assignment.current.agentId, unknown: true } : null
  const last = !current && assignment.lastEnded
    ? store.agents[assignment.lastEnded.agentId] ?? { id: assignment.lastEnded.agentId, unknown: true } : null
  return {
    current,
    last,
    // Replacement is a fact from assignment history, never from names or Agent parents.
    replaced: assignment.count > 1 && assignment.previousAgentIds.length > 0,
    previous: assignment.previousAgentIds.map((agentId) => store.agents[agentId] ?? { id: agentId, unknown: true }),
  }
}

// Children are ordered by creation, then id, so List and Map show the same stable order.
function byCreation(tasks) {
  return (a, b) => {
    const x = tasks[a].createdAt ?? ''
    const y = tasks[b].createdAt ?? ''
    return x < y ? -1 : x > y ? 1 : a < b ? -1 : a > b ? 1 : 0
  }
}

// The Task forest. `placement` says how each top-level entry got there:
//   root               a real root Task
//   parent_not_loaded  its parent is not in the store yet; more pages may bring it
//   parent_unresolved  every page is loaded and its parent is still absent (the backend's ancestry
//                      walk or ancestor list hit a bound); it is not claimed to be missing
//   missing_parent     its parent does not exist (malformed data reported by the backend)
//   cycle              its parent chain loops (malformed data)
export function buildForest(store) {
  const { tasks } = store
  const ids = Object.keys(tasks)
  const parent = {}
  const placement = {}
  for (const taskId of ids) {
    const task = tasks[taskId]
    if (!task.parentId) placement[taskId] = 'root'
    else if (task.ancestry === 'cycle') placement[taskId] = 'cycle'
    else if (tasks[task.parentId]) parent[taskId] = task.parentId
    else if (task.ancestry === 'missing_parent') placement[taskId] = 'missing_parent'
    else placement[taskId] = store.complete ? 'parent_unresolved' : 'parent_not_loaded'
  }
  // Defensive cycle check on the loaded edges (iterative; deep chains are fine).
  const state = {}
  for (const start of ids) {
    if (state[start]) continue
    const path = []
    let node = start
    while (node && !state[node]) {
      state[node] = 'visiting'
      path.push(node)
      node = parent[node]
    }
    if (node && state[node] === 'visiting') {
      for (let i = path.indexOf(node); i < path.length; i += 1) {
        delete parent[path[i]]
        placement[path[i]] = 'cycle'
      }
    }
    for (const visited of path) state[visited] = 'done'
  }
  const children = {}
  for (const [child, of] of Object.entries(parent)) (children[of] ||= []).push(child)
  const sort = byCreation(tasks)
  for (const list of Object.values(children)) list.sort(sort)
  const orderIndex = new Map(store.order.map((taskId, index) => [taskId, index]))
  const tops = ids.filter((taskId) => !(taskId in parent))
  // Page order first (newest first), then context-only Tasks by creation.
  tops.sort((a, b) => (orderIndex.get(a) ?? Infinity) - (orderIndex.get(b) ?? Infinity) || sort(b, a))
  const depth = {}
  const rootOf = {}
  const stack = tops.map((taskId) => [taskId, 0, taskId])
  while (stack.length) {
    const [taskId, level, top] = stack.pop()
    depth[taskId] = level
    rootOf[taskId] = top
    for (const child of children[taskId] || []) stack.push([child, level + 1, top])
  }
  return { tops, placement, parent, children, depth, rootOf }
}

// Ancestors of a Task from nearest to farthest, as far as they are loaded.
export function pathTo(forest, taskId) {
  const path = []
  const seen = new Set([taskId])
  let node = forest.parent[taskId]
  while (node && !seen.has(node)) {
    path.push(node)
    seen.add(node)
    node = forest.parent[node]
  }
  return path
}

// Counts for the loaded family of one top-level Task (never a percentage).
export function familySummary(store, forest, topId) {
  const summary = { tasks: 0, unmaterialized: 0, loadedChildren: 0, reportedChildren: 0 }
  const stack = [topId]
  while (stack.length) {
    const taskId = stack.pop()
    const task = store.tasks[taskId]
    summary.tasks += 1
    summary.unmaterialized += task.planned.unmaterialized
    const loaded = forest.children[taskId] || []
    summary.loadedChildren += loaded.length
    summary.reportedChildren += task.childCount ?? loaded.length
    stack.push(...loaded)
  }
  // Children the backend reported that are not loaded yet; the UI must say so rather than hide it.
  summary.notLoaded = Math.max(0, summary.reportedChildren - summary.loadedChildren)
  return summary
}
