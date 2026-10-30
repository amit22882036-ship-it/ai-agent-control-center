import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { attentionOf, attentionText, billingView, conditionOf, entryOf, humanTimes, policyChanges, readinessFacts, routeProvider,
  setupView, usageView } from '../src/atlas/model/connections.mjs'
import { KEY_GROUPS, diagnosticsText, notificationView, settingsSection, supervisionView } from '../src/atlas/model/settings.mjs'
import { ACCOUNT_NOTICES, BILLING_GUIDES, accountNotice, billingGuide } from '../src/providerGuidance.mjs'
import { connectionTool, documentTitle, parseHash, routeKey } from '../src/atlas/router.mjs'
import { outcome } from '../src/atlas/components/connections/outcome.mjs'
import { entry, meter, onboarding } from '../e2e/providers.mjs'

const facts = (item) => Object.fromEntries(readinessFacts(item).map((fact) => [fact.key, fact]))

test('installed, signed in, turned on, allowed for tasks and ready are separate facts, never one badge', () => {
  const ready = facts(entry('claude'))
  assert.deepEqual(Object.keys(ready), ['installed', 'signed-in', 'enabled', 'allowed', 'ready'])
  assert.ok(Object.values(ready).every((fact) => fact.mark === 'yes'))
  const notAllowed = facts(entry('claude', { allowed: false }))
  assert.equal(notAllowed.ready.mark, 'yes')  // Ready and allowed are independent.
  assert.equal(notAllowed.allowed.mark, 'no')
  const unknown = facts(entry('gemini', { state: 'unchecked', authenticated: null }))
  assert.equal(unknown['signed-in'].mark, 'unknown')
  assert.equal(unknown.ready.mark, 'unknown')
  assert.equal(facts(entry('codex', { installed: false, state: 'not_installed' })).installed.value, 'Not installed')
})

test('each tool has one condition from a closed set; a tool not in use is not an error', () => {
  const key = (options, provider = 'claude') => conditionOf(entry(provider, options)).key
  assert.equal(key({}), 'ready')
  assert.equal(conditionOf(entry('claude', { allowed: false })).label, 'Not allowed for tasks')
  assert.equal(key({ state: 'usage_limited' }), 'limited')
  assert.equal(key({ state: 'temporarily_unavailable' }), 'unavailable')
  assert.equal(key({ state: 'disabled', enabled: false }), 'off')
  assert.equal(key({ state: 'unchecked' }), 'unchecked')
  assert.equal(key({ state: 'not_authenticated', authenticated: false }), 'attention')
  assert.equal(key({ state: 'not_authenticated', authenticated: false, enabled: false, allowed: false }), 'off')
  assert.equal(key({ operations: [{ operation_id: 'o', action: 'update', status: 'running' }] }), 'setup')
  assert.equal(key({ operations: [{ operation_id: 'o', action: 'update', status: 'uncertain' }] }), 'attention')
  assert.equal(conditionOf(null), null)
})

test('attention lists exactly the reported tools that need the person, with no invented count', () => {
  assert.equal(attentionOf(null), null)
  const calm = attentionOf(onboarding({ gemini: { state: 'disabled', enabled: false, allowed: false } }))
  assert.equal(calm.any, false)
  assert.equal(attentionText(calm), null)
  const busy = attentionOf(onboarding({ codex: { state: 'usage_limited' }, gemini: { state: 'not_authenticated', authenticated: false },
    policy: { default_problem: 'Claude Code is not allowed.' } }))
  assert.deepEqual(busy.providers.map((item) => item.provider), ['codex', 'gemini'])
  assert.match(attentionText(busy), /Codex: usage-limited; Gemini CLI: requires attention; the default for new tasks can’t be used/)
})

