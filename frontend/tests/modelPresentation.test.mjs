import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { OTHER_MODEL, chosenConfig, defaultsPayload, effortChoices, emptyOverride, executionFacts, freshness, groupModels,
  initialChoice, instructionPlan, instructionReady, modelLimitation, newSessionLikely, optionLabel, promptPayload, selection,
  selectionReady } from '../src/modelPresentation.mjs'
import { changePayload, conditionText } from '../src/providerPresentation.mjs'
import { creationPayload } from '../src/autonomousCreation.mjs'

const model = (id, extra = {}) => ({ model_id: id, display_name: id, status: 'available', status_label: 'Available',
  explanation: 'Listed by Codex for this account.', selectable: true, requires_attempt: false, supported_efforts: ['low', 'high'],
  default_effort: 'low', resolved_model: null, plan_requirement: null, ...extra })
const catalog = {
  provider: 'codex', entitlement_verified: true, source_note: 'The catalog the Codex CLI fetched.',
  refresh: { last_success_at: '2026-10-23T10:00:00.000Z', fresh: true, scope_label: 'Codex 0.154 · ChatGPT sign-in', last_status: 'ok' },
  models: [
    model('m1', { supports_effort_updates: true }),
    model('m2'),
    model('locked', { status: 'requires_plan', status_label: 'Requires a specific plan', selectable: false,
      plan_requirement: 'Available on a higher ChatGPT plan', explanation: 'Provider: Available on a higher ChatGPT plan' }),
    model('nomention', { status: 'unavailable_subscription', status_label: 'Not available on this account', selectable: false,
      explanation: 'Not available on this account. Required plan not specified by provider.' }),
    model('maybe', { status: 'entitlement_unknown', status_label: 'Availability not verified', selectable: false, requires_attempt: true,
      supported_efforts: [], explanation: 'Listed in public documentation; access for this account is not verified.' }),
  ],
}

test('models are grouped into available, unverified and unavailable without dropping any', () => {
  const groups = groupModels(catalog)
  assert.deepEqual(groups.available.map((m) => m.model_id), ['m1', 'm2'])
  assert.deepEqual(groups.unverified.map((m) => m.model_id), ['maybe'])
  assert.deepEqual(groups.unavailable.map((m) => m.model_id), ['locked', 'nomention'])
  assert.deepEqual(groupModels(null), { available: [], unverified: [], unavailable: [] })
})

test('a disabled option names the provider plan only when the provider stated it', () => {
  assert.equal(optionLabel(catalog.models[2]), 'locked · Available on a higher ChatGPT plan')
  assert.equal(optionLabel(catalog.models[3]), 'nomention · Not available on this account')
  assert.doesNotMatch(optionLabel(catalog.models[3]), /Pro|Plus|Max|Team|Enterprise/)
  assert.equal(optionLabel(model('x', { display_name: 'Pretty' })), 'Pretty (x)')
})

test('selection: unavailable is blocked, unverified and unlisted need an explicit attempt', () => {
  assert.equal(selection(catalog, '').kind, 'default')
  assert.equal(selection(catalog, 'm1').kind, 'available')
  assert.equal(selection(catalog, 'locked').blocked, true)
  assert.equal(selection(catalog, 'maybe').needsAttempt, true)
  const unlisted = selection(catalog, OTHER_MODEL, 'brand-new')
  assert.deepEqual([unlisted.kind, unlisted.model, unlisted.needsAttempt], ['unlisted', 'brand-new', true])
  assert.equal(selection(catalog, OTHER_MODEL, 'm2').kind, 'available')  // A typed listed id is the listed model.
  assert.equal(selection(catalog, OTHER_MODEL, '  ').invalid, true)
  assert.equal(selectionReady(catalog, { choice: 'locked', typed: '', effort: '', attempt: true }), false)
  assert.equal(selectionReady(catalog, { choice: 'maybe', typed: '', effort: '', attempt: false }), false)
  assert.equal(selectionReady(catalog, { choice: 'maybe', typed: '', effort: '', attempt: true }), true)
  assert.deepEqual(chosenConfig(catalog, { choice: 'maybe', typed: '', effort: '', attempt: true }), { model: 'maybe', effort: null, attempt: true })
  assert.deepEqual(chosenConfig(catalog, { choice: 'm1', typed: '', effort: 'high', attempt: true }), { model: 'm1', effort: 'high', attempt: false })
})

