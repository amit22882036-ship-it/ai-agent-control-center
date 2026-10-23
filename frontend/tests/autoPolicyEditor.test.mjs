import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { adderSelection, catalogChoices, draftErrors, draftFromView, draftPayload, formatSpec, modelMode, modelRestrictionText,
  moveItem, parseSpec, scopeText, specLabel } from '../src/allocationPresentation.mjs'

const view = (policy, extra = {}) => ({ policy, inherited: false, own_revision: policy ? 7 : null, owner_title: 'Root',
  categories: ['architecture', 'testing'], ...extra })
const full = { task_id: 'root', enabled: true, providers: ['codex', 'claude'], models: { codex: ['gpt-deep'] },
  preferences: ['claude/opus@high', 'codex'], profile: { testing: { preferred: ['codex/gpt-fast'], acceptable: [] } } }

test('candidates are edited as structured provider/model/effort, never typed syntax', () => {
  assert.deepEqual(parseSpec('claude/opus@high'), { provider: 'claude', model: 'opus', effort: 'high' })
  assert.deepEqual(parseSpec('codex'), { provider: 'codex', model: null, effort: null })
  assert.equal(parseSpec('bad spec'), null)
  assert.equal(formatSpec({ provider: 'codex', model: 'gpt-deep', effort: null }), 'codex/gpt-deep')
  assert.equal(specLabel(parseSpec('codex')), 'Codex · provider default model · default effort')
  assert.equal(specLabel(parseSpec('codex'), { anyModel: true }), 'Codex · any model · default effort')
})

test('the draft is an isolated copy, and saving keeps every field deliberately', () => {
  const source = view(full)
  const draft = draftFromView(source)
  draft.preferences.push('codex/gpt-fast')
  draft.models.codex.push('gpt-fast')
  assert.deepEqual(source.policy.preferences, ['claude/opus@high', 'codex'])  // Cancel leaves the loaded policy untouched.
  assert.deepEqual(source.policy.models.codex, ['gpt-deep'])
  const payload = draftPayload(draftFromView(source))
  assert.deepEqual(payload, { enabled: true, providers: ['codex', 'claude'], models: { codex: ['gpt-deep'] },
    preferences: ['claude/opus@high', 'codex'], profile: { testing: { preferred: ['codex/gpt-fast'] } }, expected_revision: 7 })
})

test('an empty model list means unrestricted, never "no models"', () => {
  assert.equal(modelRestrictionText([]), 'All otherwise eligible models are allowed.')
  assert.equal(modelRestrictionText(['gpt-deep']), 'Only 1 selected model allowed.')
  const draft = draftFromView(view(full))
  draft.modes.codex = 'all'  // The explicit choice of "all otherwise eligible models".
  draft.models.gemini = ['pro']  // Not an allowed provider: dropped, never a hidden grant.
  assert.deepEqual(draftPayload(draft).models, {})
})

test('removing the last allowed model never widens the policy', () => {
  const draft = draftFromView(view(full))
  assert.equal(modelMode(draft, 'codex'), 'only')
  draft.models.codex = []  // The user unchecked "gpt-deep".
  assert.equal(modelMode(draft, 'codex'), 'only')  // Still restricted: no silent switch to "all".
  assert.match(draftErrors(draft)[0], /Select at least one Codex model/)
  assert.throws(() => draftPayload(draft), /Select at least one Codex model/)  // Never saved as unrestricted.
  draft.models.codex = ['gpt-fast']
  assert.deepEqual(draftPayload(draft).models, { codex: ['gpt-fast'] })  // Exactly the explicit choice.
  const fresh = draftFromView(view({ ...full, models: {} }))
  fresh.modes.claude = 'only'  // Switching to "only" starts empty and must be completed.
  assert.equal(draftErrors(fresh).length, 1)
  assert.deepEqual(draftErrors({ ...draftFromView(view(full)), providers: [] }), ['Allow at least one provider.'])
})

test('the adder never keeps a provider, model or effort that is no longer allowed', () => {
  const choices = [{ id: 'gpt-deep', efforts: ['low', 'high'] }, { id: 'gpt-fast', efforts: [] }]
  assert.deepEqual(adderSelection([], { provider: '', model: '', effort: '' }), { provider: '', model: '', effort: '' })
  assert.deepEqual(adderSelection(['codex'], { provider: '', model: '', effort: '' }), { provider: 'codex', model: '', effort: '' })  // None -> one.
  assert.deepEqual(adderSelection(['claude'], { provider: 'codex', model: 'gpt-deep', effort: 'high' }, choices),
    { provider: 'claude', model: '', effort: '' })  // The selected provider was removed.
  assert.deepEqual(adderSelection(['codex', 'claude'], { provider: 'codex', model: 'gpt-deep', effort: 'high' }, choices),
    { provider: 'codex', model: 'gpt-deep', effort: 'high' })
  assert.deepEqual(adderSelection(['codex'], { provider: 'codex', model: 'gpt-gone', effort: 'high' }, choices),
    { provider: 'codex', model: '', effort: '' })  // A model no longer listed.
  assert.deepEqual(adderSelection(['codex'], { provider: 'codex', model: 'gpt-fast', effort: 'high' }, choices),
    { provider: 'codex', model: 'gpt-fast', effort: '' })  // An effort the model does not list.
})

