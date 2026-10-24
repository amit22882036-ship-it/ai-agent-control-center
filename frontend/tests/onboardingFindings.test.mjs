// Regression tests for the Stage 2G browser E2E findings (P2-1 to P2-8 and the selected P3 fixes).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { BACKEND_UNAVAILABLE, isConnectionFailure, nextStep, permissionFor, retryDelay, signInRow, submissionProblem,
  summaryRows, unavailableProviders, updateText } from '../src/onboardingPresentation.mjs'
import { viewportShift } from '../src/popoverPlacement.mjs'
import { HEARTBEAT_INTERVAL_MS, MAX_SYNC_DELAY_MS, nextSyncDelay } from '../src/systemNotificationSync.js'

const source = (name) => readFileSync(new URL(`../src/${name}`, import.meta.url), 'utf8')
const entry = (provider, state, overrides = {}) => ({
  provider, allowed: true, operations: [], active_work: [],
  discovery: { state: 'installed', method: 'npm', elsewhere: [] },
  status: { provider, settings: { enabled: true, ...(overrides.settings || {}) }, state: { state, ...(overrides.state || {}) } },
  catalog: null,
  ...(overrides.entry || {}),
})
const auth = (row) => summaryRows(row).find((item) => item.key === 'auth')

// ---------------------------------------------------------------- P2-1 sign-in semantics

test('P2-1: only an explicit false is "Not signed in"; unverified sign-in is never shown as failed', () => {
  const cases = [
    // provider, readiness state, authenticated, expected text, tone
    ['claude', 'ready', true, /^Signed in/, 'ready'],
    ['claude', 'not_authenticated', false, /^Not signed in$/, 'blocked'],
    ['claude', 'disabled', undefined, /^Not checked$/, 'waiting'],
    ['claude', 'usage_limited', null, /^Not checked$/, 'waiting'],  // after a restart: not probed again
    ['codex', 'billing_blocked', undefined, /^Not checked$/, 'waiting'],
    ['claude', 'unsupported_version', null, /^Not checked$/, 'waiting'],  // the probe stopped before sign-in
    ['codex', 'unchecked', undefined, /^Not checked$/, 'waiting'],
    ['codex', 'billing_blocked', true, /^Signed in/, 'ready'],  // an API-key sign-in is still a sign-in
  ]
  for (const [provider, state, authenticated, text, tone] of cases) {
    const row = auth(entry(provider, state, { state: { authenticated } }))
    assert.match(row.value, text, `${provider}/${state}`)
    assert.equal(row.tone, tone, `${provider}/${state}`)
  }
})

test('P2-1: Gemini CLI sign-in cannot be verified locally and is worded so', () => {
  const ready = auth(entry('gemini', 'ready', { state: { authenticated: null, auth_route: 'google (oauth-personal)', installed: true } }))
  assert.equal(ready.value, 'Cannot be verified locally; the settings select google (oauth-personal)')
  assert.equal(ready.tone, 'waiting')
  assert.match(auth(entry('gemini', 'billing_blocked', { state: { installed: true } })).value, /Cannot be verified locally \(Gemini CLI has no sign-in status command\)/)
  assert.equal(auth(entry('gemini', 'unchecked')).value, 'Not checked')
  assert.equal(signInRow('gemini', { authenticated: false }).value, 'Not signed in')
})

// ---------------------------------------------------------------- P2-2 next step and other blockers