test('alias resolution and its change are shown, never inferred', () => {
  const aliased = { ...catalog, models: [model('sonnet', { resolved_model: 'x-sonnet-2', previous_resolved_model: 'x-sonnet-1' })] }
  assert.match(selection(aliased, 'sonnet').text, /resolves to x-sonnet-2 \(previously x-sonnet-1\)/)
})

test('effort levels depend on the selected model and the provider', () => {
  assert.deepEqual(effortChoices('codex', catalog, 'm1').levels, ['low', 'high'])
  assert.match(effortChoices('codex', catalog, 'm1').note, /Provider default: low/)
  assert.deepEqual(effortChoices('codex', catalog, 'maybe').levels, [])
  assert.deepEqual(effortChoices('codex', catalog, 'brand-new').levels, [])
  assert.deepEqual(effortChoices('codex', catalog, null).levels, [])
  assert.match(effortChoices('gemini', catalog, 'pro').note, /no reasoning control/)
  const claude = { provider: 'claude', models: [model('default', { supported_efforts: ['low', 'medium'] })] }
  assert.deepEqual(effortChoices('claude', claude, null).levels, ['low', 'medium'])
})

test('freshness separates discovery from verified entitlement', () => {
  assert.match(freshness(catalog).text, /current/)
  assert.equal(freshness(catalog).verified, true)
  const stale = { ...catalog, entitlement_verified: false, refresh: { ...catalog.refresh, fresh: false, last_status: 'failed' } }
  assert.equal(freshness(stale).stale, true)
  assert.equal(freshness(stale).verified, false)
  assert.match(freshness(stale).text, /stale.*latest refresh failed/)
  assert.match(freshness({ provider: 'codex', models: [], refresh: { last_status: 'unsupported', last_reason: 'No catalog command.' } }).text,
    /No model list yet\. Last attempt: No catalog command\./)
})

test('new-session warning mirrors the provider rules', () => {
  assert.equal(newSessionLikely('claude', ['sonnet', 'low'], ['opus', 'high'], catalog), false)
  assert.equal(newSessionLikely('codex', ['m1', 'low'], ['m2', 'low'], catalog), true)
  assert.equal(newSessionLikely('codex', ['m1', 'low'], ['m1', 'high'], catalog), false)
  assert.equal(newSessionLikely('codex', ['m2', 'low'], ['m2', 'high'], catalog), true)
  assert.equal(newSessionLikely('gemini', ['pro', null], ['flash', null], catalog), true)
  assert.equal(newSessionLikely('codex', null, ['m2', 'high'], catalog), false)
})

test('payloads: one-time override, explicit defaults and creation stay distinct', () => {
  assert.deepEqual(promptPayload({ enabled: false }), {})
  assert.deepEqual(promptPayload({ enabled: true, modelSet: true, model: 'm2', effortSet: true, effort: '', confirmNewSession: true }),
    { model_override: true, model: 'm2', effort_override: true, effort: null, confirm_new_session: true, attempt_unverified: false })
  assert.deepEqual(defaultsPayload('m1', 'high', false), { model_set: true, model: 'm1', effort_set: true, effort: 'high', attempt_unverified: false })
  assert.deepEqual(creationPayload('Do it', 'codex', 'read-only', false, null, 'p', 'm1', { effort: 'high' }),
    { project_id: 'p', task: 'Do it', agent_type: 'codex', sandbox: 'read-only', autonomous: false, model: 'm1', effort: 'high' })
  assert.equal(creationPayload('Do it', 'mock', 'read-only', false, null, 'p', 'm1', { effort: 'high', attempt: true }).effort, undefined)
  assert.equal(creationPayload('Do it', 'codex', 'read-only', false, null, 'p', '', { attempt: true }).attempt_unverified, undefined)
  assert.deepEqual(changePayload('codex', 'maybe', 'read-only', { effort: 'low', attempt: true }),
    { provider: 'codex', model: 'maybe', sandbox: 'read-only', effort: 'low', attempt_unverified: true })
})

