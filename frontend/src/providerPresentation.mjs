// Stage 2F provider presentation. Pure functions: the backend decides
// readiness, policy and admission; this module only words what it reports.
export const PROVIDER_IDS = ['mock', 'codex', 'claude', 'gemini']
export const PROVIDER_LABELS = { mock: 'Mock', codex: 'Codex', claude: 'Claude Code', gemini: 'Gemini CLI' }
export const PROVIDER_NOTES = {
  mock: 'Simulated work for development',
  codex: 'Uses your Codex sign-in',
  claude: 'Uses your Claude subscription sign-in',
  gemini: 'Uses your Google sign-in',
}
export const STATE_LABELS = {
  ready: 'Ready', disabled: 'Disabled', not_installed: 'Not installed', invalid_installation: 'Invalid installation',
  unsupported_version: 'Update required', not_authenticated: 'Not signed in', incompatible: 'Incompatible',
  usage_limited: 'Usage limited', temporarily_unavailable: 'Temporarily unavailable',
  billing_blocked: 'Billing policy blocked', unchecked: 'Not checked',
}
// Mirrors the backend adapters' declared capabilities for actions shown before
// a provider list has loaded; the backend still refuses anything unsupported.
const SUPPORTED = {
  mock: new Set(),
  codex: new Set(['session_resume', 'reply', 'redirect', 'waiting_input', 'decisions', 'autonomous', 'delegation', 'hybrid', 'model_selection']),
  claude: new Set(['session_resume', 'reply', 'redirect', 'waiting_input', 'decisions', 'autonomous', 'delegation', 'hybrid', 'model_selection']),
  gemini: new Set(['session_resume', 'reply', 'redirect', 'waiting_input', 'decisions', 'model_selection']),
}

export function providerLabel(id) { return PROVIDER_LABELS[id] || id || 'Unknown provider' }
export function stateOf(status) { return status?.state?.state || (status?.provider === 'mock' ? 'ready' : 'unchecked') }
export function stateLabel(state) { return STATE_LABELS[state] || 'Unknown' }
export function stateTone(state) {
  if (state === 'ready') return 'ready'
  if (['usage_limited', 'temporarily_unavailable', 'unchecked'].includes(state)) return 'waiting'
  return 'blocked'
}
export function selectable(status, id) {
  if (id === 'mock') return true
  return stateOf(status) === 'ready'
}
export function statusFor(statuses, id) { return (statuses || []).find((item) => item.provider === id) || null }

export function supports(agentType, capability, status = null) {
  const declared = status?.capabilities?.[capability]
  if (declared) return declared.supported === true
  return SUPPORTED[agentType]?.has(capability) === true
}
export function capabilityReason(status, capability) {
  const declared = status?.capabilities?.[capability]
  return declared && !declared.supported ? declared.reason : null
}

export function modelSuggestions(status) {
  return [...new Set([...(status?.documented_models || []), ...(status?.settings?.models || [])])]
}
export function modelStatus(status, model) {
  const value = (model || '').trim()
  if (!value) return { kind: 'default', text: 'Provider default' }
  if (modelSuggestions(status).includes(value)) return { kind: 'known', text: 'Known model name' }
  return { kind: 'unknown', text: 'Not in the known list: sent as typed and not verified' }
}

// Only provider-reported values are shown; nothing is estimated or derived.
export const USAGE_SOURCE_LABELS = {
  exact_when_reported: 'Usage meters reported by the provider',
  exhaustion_signal: 'Only limit-reached signals',
  none: 'No usage information exposed',
  not_applicable: 'Not applicable',
}
export const USAGE_OVERALL_LABELS = { available: 'Available', limited: 'Limit reached', unknown: 'Unknown', not_applicable: 'Not applicable' }
const WINDOW_STATUS_LABELS = { available: 'Available', exhausted: 'Limit reached', unknown: 'Unknown' }

export function usageSummary(status) {
  const usage = status?.usage
  if (!usage) return { overall: 'Unknown', source: USAGE_SOURCE_LABELS.none, explanation: '', lastObserved: null, unpersisted: 0 }
  return {
    overall: USAGE_OVERALL_LABELS[usage.overall] || 'Unknown',
    tone: usage.overall === 'available' ? 'ready' : usage.overall === 'limited' ? 'blocked' : 'waiting',
    source: USAGE_SOURCE_LABELS[usage.source] || usage.source,
    explanation: usage.explanation || '',
    lastObserved: usage.last_observed_at || null,
    unpersisted: usage.unpersisted || 0,
    refreshable: usage.refresh_supported === true,
  }
}

