// Stage 2F model catalog presentation. Pure functions: the backend discovers,
// classifies and admits models; this module only groups and words what it reports.
export const OTHER_MODEL = '__other__'
export const GROUP_LABELS = {
  available: 'Available',
  unverified: 'Unverified',
  unavailable: 'Unavailable with current account',
}

export function groupModels(catalog) {
  const groups = { available: [], unverified: [], unavailable: [] }
  for (const model of catalog?.models || []) {
    if (model.selectable) groups.available.push(model)
    else if (model.requires_attempt) groups.unverified.push(model)
    else groups.unavailable.push(model)
  }
  return groups
}

export function findModel(catalog, id) {
  return (catalog?.models || []).find((model) => model.model_id === id) || null
}

// An option's text carries its status, so a disabled entry still says why.
export function optionLabel(model) {
  const name = model.display_name && model.display_name !== model.model_id ? `${model.display_name} (${model.model_id})` : model.model_id
  if (model.selectable && model.status === 'available') return name
  if (model.status === 'requires_plan' && model.plan_requirement) return `${name} · ${model.plan_requirement}`
  return `${name} · ${model.status_label}`
}

// What a model choice means before anything is sent; the backend decides again.
export function selection(catalog, choice, typed = '') {
  if (!choice) return { kind: 'default', model: null, needsAttempt: false, text: 'Provider default model.' }
  if (choice === OTHER_MODEL) {
    const value = typed.trim()
    if (!value) return { kind: 'other', model: null, needsAttempt: false, invalid: true, text: 'Type a model identifier.' }
    const listed = findModel(catalog, value)
    if (listed) return selection(catalog, value)
    return { kind: 'unlisted', model: value, needsAttempt: true,
      text: 'Not in the current list: access is not verified for this account.' }
  }
  const model = findModel(catalog, choice)
  if (!model) return { kind: 'unlisted', model: choice, needsAttempt: true, text: 'Not in the current list: access is not verified.' }
  const changed = model.previous_resolved_model ? ` (previously ${model.previous_resolved_model})` : ''
  const resolved = model.resolved_model ? ` Currently resolves to ${model.resolved_model}${changed}.` : ''
  if (model.selectable) return { kind: 'available', model: model.model_id, needsAttempt: false, text: `${model.explanation}${resolved}` }
  if (model.requires_attempt) return { kind: 'unverified', model: model.model_id, needsAttempt: true, text: `${model.status_label}. ${model.explanation}` }
  return { kind: 'unavailable', model: model.model_id, needsAttempt: false, blocked: true, text: `${model.status_label}. ${model.explanation}` }
}

// A picker value: { choice, typed, effort, attempt }.
export function initialChoice(model = null, effort = null, catalog = null) {
  if (!model) return { choice: '', typed: '', effort: effort || '', attempt: false }
  return findModel(catalog, model) ? { choice: model, typed: '', effort: effort || '', attempt: false }
    : { choice: OTHER_MODEL, typed: model, effort: effort || '', attempt: false }
}

export function selectionReady(catalog, value) {
  const chosen = selection(catalog, value.choice, value.typed)
  return !chosen.blocked && !chosen.invalid && (!chosen.needsAttempt || value.attempt === true)
}

export function chosenConfig(catalog, value) {
  const chosen = selection(catalog, value.choice, value.typed)
  return { model: chosen.model, effort: value.effort || null, attempt: chosen.needsAttempt && value.attempt === true }
}

// Shown before execution: what the provider cannot guarantee for this choice.
export function modelLimitation(provider, model) {
  if (provider === 'gemini' && model && model !== 'auto') {
    return 'Not available: Gemini CLI cannot guarantee that a specific model runs (fallback cannot be disabled, and its output does not identify the model that answered). Choose Auto or the provider default.'
  }
  if (provider === 'gemini' && model === 'auto') return 'Auto lets Gemini CLI choose the model; the models in its usage statistics are recorded.'
  if (provider === 'gemini') return 'Provider default: Gemini CLI uses its configured model or Auto, and may fall back; nothing is pinned.'
  return ''
}

// Effort levels come from the catalog entry of the chosen model only.
export function effortChoices(provider, catalog, choice) {
  if (provider === 'gemini') return { levels: [], note: 'Provider default: Gemini CLI has no reasoning control.' }
  if (provider === 'mock') return { levels: [], note: '' }
  if (!choice) {
    const fallback = provider === 'claude' ? findModel(catalog, 'default') : null
    if (fallback?.selectable && fallback.supported_efforts?.length) return { levels: fallback.supported_efforts, note: 'Levels the default model lists.' }
    return { levels: [], note: 'Provider default effort. Choose a listed model to set an effort level.' }
  }
  const model = findModel(catalog, choice)
  if (!model) return { levels: [], note: 'Provider default effort: the levels of an unlisted model are not known.' }
  if (!model.supported_efforts?.length) return { levels: [], note: 'Provider default effort: this model lists no effort levels.' }
  const fallback = model.default_effort ? ` Provider default: ${model.default_effort}.` : ''
  return { levels: model.supported_efforts, note: `Levels this model lists.${fallback}` }
}