test('the next instruction uses defaults unless overridden, and saving a default is explicit', () => {
  const agent = { agent_type: 'codex', model: 'm1', effort: 'low', session_config: ['m1', 'low'] }
  assert.deepEqual(instructionPlan(agent, catalog, emptyOverride()), { defaults: null, fields: {} })
  const override = { ...emptyOverride(), enabled: true, picker: { choice: 'm2', typed: '', effort: 'high', attempt: false } }
  const plan = instructionPlan(agent, catalog, override)
  assert.equal(plan.defaults, null)
  assert.equal(plan.fields.model, 'm2')
  assert.equal(plan.fields.model_override, true)
  const saved = instructionPlan(agent, catalog, { ...override, saveDefault: true, confirmNewSession: true })
  assert.deepEqual(saved.defaults, defaultsPayload('m2', 'high', false))
  assert.deepEqual(saved.fields, { confirm_new_session: true })
  const unverified = { ...agent, model: 'maybe' }
  assert.equal(instructionReady(unverified, catalog, emptyOverride()), false)
  assert.equal(instructionReady(unverified, catalog, { ...emptyOverride(), defaultAttempt: true }), true)
  assert.deepEqual(instructionPlan(unverified, catalog, { ...emptyOverride(), defaultAttempt: true }).fields, { attempt_unverified: true })
  assert.deepEqual(initialChoice('unlisted-x', 'low', catalog), { choice: OTHER_MODEL, typed: 'unlisted-x', effort: 'low', attempt: false })
  // While the list loads nothing is gated; the backend decides and explains a refusal.
  assert.equal(instructionReady(unverified, null, emptyOverride()), true)
})

test('review follow-up: account change, unexposed identity and Gemini pinning are explained before execution', () => {
  const changed = { ...catalog, refresh: { ...catalog.refresh, fresh: false, account_changed: true, identity_exposed: true } }
  assert.match(freshness(changed).text, /sign-in, account or CLI changed/)
  assert.match(freshness({ ...catalog, refresh: { ...catalog.refresh, identity_exposed: false } }).text, /identity not exposed/)
  assert.match(modelLimitation('gemini', 'flash'), /cannot guarantee that a specific model runs/)
  assert.match(modelLimitation('gemini', 'auto'), /choose the model/)
  assert.match(modelLimitation('gemini', null), /nothing is pinned/)
  assert.equal(modelLimitation('claude', 'sonnet'), '')
  assert.match(conditionText({ kind: 'model_not_enforced', label: 'Selected model not verified', detail: 'Gemini CLI reported running x.' }),
    /reported running x\. It could not be verified/)
  const picker = readFileSync(new URL('../src/ModelPicker.jsx', import.meta.url), 'utf8')
  assert.match(picker, /provider !== 'gemini' && <option value=\{OTHER_MODEL\}>/)  // No typed Gemini pins.
})

test('execution facts report requested and provider-reported values only', () => {
  const facts = Object.fromEntries(executionFacts({ model: 'sonnet', resolved_model: 'x-sonnet', effective_model: 'x-sonnet',
    requested_effort: 'high', effective_effort: null, config_source: 'override', entitlement_state: 'cli_listed' }))
  assert.equal(facts['Applied effort'], 'Not reported by the provider')
  assert.equal(facts.Source, 'One-time override')
  assert.equal(facts['Reported model'], 'x-sonnet')
  assert.deepEqual(executionFacts(null), [])
})

test('the model picker has no static model list and keeps unavailable entries disabled', () => {
  const picker = readFileSync(new URL('../src/ModelPicker.jsx', import.meta.url), 'utf8')
  assert.match(picker, /groups\.unavailable\.map\(\(model\) => <option key=\{model\.model_id\} value=\{model\.model_id\} disabled>/)
  assert.doesNotMatch(picker, /sonnet|opus|gpt-|gemini-/i)
  const form = readFileSync(new URL('../src/StartAgentForm.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(form, /modelSuggestions|datalist/)
  assert.match(form, /<ModelPicker/)
  const details = readFileSync(new URL('../src/AgentDetails.jsx', import.meta.url), 'utf8')
  assert.equal((details.match(/<NextPromptConfig/g) || []).length, 2)  // Reply and Redirect.
})
