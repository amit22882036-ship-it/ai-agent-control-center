// Auto allocation presentation. Pure functions: the backend authorizes, evaluates and
// records every decision; this module only words what it reports.
import { providerLabel } from './providerPresentation.mjs'

export const AUTO_SUMMARY = 'Prioritizes task suitability and quality. Considers availability and usage. Does not automatically downgrade or switch running agents.'
export const AUTO_PROVIDERS = ['codex', 'claude', 'gemini']

export function policyPayload(enabled, providers) {
  return { enabled: enabled === true, providers: [...new Set(providers || [])] }
}

// Providers the user may allow: real providers only, labelled with their readiness.
export function providerChoices(statuses) {
  return AUTO_PROVIDERS.map((id) => {
    const status = (statuses || []).find((item) => item.provider === id)
    const state = status?.state?.state || 'unchecked'
    return { id, label: providerLabel(id), ready: state === 'ready', state }
  })
}

export function decisionTitle(decision) {
  if (!decision) return ''
  if (decision.status !== 'selected') return 'Auto allocation held'
  return `Auto: ${providerLabel(decision.provider)} · ${decision.model || 'provider default model'} · ${decision.effort ? `${decision.effort} effort` : 'provider default effort'}`
}

// The first line is the compact summary; the rest is the detailed explanation.
export function decisionLines(decision) {
  const lines = (decision?.explanation || '').split('\n').filter(Boolean)
  return { summary: lines[0] || '', details: lines.slice(1) }
}

export function policyText(policy) {
  if (!policy) return 'Not enabled: delegated children use the provider the plan requests, or this agent\'s.'
  if (!policy.enabled) return 'Disabled: delegated children use the provider the plan requests, or this agent\'s.'
  const where = policy.inherited ? ' (inherited from an ancestor task)' : ''
  return `Enabled for delegated children${where}. Allowed: ${(policy.providers || []).map(providerLabel).join(', ') || 'none'}.`
}

export function heldChildren(view) {
  return (view?.children || []).filter((child) => child.allocation && child.allocation.status !== 'selected')
}

// ---------------------------------------------------------------- advanced policy draft
// A candidate/preference is stored by the backend as provider[/model][@effort]; the UI
// edits it as { provider, model, effort } and never asks the user to type the syntax.
export function parseSpec(value) {
  const match = /^([a-z]+)(?:\/([A-Za-z0-9][A-Za-z0-9._-]{0,127}))?(?:@([a-z]+))?$/.exec(value || '')
  return match ? { provider: match[1], model: match[2] || null, effort: match[3] || null } : null
}

export function formatSpec(spec) {
  return `${spec.provider}${spec.model ? `/${spec.model}` : ''}${spec.effort ? `@${spec.effort}` : ''}`
}

export function specLabel(spec, { anyModel = false } = {}) {
  if (!spec) return 'Unrecognized entry'
  const model = spec.model || (anyModel ? 'any model' : 'provider default model')
  return `${providerLabel(spec.provider)} · ${model} · ${spec.effort ? `${spec.effort} effort` : 'default effort'}`
}

// An isolated, editable copy of the effective policy; saving it never mutates the view.
export function draftFromView(view, fallbackProvider = null) {
  const policy = view?.policy
  const providers = policy?.providers?.length ? policy.providers.filter((p) => p !== 'mock')
    : [fallbackProvider].filter((p) => AUTO_PROVIDERS.includes(p))
  const profile = {}
  for (const [category, levels] of Object.entries(policy?.profile || {})) {
    profile[category] = { preferred: [...(levels.preferred || [])], acceptable: [...(levels.acceptable || [])] }
  }
  const models = Object.fromEntries(Object.entries(policy?.models || {}).map(([p, names]) => [p, [...names]]))
  return {
    // The effective state, inherited or own: enabling Auto is always the user's explicit action.
    enabled: policy ? policy.enabled === true : false,
    providers,
    models,
    // Restriction is an explicit mode, never inferred from an empty list.
    modes: Object.fromEntries(Object.entries(models).filter(([, names]) => names.length).map(([p]) => [p, 'only'])),
    preferences: [...(policy?.preferences || [])],
    profile,
    // The own policy's revision the editor loaded (null: the Task has none yet).
    expected: view?.own_revision ?? null,
  }
}

