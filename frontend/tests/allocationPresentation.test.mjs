import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { AUTO_SUMMARY, decisionLines, decisionTitle, heldChildren, policyPayload, policyText, providerChoices } from '../src/allocationPresentation.mjs'
import { creationPayload } from '../src/autonomousCreation.mjs'

const selected = { status: 'selected', provider: 'claude', model: 'opus', effort: 'high',
  explanation: 'Selected Claude Code · opus · high effort for architecture work: rated preferred by the Parent\'s assessment.\nUsage: not reported or not current (unknown, not zero).' }
const held = { status: 'blocked', provider: null, model: null, effort: null,
  explanation: 'Held: no sufficiently suitable, authorized candidate is available.\nExcluded Claude Code · opus · provider default effort (preferred): confirmed usage limit: Weekly.' }

test('explicit versus Auto at creation: Auto only for a new autonomous root, with the allowed providers', () => {
  assert.deepEqual(creationPayload('Do it', 'codex', 'read-only', true, null, 'p', undefined, { autoProviders: ['codex', 'claude', 'codex'] }).auto_allocation_providers,
    ['codex', 'claude'])
  assert.equal(creationPayload('Do it', 'codex', 'read-only', false, null, 'p', undefined, { autoProviders: ['codex'] }).auto_allocation_providers, undefined)
  assert.equal(creationPayload('Do it', 'codex', 'read-only', true, null, 'p').auto_allocation_providers, undefined)  // Explicit (default).
  assert.equal(creationPayload('Do it', 'codex', 'read-only', true, 'parent', undefined, undefined, { autoProviders: ['codex'] }).auto_allocation_providers, undefined)
})

test('the selected provider, model and effort, and why, are worded from the decision only', () => {
  assert.equal(decisionTitle(selected), 'Auto: Claude Code · opus · high effort')
  assert.equal(decisionTitle({ ...selected, model: null, effort: null }), 'Auto: Claude Code · provider default model · provider default effort')
  assert.equal(decisionTitle(held), 'Auto allocation held')
  const lines = decisionLines(held)
  assert.match(lines.summary, /^Held/)
  assert.match(lines.details[0], /confirmed usage limit/)
  assert.deepEqual(heldChildren({ children: [{ allocation: held }, { allocation: selected }] }), [{ allocation: held }])
})

test('policy state and payload', () => {
  assert.match(policyText(null), /Not enabled/)
  assert.match(policyText({ enabled: true, providers: ['codex', 'claude'], inherited: true }), /inherited.*Codex, Claude Code/)
  assert.deepEqual(policyPayload(true, ['codex', 'codex']), { enabled: true, providers: ['codex'] })
  const choices = providerChoices([{ provider: 'claude', state: { state: 'ready' } }])
  assert.deepEqual(choices.map((c) => [c.id, c.ready]), [['codex', false], ['claude', true], ['gemini', false]])
  assert.match(AUTO_SUMMARY, /Prioritizes task suitability and quality/)
})
