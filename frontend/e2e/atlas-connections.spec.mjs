import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { announceChange, openApp } from './harness.mjs'
import { meter, onboarding, plan } from './providers.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-30T09:05:00.000Z')
const heading = (page) => page.locator('.atlas h1')
const sent = (log, method, path) => log.requests.filter((item) => item.method === method && item.path === path)
const mutations = (log) => log.requests.filter((item) => item.method !== 'GET' && !item.path.startsWith('/notifications/'))
const card = (page, title) => page.locator('.a-cx-sec').filter({ has: page.getByRole('heading', { name: title, exact: true }) })

async function open(page, hash, options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  return openApp(page, { path: `/#/${hash}`, ...options })
}

const LIMITED = onboarding({
  claude: { windows: [meter('Session limit', 42)] },
  codex: { state: 'usage_limited', reason: 'Codex reported a usage limit.', blocks: [{ block_id: 'b1', reason: 'Codex reported a usage limit.', reset_at: null,
    observed_at: '2026-10-30T08:40:00Z' }] },
  gemini: { state: 'disabled', enabled: false, allowed: false, authenticated: null, confirmed: false },
})

test('the overview answers, for each tool, whether it is ready, what usage is known and where paid use is checked', async ({ page }) => {
  const log = await open(page, 'connections', { providers: LIMITED })
  await expect(heading(page)).toHaveText('Connections')
  const codex = page.getByRole('article', { name: 'Codex' })
  await expect(codex).toContainText('Usage-limited')
  await expect(codex.getByRole('listitem').filter({ hasText: 'Allowed for tasks' })).toContainText('Yes: Allowed for tasks')
  await expect(codex.getByRole('listitem').filter({ hasText: /^No: Ready/ })).toContainText('Usage limited')
  await expect(codex).toContainText('Limit reached')
  await expect(codex).toContainText('ChatGPT credits, if your plan allows them')
  const claude = page.getByRole('article', { name: 'Claude Code' })
  await expect(claude).toContainText('Ready for tasks')
  await expect(claude).toContainText('Session limit: 42% used')
  await expect(claude).toContainText('observed')
  const gemini = page.getByRole('article', { name: 'Gemini CLI' })
  await expect(gemini).toContainText('Turned off')  // Not in use is not an error.
  await expect(gemini).toContainText('Not available')
  // The navigation marks attention in words, never with an invented number.
  const nav = page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: /Connections/ })
  await expect(nav).toHaveAccessibleName(/Needs attention: Codex: usage-limited/)
  await expect(nav).not.toContainText(/\d/)
  await expect(page.getByText('Needs your attention')).toBeVisible()
  expect(mutations(log)).toEqual([])  // Opening Connections probes and changes nothing.
})

