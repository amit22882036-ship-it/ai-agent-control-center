import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { accessOptions, capabilityReason, changeEligible, changePayload, conditionText, modelStatus, modelSuggestions, providerLabel,
  refusalMessage, selectable, stateLabel, stateOf, stateTone, supports, usageLines } from '../src/providerPresentation.mjs'
import { autonomyEligible, creationPayload } from '../src/autonomousCreation.mjs'
import { transferRequest } from '../src/agentRemoval.mjs'
import { attentionAgents } from '../src/agentPresentation.mjs'

const status = (provider, state, extra = {}) => ({ provider, state: { state, reason: `${state} reason`, guidance: 'Do this.' },
  documented_models: ['sonnet'], settings: { enabled: state !== 'disabled', models: ['team-model'] }, usage: { blocks: [], remaining: null }, ...extra })

test('every readiness state has a distinct, compact label and tone', () => {
  const states = ['ready', 'disabled', 'not_installed', 'invalid_installation', 'unsupported_version', 'not_authenticated',
    'incompatible', 'usage_limited', 'temporarily_unavailable', 'billing_blocked', 'unchecked']
  const labels = states.map(stateLabel)
  assert.equal(new Set(labels).size, states.length)
  assert.equal(stateTone('ready'), 'ready')
  assert.equal(stateTone('usage_limited'), 'waiting')
  assert.equal(stateTone('billing_blocked'), 'blocked')
  assert.equal(stateLabel('something-new'), 'Unknown')
})

test('only ready providers are selectable; Mock stays the development default', () => {
  assert.equal(selectable(null, 'mock'), true)
  assert.equal(selectable(status('claude', 'ready'), 'claude'), true)
  for (const state of ['disabled', 'not_installed', 'not_authenticated', 'usage_limited', 'billing_blocked', 'unchecked'])
    assert.equal(selectable(status('claude', state), 'claude'), false, state)
  assert.equal(selectable(null, 'gemini'), false)
  assert.equal(stateOf(null), 'unchecked')
  assert.equal(providerLabel('claude'), 'Claude Code')
})

test('capabilities follow the backend declaration and gate actions before use', () => {
  const gemini = status('gemini', 'ready', { capabilities: { autonomous: { supported: false, reason: 'No managed sub-agents.' } } })
  assert.equal(capabilityReason(gemini, 'autonomous'), 'No managed sub-agents.')
  assert.equal(supports('gemini', 'autonomous', gemini), false)
  assert.equal(supports('gemini', 'reply'), true)
  assert.equal(supports('mock', 'reply'), false)
  assert.equal(supports('claude', 'redirect'), true)
  assert.equal(autonomyEligible('claude'), true)
  assert.equal(autonomyEligible('gemini'), false)
  assert.equal(autonomyEligible('claude', 'parent'), false)
})

test('models are suggested from documentation and the user catalog, never advertised as verified', () => {
  const claude = status('claude', 'ready')
  assert.deepEqual(modelSuggestions(claude), ['sonnet', 'team-model'])
  assert.equal(modelStatus(claude, '').kind, 'default')
  assert.equal(modelStatus(claude, 'sonnet').kind, 'known')
  assert.match(modelStatus(claude, 'brand-new').text, /not verified/)
  assert.deepEqual(creationPayload('Work', 'claude', 'read-only', true, null, 'p', ' sonnet '),
    { project_id: 'p', task: 'Work', agent_type: 'claude', sandbox: 'read-only', autonomous: true, model: 'sonnet' })
  assert.equal('model' in creationPayload('Work', 'mock', 'read-only', true, null, 'p', 'sonnet'), false)
  assert.equal('model' in creationPayload('Work', 'codex', 'read-only', true, null, 'p', '  '), false)
})

