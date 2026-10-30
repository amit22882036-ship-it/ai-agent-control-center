// What the Task page says about one Task, in plain language, from the backend's own records only:
// the Task projection (store), GET /tasks/{id}, /assignments, /orchestration, /integrations, the
// lead's /delegations (a helper's recorded result) and POST /tasks/{id}/control-impact. Nothing
// here infers progress, invents an outcome or treats an agent's words as checked facts. Pure.

// A recorded time in a short, readable form (or null when none was recorded).
export const when = (value) => {
  const time = Date.parse(value || '')
  return Number.isFinite(time) ? new Date(time).toLocaleString('en-GB', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : null
}

const humanize = (code) => String(code || '').replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase())

export const BLOCKER_TEXT = {
  dependency_incomplete: 'A task it depends on hasn’t finished',
  dependency_paused: 'A task it depends on is paused',
  dependency_canceled: 'A task it depends on was canceled',
  resource_conflict: 'Another task holds a resource it needs',
  resource_deadlock: 'Tasks are waiting on each other for resources',
  resource_unavailable: 'A resource it needs is unavailable',
  external_resource_unavailable: 'A resource outside the Control Center is unavailable',
  work_overlap: 'Another task is working on the same part',
  child_canceled: 'One of its helper tasks was canceled',
  delegation_pending: 'Waiting for its helpers',
  delegation_requested: 'Waiting for its helpers',
}
export const blockerText = (code) => BLOCKER_TEXT[code] || humanize(code)

const CONTINUATION_TEXT = {
  waiting: 'Waiting for its helpers to finish.',
  launching: 'Starting its helpers.',
  independent: 'Working on its own part while its helpers work.',
  reviewing: 'Reviewing its helpers’ results.',
  finishing: 'Finishing up after its helpers.',
  recovery_required: 'Needs inspection before it continues.',
  stopped: 'Its work with helpers was stopped.',
  completed: 'Finished its rounds with helpers.',
}

const DECISION_TEXT = {
  not_authorized: 'This task’s agent works without planning helpers.',
  not_recorded: 'No plan has been recorded for the current agent.',
  independent: 'The lead decided to do the work itself.',
  proposed: 'The lead proposed a plan; no helper task was started from it.',
  promoted: 'The lead turned its plan into helper tasks.',
}

const DISPOSITION_TEXT = {
  ready: 'Ready to start', pending: 'Waiting to start', blocked: 'Held', conflict: 'In conflict with other work',
  unauthorized: 'Not allowed', invalid: 'Rejected as invalid', reuse_available: 'Can reuse earlier work', direct: 'Requested directly',
  launching: 'Starting', started: 'Started', in_progress: 'Working', waiting: 'Waiting', completed_pending_review: 'Done; waiting for the lead’s review',
  result_ready: 'Result ready for the lead', acknowledged: 'Reviewed by the lead', canceled: 'Canceled', recovery_required: 'Needs inspection',
  completed: 'Completed', paused: 'Paused',
}
export const dispositionText = (code) => DISPOSITION_TEXT[code] || humanize(code)

const ENDED_TEXT = {
  completed: 'finished its work', stopped: 'was stopped', reassigned: 'was replaced', paused: 'stopped when the task was paused',
  blocked: 'stopped when the task was blocked', resource_conflict: 'stopped for a resource conflict', resource_deadlock: 'stopped for a resource deadlock',
  external_resource_unavailable: 'stopped: a resource was unavailable', removed: 'was removed', canceled: 'stopped when the task was canceled',
}

const ok = (resource) => (resource && !resource.error && resource.data ? resource.data : null)

// How the work is going: the state in words, what holds it, the plan and its rounds.
export function progressView({ task, status, detail, orchestration }) {
  const facts = []
  const intent = detail?.control_intent ?? task?.controlIntent
  if (intent === 'paused') facts.push({ tone: 'neutral', text: 'Paused. Nothing runs until it is resumed.' })
  if (intent === 'canceled' && detail?.status !== 'canceled' && task?.lifecycle !== 'canceled') {
    facts.push({ tone: 'alert', text: 'Canceling did not finish for every part of this task. Canceling again completes it.' })
  }
  if (detail?.stop_required) facts.push({ tone: 'alert', text: 'A stop was requested and is not confirmed yet.' })
  const blockers = (detail?.active_blockers || []).map((item) => ({ code: item.reason_code, text: blockerText(item.reason_code), source: item.source_task_id || null, hard: Boolean(item.hard) }))
  const replanning = (detail?.replanning_reasons || []).map((item) => ({ code: item.reason_code, text: blockerText(item.reason_code), source: item.source_task_id || null }))
  const continuation = orchestration?.continuation || (task?.continuation ? { phase: task.continuation.phase, mode: task.continuation.mode } : null)
  const decision = orchestration?.decision || null
  const plan = decision ? {
    state: decision.state,
    text: DECISION_TEXT[decision.state] || 'The plan state is not reported.',
    revision: decision.revision,
    // Written by the agent; shown as its words, never as a checked fact.
    rationale: decision.rationale || null,
    rejected: Boolean(decision.promotion_rejected),
    superseded: decision.superseded ? `An earlier agent proposed revision ${decision.superseded.revision}; that is history, not this agent’s plan.` : null,
    lastRejection: decision.last_rejection ? `${humanize(decision.last_rejection.kind || 'message')} was refused: ${decision.last_rejection.reason || 'no reason reported'}` : null,
    items: (decision.promotions || []).map((item) => ({ id: item.proposal_id, revision: item.revision, text: dispositionText(item.disposition),
      admission: item.admission_disposition !== item.disposition ? dispositionText(item.admission_disposition) : null, childTaskId: item.child_task_id || null })),
  } : null
  const issues = (orchestration?.materializations || []).filter((item) => item.last_error || item.phase === 'recovery_required')
    .map((item) => ({ id: item.delegation_id, text: item.phase === 'recovery_required' ? 'A helper’s start needs inspection.' : `A helper could not start yet (attempt ${item.attempts}).` }))
  return {
    status: status ? { label: status.label, tone: status.tone, detail: status.detail } : null,
    facts, blockers, replanning,
    continuation: continuation ? { phase: continuation.phase, text: CONTINUATION_TEXT[continuation.phase] || humanize(continuation.phase) } : null,
    plan, issues,
    rounds: task?.rounds ?? 0,
    planRevision: task?.planRevision ?? decision?.revision ?? null,
    planned: task?.planned?.unmaterialized ?? 0,
  }
}

