import test from 'node:test'
import assert from 'node:assert/strict'
import { addTemplate, draftView, emptyDraft, newTaskPayload, TEMPLATES, toolState } from '../src/atlas/model/newTask.mjs'

const policy = { allowed: ['claude', 'codex'] }
const statuses = [{ provider: 'claude', state: { state: 'ready' } }, { provider: 'codex', state: { state: 'not_authenticated', reason: 'Sign in with codex login.' } },
  { provider: 'gemini', state: { state: 'ready' } }]
const catalog = { provider: 'claude', models: [
  { model_id: 'Sonnet 4.6', selectable: true, status: 'available', status_label: 'Available', supported_efforts: ['low', 'high'], explanation: 'Listed.' },
  { model_id: 'Preview X', selectable: false, requires_attempt: true, status: 'unverified', status_label: 'Unverified', explanation: 'Not listed.' }] }
const projects = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:/repo' }]
const ready = { ...emptyDraft(), text: 'Fix the parser', projectId: 'p1', provider: 'claude', picker: { choice: 'Sonnet 4.6', typed: '', effort: 'high', attempt: false } }

test('tool state separates permission from readiness, and mock is always usable', () => {
  assert.deepEqual(toolState('mock', null, null), { allowed: true, usable: true, note: 'Simulated work. No AI tool runs.' })
  assert.deepEqual(toolState('claude', policy, statuses), { allowed: true, usable: true, note: 'Ready' })
  assert.equal(toolState('codex', policy, statuses).usable, false)
  assert.match(toolState('codex', policy, statuses).note, /Sign in with codex login/)
  assert.equal(toolState('gemini', policy, statuses).allowed, false) // ready, but not allowed
  assert.equal(toolState('claude', undefined, statuses).allowed, false) // permissions still loading
  assert.equal(toolState('claude', null, statuses).allowed, false) // permissions unavailable: nothing real is allowed
})

test('the disabled reason names the first missing requirement', () => {
  const context = { policy, statuses, catalog, projects }
  assert.equal(draftView(emptyDraft(), context).problem, 'Describe the task to start.')
  assert.equal(draftView({ ...ready, projectId: null }, context).problem, 'Choose a project to start.')
  assert.match(draftView({ ...ready, provider: 'codex' }, context).problem, /^Codex can’t start now/)
  assert.match(draftView({ ...ready, picker: { choice: 'Preview X', typed: '', effort: '', attempt: false } }, context).problem, /explicit attempt/)
  assert.equal(draftView({ ...ready, picker: { choice: 'Preview X', typed: '', effort: '', attempt: true } }, context).problem, null)
  assert.equal(draftView({ ...ready, autonomous: true, allocation: 'auto', autoProviders: [] }, context).problem, 'Check at least one tool for automatic choice.')
  assert.match(draftView({ ...ready, autonomous: true, allocation: 'auto', autoProviders: ['gemini'] }, context).problem, /not in your allowed providers/)
  assert.equal(draftView(ready, context).problem, null)
})

test('the creation payload is the classic one, with helpers and Auto only for an eligible lead', () => {
  const context = { policy, statuses, catalog, projects }
  const solo = { ...ready, autonomous: false }
  assert.deepEqual(newTaskPayload(solo, draftView(solo, context)), { project_id: 'p1', task: 'Fix the parser', agent_type: 'claude', sandbox: 'read-only',
    autonomous: false, model: 'Sonnet 4.6', effort: 'high' })
  const auto = { ...ready, autonomous: true, allocation: 'auto', autoProviders: ['claude', 'codex'], sandbox: 'workspace-write' }
  assert.deepEqual(newTaskPayload(auto, draftView(auto, context)), { project_id: 'p1', task: 'Fix the parser', agent_type: 'claude',
    sandbox: 'workspace-write', autonomous: true, model: 'Sonnet 4.6', effort: 'high', auto_allocation_providers: ['claude', 'codex'],
    split_policy: 'lead_decides' })
  assert.equal(newTaskPayload({ ...auto, splitPolicy: 'approve' }, draftView(auto, context)).split_policy, 'approve')
  assert.equal(newTaskPayload(solo, draftView(solo, context)).split_policy, undefined)
  const explicit = { ...auto, allocation: 'explicit' }
  assert.equal(newTaskPayload(explicit, draftView(explicit, context)).auto_allocation_providers, undefined)
  const attempt = { ...ready, autonomous: false, picker: { choice: 'Preview X', typed: '', effort: '', attempt: true } }
  assert.equal(newTaskPayload(attempt, draftView(attempt, context)).attempt_unverified, true)
  // Gemini cannot lead helpers: autonomy is never sent for it, whatever the switch says.
  const gemini = { ...ready, provider: 'gemini', autonomous: true, picker: { choice: '', typed: '', effort: '', attempt: false } }
  const geminiView = draftView(gemini, { policy: { allowed: ['gemini'] }, statuses, catalog: null, projects })
  assert.equal(newTaskPayload(gemini, geminiView).autonomous, false)
  assert.equal(geminiView.canLead, false)
  // Mock sends no model.
  const mock = { ...ready, provider: 'mock' }
  assert.deepEqual(newTaskPayload(mock, draftView(mock, context)), { project_id: 'p1', task: 'Fix the parser', agent_type: 'mock', sandbox: 'read-only', autonomous: false })
})