test('usage shows only provider-reported values and says when nothing is reported', () => {
  const limited = status('claude', 'usage_limited', { usage: { blocks: [{ reason: 'Rate limit reported.', reset_at: null }], remaining: null } })
  const lines = usageLines(limited)
  assert.ok(lines.some((line) => line.includes('Reset time unknown')))
  assert.ok(lines.some((line) => line.includes('not reported')))
  assert.ok(!lines.join(' ').match(/%|\$|remaining: \d/))
  const reported = usageLines(status('codex', 'usage_limited', { usage: { blocks: [{ reason: 'Limit.', reset_at: '2026-10-24T00:00:00Z' }] } }))
  assert.ok(reported[0].includes('Provider-reported reset: 2026-10-24T00:00:00Z'))
  assert.deepEqual(usageLines(status('mock', 'ready')), [])
})

test('provider waits read accurately and changes never widen access', () => {
  assert.match(conditionText({ kind: 'usage_limited', label: 'Waiting for provider usage', reset_at: null }), /session are kept/)
  assert.doesNotMatch(conditionText({ kind: 'usage_limited', label: 'Waiting for provider usage', reset_at: null }), /reset:/)
  assert.match(conditionText({ kind: 'auth_required', label: 'Waiting for authentication' }), /Sign in/)
  assert.deepEqual(accessOptions('read-only'), ['read-only'])
  assert.deepEqual(accessOptions('workspace-write'), ['workspace-write', 'read-only'])
  assert.deepEqual(changePayload('gemini', '  ', 'read-only'), { provider: 'gemini', model: null, sandbox: 'read-only' })
  const waiting = { status: 'waiting', agent_type: 'claude', waiting_context: { kind: 'provider' } }
  assert.equal(changeEligible(waiting), true)
  assert.equal(changeEligible({ ...waiting, status: 'running' }), false)
  assert.equal(changeEligible({ ...waiting, agent_type: 'mock' }), false)
  assert.equal(changeEligible({ ...waiting, autonomous: true }), false)
  assert.equal(changeEligible({ ...waiting, waiting_context: { kind: 'recovery' } }), false)
  assert.equal(changeEligible({ ...waiting, managed: true }), false)
  // A provider wait needs the user eventually (allow work, sign in, continue).
  assert.deepEqual(attentionAgents([{ ...waiting, agent_id: 'a' }, { agent_id: 'b', status: 'waiting', waiting_context: { kind: 'delegation' } }]).map((a) => a.agent_id), ['a'])
})

test('refusals are shown as the backend wrote them', () => {
  assert.equal(refusalMessage({ detail: 'Claude Code: not enabled.' }, 'x'), 'Claude Code: not enabled.')
  assert.equal(refusalMessage({ detail: { message: 'Gemini CLI: not installed.' } }, 'x'), 'Gemini CLI: not installed.')
  assert.equal(refusalMessage({}, 'fallback'), 'fallback')
})

test('responsibility transfer can choose any provider and keeps Mock without access', () => {
  const state = { transferable: true, removed_owner: { assignment_id: 'a' } }
  assert.equal(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'claude', sandbox: 'workspace-write' }).agent_type, 'claude')
  assert.equal(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'gemini' }).sandbox, 'read-only')
  assert.equal(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'other' }).agent_type, 'mock')
  assert.equal(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'mock', sandbox: 'workspace-write' }).sandbox, null)
})