test('usage shows only provider-reported values, with when they were observed; unknown is “Not available”', () => {
  const claude = usageView(entry('claude', { windows: [meter('Session limit', 42)] }).status)
  assert.equal(claude.headline, 'Session limit: 42% used')
  assert.equal(claude.rows[0].usedPercent, 42)
  assert.ok(claude.rows[0].observedAt)
  assert.equal(claude.remaining, 'Not available')
  assert.equal(claude.refreshable, true)
  const noPercent = usageView(entry('claude', { windows: [meter('Session limit', 42, { used_percent: null, status: 'unknown' })] }).status)
  assert.equal(noPercent.rows[0].usedPercent, null)  // No bar without a reported percentage.
  assert.equal(noPercent.headline, 'No reading yet')
  assert.equal(usageView(entry('codex').status).headline, 'Only limit-reached signals')
  assert.equal(usageView(entry('gemini').status).headline, 'Not available')
  assert.equal(usageView(entry('gemini').status).refreshable, false)
  const stale = usageView(entry('claude', { windows: [meter('Session limit', 10, { fresh: false })] }).status)
  assert.equal(stale.stale, true)
  const limited = usageView(entry('codex', { blocks: [{ block_id: 'b', reason: 'Codex reported a usage limit.', reset_at: null }] }).status)
  assert.equal(limited.headline, 'Limit reached')
  assert.equal(limited.clearable, true)
  assert.equal(limited.blocks[0].resetText, 'Reset time not reported')  // Never a guessed reset.
  assert.ok(!limited.lines.some((line) => line.startsWith('Codex reported')), 'a block is listed once')
})

test('billing keeps Classic’s exact payloads and sequence; the acknowledgment is the person’s statement', () => {
  const claudeOff = billingView(entry('claude', { enabled: false, confirmed: false, state: 'disabled' }))
  assert.deepEqual(claudeOff.form, { action: 'enable', payload: { enabled: true, billing_confirmed: true }, button: 'Confirm and turn on' })
  assert.match(claudeOff.text, /paid overage, extra usage, credits and API billing are not enabled/)
  assert.equal(claudeOff.canEnable, false)  // Claude Code is turned on only together with the confirmation.
  const claudeOn = billingView(entry('claude'))
  assert.equal(claudeOn.form, null)
  assert.equal(claudeOn.canDisable, true)
  assert.ok(claudeOn.recorded.at)
  assert.equal(claudeOn.route, 'subscription')
  const codex = billingView(entry('codex', { confirmed: false }))
  assert.deepEqual(codex.form, { action: 'acknowledge', payload: { billing_confirmed: true }, button: 'Record acknowledgment' })
  assert.equal(codex.kind, 'acknowledgment')
  assert.equal(billingView(entry('codex', { enabled: false })).canEnable, true)  // Codex turns on separately.
  assert.equal(billingView(entry('gemini', { enabled: false, confirmed: false, state: 'disabled' })).form.action, 'enable')
})

test('setup offers reviewed actions only when nothing runs or awaits confirmation', () => {
  assert.deepEqual(setupView(entry('claude')).actions, ['update', 'login', 'logout'])
  assert.deepEqual(setupView(entry('codex', { installed: false, state: 'not_installed' })).actions, ['install'])
  const running = setupView(entry('claude', { operations: [{ operation_id: 'o', action: 'update', status: 'running' }] }))
  assert.deepEqual(running.actions, [])
  assert.equal(running.running.operation_id, 'o')
  const uncertain = setupView(entry('claude', { operations: [{ operation_id: 'u', action: 'install', status: 'uncertain', started_at: '2026-10-30T08:00:00Z' }] }))
  assert.deepEqual(uncertain.actions, [])
  assert.equal(uncertain.uncertain.operation_id, 'u')
  const update = setupView(entry('claude'), { status: 'update_available', latest_version: '2.2.0', installed_version: '2.1.0', channel: 'latest', checked_at: '2026-10-30T07:00:00Z' })
  assert.equal(update.update.available, true)
  assert.doesNotMatch(update.update.text, /2026-10-30T/, 'recorded times are shown in the short form')
})