test('the summary explains lead and helpers without promising either', () => {
  const context = { policy, statuses, catalog, projects }
  const solo = draftView({ ...ready, autonomous: false }, context).summary
  assert.match(solo[0], /^Claude Code \(Sonnet 4\.6, high effort\) leads this task in a private copy of Release planning and can only read files\.$/)
  assert.equal(solo[1], 'It works alone. No helper creation is authorized for this task.')
  const helpers = draftView({ ...ready, autonomous: true }, context).summary
  assert.match(helpers[1], /It plans first, with read-only access/)
  assert.match(helpers[2], /never replaced/)
  const auto = draftView({ ...ready, autonomous: true, allocation: 'auto', autoProviders: ['claude', 'codex'] }, context).summary
  assert.match(auto[2], /may pick Claude Code or Codex, only if allowed, ready and suited/)
  assert.equal(draftView({ ...ready, provider: 'mock' }, context).summary.length, 1)
})

test('templates add structure without losing text', () => {
  assert.equal(addTemplate('', TEMPLATES[0]), TEMPLATES[0].text)
  assert.equal(addTemplate('Keep this.  ', TEMPLATES[2]), `Keep this.\n\n${TEMPLATES[2].text}`)
})

test('Atlas starts with helpers off, without changing the classic default', async () => {
  const { initialAutonomousChoice } = await import('../src/autonomousCreation.mjs')
  assert.equal(emptyDraft().autonomous, false)
  assert.equal(initialAutonomousChoice, true) // Classic's own default is untouched
  const context = { policy, statuses, catalog, projects }
  const fresh = { ...emptyDraft(), text: 'Fix the parser', projectId: 'p1', provider: 'claude', picker: { choice: 'Sonnet 4.6', typed: '', effort: '', attempt: false } }
  const view = draftView(fresh, context)
  assert.equal(view.canLead, true)
  assert.equal(view.helpers, false)
  assert.equal(view.summary[1], 'It works alone. No helper creation is authorized for this task.')
  assert.deepEqual(newTaskPayload(fresh, view), { project_id: 'p1', task: 'Fix the parser', agent_type: 'claude', sandbox: 'read-only',
    autonomous: false, model: 'Sonnet 4.6' })
})

test('Full Access needs an explicit confirmation and sends the helpers’ access only when chosen', async () => {
  const { creationPayload } = await import('../src/autonomousCreation.mjs')
  const { accessText, accessSummary } = await import('../src/atlas/model/access.mjs')
  const full = creationPayload('Fix it', 'claude', 'full-access', true, null, 'p1', undefined, { helperAccess: 'workspace-write' })
  assert.equal(full.sandbox, 'full-access')
  assert.equal(full.helper_access, 'workspace-write')
  assert.equal(creationPayload('Fix it', 'claude', 'full-access', false, null, 'p1', undefined, { helperAccess: 'full-access' }).helper_access, undefined)
  assert.equal(creationPayload('Fix it', 'codex', 'workspace-write', true, null, 'p1', undefined, { helperAccess: 'full-access' }).helper_access, undefined)
  assert.match(accessText('claude', 'workspace-write'), /cannot confine/)
  assert.match(accessText('codex', 'workspace-write'), /tests, builds and Git/)
  assert.equal(accessSummary('full-access'), 'has Full Access (not sandboxed)')
})

test('publishing and outside integrations are off unless chosen, and only a new root task sends the choice', async () => {
  const { creationPayload } = await import('../src/autonomousCreation.mjs')
  const context = { policy, statuses, catalog, projects }
  assert.equal(emptyDraft().external, false)
  const local = draftView({ ...ready, autonomous: false }, context)
  assert.equal(local.summary.at(-1), 'It works locally: publishing, deploying and the tool’s outside apps and plugins stay off.')
  assert.equal('external_integrations' in newTaskPayload({ ...ready, autonomous: false }, local), false)
  const allowedDraft = { ...ready, autonomous: false, external: true }
  const allowed = draftView(allowedDraft, context)
  assert.match(allowed.summary.at(-1), /^It may use its tool’s outside apps, plugins and connectors, publishing included\./)
  assert.equal(newTaskPayload(allowedDraft, allowed).external_integrations, true)
  assert.equal(creationPayload('Fix it', 'mock', 'read-only', false, null, 'p1', undefined, { external: true }).external_integrations, undefined)
  assert.equal(creationPayload('Help', 'codex', 'workspace-write', false, 'parent-agent', undefined, undefined, { external: true }).external_integrations, undefined)
})

test('the split policy and plan phase read in plain words', async () => {
  const { SPLIT_POLICIES, planPhaseView, splitPolicySummary } = await import('../src/atlas/model/planPhase.mjs')
  assert.deepEqual(SPLIT_POLICIES.map((item) => item.value), ['lead_decides', 'approve', 'always'])
  assert.equal(planPhaseView(null), null)
  assert.equal(planPhaseView({ policy: 'unknown', phase: 'planning' }), null)
  const waiting = planPhaseView({ policy: 'approve', phase: 'awaiting_approval', retries: 0 })
  assert.equal(waiting.awaiting, true)
  assert.equal(waiting.policyTitle, 'Lead proposes, I approve')
  assert.equal(waiting.text, 'The plan is waiting for your approval.')
  assert.equal(planPhaseView({ policy: 'always', phase: 'planning', retries: 1 }).retried, true)
  assert.match(splitPolicySummary('approve'), /waits for your approval/)
})