test('provider UI stays inline (no modal), labelled and theme-token based', () => {
  const panel = readFileSync(new URL('../src/ProviderPanel.jsx', import.meta.url), 'utf8')
  const controls = readFileSync(new URL('../src/ProviderControls.jsx', import.meta.url), 'utf8')
  const form = readFileSync(new URL('../src/StartAgentForm.jsx', import.meta.url), 'utf8')
  const picker = readFileSync(new URL('../src/ModelPicker.jsx', import.meta.url), 'utf8')
  const settings = readFileSync(new URL('../src/AgentModelSettings.jsx', import.meta.url), 'utf8')
  const next = readFileSync(new URL('../src/NextPromptConfig.jsx', import.meta.url), 'utf8')
  const css = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')
  for (const source of [panel, controls, form, picker, settings, next]) {
    assert.doesNotMatch(source, /role="dialog"|aria-modal|<dialog/)
  }
  assert.match(panel, /<details className="provider-panel">/)
  assert.match(form, /<legend>Provider<\/legend>/)
  assert.match(picker, /htmlFor={`\${id}-model`}/)
  assert.match(picker, /htmlFor={`\${id}-effort`}/)
  const block = css.slice(css.indexOf('/* Stage 2F providers'))
  assert.doesNotMatch(block, /#[0-9a-f]{3,6}\b/i)
  assert.match(block, /prefers-reduced-motion/)
})

import { blockingText, preservedText, resumeText, usageSummary, windowRows } from '../src/providerPresentation.mjs'

const usage = (windows, extra = {}) => ({ provider: 'claude', usage: { source: 'exact_when_reported', overall: 'limited',
  explanation: 'x', windows, blocks: [], unpersisted: 0, refresh_supported: true, last_observed_at: '2026-10-23T09:00:00Z', ...extra } })

test('usage windows show only provider-reported values, separately and with staleness', () => {
  const rows = windowRows(usage([
    { window_id: 'session', label: 'Session limit', status: 'exhausted', used_percent: 100, reset_at: '2026-10-23T14:00:00Z', observed_at: 't', fresh: true },
    { window_id: 'weekly_all', label: 'Weekly limit (all models)', status: 'available', used_percent: 45, reset_at: null, observed_at: 't', fresh: false },
    { window_id: 'active_limit', label: 'Current limit', status: 'unknown', used_percent: null, reset_at: null, observed_at: 't', fresh: true },
  ]))
  assert.deepEqual(rows.map((row) => row.used), ['100% used', '45% used', null])
  assert.equal(rows[2].usedPercent, null)  // No fabricated progress bar.
  assert.match(rows[0].reset, /Provider-reported reset/)
  assert.equal(rows[1].reset, 'Reset time not reported')  // Never derived from a window length.
  assert.ok(rows[1].stale && /stale/.test(rows[1].observed))
  assert.ok(rows.every((row) => !/remaining/i.test(JSON.stringify(row))))
  const summary = usageSummary(usage([]))
  assert.equal(summary.overall, 'Limit reached')
  assert.equal(summary.refreshable, true)
  assert.equal(usageSummary({ provider: 'gemini', usage: { source: 'none', overall: 'unknown', windows: [] } }).source, 'No usage information exposed')
})

test('waiting explains which limits block, what is preserved and whether resume is possible', () => {
  assert.equal(blockingText({ kind: 'usage_limited', blocking: [{ label: 'Session limit' }] }), 'Session limit.')
  assert.equal(blockingText({ kind: 'usage_limited', blocking: [{ label: 'Weekly limit' }] }), 'Weekly limit.')
  assert.match(blockingText({ kind: 'usage_limited', blocking: [{ label: 'Session limit' }, { label: 'Weekly limit' }] }), /^Multiple quota limits reached/)
  assert.equal(blockingText({ kind: 'usage_limited', blocking: [] }), 'Quota availability unknown.')
  assert.match(preservedText({ session_preserved: true }), /resumable provider session/)
  assert.doesNotMatch(preservedText({ session_preserved: false }), /session/)
  assert.match(resumeText({ available: false, reason: 'Session limit. Provider-reported reset: x.' }), /^Resume unavailable/)
  assert.equal(resumeText({ available: true, mode: 'explicit_retry', reason: 'Cannot be confirmed.' }), 'Cannot be confirmed.')
  assert.match(resumeText({ available: true, mode: 'verified' }), /Manual resume available/)
  assert.match(conditionText({ kind: 'usage_limited', label: 'Waiting for provider usage', reset_at: '2026-10-23T14:00:00Z' }), /not a guarantee/)
  assert.match(conditionText({ kind: 'usage_limited', label: 'Waiting for provider usage' }), /Nothing resumes automatically/)
})

test('the inspector resumes only through an explicit, labelled action', () => {
  const controls = readFileSync(new URL('../src/ProviderControls.jsx', import.meta.url), 'utf8')
  assert.match(controls, /'\/resume'/)
  assert.match(controls, /explicit_retry/)
  assert.match(controls, /Resume task/)
  assert.doesNotMatch(controls, /setInterval|setTimeout/)  // No hidden automatic resumption.
})