export function freshness(catalog) {
  const refresh = catalog?.refresh
  if (!catalog || catalog.provider === 'mock') return { text: '', stale: false, verified: false }
  if (!refresh?.last_success_at) {
    const failed = refresh?.last_status && refresh.last_status !== 'ok' ? ` Last attempt: ${refresh.last_reason || refresh.last_status}` : ''
    return { text: `No model list yet.${failed}`, stale: true, verified: false }
  }
  const parts = [`Last checked ${refresh.last_success_at}`, refresh.fresh ? 'current' : 'stale']
  if (refresh.account_changed) parts.push('the sign-in, account or CLI changed since then')
  if (refresh.scope_label) parts.push(refresh.scope_label)
  if (refresh.identity_exposed === false) parts.push('account identity not exposed: rechecked before each launch')
  if (refresh.last_status && refresh.last_status !== 'ok') parts.push(`latest refresh ${refresh.last_status}`)
  return {
    text: parts.join(' · '),
    stale: !refresh.fresh,
    verified: catalog.entitlement_verified === true,
    source: catalog.source_note || '',
  }
}

// Mirrors the backend's session compatibility rule only to warn early.
export function newSessionLikely(provider, sessionConfig, requested, catalog) {
  if (!sessionConfig || provider === 'claude' || provider === 'mock') return false
  const [oldModel, oldEffort] = sessionConfig
  const [newModel, newEffort] = requested
  if ((oldModel || null) !== (newModel || null)) return true
  if ((oldEffort || null) === (newEffort || null)) return false
  if (provider === 'codex') return !findModel(catalog, newModel)?.supports_effort_updates
  return true
}

export function promptPayload(override) {
  if (!override?.enabled) return {}
  return {
    model_override: override.modelSet === true, model: override.modelSet ? override.model || null : null,
    effort_override: override.effortSet === true, effort: override.effortSet ? override.effort || null : null,
    confirm_new_session: override.confirmNewSession === true, attempt_unverified: override.attempt === true,
  }
}

export function defaultsPayload(model, effort, attempt) {
  return { model_set: true, model: model || null, effort_set: true, effort: effort || null, attempt_unverified: attempt === true }
}

const SOURCE_LABELS = { agent_default: 'Agent default', override: 'One-time override' }
const ENTITLEMENT_LABELS = {
  cli_listed: 'Listed for this account', provider_default: 'Provider default', explicit_attempt: 'Explicit attempt (not verified)',
  not_applicable: 'Not applicable',
}

// The latest execution's requested and reported configuration. Nothing is inferred.
export function executionFacts(latest) {
  if (!latest) return []
  const facts = [['Requested model', latest.model || 'Provider default']]
  if (latest.resolved_model) facts.push(['Listed resolution', latest.resolved_model])
  facts.push(['Reported model', latest.effective_model || 'Not reported'])
  facts.push(['Requested effort', latest.requested_effort || 'Provider default'])
  facts.push(['Applied effort', latest.effective_effort || 'Not reported by the provider'])
  if (latest.config_source) facts.push(['Source', SOURCE_LABELS[latest.config_source] || latest.config_source])
  if (latest.entitlement_state) facts.push(['Access', ENTITLEMENT_LABELS[latest.entitlement_state] || latest.entitlement_state])
  if (latest.effective_note) facts.push(['Note', latest.effective_note])
  return facts
}

// The next instruction: the Agent's defaults, or a one-time override.
export function emptyOverride() {
  return { enabled: false, picker: initialChoice(), saveDefault: false, confirmNewSession: false, defaultAttempt: false }
}

// Whether the Agent's default model needs an explicit attempt. Unknown while the
// list loads: the backend then decides and explains a refusal.
export function defaultNeedsAttempt(agent, catalog, failed = false) {
  if (!agent?.model) return false
  return catalog ? selection(catalog, agent.model).needsAttempt : failed
}

export function instructionReady(agent, catalog, value) {
  if (value.enabled) return selectionReady(catalog, value.picker)
  return !defaultNeedsAttempt(agent, catalog) || value.defaultAttempt === true
}

// What to send: an explicit defaults change first (only when asked), then the
// instruction's own fields. An override never changes the Agent's defaults.
export function instructionPlan(agent, catalog, value) {
  const confirm = value.confirmNewSession === true ? { confirm_new_session: true } : {}
  if (!value.enabled) return { defaults: null, fields: { ...confirm, ...(value.defaultAttempt === true ? { attempt_unverified: true } : {}) } }
  const config = chosenConfig(catalog, value.picker)
  const attempt = config.attempt ? { attempt_unverified: true } : {}
  if (value.saveDefault) {
    const unchanged = config.model === (agent.model || null) && config.effort === (agent.effort || null)
    return { defaults: unchanged ? null : defaultsPayload(config.model, config.effort, config.attempt), fields: { ...confirm, ...attempt } }
  }
  return { defaults: null, fields: promptPayload({ enabled: true, modelSet: true, model: config.model, effortSet: true,
    effort: config.effort, confirmNewSession: value.confirmNewSession, attempt: config.attempt }) }
}
