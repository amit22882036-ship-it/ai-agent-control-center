import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { allowedText, approvalRequest, availableActions, defaultChoice, defaultText, nextStep, noticeText, operationText,
  operationTone, permissionFor, planCommand, planRequest, planSource, policyDraft, policyPayload, restrictionPayload,
  restrictionText, summaryRows, updateText } from '../src/onboardingPresentation.mjs'

const source = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')
const entry = (overrides = {}) => ({
  provider: 'claude', allowed: true, operations: [], active_work: [],
  discovery: { state: 'installed', method: 'npm', version: '2.1.0', elsewhere: [] },
  status: { provider: 'claude', settings: { enabled: true }, state: { state: 'ready', authenticated: true, version: '2.1.0', auth_route: 'claude_ai', billing_route: 'subscription' } },
  catalog: { fresh: true, last_success_at: '2026-10-24T08:00:00Z' },
  ...overrides,
})

test('the next step follows the order a new user meets: install, update, check, sign in, enable, allow, ready', () => {
  assert.equal(nextStep(entry({ discovery: { state: 'not_installed' }, status: { state: { state: 'not_installed' }, settings: {} } })).kind, 'install')
  assert.equal(nextStep(entry({ discovery: { state: 'unsupported_location', elsewhere: ['C:/x/claude.exe'] } })).kind, 'install')
  assert.equal(nextStep(entry({ status: { settings: { enabled: true }, state: { state: 'unsupported_version' } } })).kind, 'update')
  assert.equal(nextStep(entry({ status: { settings: { enabled: true }, state: { state: 'unchecked' } } })).kind, 'check')
  assert.equal(nextStep(entry({ status: { settings: { enabled: true }, state: { state: 'not_authenticated' } } })).kind, 'login')
  assert.equal(nextStep(entry({ status: { settings: { enabled: false }, state: { state: 'disabled', authenticated: true } } })).kind, 'enable')
  assert.equal(nextStep(entry({ allowed: false })).kind, 'allow')
  assert.equal(nextStep(entry(), { status: 'update_available', latest_version: '2.2.0' }).kind, 'update')
  assert.equal(nextStep(entry(), { status: 'up_to_date' }).kind, 'ready')
  assert.equal(nextStep(entry({ operations: [{ status: 'running', action: 'update' }] })).kind, 'wait')
  assert.equal(nextStep({ provider: 'mock' }), null)
})

test('installation, sign-in, enablement, permission, billing, models and updates are separate facts', () => {
  const rows = Object.fromEntries(summaryRows(entry({ allowed: false }), { status: 'up_to_date', checked_at: 't' }).map((row) => [row.key, row]))
  assert.deepEqual(Object.keys(rows), ['install', 'auth', 'enabled', 'allowed', 'billing', 'models', 'update'])
  assert.match(rows.install.value, /Installed · npm \(global\) · 2\.1\.0/)
  assert.equal(rows.enabled.value, 'Enabled')
  assert.equal(rows.allowed.value, 'Not allowed')  // Enabled and allowed are independent.
  assert.equal(rows.allowed.tone, 'blocked')
  assert.match(rows.auth.value, /Signed in/)
  const unknown = Object.fromEntries(summaryRows(entry({ status: { settings: {}, state: { state: 'unchecked' } }, catalog: null })).map((row) => [row.key, row]))
  assert.equal(unknown.auth.value, 'Not checked')
  assert.equal(unknown.billing.value, 'Not verified yet')
  assert.equal(unknown.models.value, 'Not listed yet')
})

test('setup actions are offered from installation and sign-in state; the backend validates each one', () => {
  assert.deepEqual(availableActions(entry({ discovery: { state: 'not_installed' } })), ['install'])
  assert.deepEqual(availableActions(entry()), ['update', 'login', 'logout'])
  assert.deepEqual(availableActions(entry({ status: { settings: {}, state: { state: 'not_authenticated', authenticated: false } } })), ['update', 'login'])
  assert.deepEqual(availableActions({ provider: 'mock' }), [])
})

test('an approval names the exact reviewed plan and nothing else', () => {
  assert.deepEqual(planRequest('update', 'stable'), { action: 'update', channel: 'stable' })
  assert.deepEqual(planRequest('login', null), { action: 'login' })
  const plan = { plan_id: 'p1', digest: 'd'.repeat(64), command: ['npm.cmd', 'install', '-g', '@anthropic-ai/claude-code@latest'] }
  assert.deepEqual(approvalRequest(plan), { plan_id: 'p1', digest: 'd'.repeat(64), approved: true })
  assert.equal(planCommand(plan), 'npm.cmd install -g @anthropic-ai/claude-code@latest')
  assert.throws(() => approvalRequest({ plan_id: null, digest: null, manual: ['Run gemini'] }), /no automated path/)
})