// Assignment history, newest first: every agent that held the task and how it ended.
export function assignmentsView(assignments, store) {
  return [...(assignments || [])].reverse().map((entry) => {
    const agent = store.agents[entry.agent_id] || null
    return {
      id: entry.assignment_id, agentId: entry.agent_id, agent,
      name: agent?.name || (agent ? null : 'Earlier agent'),
      current: !entry.ended_at,
      text: entry.ended_at ? (ENDED_TEXT[entry.ended_reason] || 'ended') : 'working on it now',
      removed: Boolean(agent?.removed) || entry.ended_reason === 'removed',
      assignedAt: entry.assigned_at, endedAt: entry.ended_at,
    }
  })
}

const INTEGRATION_TEXT = {
  preparing: 'Being prepared', ready: 'Prepared, not applied yet', applying: 'Being applied', applied: 'Applied', noop: 'Nothing to apply (no changes)',
  conflict: 'Conflicting changes: nothing was applied', failed: 'Could not be applied; the destination was left as it was',
  source_changed: 'The work changed after preparation; nothing was applied', destination_changed: 'The destination changed after preparation; nothing was applied',
  recovery_required: 'Interrupted: the destination needs inspection',
}

function integrationEntry(record) {
  return {
    id: record.integration_id, kind: record.destination_kind, status: record.status, text: INTEGRATION_TEXT[record.status] || humanize(record.status),
    attention: ['conflict', 'failed', 'source_changed', 'destination_changed', 'recovery_required'].includes(record.status),
    changed: record.changed_paths || [], changedMore: record.changed_paths_remaining || 0,
    conflicts: record.conflict_paths || [], conflictsMore: record.conflict_paths_remaining || 0,
    reason: ['applied', 'noop'].includes(record.status) ? null : record.failure_reason || null,
    createdAt: record.created_at, appliedAt: record.applied_at, validation: record.validation_status || 'not_run',
  }
}

// The result, kept in five separate statements: what the agent reported, what the backend
// recorded, what was independently checked, what was combined into the lead's copy, and what was
// applied to the project. A child's work goes into its lead; a root's work goes to the project.
export function resultView({ task, detail, integrations, parentDelegations, taskId }) {
  const isChild = Boolean(task?.parentId || detail?.parent_task_id)
  const lifecycle = detail?.status ?? task?.lifecycle
  const records = (integrations || []).map(integrationEntry)
  const latest = (kind) => records.filter((item) => item.kind === kind).at(-1) || null
  const delegation = (parentDelegations || []).find((item) => item.child_task_id === taskId) || null
  const result = delegation?.results?.at(-1) || null
  const reported = isChild
    ? (result ? { available: true, outcome: result.outcome, text: `The helper reported: ${humanize(result.outcome)}.`, summary: result.summary || null }
      : { available: false, text: lifecycle === 'completed' ? 'No structured report was recorded for this helper.' : 'Nothing reported yet.' })
    : { available: false, text: 'A lead’s own account of its work is in its output; the Control Center doesn’t summarize it.' }
  const recorded = lifecycle === 'completed' ? { text: 'The task is marked completed.', done: true }
    : lifecycle === 'canceled' ? { text: 'The task was canceled.', done: true }
      : { text: 'The task is not finished.', done: false }
  if (result) {
    recorded.result = { delivered: 'Its result was delivered to its lead.', accepted: 'Its lead accepted the result.', rejected: 'Its lead rejected the result.',
      recorded: 'Its result was recorded; it hasn’t reached its lead yet.', ready: 'Its result is ready for its lead.', blocked: 'Its result is held.',
      failure: 'Its result records a failure.' }[result.phase] || `Result: ${humanize(result.phase)}.`
  }
  return {
    isChild,
    reported,
    recorded,
    verified: { text: 'Not independently verified. The Control Center does not run your tests; any test results are the agent’s own report.' },
    integrated: isChild ? latest('task') : null,
    applied: isChild ? null : latest('project'),
    history: records,
  }
}

