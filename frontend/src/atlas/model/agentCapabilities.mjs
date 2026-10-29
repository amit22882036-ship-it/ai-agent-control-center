// Which agent controls Atlas offers, from the agent's own read model (GET /agents/{id}). Each rule
// mirrors the classic inspector's condition for the same control, so Atlas never offers less or
// more. They decide only what is shown: the backend validates and may still refuse every request.
import { changeEligible, supports } from '../../providerPresentation.mjs'
import { removalState } from '../../agentRemoval.mjs'

export function capabilitiesFor(detail) {
  if (!detail || typeof detail !== 'object' || !detail.agent_id) return null
  const type = detail.agent_type
  const status = detail.status
  const waitingFor = detail.waiting_context?.kind || null
  const removal = removalState(detail)
  const asking = supports(type, 'reply') && status === 'waiting' && waitingFor === 'user'
  const decisions = supports(type, 'decisions')
  return {
    removal,
    // Answering a question (classic "Waiting for you" panel).
    reply: asking,
    decide: asking,
    enableSimilar: asking,
    enableAlways: asking && !detail.always_decide_enabled,
    // Saved decision policies, which can be turned off at any time.
    similarOn: decisions && detail.similar_decisions_enabled === true,
    alwaysOn: decisions && detail.always_decide_enabled === true,
    // Execution control.
    stop: status === 'running' || status === 'waiting',
    stopBranch: Array.isArray(detail.child_ids) && detail.child_ids.length > 0,
    redirect: supports(type, 'redirect') && status === 'running',
    redirectReady: supports(type, 'redirect') && status === 'running' && Boolean(detail.session_id),
    // Provider, model and defaults.
    providerWait: status === 'waiting' && waitingFor === 'provider',
    providerChange: changeEligible(detail),
    defaults: type !== 'mock',
    instructionOptions: type !== 'mock',
    // Task policies of the agent's allocation task.
    allocationTask: detail.allocation_task_id || null,
  }
}

// What a running or waiting agent's Stop does, in the words the person sees. Stop and Stop branch
// act on agents (the agent lineage the backend records), never on the task itself.
export const STOP_TEXT = {
  single: 'Stops this agent’s current run. Its task is kept and normally returns to “not started”; it does not pause or cancel the task.',
  branch: 'Stops this agent and every agent it started, following the agents’ own lineage. This does not affect its parent or agents outside this branch, and it does not pause or cancel any task.',
}
