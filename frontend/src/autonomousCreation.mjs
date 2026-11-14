// Store the user's choice independently of provider eligibility.
export const initialAutonomousChoice = true
// Providers whose CLIs can run with native sub-agents disabled (Stage 2F).
export const MANAGED_PROVIDERS = ['codex', 'claude']
export function autonomyEligible(provider, parentId = null) {
  return MANAGED_PROVIDERS.includes(provider) && parentId === null
}
export function creationPayload(task, provider, sandbox, choice, parentId = null, projectId = undefined, model = undefined,
  { effort = null, attempt = false, autoProviders = null, helperAccess = null, external = false, splitPolicy = null,
    helperMaxEffort = null, helperModels = [] } = {}) {
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
  // With Full Access only: the person's explicit choice for the lead's helpers (Workspace unless chosen).
  if (real && parentId === null && sandbox === 'full-access' && autonomous && helperAccess) selected.helper_access = helperAccess
  // Publishing and the tool's outside apps, plugins and connectors stay off unless the person allows them
  // for a new root task (its helpers inherit the choice).
  if (real && parentId === null && external === true) selected.external_integrations = true
  // With helpers, the lead plans first; the policy says how that plan becomes work.
  if (autonomous && splitPolicy) selected.split_policy = splitPolicy
  // The person's limit on what the lead may choose per helper (real tools, a new lead with helpers only).
  if (autonomous && real && helperMaxEffort) selected.helper_max_effort = helperMaxEffort
  if (autonomous && real && Array.isArray(helperModels) && helperModels.length) selected.helper_models = [...new Set(helperModels)]
  return { ...(parentId === null && projectId !== undefined ? { project_id: projectId } : {}), task: task.trim(), agent_type: provider, sandbox,
    autonomous, ...selected }
}