test('the AI Providers view never accepts a command, path or package from the user', () => {
  const view = source('ProvidersView.jsx')
  assert.doesNotMatch(view, /<textarea/)
  assert.doesNotMatch(view, /type="text"/)
  assert.doesNotMatch(view, /command:\s*[a-z]/i)  // The request carries only action and channel.
  assert.match(view, /approvalRequest\(plan\)/)
  assert.match(view, /setPlan\(null\).*used once/s)
})

test('operation results report what was verified, never just that a command ran', () => {
  assert.equal(operationText({ action: 'login', status: 'needs_attention', result: 'The sign-in window closed, but Claude Code does not report a sign-in.' }),
    'Sign in: Needs attention. The sign-in window closed, but Claude Code does not report a sign-in.')
  assert.equal(operationTone({ status: 'succeeded' }), 'ready')
  assert.equal(operationTone({ status: 'interrupted' }), 'waiting')
  assert.equal(operationTone({ status: 'failed' }), 'blocked')
})

test('update status is worded with versions, channel and freshness; failures are not shown as current', () => {
  assert.equal(updateText({ status: 'update_available', installed_version: '2.1.0', latest_version: '2.2.0', channel: 'stable', checked_at: 't' }),
    'Update available · 2.1.0 → 2.2.0 (stable) · checked t')
  assert.equal(updateText({ status: 'check_failed', error: 'Release metadata could not be read (URLError).', checked_at: 't', stale: true }),
    'Check failed · checked t (stale) · Release metadata could not be read (URLError).')
  assert.equal(updateText(null), 'Not checked yet')
  assert.match(noticeText({ label: 'Codex', available_version: '0.50.0', installed_version: '0.49.0', channel: 'latest', manual: true }),
    /Codex 0\.50\.0 is available .* updated manually/)
})

test('global permissions: an empty set allows none, and the revision loaded is required to save', () => {
  assert.equal(allowedText([]), 'No real provider is allowed. Only Mock agents can start.')
  assert.equal(allowedText(['codex', 'claude', 'gemini']), 'All providers are allowed.')
  assert.equal(allowedText(['gemini', 'codex']), 'Allowed: Codex, Gemini CLI.')
  const draft = policyDraft({ allowed: ['claude', 'codex'], default_provider: 'claude', default_model: 'opus', default_effort: 'high', revision: 7 })
  assert.deepEqual(policyPayload(draft, { model: 'opus', effort: 'high' }),
    { allowed: ['codex', 'claude'], default_provider: 'claude', default_model: 'opus', default_effort: 'high', expected_revision: 7 })
  assert.deepEqual(policyPayload({ ...draft, defaultProvider: 'mock' }, { model: 'opus', effort: 'high' }),
    { allowed: ['codex', 'claude'], default_provider: 'mock', default_model: null, default_effort: null, expected_revision: 7 })
  assert.throws(() => policyPayload({ ...draft, revision: null }), /Reload/)
})

test('the New Agent form disables a disallowed provider with its reason; Mock is never restricted', () => {
  const policy = { allowed: ['codex'] }
  assert.deepEqual(permissionFor(policy, 'codex'), { allowed: true, reason: null })
  assert.equal(permissionFor(policy, 'claude').allowed, false)
  assert.match(permissionFor(policy, 'claude').reason, /Claude Code is not in your allowed providers/)
  assert.equal(permissionFor({ allowed: [] }, 'mock').allowed, true)
  const form = source('StartAgentForm.jsx')
  assert.match(form, /disabled=\{unavailable \|\| !permissionFor\(policy, value\)\.allowed\}/)
  assert.match(form, /Set up in AI Providers/)
})

test('a forbidden or unusable default is shown with its reason and never replaced', () => {
  assert.equal(defaultChoice({ default_provider: null }), null)
  const bad = defaultChoice({ default_provider: 'gemini', default_model: null, default_effort: null, default_problem: 'Gemini CLI is not in your allowed providers.' })
  assert.equal(bad.provider, 'gemini')
  assert.match(defaultText(bad), /cannot be used: Gemini CLI is not in your allowed providers\. Nothing else was chosen for you\./)
  assert.equal(defaultText(defaultChoice({ default_provider: 'codex', default_model: 'gpt-5', default_effort: 'high' })),
    'Using your default: Codex · gpt-5 · high effort.')
  const form = source('StartAgentForm.jsx')
  assert.match(form, /touched\.current \|\| defaultApplied\.current/)  // Applied once, never over the user's choice.
})

