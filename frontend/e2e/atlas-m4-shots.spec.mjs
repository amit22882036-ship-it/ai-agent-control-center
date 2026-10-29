import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const PREVIEW = { mode: 'single', agent: { status: 'running' }, members: [{ agent_id: 'rep-2', status: 'running', unresolved_executions: 0 }],
  descendants: [], options: { single: { available: true, reasons: [] } }, plan_token: 't',
  consequences: ['stop_running_worker', 'task_awaits_responsibility_transfer', 'identity_retired_permanently'] }

test('capture M4 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(180_000)
  const shot = async (name, taskId, theme, prepare, options = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width: options.width || 1440, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/work?task=${taskId}`, tasks: showcase(), ...options })
    const inspector = page.getByRole('complementary', { name: 'Task details' })
    await expect(inspector).toBeVisible()
    await page.waitForTimeout(400)
    if (prepare) await prepare(inspector)
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  await shot('m4-reply-light', 'quoted', 'light', async (inspector) => inspector.getByLabel('Your answer').fill('Keep the line breaks and escape them inside quotes.'))
  await shot('m4-reply-override-dark', 'quoted', 'dark', async (inspector) => {
    await inspector.getByText('Use a different model or effort for this instruction only').click()
  })
  await shot('m4-provider-wait-light', 'limits', 'light', null, { agents: { logs: {
    provider_condition: { kind: 'usage_limited', label: 'Waiting for provider usage', provider: 'claude', blocking: [{ label: '5-hour limit', reset_at: '2026-10-29T14:30:00Z' }] },
    resume: { available: true, mode: 'explicit_retry', reason: 'The provider reported a limit; retrying may hit it again.', blocking: [] } } } })
  await shot('m4-stop-confirm-light', 'report', 'light', async (inspector) => inspector.getByRole('button', { name: 'Stop agent' }).click())
  await shot('m4-settings-light', 'report', 'light', async (inspector) => inspector.getByRole('tab', { name: 'Settings' }).click())
  await shot('m4-settings-dark', 'csv', 'dark', async (inspector) => {
    await inspector.getByRole('tab', { name: 'Settings' }).click()
    await inspector.getByText('Change this agent’s defaults').click()
  })
  await shot('m4-removal-preview-light', 'report', 'light', async (inspector) => {
    await inspector.getByRole('tab', { name: 'Settings' }).click()
    await inspector.getByRole('button', { name: 'Review removal…' }).click()
    await inspector.page().getByRole('dialog').getByRole('button', { name: 'Review impact' }).click()
  }, { api: ({ method, path }) => (method === 'POST' && path === '/agents/rep-2/removal-preview' ? { body: PREVIEW } : undefined) })
  await shot('m4-new-owner-light', 'migrate', 'light', async (inspector) => inspector.getByText('Choose a new owner', { exact: true }).click(),
    { api: ({ method, path }) => (method === 'GET' && path === '/tasks/migrate/responsibility'
      ? { body: { awaiting_transfer: true, transferable: true, task_status: 'in_progress', removed_owner: { assignment_id: 'as-mig' }, reasons: [], transfers: [] } } : undefined) })
  await shot('m4-narrow-1024-dark', 'quoted', 'dark', null, { width: 1024 })
})