test('P2-2: blocking conditions come before permissions and optional updates, and none is hidden', () => {
  const update = { status: 'update_available', latest_version: '9.9.9' }
  const table = [
    // [description, entry, update, expected primary kind, expected `also` kinds]
    ['usage limit, not allowed', entry('claude', 'usage_limited', { state: { reason: 'Session limit. Provider-reported reset: 2026-10-24T20:00:00Z.' }, entry: { allowed: false } }),
      null, 'usage', ['allow']],
    ['billing block with an update', entry('codex', 'billing_blocked', { state: { reason: 'The billing acknowledgment is required before new executions.' } }),
      update, 'billing', ['optional-update']],
    ['billing block, not allowed', entry('gemini', 'billing_blocked', { state: { reason: 'The user settings select gemini-api-key.' }, entry: { allowed: false } }),
      null, 'billing', ['allow']],
    ['disabled and not allowed', entry('claude', 'disabled', { settings: { enabled: false } , entry: { allowed: false } }), update, 'enable', ['allow', 'optional-update']],
    ['not installed and disabled', entry('gemini', 'not_installed', { settings: { enabled: false }, entry: { discovery: { state: 'not_installed' } } }),
      null, 'install', ['enable']],
    ['unsupported version', entry('claude', 'unsupported_version'), update, 'update', []],
    ['not signed in, not allowed', entry('codex', 'not_authenticated', { entry: { allowed: false } }), null, 'login', ['allow']],
    ['ready but not allowed, update', entry('codex', 'ready', { entry: { allowed: false } }), update, 'allow', ['optional-update']],
    ['ready with an update', entry('codex', 'ready'), update, 'update', []],
    ['ready', entry('codex', 'ready'), { status: 'up_to_date' }, 'ready', []],
    ['temporarily unavailable', entry('claude', 'temporarily_unavailable', { state: { reason: 'Overloaded.' } }), null, 'check', []],
    ['maintenance running, usage limit', entry('claude', 'usage_limited', { state: { reason: 'Limit.' }, entry: { operations: [{ status: 'running' }] } }),
      null, 'wait', ['usage']],
    ['uncertain maintenance, billing block', entry('codex', 'billing_blocked', { state: { reason: 'Blocked.' }, entry: { operations: [{ status: 'uncertain' }] } }),
      null, 'resolve', ['billing']],
  ]
  for (const [description, row, available, kind, also] of table) {
    const step = nextStep(row, available)
    assert.equal(step.kind, kind, description)
    assert.deepEqual(step.also.map((item) => item.kind), also, description)
  }
})

test('P2-2: billing and usage blockers show the backend reason verbatim, never an invented reset or a permission fix', () => {
  const reason = 'Session limit. Provider-reported reset: 2026-10-24T20:00:00Z (not a guarantee of availability).'
  const usage = nextStep(entry('claude', 'usage_limited', { state: { reason }, entry: { allowed: false } }))
  assert.equal(usage.text, reason)
  assert.doesNotMatch(usage.text, /Permissions/)
  const noReset = nextStep(entry('claude', 'usage_limited'))
  assert.equal(noReset.text, 'A provider usage limit applies.')
  assert.doesNotMatch(noReset.text, /reset|\d{4}-\d{2}/i)
  const billing = nextStep(entry('codex', 'billing_blocked', { state: { reason: 'Codex is signed in with an API key (pay-per-use billing).' } }),
    { status: 'update_available', latest_version: '1.0.0' })
  assert.equal(billing.text, 'Codex is signed in with an API key (pay-per-use billing).')
  assert.equal(billing.also[0].text, 'Optional: Version 1.0.0 is available.')
})

// ---------------------------------------------------------------- P2-3 presentation of an unresolved comparison

test('P2-3: an unresolved comparison is not "Not checked yet · checked <time>"', () => {
  assert.equal(updateText({ status: 'unknown', checked_at: null }), 'Not checked yet')
  const text = updateText({ status: 'unknown', checked_at: '2026-10-24T15:23:09Z', latest_version: '0.156.0' })
  assert.match(text, /^Not compared yet: the installed version is not known · checked 2026-10-24T15:23:09Z$/)
  assert.doesNotMatch(text, /Not checked yet/)
})

// ---------------------------------------------------------------- P2-8 unknown policy