test('a calm set of tools shows no attention mark, and a tool page is its own address with Back and Forward', async ({ page }) => {
  await open(page, 'work', { providers: onboarding({ gemini: { state: 'disabled', enabled: false, allowed: false } }) })
  const nav = page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Connections', exact: true })
  await expect(nav).toBeVisible()
  await expect(page.locator('.a-nav-mark')).toHaveCount(0)
  await nav.click()
  await expect(heading(page)).toHaveText('Connections')
  await page.getByRole('article', { name: 'Claude Code' }).getByRole('link', { name: 'Claude Code', exact: true }).click()
  await expect(page).toHaveURL(/#\/connections\?provider=claude$/)
  await expect(heading(page)).toHaveText('Claude Code')
  await expect(heading(page)).toBeFocused()
  await expect(page).toHaveTitle(/^Claude Code · Connections/)
  await page.goBack()
  await expect(heading(page)).toHaveText('Connections')
  await page.goForward()
  await expect(heading(page)).toHaveText('Claude Code')
  await page.getByRole('button', { name: 'Connections' }).click()
  await expect(page).toHaveURL(/#\/connections$/)
})

test('install and update run only a reviewed plan, approved once with its identity and digest', async ({ page }) => {
  const log = await open(page, 'connections?provider=claude', { api: ({ method, path }) => {
    if (method === 'POST' && path === '/providers/claude/operations/plan') return { body: plan('claude', 'update') }
    if (method === 'POST' && path === '/providers/claude/operations') return { status: 202, body: { operation_id: 'op-1', status: 'running' } }
    return undefined
  } })
  const setup = card(page, 'Installation and sign-in')
  await setup.getByLabel('Channel').selectOption('stable')
  await setup.getByRole('button', { name: 'Update…' }).click()
  await expect.poll(() => sent(log, 'POST', '/providers/claude/operations/plan').map((item) => item.body)).toEqual([{ action: 'update', channel: 'stable' }])
  const review = setup.getByRole('group', { name: /Update Claude Code: review/ })
  await expect(review).toContainText('npm.cmd install --global @fixture/claude@2.1.301 --ignore-scripts')
  await expect(review).toContainText('resolved to 2.1.301 for this approval')
  const run = review.getByRole('button', { name: 'Run update' })
  await expect(run).toBeDisabled()
  await review.getByLabel(/I reviewed this exact operation/).check()
  await run.click()
  await expect(setup).toContainText('Update started')
  await expect.poll(() => sent(log, 'POST', '/providers/claude/operations').map((item) => item.body)).toEqual([{ plan_id: 'plan-claude-update', digest: 'd'.repeat(64), approved: true }])
  await expect(review).toHaveCount(0)  // Used once: another run needs a new review.
  await expect(setup.getByRole('button', { name: 'Update…' })).toBeVisible()
})

test('sign-in and sign-out are reviewed too; a manual-only plan shows its steps and cannot be run', async ({ page }) => {
  const log = await open(page, 'connections?provider=gemini', { providers: onboarding({ gemini: { authenticated: true } }), api: ({ method, path, body }) => {
    if (method === 'POST' && path === '/providers/gemini/operations/plan') {
      return { body: plan('gemini', body.action, { plan_id: null, digest: null, command: null, package: null, method: 'manual',
        manual: ['Run gemini in a terminal and choose Sign in with Google.'], changes: [], risks: [] }) }
    }
    return undefined
  } })
  const setup = card(page, 'Installation and sign-in')
  await setup.getByRole('button', { name: 'Sign out…' }).click()
  await expect.poll(() => sent(log, 'POST', '/providers/gemini/operations/plan').map((item) => item.body)).toEqual([{ action: 'logout' }])
  const review = setup.getByRole('group', { name: /Sign out Gemini CLI: review/ })
  await expect(review).toContainText('Run gemini in a terminal')
  await expect(review.getByRole('button', { name: /^Run/ })).toHaveCount(0)
  await review.getByRole('button', { name: 'Close' }).click()
  expect(sent(log, 'POST', '/providers/gemini/operations')).toEqual([])
})

test('Gemini CLI says which accounts Google still serves, apart from the one route the Control Center allows', async ({ page }) => {
  const log = await open(page, 'connections', { providers: onboarding({ gemini: { state: 'not_authenticated', authenticated: false, enabled: true } }) })
  await expect(page.getByRole('article', { name: 'Gemini CLI' })).toContainText('Personal Google accounts no longer work with Gemini CLI.')
  await expect(page.getByRole('article', { name: 'Claude Code' })).not.toContainText('no longer work')
  await open(page, 'connections?provider=gemini', { providers: onboarding({ gemini: { state: 'not_authenticated', authenticated: false, enabled: true } }),
    api: ({ method, path, body }) => (method === 'POST' && path === '/providers/gemini/operations/plan'
      ? { body: plan('gemini', body.action, { plan_id: null, digest: null, command: null, package: null, method: 'manual',
        manual: ['Open a terminal, run gemini, choose Sign in with Google.'], changes: [], risks: [] }) } : undefined) })
  const notice = page.getByRole('region', { name: 'Personal Google accounts no longer work with Gemini CLI' })
  await expect(notice).toContainText('free, Google AI Pro or Google AI Ultra accounts')
  await expect(notice).toContainText('Gemini Code Assist Standard or Enterprise')
  await expect(notice).toContainText('Paid Gemini API keys and Vertex AI still work in Gemini CLI but stay refused here')
  await expect(notice).toContainText('Antigravity CLI is not a Control Center tool')
  await expect(notice.getByRole('link', { name: /transition announcement/ })).toHaveAttribute('target', '_blank')
  await expect(card(page, 'Turned on and billing')).not.toContainText('fixed cost')
  // The sign-in plan repeats who Sign in with Google still works for.
  const setup = card(page, 'Installation and sign-in')
  await setup.getByRole('button', { name: 'Sign in…' }).click()
  const review = setup.getByRole('group', { name: /Sign in Gemini CLI: review/ })
  await expect(review).toContainText('still works for organizations with a Gemini Code Assist Standard or Enterprise license')
  expect(mutations(log)).toEqual([])
})

test('an operation whose end is not verified needs the person’s confirmation, and a running one can be canceled', async ({ page }) => {
  const uncertain = onboarding({ claude: { operations: [{ operation_id: 'op-u', provider: 'claude', action: 'install', status: 'uncertain', started_at: '2026-10-30T08:00:00Z' }] } })
  const log = await open(page, 'connections?provider=claude', { providers: uncertain })
  await expect(page.getByRole('article', { name: 'Claude Code' })).toHaveCount(0)
  const setup = card(page, 'Installation and sign-in')
  await expect(setup.getByRole('button', { name: 'Update…' })).toHaveCount(0)  // Nothing new starts until it is confirmed.
  const confirm = setup.getByRole('button', { name: 'Confirm it ended' })
  await expect(confirm).toBeDisabled()
  await setup.getByLabel('The setup window and installer have ended.').check()
  await confirm.click()
  await expect(setup).toContainText('Recorded that the operation ended.')
  await expect.poll(() => sent(log, 'POST', '/provider-operations/op-u/resolve').map((item) => item.body)).toEqual([{ confirm_ended: true }])

  const running = onboarding({ claude: { operations: [{ operation_id: 'op-r', provider: 'claude', action: 'login', status: 'running', started_at: '2026-10-30T08:00:00Z' }] } })
  const second = await open(page, 'connections?provider=claude', { providers: running })
  await expect(page.getByRole('button', { name: 'Check again' })).toBeVisible()
  await expect(card(page, 'Installation and sign-in')).toContainText('Finish signing in in the window that opened.')
  await card(page, 'Installation and sign-in').getByRole('button', { name: 'Cancel it' }).click()
  await expect.poll(() => sent(second, 'POST', '/provider-operations/op-r/cancel')).toHaveLength(1)
})

test('a lost connection while approving is an unknown outcome: said plainly, read again and never retried', async ({ page }) => {
  const log = await open(page, 'connections?provider=claude', { api: ({ method, path }) => {
    if (method === 'POST' && path === '/providers/claude/operations/plan') return { body: plan('claude', 'update') }
    if (method === 'POST' && path === '/providers/claude/operations') return { abort: true }
    return undefined
  } })
  const setup = card(page, 'Installation and sign-in')
  await setup.getByRole('button', { name: 'Update…' }).click()
  await setup.getByLabel(/I reviewed this exact operation/).check()
  const reads = sent(log, 'GET', '/providers/onboarding').length
  await setup.getByRole('button', { name: 'Run update' }).click()
  await expect(setup.getByRole('status').filter({ hasText: 'may or may not have started' })).toBeVisible()
  await expect.poll(() => sent(log, 'GET', '/providers/onboarding').length).toBeGreaterThan(reads)
  await page.waitForTimeout(600)
  await expect.poll(() => sent(log, 'POST', '/providers/claude/operations')).toHaveLength(1)
})

test('billing: Claude Code turns on only with the confirmation; Codex records its acknowledgment; both are the person’s statements', async ({ page }) => {
  const log = await open(page, 'connections?provider=claude', { providers: onboarding({ claude: { state: 'disabled', enabled: false, confirmed: false } }) })
  const billing = card(page, 'Turned on and billing')
  await expect(billing.getByRole('button', { name: 'Turn on' })).toHaveCount(0)
  const submit = billing.getByRole('button', { name: 'Confirm and turn on' })
  await expect(submit).toBeDisabled()
  await billing.getByLabel(/paid overage, extra usage, credits and API billing are not enabled/).check()
  await submit.click()
  await expect(billing).toContainText('Claude Code is on, with your confirmation recorded.')
  await expect.poll(() => sent(log, 'PUT', '/providers/claude/settings').map((item) => item.body)).toEqual([{ enabled: true, billing_confirmed: true }])
  await expect(billing.getByRole('link', { name: /Your Claude usage settings/ })).toHaveAttribute('href', 'https://claude.ai/settings/usage')
  await expect(billing.getByRole('link', { name: /Your Claude usage settings/ })).toHaveAttribute('target', '_blank')

  const codexLog = await open(page, 'connections?provider=codex&section=billing', { providers: onboarding({ codex: { confirmed: false, state: 'billing_blocked' } }) })
  const codex = card(page, 'Turned on and billing')
  await expect(codex.getByRole('heading', { name: 'Turned on and billing' })).toBeFocused()
  await codex.getByLabel(/runs through my ChatGPT subscription/).check()
  await codex.getByRole('button', { name: 'Record acknowledgment' }).click()
  await expect.poll(() => sent(codexLog, 'PUT', '/providers/codex/settings').map((item) => item.body)).toEqual([{ billing_confirmed: true }])
  await expect(codex).toContainText('depends on your account and its settings')
  await expect(codex.getByRole('link', { name: /Your Codex usage dashboard/ })).toHaveAttribute('href', 'https://chatgpt.com/codex/settings/usage')
  await expect(codex).toContainText('never buys credits')

  await open(page, 'connections?provider=claude')
  await expect(card(page, 'Turned on and billing')).toContainText('It is not proof of an account setting')
})

test('a disabled tool turns on separately from permission, and turning it off says what that means', async ({ page }) => {
  const log = await open(page, 'connections?provider=codex', { providers: onboarding({ codex: { state: 'disabled', enabled: false } }) })
  await expect(page.locator('.a-cx-head-acts')).toContainText('Turned off')
  const billing = card(page, 'Turned on and billing')
  await billing.getByRole('button', { name: 'Turn on' }).click()
  await expect(billing).toContainText('Codex is on.')
  await expect.poll(() => sent(log, 'PUT', '/providers/codex/settings').map((item) => item.body)).toEqual([{ enabled: true }])
  expect(sent(log, 'PUT', '/provider-policy')).toEqual([])  // Turning on never allows it for tasks.
})

test('usage: meters only from reported values, unknown usage is not available, a limit is cleared only after confirming', async ({ page }) => {
  const log = await open(page, 'connections?provider=claude&section=usage', { providers: onboarding({ claude: { windows: [meter('Session limit', 42),
    meter('Weekly limit', 0, { used_percent: null, status: 'unknown', reset_at: null })] } }) })
  const usage = card(page, 'Usage')
  await expect(usage.getByRole('meter', { name: 'Session limit used' })).toHaveAttribute('aria-valuenow', '42')
  await expect(usage.getByRole('meter')).toHaveCount(1)  // No bar for a meter without a reported percentage.
  await expect(usage).toContainText('Reset time not reported')
  await expect(usage).toContainText('Remaining')
  await expect(usage).toContainText('Not available: Claude Code doesn’t report remaining usage.')
  await usage.getByRole('button', { name: 'Refresh usage' }).click()
  await expect(usage).toContainText('No agent was resumed.')
  await expect.poll(() => sent(log, 'POST', '/providers/claude/usage/refresh')).toHaveLength(1)

  const limited = await open(page, 'connections?provider=codex&section=usage', { providers: LIMITED })
  const codex = card(page, 'Usage')
  await expect(codex).toContainText('Codex reported a usage limit. Reset time not reported')
  await expect(codex.getByRole('button', { name: 'Refresh usage' })).toHaveCount(0)
  await codex.getByRole('button', { name: 'Clear recorded limit…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Clear the recorded limit?' })
  await expect(dialog).toContainText('only after the limit has renewed')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  expect(sent(limited, 'POST', '/providers/codex/usage/allow')).toEqual([])
  await codex.getByRole('button', { name: 'Clear recorded limit…' }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Clear recorded limit' }).click()
  await expect.poll(() => sent(limited, 'POST', '/providers/codex/usage/allow')).toHaveLength(1)

  await open(page, 'connections?provider=gemini&section=usage')
  await expect(card(page, 'Usage')).toContainText('No usage readings. Unknown usage is neither zero nor confirmed available.')
})

test('models: each lists its own effort levels, the catalog refreshes without a model request', async ({ page }) => {
  const log = await open(page, 'connections?provider=claude&section=models')
  const models = card(page, 'Models')
  await expect(models.getByRole('row', { name: /Opus 4.8/ })).toContainText('low · medium · high')
  await models.getByRole('button', { name: 'Refresh models' }).click()
  await expect.poll(() => sent(log, 'POST', '/providers/claude/models/refresh').length).toBe(1)
  await open(page, 'connections?provider=gemini&section=models')
  await expect(card(page, 'Models').getByRole('row', { name: /auto/ })).toContainText('None (no reasoning control)')
})

test('allowed tools and the default model are saved with the loaded revision; every change is listed first; a conflict keeps the draft', async ({ page }) => {
  let conflict = true
  const log = await open(page, 'connections?section=permissions', { providers: onboarding({ gemini: { allowed: false } }), api: ({ method, path }) => {
    if (method === 'PUT' && path === '/provider-policy' && conflict) { conflict = false; return { status: 409, body: { detail: 'Provider permissions changed since they were loaded. Reload them.' } } }
    return undefined
  } })
  const perm = card(page, 'Allowed tools and defaults')
  await expect(perm.getByRole('heading', { name: 'Allowed tools and defaults' })).toBeFocused()
  await perm.getByLabel('Gemini CLI').check()
  await perm.getByLabel('Codex').uncheck()
  await perm.getByLabel('Default tool for new tasks').selectOption('claude')
  await perm.getByLabel('Default model').selectOption('Opus 4.8')
  await perm.getByLabel('Effort').selectOption('high')
  const changes = perm.getByRole('status').filter({ hasText: 'If you save' })
  await expect(changes).toContainText('Allows Gemini CLI for every new task')
  await expect(changes).toContainText('Stops allowing Codex for new work')
  await expect(changes).toContainText('Default tool for new tasks: Claude Code')
  await expect(changes).toContainText('running agents keep their own settings')
  await perm.getByRole('button', { name: 'Save allowed tools and defaults' }).click()
  await expect(perm.getByRole('alert')).toContainText('changed since they were loaded')
  await expect(perm.getByLabel('Gemini CLI')).toBeChecked()  // The draft is kept.
  await expect(perm.getByRole('button', { name: 'Discard and load the newer version' })).toBeVisible()
  await perm.getByRole('button', { name: 'Save allowed tools and defaults' }).click()
  await expect(perm).toContainText('Allowed tools and defaults saved.')
  const bodies = sent(log, 'PUT', '/provider-policy').map((item) => item.body)
  expect(bodies).toHaveLength(2)
  expect(bodies[1]).toEqual({ allowed: ['claude', 'gemini'], default_provider: 'claude', default_model: 'Opus 4.8', default_effort: 'high', expected_revision: 4 })
})

test('one request at a time: a second click while one runs sends nothing', async ({ page }) => {
  let release
  const log = await open(page, 'connections', { api: async ({ method, path }) => {
    if (method === 'POST' && path === '/providers/check') { await new Promise((resolve) => { release = resolve }); return { body: { providers: [] } } }
    return undefined
  } })
  const check = page.getByRole('button', { name: 'Check all' })
  await check.click()
  await expect(page.getByRole('button', { name: 'Checking…' })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Check for updates' })).toBeDisabled()
  await page.getByRole('button', { name: 'Checking…' }).click({ force: true })
  await expect.poll(() => typeof release).toBe('function')
  release()
  await expect(page.getByRole('status').filter({ hasText: 'No model was asked anything' })).toBeVisible()
  await expect.poll(() => sent(log, 'POST', '/providers/check')).toHaveLength(1)
})

test('while the backend is unreachable nothing can be changed, the last answer stays visible, and actions return when it answers', async ({ page }) => {
  let down = false
  await open(page, 'connections?provider=claude', { api: ({ method, path }) => (down && method === 'GET' && (path === '/workspace' || path === '/providers/onboarding') ? { abort: true } : undefined) })
  const check = page.getByRole('button', { name: 'Check again' })
  await expect(check).toBeEnabled()
  down = true
  await announceChange(page)
  await expect(page.locator('.a-cx-page .a-notice')).toContainText('Backend unavailable')
  await expect(check).toBeDisabled()
  await expect(card(page, 'Installation and sign-in').getByRole('button', { name: 'Update…' })).toBeDisabled()
  await expect(card(page, 'Usage')).toContainText('Session limit')  // Read-only facts stay.
  down = false
  await announceChange(page)
  await expect(check).toBeEnabled()
  await expect(page.locator('.a-cx-page .a-notice')).toHaveCount(0)
})

test('when the tools can’t be read at all nothing is assumed about any of them', async ({ page }) => {
  await open(page, 'connections', { providers: null })
  await expect(page.getByRole('alert')).toContainText('Your tools couldn’t be read')
  await expect(page.getByRole('article')).toHaveCount(0)
  await expect(page.locator('.a-nav-mark')).toHaveCount(0)
})

test('New Task and an agent’s settings lead to the tool in Connections', async ({ page }) => {
  await open(page, 'new', { providers: LIMITED })
  const more = page.locator('.a-tools-more')
  await expect(more.getByRole('link')).toHaveAttribute('target', '_blank')
  await expect(more.getByRole('link')).toHaveAttribute('href', /#\/connections/)
})

test('right-to-left names stay readable and the pages pass accessibility checks in light and dark, wide and narrow', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  const hebrew = onboarding({ claude: { activeWork: [{ execution_id: 'e1', agent_id: 'a1', state: 'running', name: 'סוכן התרגום' }], windows: [meter('Session limit', 42)] } })
  for (const [width, theme, hash] of [[1440, 'light', 'connections'], [1440, 'dark', 'connections?provider=claude'], [1024, 'light', 'connections?provider=codex'],
    [1180, 'dark', 'settings']]) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await open(page, hash, { providers: hash.includes('claude') ? hebrew : LIMITED })
    await expect(heading(page)).toBeVisible()
    await page.waitForTimeout(300)
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0)
    if (hash.includes('claude')) {
      const name = page.locator('.a-cx-agent').first()
      await expect(name).toHaveText('סוכן התרגום')
      expect(await name.evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
    }
    await page.addScriptTag({ path: axePath })
    const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
    const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
    console.log(`axe ${hash} ${width} ${theme}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
    expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    await page.unroute('**/*')
  }
})

test('a tool card opens with the keyboard, and the review checkbox and buttons are reachable without a mouse', async ({ page }) => {
  await open(page, 'connections', { api: ({ method, path }) => (method === 'POST' && path === '/providers/claude/operations/plan' ? { body: plan('claude', 'update') } : undefined) })
  await page.getByRole('article', { name: 'Claude Code' }).getByRole('link', { name: 'Claude Code', exact: true }).focus()
  await page.keyboard.press('Enter')
  await expect(heading(page)).toHaveText('Claude Code')
  await card(page, 'Installation and sign-in').getByRole('button', { name: 'Update…' }).focus()
  await page.keyboard.press('Enter')
  const box = page.getByLabel(/I reviewed this exact operation/)
  await box.focus()
  await page.keyboard.press('Space')
  await expect(box).toBeChecked()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Run update' })).toBeFocused()
})

test.describe('Settings', () => {
  test('appearance persists across reloads and is shared with Classic view', async ({ page }) => {
    await open(page, 'settings')
    await page.getByRole('radiogroup', { name: 'Appearance' }).getByRole('radio', { name: 'Dark' }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    expect(await page.evaluate(() => localStorage.getItem('control-center.theme'))).toBe('dark')
    await page.reload()
    await expect(page.getByRole('radio', { name: 'Dark' })).toHaveAttribute('aria-checked', 'true')
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    // Arrow keys move the choice (a radio group).
    await page.getByRole('radio', { name: 'Dark' }).focus()
    await page.keyboard.press('ArrowLeft')
    await expect(page.getByRole('radio', { name: 'Light' })).toHaveAttribute('aria-checked', 'true')
  })

  test('sections are part of the address, so Back and Forward move between them', async ({ page }) => {
    await open(page, 'settings')
    const index = page.getByRole('navigation', { name: 'Settings sections' })
    await index.getByRole('button', { name: 'Notifications' }).click()
    await expect(page).toHaveURL(/#\/settings\?section=notifications$/)
    await expect(page.getByRole('heading', { name: 'Notifications', exact: true })).toBeFocused()
    await index.getByRole('button', { name: 'Keyboard' }).click()
    await expect(page.getByRole('heading', { name: 'Keyboard', exact: true })).toBeFocused()
    await page.goBack()
    await expect(page).toHaveURL(/section=notifications$/)
    await expect(index.getByRole('button', { name: 'Notifications' })).toHaveAttribute('aria-current', 'true')
  })

  test('the keyboard reference lists the Work and New Task keys; accessibility follows reduced motion', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await open(page, 'settings?section=keyboard')
    const keys = page.locator('#atlas-settings-keyboard')
    for (const [action, combo] of [['Show the List', 'L'], ['Show the Map', 'M'], ['Open the selected task’s page', 'O'], ['Start the task', 'Ctrl Enter']]) {
      await expect(keys.locator('div', { hasText: action }).last()).toContainText(combo)
    }
    await expect(page.locator('#atlas-settings-accessibility')).toContainText('Reduced: your system asks for less motion')
  })

  test('notifications: turning them on asks the browser, is shared with Classic and tells the backend', async ({ page }) => {
    // The test origin is not a secure context, so the browser's permission prompt is stood in for.
    await page.addInitScript(() => {
      window.Notification = class { static permission = 'default'; static requestPermission() { window.Notification.permission = 'granted'; return Promise.resolve('granted') } }
    })
    const log = await open(page, 'settings?section=notifications')
    const toggle = page.getByRole('switch', { name: 'Control Center notifications' })
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(sent(log, 'POST', '/notifications/preferences')).toEqual([])  // Nothing sent for an untouched "off".
    await toggle.click()
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(await page.evaluate(() => localStorage.getItem('ai-agent-control-center.notifications'))).toBe('enabled')
    await expect.poll(() => sent(log, 'POST', '/notifications/preferences').map((item) => item.body)).toEqual([{ enabled: true }])
    await expect(page.locator('#atlas-settings-notifications')).toContainText('The backend has your preference.')
    await expect(page.locator('#atlas-settings-notifications')).toContainText('Windows notifications')
    await toggle.click()
    await expect.poll(() => sent(log, 'POST', '/notifications/preferences').map((item) => item.body).at(-1)).toEqual({ enabled: false })
  })

  test('notifications blocked by the browser are explained and cannot be switched on here', async ({ page }) => {
    await page.addInitScript(() => { window.Notification = class { static permission = 'denied'; static requestPermission() { return Promise.resolve('denied') } } })
    await open(page, 'settings?section=notifications')
    await expect(page.getByRole('switch', { name: 'Control Center notifications' })).toBeDisabled()
    await expect(page.locator('#atlas-settings-notifications')).toContainText('Blocked by the browser')
    await expect(page.locator('#atlas-settings-notifications')).toContainText('Windows Settings')
  })

  test('about shows process supervision and copies diagnostics without task text', async ({ page }) => {
    await page.addInitScript(() => { Object.defineProperty(navigator, 'clipboard', { value: { writeText: (text) => { window.__copied = text; return Promise.resolve() } } }) })
    await open(page, 'settings?section=about')
    const about = page.locator('#atlas-settings-about')
    await expect(about).toContainText('Supervised: every agent and setup process')
    await expect(about).not.toContainText('milestone M1')
    await about.getByRole('button', { name: 'Copy diagnostics' }).click()
    await expect(about).toContainText('Copied.')
    const text = await page.evaluate(() => window.__copied)
    expect(text).toContain('Process supervision: supervised')
    expect(text).not.toMatch(/Synthetic|Release planning/)
  })

  test('the details panel width can be reset to its default', async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem('control-center.atlas.inspector-width', '520'))
    await open(page, 'settings?section=interface')
    const row = page.locator('#atlas-settings-interface')
    await expect(row).toContainText('520 px')
    await row.getByRole('button', { name: 'Reset to default' }).click()
    await expect(row).toContainText('404 px (default)')
    expect(await page.evaluate(() => localStorage.getItem('control-center.atlas.inspector-width'))).toBe('404')
  })
})
