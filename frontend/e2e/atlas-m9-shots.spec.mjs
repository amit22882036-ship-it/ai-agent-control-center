import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const UPDATES = [
  { id: 'update:claude:2.2.0', provider: 'claude', kind: 'update', label: 'Claude Code', installed_version: '2.1.300', available_version: '2.2.0',
    channel: 'latest', manual: false, checked_at: '2026-10-29T11:00:00Z', stale: false },
  { id: 'update:gemini:0.42.0', provider: 'gemini', kind: 'update', label: 'Gemini CLI', installed_version: '0.41.2', available_version: '0.42.0',
    channel: 'stable', manual: false, checked_at: '2026-10-28T09:00:00Z', stale: true },
]

test('capture M9 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(300_000)
  const shot = async (name, hash, theme, { width = 1440, height = 900, notices = UPDATES, backend = 'online', prepare = null } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    // The test origin is not a secure context; the browser's notification permission is stood in for
    // (allowed, with the preference on), as it is on the real dashboard address.
    await page.addInitScript(() => {
      window.Notification = class { static permission = 'granted'; static requestPermission() { return Promise.resolve('granted') } close() {} }
      localStorage.setItem('ai-agent-control-center.notifications', 'enabled')
      Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: (name, options, callback) => Promise.resolve(callback({ name })) } })
    })
    await openApp(page, { path: `/#/${hash}`, tasks: showcase(), notices, backend })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(700)
    if (prepare) await prepare()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  const openBell = async () => { await page.locator('.a-bell').click(); await expect(page.getByRole('dialog', { name: 'Notifications' })).toBeVisible() }
  const reply = async () => {
    await page.getByRole('treeitem', { name: /^Handle quoted fields/ }).hover()
    await page.locator('.a-row-reply').click()
    await page.getByRole('complementary', { name: 'Task details' }).getByLabel('Your answer').fill('Keep them, and escape quotes inside quoted fields.')
  }
  await shot('m9-01-bell-panel-light', 'work', 'light', { prepare: openBell })
  await shot('m9-02-provider-updates-light', 'work', 'light', { prepare: async () => {
    await openBell()
    await page.getByRole('region', { name: 'Tool updates' }).scrollIntoViewIfNeeded()
  } })
  await shot('m9-03-attention-navigation-light', 'work?task=quoted', 'light', { prepare: async () => {
    await page.getByRole('navigation', { name: 'Tasks that need you' }).getByRole('button', { name: 'Next task that needs you' }).click()
  } })
  await shot('m9-04-task-needs-reply-light', 'work?task=quoted', 'light', { prepare: async () => {
    await page.getByRole('treeitem', { name: /^Handle quoted fields/ }).hover()
  } })
  await shot('m9-05-quick-reply-light', 'work', 'light', { prepare: reply })
  await shot('m9-06-backend-unavailable-light', 'work', 'light', { backend: 'offline', prepare: openBell })
  await shot('m9-07-map-attention-light', 'work?lens=map&task=quoted', 'light')
  await shot('m9-08-bell-panel-dark', 'work?task=quoted', 'dark', { prepare: openBell })
  await shot('m9-08b-quick-reply-dark', 'work', 'dark', { prepare: reply })
  await shot('m9-09-narrow-light', 'work?task=quoted', 'light', { width: 1024, height: 768, prepare: openBell })
  await shot('m9-09b-narrow-reply-light', 'work', 'light', { width: 1024, height: 768, prepare: reply })
})
