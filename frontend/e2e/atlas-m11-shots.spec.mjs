import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'
import { DEFAULT_ONBOARDING, plan } from './providers.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')

// Key steps of the user journeys reviewed at M11, with synthetic data only.
test('capture M11 journey screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(300_000)
  const shot = async (name, hash, { api = null, prepare = null, providers = DEFAULT_ONBOARDING } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: hash === null ? '/' : `/#/${hash}`, tasks: showcase(), api, providers })
    await page.waitForTimeout(700)
    if (prepare) await prepare()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  await shot('m11-01-classic-default', null, { prepare: async () => { await expect(page.getByRole('heading', { level: 1, name: 'Agent Workspace' })).toBeVisible() } })
  await shot('m11-02-new-task-configured', 'new', { prepare: async () => {
    await page.getByLabel('What should be done?').fill('Make CSV export keep quoted line breaks, with a regression test.')
    await page.getByRole('radio', { name: /Claude Code/ }).check()
    await page.getByRole('switch', { name: 'Let the lead bring in helpers' }).click()
  } })
  await shot('m11-03-reply-in-work', 'work?task=quoted', { prepare: async () => {
    await page.getByRole('complementary', { name: 'Task details' }).getByLabel('Your answer').fill('Keep them, and escape quotes inside quoted fields.')
  } })
  await shot('m11-04-pause-impact', 'task?id=csv', { prepare: async () => {
    await page.getByRole('button', { name: 'Pause…' }).click()
    await expect(page.getByRole('dialog', { name: 'Pause this task' })).toBeVisible()
  } })
  await shot('m11-05-results', 'task?id=cli', { prepare: async () => { await page.getByRole('heading', { name: 'Result' }).scrollIntoViewIfNeeded() } })
  await shot('m11-06-provider-update-review', 'connections?provider=claude', {
    api: ({ method, path }) => (method === 'POST' && path === '/providers/claude/operations/plan' ? { body: plan('claude', 'update') } : undefined),
    prepare: async () => { await page.getByRole('button', { name: 'Update…' }).click(); await page.getByRole('group', { name: /review/ }).scrollIntoViewIfNeeded() } })
})