test('saving permissions lists every widening and narrowing before anything is sent', () => {
  const policy = { allowed: ['codex', 'claude'], default_provider: 'claude', default_model: 'opus', default_effort: null, revision: 3 }
  const changes = policyChanges(policy, { allowed: ['claude', 'gemini'], defaultProvider: 'claude' }, { model: 'opus', effort: null })
  assert.deepEqual(changes.map((item) => item.kind), ['narrow', 'widen'])
  assert.match(changes.find((item) => item.kind === 'widen').text, /Allows Gemini CLI/)
  const model = policyChanges(policy, { allowed: ['codex', 'claude'], defaultProvider: 'claude' }, { model: 'sonnet', effort: 'high' })
  assert.match(model[0].text, /Default model for new tasks: sonnet, high effort/)
  assert.match(model.at(-1).text, /running agents keep their own settings/)
  assert.deepEqual(policyChanges(policy, { allowed: ['codex', 'claude'], defaultProvider: 'claude' }, { model: 'opus', effort: null }), [])
})

test('an outcome is done, refused (nothing changed) or unknown (never retried)', () => {
  const words = { done: 'Done.', refused: 'Refused.', unknown: 'Read again.' }
  assert.deepEqual(outcome({ ok: true, status: 200 }, words), { ok: true, message: 'Done.' })
  assert.equal(outcome({ ok: false, status: 409, data: { detail: { message: 'Billing confirmation required.' } } }, words).message, 'Nothing was changed. Billing confirmation required.')
  const offline = outcome({ ok: false, status: 0, offline: true }, words)
  assert.equal(offline.ok, null)
  assert.equal(offline.tone, 'warn')
  assert.equal(outcome({ ok: false, status: 503, data: {} }, words).ok, null)
})

test('billing guidance names official places only and claims nothing the Control Center cannot see', () => {
  for (const provider of ['claude', 'codex', 'gemini']) {
    const guide = billingGuide(provider)
    assert.ok(guide.short && guide.summary && guide.where.length && guide.links.length, provider)
    for (const link of guide.links) assert.match(link.url, OFFICIAL)
    // The Control Center never claims to see or set the provider's spending controls.
    assert.doesNotMatch([guide.short, guide.summary, ...guide.where].join(' '), /Control Center (can|will) (see|read|turn|set|change|limit)/)
  }
  assert.equal(billingGuide('mock'), null)
  assert.match(BILLING_GUIDES.claude.where[0], /Settings › Usage › Usage credits/)
  assert.match(BILLING_GUIDES.claude.summary, /account setting/)
  assert.match(BILLING_GUIDES.codex.where.join(' '), /never buys credits/)
  assert.match(BILLING_GUIDES.codex.where.join(' '), /depends on your account and its settings/)
  assert.match(BILLING_GUIDES.codex.summary, /eligible plans/)
})

const OFFICIAL = /^https:\/\/(claude\.ai|support\.claude\.com|help\.openai\.com|learn\.chatgpt\.com|chatgpt\.com\/codex|geminicli\.com|developers\.google\.com|developers\.googleblog\.com)\//

test('Gemini CLI guidance separates what Google offers which accounts from what the Control Center allows', () => {
  const guide = BILLING_GUIDES.gemini
  const notice = accountNotice('gemini')
  const all = [guide.short, guide.summary, ...guide.where, notice.title, ...notice.lines].join(' ')
  // Personal accounts are no longer served; it is not presented as working for everyone.
  assert.match(notice.title, /Personal Google accounts no longer work/)
  assert.match(all, /June 18, 2026/)
  assert.match(all, /free, Google AI Pro or Google AI Ultra/)
  assert.doesNotMatch(all, /fixed cost with a Google sign-in/i)
  // Nor as gone for everyone: organization licenses and paid API routes remain with the provider.
  assert.match(all, /Gemini Code Assist Standard or Enterprise/)
  assert.doesNotMatch(all, /(discontinued|shut down|no longer exists)/i)
  // Supported by Gemini CLI is not allowed here; the backend's refusal stands.
  assert.match(all, /Paid Gemini API keys and Vertex AI still work in Gemini CLI but stay refused here/)
  assert.match(all, /accepts only the Google sign-in/)
  // Antigravity CLI is named as Google's replacement, never as a Control Center tool.
  assert.match(all, /Antigravity CLI is not a Control Center tool/)
  for (const link of [...guide.links, ...notice.links]) assert.match(link.url, OFFICIAL)
  assert.equal(accountNotice('claude'), null)
  assert.deepEqual(Object.keys(ACCOUNT_NOTICES), ['gemini'])
})

