import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { DEFAULT_ONBOARDING, meter, onboarding, plan } from './providers.mjs'

const NOW = new Date('2026-10-30T09:05:00.000Z')
// A morning with something to look at: Codex has hit a limit, Gemini CLI is allowed but not signed in.
const MIXED = onboarding({
  claude: { windows: [meter('Session limit', 42), meter('Weekly limit (all models)', 18, { reset_at: '2026-11-03T00:00:00Z' })] },
  codex: { state: 'usage_limited', reason: 'Codex reported a usage limit.', blocks: [{ block_id: 'b1', model: null, condition: 'usage_limited',
    reason: 'Codex reported a usage limit.', reset_at: '2026-10-30T13:00:00Z', reset_source: 'provider', observed_at: '2026-10-30T08:40:00Z', window_id: null }] },
  gemini: { state: 'not_authenticated', authenticated: false, enabled: true, reason: 'Gemini CLI is not signed in.' },
})

test('capture M8 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(300_000)
  const shot = async (name, hash, theme, { width = 1440, height = 900, providers = MIXED, api = null, prepare = null, full = false, init = null } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    if (init) await page.addInitScript(init)
    await openApp(page, { path: `/#/${hash}`, providers, api })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(700)
    if (prepare) await prepare()
    await page.waitForTimeout(500)
    if (full) {
      // The page scrolls inside the main area, so the viewport grows to show all of it.
      const tall = await page.evaluate(() => document.querySelector('.a-main').scrollHeight + document.querySelector('.a-topbar').offsetHeight)
      await page.setViewportSize({ width, height: Math.max(height, Math.min(tall, 3200)) })
      await page.waitForTimeout(300)
    }
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  await shot('m8-01-connections-overview-light', 'connections', 'light', { full: true })
  await shot('m8-02-claude-detail-light', 'connections?provider=claude', 'light', { full: true, providers: DEFAULT_ONBOARDING })
  await shot('m8-03-codex-detail-light', 'connections?provider=codex', 'light', { full: true })
  await shot('m8-04-gemini-detail-light', 'connections?provider=gemini', 'light', { full: true })
  await shot('m8-05-setup-review-light', 'connections?provider=claude', 'light', { providers: DEFAULT_ONBOARDING,
    api: ({ method, path }) => (method === 'POST' && path === '/providers/claude/operations/plan' ? { body: plan('claude', 'update') } : undefined),
    prepare: async () => { await page.getByRole('button', { name: 'Update…' }).click(); await page.getByRole('group', { name: /review/ }).scrollIntoViewIfNeeded() } })
  await shot('m8-06-usage-light', 'connections?provider=claude&section=usage', 'light', { providers: DEFAULT_ONBOARDING })
  await shot('m8-07-billing-guidance-light', 'connections?provider=codex&section=billing', 'light', { providers: onboarding({ codex: { confirmed: false, state: 'billing_blocked', reason: 'The billing acknowledgment is required before new executions.' } }) })
  await shot('m8-08-unavailable-light', 'connections', 'light', { providers: null })
  await shot('m8-08b-uncertain-operation-light', 'connections?provider=claude', 'light', { providers: onboarding({ claude: { operations: [{ operation_id: 'op1',
    provider: 'claude', action: 'update', status: 'uncertain', result: 'The installer did not report that it ended.', started_at: '2026-10-30T08:30:00Z', ended_at: null }] } }) })
  await shot('m8-09-settings-light', 'settings', 'light', { full: true })
  await shot('m8-10-appearance-dark', 'settings?section=appearance', 'dark')
  // The test origin is not a secure context; the browser's permission is stood in for, as a person would see it before deciding.
  await shot('m8-11-notifications-light', 'settings?section=notifications', 'light', {
    init: () => { window.Notification = class { static permission = 'default'; static requestPermission() { return Promise.resolve('default') } } } })
  await shot('m8-12-connections-overview-dark', 'connections', 'dark', { full: true })
  await shot('m8-12b-claude-detail-dark', 'connections?provider=claude', 'dark', { full: true, providers: DEFAULT_ONBOARDING })
  await shot('m8-13-narrow-overview-light', 'connections', 'light', { width: 1024, height: 768, full: true })
  await shot('m8-13b-narrow-detail-light', 'connections?provider=codex', 'light', { width: 1024, height: 768, full: true })
})
