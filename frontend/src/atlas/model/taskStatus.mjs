// What Atlas says about a Task's state. One function, one precedence order, used by every view.
//
// Rules:
// - Only facts the backend reported. Agent text is never read for verification or success.
// - No percentages or estimates.
// - "Stopped" never means failed; a failure is shown only when a result recorded it.
// - Execution, child integration and root application stay separate: `taskStatus` describes the
//   Task's own state, `integrationView` describes where its finished work went.
// - When the data cannot support a statement, the answer says so (certainty "unknown").

export const STATUS = {
  recovery: { label: 'Needs attention', bead: 'blocked', tone: 'alert', attention: true },
  removing: { label: 'Removal in progress', bead: 'removed', tone: 'neutral', attention: false },
  canceled: { label: 'Canceled', bead: 'stopped', tone: 'neutral', attention: false },
  integrated: { label: 'Integrated', bead: 'done', tone: 'done', attention: false },
  completed: { label: 'Completed', bead: 'done', tone: 'done', attention: false },
  paused: { label: 'Paused', bead: 'paused', tone: 'neutral', attention: false },
  needs_owner: { label: 'Waiting for a new owner', bead: 'ask', tone: 'ask', attention: true },
  needs_input: { label: 'Waiting for you', bead: 'ask', tone: 'ask', attention: true },
  provider_wait: { label: 'Waiting for the provider', bead: 'blocked', tone: 'wait', attention: false },
  blocked: { label: 'Blocked', bead: 'blocked', tone: 'wait', attention: false },
  waiting_helpers: { label: 'Waiting for helpers', bead: 'helpers', tone: 'wait', attention: false },
  reviewing: { label: 'Reviewing helper results', bead: 'work', tone: 'work', attention: false },
  running: { label: 'Working', bead: 'work', tone: 'work', attention: false },
  pending: { label: 'Not started', bead: 'pending', tone: 'neutral', attention: false },
  stopped: { label: 'Stopped', bead: 'stopped', tone: 'neutral', attention: false },
  waiting: { label: 'Waiting', bead: 'pending', tone: 'wait', attention: false },
  unknown: { label: 'Status unavailable', bead: 'pending', tone: 'neutral', attention: false },
}

function make(key, { label, detail = null, certainty = 'reported' } = {}) {
  return { key, ...STATUS[key], label: label || STATUS[key].label, detail, certainty }
}

const RECOVERY_LABELS = {
  'Process end unconfirmed': 'Process end unconfirmed',
  'Recovery required': 'Recovery required',
  'Removal blocked': 'Removal blocked',
}

// Map the backend's waiting context (only present while an Agent holds the Task).
function fromWaiting(context) {
  switch (context.kind) {
    case 'recovery': return make('recovery', { label: RECOVERY_LABELS[context.label] || 'Recovery required' })
    case 'removal': return make('removing')
    case 'control': return make('paused')
    case 'user': return make('needs_input')
    case 'provider':
      return context.condition === 'resume_ready'
        ? make('provider_wait', { label: 'Ready to continue' })
        : make('provider_wait', { label: context.label || STATUS.provider_wait.label })
    case 'dependency': return make('blocked', { label: 'Waiting for dependencies' })
    case 'coordination': return make('blocked', { label: 'Coordination blocked' })
    case 'delegation': return make('waiting_helpers')
    // Helpers finished; the lead has not been resumed to review them yet.
    case 'continuation': return make('waiting_helpers', { label: 'Helper results ready', detail: 'Waiting for the lead to continue' })
    case 'removed': return null
    default: return make('waiting', { certainty: 'unknown' })
  }
}