test('tool pages are addressable, titled and each its own page; a section is not', () => {
  assert.equal(routeProvider({ provider: 'claude' }), 'claude')
  assert.equal(routeProvider({ provider: 'mock' }), null)
  const route = parseHash('#/connections?provider=codex&section=usage')
  assert.equal(connectionTool(route), 'codex')
  assert.equal(routeKey(route), 'connections:codex')
  assert.equal(routeKey(parseHash('#/connections?provider=codex')), routeKey(route))
  assert.equal(routeKey(parseHash('#/connections')), 'connections:')
  assert.match(documentTitle(route), /^Codex · Connections/)
  assert.equal(entryOf(onboarding(), 'gemini').provider, 'gemini')
  assert.equal(humanTimes('checked 2026-10-30T07:00:00Z').includes('T07'), false)
})

test('settings: sections, keys, notification permission and supervision are worded from what is reported', () => {
  assert.equal(settingsSection({ section: 'notifications' }), 'notifications')
  assert.equal(settingsSection({ section: 'nope' }), null)
  const keys = KEY_GROUPS.flatMap(([, list]) => list.map(([, combo]) => combo))
  for (const key of ['L', 'M', 'F', 'O', 'Ctrl Enter']) assert.ok(keys.includes(key), key)
  assert.equal(notificationView({ supported: false }).state, 'This browser can’t show notifications.')
  assert.match(notificationView({ supported: true, permission: 'denied', enabled: false }).state, /Blocked by the browser/)
  assert.match(notificationView({ supported: true, permission: 'default', enabled: false }).state, /asks your browser for permission/)
  const on = notificationView({ supported: true, permission: 'granted', enabled: true, synced: 'offline' })
  assert.equal(on.state, 'On')
  assert.match(on.backend, /told again when it answers/)
  assert.match(on.prerequisite, /Windows Settings/)
  assert.match(supervisionView({ data: { status: 'supervised' } }).text, /^Supervised/)
  assert.match(supervisionView({ data: { status: 'unavailable', reason: 'self-test failed' } }).text, /self-test failed/)
  assert.match(supervisionView({ error: 'unreachable' }).text, /isn’t reachable/)
  const text = diagnosticsText({ live: { transport: 'live', receivedAt: null }, connection: { label: 'Live' }, supervision: { data: { status: 'supervised' } },
    notifications: { enabled: false, permission: 'default' }, connections: { data: {}, error: null }, appearance: 'dark', userAgent: 'test', now: new Date('2026-10-30T00:00:00Z') })
  assert.match(text, /Process supervision: supervised/)
  assert.match(text, /Notifications: off · browser permission default/)
})

test('Connections changes state only through the action client, with Classic’s endpoints', () => {
  const client = readFileSync(new URL('../src/atlas/api/agentClient.mjs', import.meta.url), 'utf8')
  for (const path of ['/operations/plan', '/provider-operations/', '/resolve', '/usage/allow', '/usage/refresh', "'/provider-policy'", '/providers/updates/check', '/notifications/preferences']) {
    assert.ok(client.includes(path), path)
  }
  const resolve = client.match(/resolveOperation:.*$/m)[0]
  assert.match(resolve, /confirm_ended: true/)
})