test('P2-8: an unloaded or unavailable permission policy authorizes no real provider', () => {
  for (const provider of ['codex', 'claude', 'gemini']) {
    assert.deepEqual(permissionFor(undefined, provider), { allowed: false, reason: 'Checking which providers you allow…', state: 'loading' })
    assert.equal(permissionFor(null, provider).allowed, false)
    assert.equal(permissionFor(null, provider).state, 'unavailable')
    assert.match(permissionFor(null, provider).reason, /could not be loaded.*Retry, or use Mock/)
    assert.equal(permissionFor({}, provider).allowed, false)  // A malformed policy is not an allowed set.
  }
  assert.equal(permissionFor(undefined, 'mock').allowed, true)
  assert.equal(permissionFor(null, 'mock').allowed, true)
  assert.equal(permissionFor({ allowed: ['claude'] }, 'claude').allowed, true)
  assert.equal(permissionFor({ allowed: [] }, 'claude').state, 'forbidden')
})

// ---------------------------------------------------------------- P2-4 visible, accessible reasons

test('P2-4: every unavailable provider has a visible reason, permission first, without selecting it', () => {
  const statuses = [{ provider: 'codex', state: { state: 'ready' } },
    { provider: 'claude', state: { state: 'usage_limited', reason: 'Session limit.' } },
    { provider: 'gemini', state: { state: 'ready' } }]
  assert.deepEqual(unavailableProviders({ allowed: ['codex', 'claude'] }, statuses), [
    { provider: 'claude', kind: 'setup', reason: 'Claude Code: Usage limited. Session limit.' },
    { provider: 'gemini', kind: 'forbidden', reason: 'Gemini CLI is not in your allowed providers. Allow it in AI Providers.' },
  ])
  assert.deepEqual(unavailableProviders(null, null).map((item) => item.kind), ['unavailable', 'unavailable', 'unavailable'])
  assert.deepEqual(unavailableProviders({ allowed: ['codex'] }, null).map((item) => item.provider), ['claude', 'gemini'])
  const form = source('StartAgentForm.jsx')
  assert.match(form, /aria-describedby=\{\[blocked\.some\(\(item\) => item\.provider === value\) && reasonId\(value\),/)  // Each choice: its own reason.
  assert.match(form, /<ul className="provider-unavailable" aria-label="Providers that cannot start now">/)
  assert.doesNotMatch(form, /<span title=\{permissionFor/)  // The reason is no longer only a hover title.
  assert.match(form, /Set up in AI Providers/)
  assert.match(form, /Retry loading permissions/)
})

// ---------------------------------------------------------------- P2-5 stale composer

test('P2-5: a choice made under an older policy is not sent; nothing is replaced for the user', () => {
  // Another tab narrowed the policy to Codex after this form was opened with all three allowed.
  const fresh = { allowed: ['codex'] }
  assert.match(submissionProblem(fresh, 'gemini'), /Gemini CLI is no longer in your allowed providers/)
  assert.match(submissionProblem(fresh, 'codex', ['codex', 'claude']), /Auto may no longer use Claude Code/)
  assert.match(submissionProblem(fresh, 'codex', ['claude', 'gemini']), /Claude Code, Gemini CLI: they are not/)
  assert.equal(submissionProblem(fresh, 'codex', ['codex']), null)
  assert.equal(submissionProblem(fresh, 'mock'), null)
  assert.match(submissionProblem(null, 'codex'), /could not be loaded/)  // A failed refresh never falls back to the old set.
  const form = source('StartAgentForm.jsx')
  assert.match(form, /const problem = submissionProblem\(await refreshPolicy\(\), agentType, autoChosen\)/)
  assert.match(form, /if \(request !== policyRequest\.current\) return undefined/)  // Only the newest response applies.
  assert.match(form, /window\.addEventListener\('focus', onFocus\)/)
  assert.match(form, /\[visible, refreshProviders, refreshPolicy, refreshVersion\]/)
  assert.match(form, /disabled=\{unavailable \|\| \(!permissionFor\(policy, choice\.id\)\.allowed && !autoProviders\.includes\(choice\.id\)\)\}/)
  assert.match(source('App.jsx'), /visible=\{view === 'workspace'\} refreshVersion=\{detailsRefresh\}/)
})

// ---------------------------------------------------------------- P2-6 backend disconnect

test('P2-6: automatic reloads back off while the backend is unreachable; Retry is immediate', () => {
  assert.equal(retryDelay(0), 0)
  assert.deepEqual([1, 2, 3, 4, 5, 9].map(retryDelay), [2000, 5000, 10000, 20000, 30000, 30000])
  assert.equal(isConnectionFailure(Object.assign(new Error('x'), { offline: true })), true)
  assert.equal(isConnectionFailure(Object.assign(new Error('Claude Code is not signed in'), { status: 409 })), false)
  assert.match(BACKEND_UNAVAILABLE, /backend is not reachable/)
  assert.equal(nextSyncDelay(0), HEARTBEAT_INTERVAL_MS)
  assert.deepEqual([1, 2, 3, 8].map(nextSyncDelay), [4000, 8000, MAX_SYNC_DELAY_MS, MAX_SYNC_DELAY_MS])
  const view = source('ProvidersView.jsx')
  assert.match(view, /onClick=\{\(\) => loader\.retry\(\)\}>Retry<\/button>/)  // The only manual read.
  assert.match(view, /useEffect\(\(\) => \{ loader\.request\(\) \}, \[refreshVersion, loader\]\)/)  // Live/polling: automatic.
  assert.match(view, /const reload = useCallback\(\(\) => loader\.request\(\), \[loader\]\)/)  // After actions: automatic.
  assert.doesNotMatch(view, /load\(version > 0\)/)  // No earlier action enables the bypass.
  assert.match(view, /may be out of date; unsaved changes are kept/)
  assert.match(view, /error\.offline = true/)
  assert.doesNotMatch(view, /return \(\) => \{ active = false \}\n  \}, \[refreshVersion, version\]\)/)
  assert.match(source('useProviderNotices.js'), /if \(state\.running \|\| Date\.now\(\) < state\.notBefore\) return/)
  assert.match(source('useAgentNotifications.js'), /if \(syncing \|\| Date\.now\(\) < notBefore\) return/)
})

// ---------------------------------------------------------------- P2-7 popover placement

test('P2-7: a popover is moved back inside the viewport at phone width', () => {
  // Observed at 375px: the 310px surface anchored at the bell's right edge (252px) began at -58px.
  assert.equal(viewportShift({ left: -58, right: 252 }, 375), 74)
  assert.equal(viewportShift({ left: 600, right: 910 }, 1024), 0)  // Desktop: unchanged.
  assert.equal(viewportShift({ left: 100, right: 400 }, 375), -41)
  assert.equal(viewportShift({ left: 20, right: 400 }, 375), -4)  // Never past the left margin.
  assert.equal(viewportShift(null, 375), 0)
  assert.match(source('App.css'), /\.popover-surface \{[^}]*max-height:calc\(100vh - 96px\); overflow:auto;/)
  const popover = source('Popover.jsx')
  assert.match(popover, /event\.key === 'Escape'/)
  assert.match(popover, /viewportShift\(element\.getBoundingClientRect\(\)/)
})

// ---------------------------------------------------------------- P3-A Review update

test('P3-A: Review update opens that provider\'s update review, which still needs approval', () => {
  const bar = source('WorkspaceBar.jsx')
  assert.match(bar, /onReviewUpdate\(notice\.provider\)/)
  assert.match(bar, /data-popover-close/)
  assert.match(source('App.jsx'), /focus=\{providerFocus\}/)
  const view = source('ProvidersView.jsx')
  assert.match(view, /focus\.action === 'update' && availableActions\(entry\)\.includes\('update'\)/)
  assert.match(view, /setTimeout\(\(\) => review\('update'\), 0\)/)  // Builds the plan to read only.
  assert.match(view, /disabled=\{!reviewed \|\| Boolean\(busy\)\}/)  // Running still needs the explicit review.
})