// `task` and `agents` come from the store (`agents` is taskAgents(store, id)).
export function taskStatus(task, agents = { current: null, last: null }) {
  if (!task) return make('unknown', { certainty: 'unknown' })
  const current = agents.current && !agents.current.unknown ? agents.current : null
  const removal = (agents.current || agents.last)?.removal
  if (removal?.state === 'blocked') return make('recovery', { label: 'Removal blocked' })
  if (task.waitingContext?.kind === 'recovery') return fromWaiting(task.waitingContext)
  if (task.continuation?.phase === 'recovery_required') return make('recovery', { label: 'Recovery required' })
  if (task.lifecycle === 'canceled' || task.controlIntent === 'canceled') return make('canceled')
  if (task.lifecycle === 'completed') {
    const into = integrationView(task)
    if (into?.status === 'applied') return make('integrated', { label: into.scope === 'project' ? 'Applied to the project' : 'Integrated into the lead' })
    return make('completed')
  }
  if (removal?.state === 'removing') return make('removing')
  if (task.lifecycle === 'paused' || task.controlIntent === 'paused') return make('paused')
  if (task.responsibilityAwaiting) return make('needs_owner')
  if (task.transfer?.phase === 'admitted') return make('needs_owner', { label: 'Changing owner', detail: 'A new owner is being assigned' })
  if (current) {
    const waiting = task.waitingContext ? fromWaiting(task.waitingContext) : null
    if (waiting) return waiting
    const phase = task.continuation?.currentAssignment ? task.continuation.phase : null
    if (phase === 'reviewing') return make('reviewing')
    if (phase === 'waiting') return make('waiting_helpers')
    if (current.status === 'running') return make('running')
    if (current.status === 'waiting') return make('waiting', { certainty: 'unknown' })
    // The Agent's process ended but the Task is not complete: report what is known, no outcome.
    if (current.status === 'finished') return make('stopped', { label: 'Agent finished', detail: 'The Task is not marked complete', certainty: 'unknown' })
    if (current.status === 'stopped') return make('stopped')
    return make('unknown', { certainty: 'unknown' })
  }
  // Nobody holds the Task now.
  switch (task.lifecycle) {
    case 'blocked': return make('blocked', { certainty: task.blockers.length ? 'reported' : 'unknown' })
    case 'pending': return make('pending')
    case 'waiting': return make('waiting', { certainty: 'unknown' })
    case 'in_progress': return make('stopped', { label: 'No agent working', detail: 'In progress without a current agent', certainty: 'unknown' })
    default: return make('unknown', { certainty: 'unknown' })
  }
}

const INTEGRATION_LABELS = {
  preparing: 'Preparing to combine', ready: 'Ready to combine', applying: 'Combining', applied: 'Combined', noop: 'No changes to combine',
  conflict: 'Conflicting changes', failed: 'Could not combine', source_changed: 'Work changed; prepare again',
  destination_changed: 'Destination changed; prepare again', recovery_required: 'Needs inspection',
}
const INTEGRATION_ATTENTION = new Set(['conflict', 'failed', 'recovery_required', 'source_changed', 'destination_changed'])

// Where a Task's work went. A child's result is combined into its lead; a root Task's work is
// applied to the project. Validation is never run by the backend, so nothing is "verified".
export function integrationView(task) {
  if (!task) return null
  const linked = task.delegation?.result?.integration
  const own = task.integration
  const source = linked?.status ? { status: linked.status, appliedAt: linked.appliedAt, scope: 'lead' }
    : own?.status ? { status: own.status, appliedAt: own.appliedAt, scope: own.destinationKind === 'project' ? 'project' : 'lead' }
      : null
  if (!source) {
    if (task.lifecycle !== 'completed') return null
    return { status: 'not_started', scope: task.parentId ? 'lead' : 'project', label: 'Not combined yet', attention: false, verified: false, appliedAt: null }
  }
  return { ...source, label: INTEGRATION_LABELS[source.status] || 'Unknown', attention: INTEGRATION_ATTENTION.has(source.status), verified: false }
}

const RESULT_LABELS = { completed: 'Reported complete', stopped: 'Stopped before finishing', failed: 'Reported failure',
  canceled: 'Canceled', unverified: 'Outcome not confirmed' }

// A delegated child's recorded result, as the backend recorded it.
export function resultView(task) {
  const result = task?.delegation?.result
  if (!result) return null
  return { outcome: result.outcome, phase: result.phase, label: RESULT_LABELS[result.outcome] || 'Unknown result' }
}

// Work the lead proposed that is not a Task yet. It is never drawn as a Task.
export function plannedView(task) {
  if (!task || !task.planned.unmaterialized) return null
  return { count: task.planned.unmaterialized, byDisposition: { ...task.planned.byDisposition } }
}

// Helpers as a parent sees them: counts by delegation state, nothing inferred.
export function helpersView(task) {
  if (!task) return null
  const d = task.delegations
  const active = d.materialized + d.result_ready
  return { proposed: d.requested, active, resultsReady: d.result_ready, closed: d.acknowledged, canceled: d.canceled, rounds: task.rounds }
}