export function windowRows(status) {
  return (status?.usage?.windows || []).map((window) => ({
    key: `${window.window_id}:${window.scope_key || ''}`,
    label: window.label,
    status: WINDOW_STATUS_LABELS[window.status] || 'Unknown',
    exhausted: window.status === 'exhausted',
    // Used percent only when the provider reported it; remaining is never derived from it.
    used: typeof window.used_percent === 'number' ? `${Math.round(window.used_percent)}% used` : null,
    usedPercent: typeof window.used_percent === 'number' ? Math.max(0, Math.min(100, window.used_percent)) : null,
    reset: window.reset_at ? `Provider-reported reset: ${window.reset_at}` : 'Reset time not reported',
    observed: window.observed_at ? `Observed ${window.observed_at}${window.fresh ? '' : ' (stale)'}` : 'Never observed',
    stale: window.fresh === false,
    scope: window.applies_to || null,
  }))
}

export function usageLines(status) {
  const lines = (status?.usage?.blocks || []).map((block) => `${block.reason} ${block.reset_at ? `Provider-reported reset: ${block.reset_at}.` : 'Reset time unknown.'}`)
  if (status?.usage?.unpersisted) lines.push('A reached limit could not be saved yet; it stays enforced while the backend runs.')
  if (status && status.provider !== 'mock') lines.push('Remaining usage is not reported by this provider.')
  return lines
}

export function conditionText(condition) {
  if (!condition) return ''
  const reset = condition.reset_at ? ` Provider-reported reset: ${condition.reset_at} (not a guarantee of availability).` : ''
  const next = {
    usage_limited: 'The agent, task, output and provider session are kept. Nothing resumes automatically, not even after a reset time.',
    auth_required: 'Sign in again (AI Providers > Sign in, or the provider CLI), choose Check again, then resume.',
    billing_blocked: 'The provider reported a billing problem. Nothing is retried automatically.',
    provider_unavailable: 'The provider was temporarily unavailable. Resume when it is available again.',
    resume_ready: 'The next run uses the selected provider. Its context comes from the task and workspace.',
    model_not_enforced: 'It could not be verified that the selected model produced the response, so the task was not completed. Review the output, then resume or choose another model. Nothing is retried automatically.',
  }[condition.kind] || ''
  const detail = condition.detail ? ` ${condition.detail}` : ''
  return `${condition.label}.${reset}${detail} ${next}`.trim()
}

export function blockingText(condition) {
  const items = condition?.blocking || []
  if (items.length === 0) return condition?.kind === 'usage_limited' ? 'Quota availability unknown.' : ''
  if (items.length > 1) return `Multiple quota limits reached: ${items.map((item) => item.label).join('; ')}.`
  return `${items[0].label}.`
}

export function resumeText(resume) {
  if (!resume) return ''
  if (!resume.available) return `Resume unavailable: ${resume.reason}`
  if (resume.mode === 'explicit_retry') return resume.reason
  return 'Manual resume available. Every readiness, billing and admission check runs again first.'
}

export function preservedText(condition, resume) {
  const session = condition?.session_preserved || resume?.session_resumable
  return `Preserved: the agent, its task and assignment, output and workspace${session ? ', and the resumable provider session' : ''}.`
}

// Access may stay or narrow on a provider change; it never widens.
export function accessOptions(current) {
  return current === 'workspace-write' ? ['workspace-write', 'read-only'] : ['read-only']
}
export function changeEligible(agent) {
  return Boolean(agent && agent.status === 'waiting' && agent.agent_type !== 'mock' && !agent.autonomous && !agent.managed
    && ['user', 'provider'].includes(agent.waiting_context?.kind))
}
export function changePayload(provider, model, sandbox, { effort = null, attempt = false } = {}) {
  const value = (model || '').trim()
  return { provider, model: value || null, sandbox, ...(effort ? { effort } : {}), ...(attempt && value ? { attempt_unverified: true } : {}) }
}

export function refusalMessage(data, fallback) {
  if (typeof data?.detail === 'string') return data.detail
  if (typeof data?.detail?.message === 'string') return data.detail.message
  return fallback
}
