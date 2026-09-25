import test from 'node:test'
import assert from 'node:assert/strict'
import { readTheme, applyTheme, connectTheme } from '../src/theme.mjs'
import { agentName, attentionAgents, statusLabels, workspaceSummary, attentionContext } from '../src/agentPresentation.mjs'
import { matchesSearch } from '../src/agentListView.mjs'

test('theme preference is validated and unavailable storage falls back safely', () => {
  assert.equal(readTheme({ getItem: () => 'dark' }), 'dark')
  assert.equal(readTheme({ getItem: () => 'unknown' }), 'automatic')
  assert.equal(readTheme({ getItem() { throw Error() } }), 'automatic')
})
test('automatic theme tracks live system changes and cleans up listener', () => {
  const root = { dataset: {} }
  let listener
  const media = { matches: false, addEventListener(_, fn) { listener = fn }, removeEventListener(_, fn) { assert.equal(fn, listener); listener = null } }
  const cleanup = connectTheme('automatic', root, media)
  assert.equal(root.dataset.theme, 'light')
  media.matches = true; listener()
  assert.equal(root.dataset.theme, 'dark')
  cleanup(); assert.equal(listener, null)
  assert.equal(applyTheme('light', true), 'light')
  assert.equal(applyTheme('dark', false), 'dark')
})
test('attention uses waiting only, names are searchable and UUID is not the title', () => {
  const agent = { agent_id: 'uuid', display_name: 'Backend Engineer', task: 'Review', status: 'waiting' }
  assert.equal(agentName(agent), 'Backend Engineer')
  assert.equal(agentName({ agent_id: 'uuid' }), 'Untitled agent')
  assert.equal(matchesSearch(agent, 'engineer'), true)
  assert.deepEqual(attentionAgents([agent, { status: 'stopped' }, { status: 'finished' }]), [agent])
  assert.equal(statusLabels.waiting, 'Needs you')
})


test('workspace summary counts only real working and waiting agents', () => {
  assert.equal(workspaceSummary([]), '0 working · 0 need you')
  assert.equal(workspaceSummary([{ status: 'running' }, { status: 'waiting' }, { status: 'finished' }, { status: 'stopped' }]), '1 working · 1 need you')
})
test('attention context is concise and never copies the blocking question', () => {
  const agent = { display_name: 'Engineer', task: 'Review API behavior', waiting_question: 'What secret key should I use?' }
  assert.equal(attentionContext(agent), 'Review API behavior')
  assert.equal(attentionContext({ ...agent, task: 'Engineer' }), 'Waiting for your guidance')
  assert.ok(attentionContext({ ...agent, task: 'x'.repeat(1000) }).length <= 60)
})