test('Task restrictions narrow inherited permissions and explain where they come from', () => {
  const view = { task_id: 't2', own: { providers: ['codex'], revision: 4 }, inherited: ['codex', 'claude'], effective: ['codex'],
    sources: [{ scope: 'global', providers: ['codex', 'claude'] }, { scope: 'task', task_id: 't1', title: 'Parent', providers: ['codex', 'claude'] },
      { scope: 'task', task_id: 't2', title: 'Child', providers: ['codex'] }] }
  assert.equal(restrictionText(view), 'This Task allows Codex. Inherited from "Parent". Effective: Codex.')
  assert.equal(restrictionText({ task_id: 't', own: null, inherited: [], effective: [], sources: [] }),
    'This Task has no restriction of its own. Effective: no real provider (Mock only).')
  assert.deepEqual(restrictionPayload(['claude', 'codex'], view.own), { providers: ['codex', 'claude'], expected_revision: 4 })
  assert.deepEqual(restrictionPayload([], null), { providers: [], expected_revision: null })
})

test('the New Agent draft survives a visit to AI Providers: the composer stays mounted, hidden', () => {
  const app = source('App.jsx')
  assert.match(app, /<main id="workspace" className="dashboard" hidden=\{view !== 'workspace'\}>/)
  assert.match(app, /\{showStart && <StartAgentForm /)
  assert.doesNotMatch(app, /view === 'workspace' && showStart && <StartAgentForm/)
  assert.match(source('WorkspaceBar.jsx'), /AI Providers/)
})

test('a conflicting permissions edit from another tab keeps the draft and offers a reload', () => {
  const view = source('ProvidersView.jsx')
  assert.match(view, /setConflict\(err\.status === 409 && \/changed since\/\.test\(err\.message\)\)/)
  assert.match(view, /setError\(err\.message\)  \/\/ The draft is kept\./)
  assert.match(view, /Discard and reload/)
  const restrictions = source('ProviderRestrictions.jsx')
  assert.match(restrictions, /Your selection is kept/)
  assert.match(restrictions, /expected_revision=\$\{encodeURIComponent\(view\.own\.revision\)\}/)
})

test('critical setup interactions are labelled, announced and gated by an explicit review', () => {
  const view = source('ProvidersView.jsx')
  assert.match(view, /role="alert"/)
  assert.match(view, /role="status"/)
  assert.match(view, /aria-labelledby=\{`\$\{id\}-heading`\}/)
  assert.match(view, /disabled=\{!reviewed \|\| Boolean\(busy\)\}/)  // Run stays disabled until reviewed.
  const bar = source('WorkspaceBar.jsx')
  assert.match(bar, /aria-current=\{view === 'providers' \? 'page' : undefined\}/)
  assert.match(bar, /provider update\$\{notices\.length > 1 \? 's' : ''\}/)  // The bell names pending updates.
  assert.match(source('ProviderRestrictions.jsx'), /Review in AI Providers/)  // The inspector links to setup.
})

test('an operation whose end was not verified blocks the provider until the user confirms it ended', () => {
  const uncertain = entry({ operations: [{ operation_id: 'o', action: 'update', status: 'uncertain', result: 'could not be verified' }] })
  assert.equal(nextStep(uncertain).kind, 'resolve')
  assert.equal(operationTone({ status: 'uncertain' }), 'blocked')
  assert.match(operationText({ action: 'update', status: 'uncertain' }), /End not verified/)
  const view = source('ProvidersView.jsx')
  assert.match(view, /disabled=\{!ended \|\| Boolean\(busy\)\} onClick=\{confirmEnded\}/)  // Only after an explicit confirmation.
  assert.match(view, /resolve`, json\('POST', \{ confirm_ended: true \}\)/)
  assert.match(view, /!plan && !running && !unverified &&/)  // No new setup is offered meanwhile.
})

test('the reviewed plan names the exact package version, the official registry and no package scripts', () => {
  assert.equal(planSource({ package: '@openai/codex', target_version: '0.154.0', registry: 'https://registry.npmjs.org/' }),
    '@openai/codex@0.154.0 from https://registry.npmjs.org/, without package scripts')
  assert.equal(planSource({ package: null }), null)
  assert.match(source('ProvidersView.jsx'), /resolved to \$\{plan\.target_version\} for this approval/)
})

test('a Task hierarchy that cannot be verified is shown as permitting no real provider', () => {
  assert.equal(restrictionText({ task_id: 't', own: null, inherited: [], effective: [],
    sources: [{ scope: 'global', providers: ['codex'] }, { scope: 'unverified', reason: 'its ancestry contains a cycle' }] }),
  'The Task hierarchy could not be verified (its ancestry contains a cycle), so no real provider is permitted here. Mock only.')
})