test('preference order is reorderable and bounded', () => {
  assert.deepEqual(moveItem(['a', 'b', 'c'], 2, -1), ['a', 'c', 'b'])
  assert.deepEqual(moveItem(['a', 'b'], 0, -1), ['a', 'b'])
  assert.deepEqual(moveItem(['a', 'b'], 1, 1), ['a', 'b'])
})

test('inheritance and local overrides are explicit', () => {
  assert.match(scopeText(view(null)), /No Auto policy applies yet/)
  assert.match(scopeText(view(full, { inherited: true, own_revision: null })), /inherited from parent task “Root”.*local override.*not changed/)
  assert.match(scopeText(view(full)), /owns its Auto policy/)
  const inherited = draftFromView(view({ ...full, enabled: false }, { inherited: true, own_revision: null }))
  assert.equal(inherited.expected, null)  // A new local override: the backend refuses it if one appeared meanwhile.
  assert.equal(draftFromView(view(null), 'claude').providers[0], 'claude')
  assert.deepEqual(draftFromView(view({ ...full, providers: ['mock', 'codex'] })).providers, ['codex'])  // Mock is not offered.
})

test('catalog choices keep saved identifiers and never newly allow restricted models', () => {
  const catalog = { models: [
    { model_id: 'gpt-deep', display_name: 'GPT Deep', status_label: 'Available', selectable: true, supported_efforts: ['high'] },
    { model_id: 'gpt-locked', display_name: 'Locked', status_label: 'Requires a specific plan', selectable: false, supported_efforts: [] },
    { model_id: 'gpt-old', display_name: 'Old', status_label: 'Removed by provider', selectable: false, supported_efforts: [] }] }
  const choices = catalogChoices(catalog, ['gpt-gone', 'provider_default'])
  assert.deepEqual(choices.map((c) => [c.id, c.selectable]),
    [['gpt-deep', true], ['gpt-locked', false], ['gpt-old', false], ['gpt-gone', false]])
  assert.equal(choices[3].status, 'Not in the current model list (kept)')
})

test('the editor is inline, labelled, keyboard operable and token-based', () => {
  const editor = readFileSync(new URL('../src/AutoPolicyEditor.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(editor, /role="dialog"|aria-modal|<dialog/)
  for (const pattern of [/<summary>Allowed models<\/summary>/, /<summary>Preference order/, /<summary>Suitability profiles/,
    /aria-label=\{`Move \$\{/, /aria-label=\{`Remove \$\{/, /All otherwise eligible models are allowed/, /Save local override/,
    /onChange\(\[\], 'all'\)/, /disabled=\{busy \|\| errors\.length > 0\}/]) {
    assert.match(editor, pattern)
  }
  assert.doesNotMatch(editor, /sonnet|opus|gpt-|gemini-/i)  // No frontend model list.
  assert.match(editor, /view\?\.categories/)  // Categories come from the backend.
  const panel = readFileSync(new URL('../src/AutoAllocationPanel.jsx', import.meta.url), 'utf8')
  assert.doesNotMatch(panel, /useEffect\([^)]*PUT/)  // Opening the inspector never writes the policy.
  const css = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')
  assert.doesNotMatch(css.slice(css.indexOf('/* Advanced Auto policy editor')), /#[0-9a-f]{3,6}\b/i)
})

test('an inherited disabled policy stays disabled until the user enables Auto', () => {
  const disabled = draftFromView(view({ ...full, enabled: false }, { inherited: true, own_revision: null }))
  assert.equal(disabled.enabled, false)  // Never silently enabled by opening a local override.
  const payload = draftPayload(disabled)
  assert.equal(payload.enabled, false)
  assert.deepEqual([payload.providers, payload.models, payload.preferences, payload.expected_revision],
    [['codex', 'claude'], { codex: ['gpt-deep'] }, ['claude/opus@high', 'codex'], null])  // Everything else is kept.
  assert.equal(draftFromView(view(full, { inherited: true, own_revision: null })).enabled, true)  // Enabled stays enabled.
  const created = draftFromView(view(null), 'codex')
  assert.equal(created.enabled, false)  // A new policy starts off; enabling is the user's explicit action.
  assert.deepEqual(created.providers, ['codex'])
  assert.equal(draftPayload({ ...created, enabled: true }).enabled, true)
})