// Which lifecycle controls apply, mirroring the backend's rules; the backend still decides.
// Resume needs a pause this task owns (its own pause operation, or an older single-task pause);
// a pause started from a task above it is released there.
export function controlState({ task, detail, operations = null }) {
  const lifecycle = detail?.status ?? task?.lifecycle
  const intent = detail?.control_intent ?? task?.controlIntent ?? 'active'
  const none = (reason) => ({ pause: { available: false, reason }, resume: { available: false, reason }, cancel: { available: false, reason } })
  if (!lifecycle) return none('The task’s state isn’t loaded yet.')
  if (lifecycle === 'completed') return none('Finished work can’t be paused, resumed or canceled.')
  if (lifecycle === 'canceled') return none('This task was canceled.')
  if (intent === 'canceled') {
    return { pause: { available: false, reason: 'Canceling is in progress.' }, resume: { available: false, reason: 'Canceling is in progress.' },
      cancel: { available: true, retry: true, reason: null } }
  }
  if (intent === 'paused' || lifecycle === 'paused') {
    const owned = Boolean(pauseHeld(operations) || detail?.legacy_pause)
    const settled = lifecycle === 'paused' && intent === 'paused'
    const resume = !settled ? { available: false, reason: 'Pausing hasn’t finished yet.' }
      : operations === null ? { available: false, reason: 'Checking who paused it…' }
        : owned ? { available: true, reason: null } : { available: false, reason: 'It was paused together with a task above it; resume that task.' }
    return { pause: { available: false, reason: 'Already paused.' }, resume, cancel: { available: true, reason: null } }
  }
  return { pause: { available: true, reason: null }, resume: { available: false, reason: 'The task isn’t paused.' }, cancel: { available: true, reason: null } }
}

const sorted = (list) => [...(list || [])].sort()

// What a Pause or Cancel would affect (POST /tasks/{id}/control-impact), in words, and a
// fingerprint to notice when it changed before the action is sent.
export function impactView(impact, store) {
  if (!impact) return null
  const title = (id) => store.tasks[id]?.title || null
  const named = (ids) => ids.map((id) => ({ id, title: title(id) }))
  const workers = (impact.workers_to_stop || []).map((item) => ({ taskId: item.task_id, agentId: item.agent_id,
    name: store.agents[item.agent_id]?.name || null, task: title(item.task_id) }))
  const verb = impact.action === 'cancel' ? 'canceled' : 'paused'
  const lines = []
  const affected = impact.affected_task_ids || []
  lines.push(`${affected.length} task${affected.length === 1 ? '' : 's'} will be ${verb}: this one${affected.length > 1 ? ` and ${affected.length - 1} below it` : ''}.`)
  if (workers.length) lines.push(`${workers.length} running agent${workers.length === 1 ? '' : 's'} will be stopped.`)
  if ((impact.completed_descendants || []).length) lines.push(`${impact.completed_descendants.length} finished task${impact.completed_descendants.length === 1 ? ' stays' : 's stay'} as ${impact.completed_descendants.length === 1 ? 'it is' : 'they are'}.`)
  if ((impact.external_dependent_task_ids || []).length) lines.push(`${impact.external_dependent_task_ids.length} task${impact.external_dependent_task_ids.length === 1 ? '' : 's'} elsewhere that depend${impact.external_dependent_task_ids.length === 1 ? 's' : ''} on this work will be blocked.`)
  if ((impact.replanning_task_ids || []).length) lines.push(`${impact.replanning_task_ids.length} task${impact.replanning_task_ids.length === 1 ? '' : 's'} will be told to reconsider ${impact.replanning_task_ids.length === 1 ? 'its' : 'their'} plan.`)
  const fingerprint = JSON.stringify([impact.action, sorted(affected), sorted(impact.external_dependent_task_ids),
    sorted((impact.workers_to_stop || []).map((item) => `${item.task_id}:${item.agent_id}`)), sorted(impact.replanning_task_ids), sorted(impact.blocked_task_ids)])
  return { action: impact.action, affected: named(affected), external: named(impact.external_dependent_task_ids || []), workers,
    replanning: named(impact.replanning_task_ids || []), untouched: (impact.completed_descendants || []).length + (impact.canceled_descendants || []).length,
    unloaded: affected.filter((id) => !store.tasks[id]).length, lines, fingerprint }
}

export const sameImpact = (a, b) => Boolean(a && b && a.fingerprint === b.fingerprint)

// The pause this task started, if it holds one (GET /tasks/{id}/control-operations), so Resume can
// say what it releases. Pause ownership is per operation: Resume releases only this one.
export function pauseHeld(operations) {
  const held = (operations || []).filter((item) => item.action === 'pause' && !item.released).at(-1)
  return held ? { affected: held.impact?.affected_task_ids || [], status: held.status, failures: held.failures || [] } : null
}

export { ok as resourceData }
