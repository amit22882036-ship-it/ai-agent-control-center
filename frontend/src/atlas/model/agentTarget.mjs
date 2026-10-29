// Which agent an action on a task may address. Atlas is task-centred, but most actions act on an
// agent: only the task's current assignment qualifies. A replaced, ended or removed agent is
// history, and a task without a current assignment has no agent to act on. The backend still
// decides eligibility and refuses anything stale; this only keeps Atlas from aiming at history.

export function actionTarget(store, taskId) {
  const task = store?.tasks?.[taskId]
  if (!task) return { kind: 'missing', agentId: null, assignmentId: null, lastAgentId: null }
  const assignment = store.assignments?.[taskId]
  const current = assignment?.current
  if (!current?.agentId) {
    return { kind: 'none', agentId: null, assignmentId: null, lastAgentId: assignment?.lastEnded?.agentId || null }
  }
  const agent = store.agents?.[current.agentId]
  const removal = agent?.removal?.state || (agent?.removed ? 'removed' : null)
  if (removal === 'removed') return { kind: 'removed', agentId: null, assignmentId: null, lastAgentId: current.agentId }
  return { kind: 'current', agentId: current.agentId, assignmentId: current.id || null, lastAgentId: null, removal }
}

// Whether a target captured earlier (when a form opened or a button rendered) still names the
// task's current agent in the latest store. A change means the person's intent may no longer
// apply, so the action is not sent.
export function targetStillCurrent(store, taskId, captured) {
  if (!captured?.agentId) return false
  const now = actionTarget(store, taskId)
  return now.kind === 'current' && now.agentId === captured.agentId
    && (!captured.assignmentId || !now.assignmentId || now.assignmentId === captured.assignmentId)
}

// The agent Classic should open with when leaving Atlas from this task, or null.
export function classicHandoffAgent(store, taskId) {
  const target = actionTarget(store, taskId)
  return target.kind === 'current' ? target.agentId : null
}