export function modelMode(draft, provider) {
  return draft.modes?.[provider] === 'only' ? 'only' : 'all'
}

// Blocking problems: a restricted provider with no selected model would otherwise be saved
// as an empty list, which the backend reads as "all otherwise eligible models".
export function draftErrors(draft) {
  const errors = []
  if (draft.enabled && !draft.providers.length) errors.push('Allow at least one provider.')
  for (const provider of draft.providers) {
    if (modelMode(draft, provider) === 'only' && !(draft.models[provider] || []).length) {
      errors.push(`Select at least one ${providerLabel(provider)} model, or choose "All otherwise eligible models are allowed".`)
    }
  }
  return errors
}

export function draftPayload(draft) {
  const errors = draftErrors(draft)
  if (errors.length) throw new Error(errors[0])
  const providers = [...new Set(draft.providers)]
  const models = {}
  for (const provider of providers) {
    // Only an explicit "only these models" restriction is sent; "all" sends no list (the
    // backend's documented meaning of an absent list). An empty restriction never reaches here.
    if (modelMode(draft, provider) === 'only') models[provider] = [...new Set(draft.models[provider])]
  }
  const profile = {}
  for (const [category, levels] of Object.entries(draft.profile)) {
    const entry = {}
    for (const level of ['preferred', 'acceptable']) if (levels[level]?.length) entry[level] = levels[level]
    if (Object.keys(entry).length) profile[category] = entry
  }
  return { enabled: draft.enabled === true, providers, models, preferences: draft.preferences, profile,
    expected_revision: draft.expected }
}

export function moveItem(list, index, delta) {
  const target = index + delta
  if (target < 0 || target >= list.length) return list
  const copy = [...list]
  ;[copy[index], copy[target]] = [copy[target], copy[index]]
  return copy
}

export function modelRestrictionText(names) {
  return names?.length ? `Only ${names.length} selected model${names.length === 1 ? '' : 's'} allowed.`
    : 'All otherwise eligible models are allowed.'
}

// The scope the user edits: the Task's own policy, a new local override, or none yet.
export function scopeText(view) {
  if (!view?.policy) return 'No Auto policy applies yet. Saving creates one for this task and its subtree.'
  if (view.inherited) return `Auto policy inherited from parent task “${view.owner_title || 'ancestor'}”. Saving creates a local override for this task and its subtree; the parent task's policy is not changed.`
  return 'This task owns its Auto policy; it applies to this task and its subtree.'
}

// The adder's effective selection: never a provider that is no longer allowed, and never a
// model or effort left over from another provider or model.
export function adderSelection(providers, state, choices = null) {
  const provider = providers.includes(state.provider) ? state.provider : (providers[0] || '')
  let model = provider === state.provider ? state.model : ''
  if (model && choices && !choices.some((choice) => choice.id === model)) model = ''
  const efforts = model && choices ? (choices.find((choice) => choice.id === model)?.efforts || []) : []
  const effort = model && state.effort && efforts.includes(state.effort) ? state.effort : ''
  return { provider, model, effort }
}

// Which catalog entries may be added to an allowlist or a candidate list.
export function catalogChoices(catalog, saved = []) {
  const listed = (catalog?.models || []).map((model) => ({ id: model.model_id, label: model.display_name || model.model_id,
    status: model.status_label, selectable: model.selectable, efforts: model.supported_efforts || [], known: true }))
  const ids = new Set(listed.map((m) => m.id))
  const kept = saved.filter((id) => id && id !== 'provider_default' && !ids.has(id))
    .map((id) => ({ id, label: id, status: 'Not in the current model list (kept)', selectable: false, efforts: [], known: false }))
  return [...listed, ...kept]
}
