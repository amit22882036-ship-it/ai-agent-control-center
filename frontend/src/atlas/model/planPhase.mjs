// The plan phase of a task whose lead may bring in helpers (GET /tasks/{id}/orchestration → plan_phase):
// how its plan becomes work, and where that stands. Approving or rejecting is an ordinary reply to the
// waiting lead ('approve' / 'reject'); any other reply asks it to change the plan first.

export const SPLIT_POLICIES = [
  { value: 'lead_decides', title: 'Lead decides', text: 'It plans first, then splits the work or does it itself.' },
  { value: 'approve', title: 'Lead proposes, I approve', text: 'It plans first and waits in Needs you until you approve, change or reject the plan.' },
  { value: 'always', title: 'Always split when possible', text: 'It plans first and splits whenever two or more substantial parts can be separated.' },
]

const POLICY_TITLE = Object.fromEntries(SPLIT_POLICIES.map((item) => [item.value, item.title]))

const PHASE_TEXT = {
  planning: 'Planning: the lead is deciding how to split the work. It changes no files in this turn.',
  awaiting_approval: 'The plan is waiting for your approval.',
  executing: 'Carrying out the plan.',
}

export function planPhaseView(phase) {
  if (!phase || typeof phase !== 'object' || !POLICY_TITLE[phase.policy]) return null
  return {
    policy: phase.policy,
    policyTitle: POLICY_TITLE[phase.policy],
    phase: phase.phase,
    text: PHASE_TEXT[phase.phase] || 'The plan phase is not reported.',
    awaiting: phase.phase === 'awaiting_approval',
    retried: Number(phase.retries) > 0,
  }
}

// The New task summary line for the chosen policy (only when helpers are on).
export function splitPolicySummary(policy) {
  if (policy === 'approve') return 'It plans first and waits for your approval before any work starts.'
  if (policy === 'always') return 'It plans first and splits the work whenever separate parts are possible.'
  return 'It plans first, with read-only access, then splits the work or does it itself.'
}
