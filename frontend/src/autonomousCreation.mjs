// Store the user's choice independently of provider eligibility.
export const initialAutonomousChoice = true
// Providers whose CLIs can run with native sub-agents disabled (Stage 2F).
export const MANAGED_PROVIDERS = ['codex', 'claude']
export function autonomyEligible(provider, parentId = null) {
  return MANAGED_PROVIDERS.includes(provider) && parentId === null
}
export function creationPayload(task, provider, sandbox, choice, parentId = null, projectId = undefined, model = undefined,
  { effort = null, attempt = false, autoProviders = null } = {}) {
  if (parentId === null && projectId !== undefined && (typeof projectId !== 'string' || !projectId.trim())) throw new Error('Choose a valid Project before starting.')
  const real = provider !== 'mock'
  const selected = real && typeof model === 'string' && model.trim() ? { model: model.trim() } : {}
  // The Agent's own default effort; an unverified model needs the user's explicit attempt.
  if (real && effort) selected.effort = effort
  if (real && attempt === true && selected.model) selected.attempt_unverified = true
  const autonomous = autonomyEligible(provider, parentId) && choice === true
  // Auto allocation for delegated children: the user's explicit authorization, only
  // for a new autonomous root agent.
  if (autonomous && Array.isArray(autoProviders) && autoProviders.length) selected.auto_allocation_providers = [...new Set(autoProviders)]
  return { ...(parentId === null && projectId !== undefined ? { project_id: projectId } : {}), task: task.trim(), agent_type: provider, sandbox,
    autonomous, ...selected }
}
