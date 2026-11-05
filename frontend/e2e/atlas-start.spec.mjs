import { test, expect } from '@playwright/test'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
// Mutations only: the provider readiness check is a POST but changes nothing.
const posts = (log, path) => log.requests.filter((item) => item.method !== 'GET' && item.path !== '/providers/check' && (!path || item.path === path))
const row = (page, title) => page.getByRole('treeitem', { name: new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) })

async function openTask(page, taskId, options = {}, hash = `?task=${taskId}`) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work${hash}`, tasks: showcase(), ...options })
  return log
}

test('a helper is added under the current agent with the classic child payload', async ({ page }) => {
  const log = await openTask(page, 'report')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await inspector.getByRole('button', { name: 'Add a helper…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Add a helper to Report builder' })
  await expect(dialog).toContainText('Autonomous helpers are not started from here.')
  const submit = dialog.getByRole('button', { name: 'Start helper' })
  await expect(submit).toBeDisabled()
  await dialog.getByLabel('What should the helper do?').fill('Add a totals row to the summary')
  await dialog.getByRole('radio', { name: /Codex/ }).check()
  await dialog.getByLabel('Model', { exact: true }).selectOption('GPT-5.5')
  await dialog.getByRole('radio', { name: 'Workspace', exact: true }).check()
  await submit.evaluate((button) => { button.click(); button.click() })
  await expect(dialog).toBeHidden()
  await expect(inspector.getByText('Helper started. Its task appears under this one.')).toBeVisible()
  expect(posts(log).map((item) => [item.path, item.body])).toEqual([['/agents/rep-2/children/start',
    { task: 'Add a totals row to the summary', agent_type: 'codex', sandbox: 'workspace-write', autonomous: false, model: 'GPT-5.5' }]])
})

test('a helper start that is refused or loses its connection is said plainly', async ({ page }) => {
  let reply = { status: 409, body: { detail: 'Codex is not ready on this machine.' } }
  const log = await openTask(page, 'report', { api: ({ method, path }) => (method === 'POST' && path === '/agents/rep-2/children/start' ? reply : undefined) })
  await page.getByRole('complementary', { name: 'Task details' }).getByRole('button', { name: 'Add a helper…' }).click()
  const dialog = page.getByRole('dialog', { name: /Add a helper/ })
  await dialog.getByLabel('What should the helper do?').fill('Check the totals')
  await dialog.getByRole('button', { name: 'Start helper' }).click()
  await expect(dialog.getByRole('alert')).toHaveText('Codex is not ready on this machine.')
  reply = { abort: true }
  await dialog.getByRole('button', { name: 'Start helper' }).click()
  await expect(dialog.getByRole('status').filter({ hasText: 'may or may not have started' })).toBeVisible()
  expect(posts(log, '/agents/rep-2/children/start')).toHaveLength(2)
})

test('a task with nobody on it gets a new agent assignment on the same task, not a new task', async ({ page }) => {
  const log = await openTask(page, 'backlog')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector.getByRole('heading', { name: 'Nobody is working on this task' })).toBeVisible()
  await inspector.getByRole('button', { name: 'Start a new agent…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Start a new agent on this task' })
  await expect(dialog).toContainText('It does not resume the earlier agent or its provider session, and it does not create another task.')
  await expect(dialog.getByLabel('What should the helper do?')).toHaveCount(0)
  await dialog.getByRole('radio', { name: /Claude Code/ }).check()
  await dialog.getByLabel('Model', { exact: true }).selectOption('Opus 4.8')
  await dialog.getByRole('radiogroup', { name: 'Reasoning effort' }).getByText('High', { exact: true }).click()
  await expect(dialog.getByRole('radio', { name: 'High' })).toBeChecked()
  await dialog.getByRole('button', { name: 'Start new agent' }).click()
  await expect(inspector.getByText('A new agent started on this task.')).toBeVisible()
  expect(posts(log).map((item) => [item.path, item.body])).toEqual([['/tasks/backlog/start-agent',
    { agent_type: 'claude', sandbox: 'read-only', model: 'Opus 4.8', effort: 'high' }]])
  // Permissions were read again right before the request.
  const order = log.requests.map((item) => `${item.method} ${item.path}`)
  expect(order.lastIndexOf('GET /provider-policy')).toBeLessThan(order.indexOf('POST /tasks/backlog/start-agent'))
})

test('starting again is not offered where it would be unsafe or meaningless', async ({ page }) => {
  // migrate: its agent was removed, so it needs a new owner through the transfer; cli: completed;
  // quoted: has a current agent (helpers only).
  for (const taskId of ['migrate', 'cli', 'quoted']) {
    await openTask(page, taskId)
    const inspector = page.getByRole('complementary', { name: 'Task details' })
    await expect(inspector).toBeVisible()
    await expect(inspector.getByRole('button', { name: 'Start a new agent…' })).toHaveCount(0)
    await page.unroute('**/*')
  }
})

test('the colour filter narrows to several colours, covers tasks without a colour, and lives in the address', async ({ page }) => {
  await openTask(page, null, {}, '')
  await expect(row(page, 'Build the CSV import pipeline')).toBeVisible()
  const button = page.getByRole('button', { name: /Colour · Any/ })
  await button.click()
  const pop = page.getByRole('dialog', { name: 'Filter by agent colour' })
  await pop.getByRole('checkbox', { name: 'Green' }).check()
  await expect(page).toHaveURL(/colors=green/)
  await expect(row(page, 'Document the CLI flags')).toBeVisible()
  await expect(row(page, 'לתרגם את מסך ההגדרות')).toBeVisible()
  await expect(row(page, 'Build the CSV import pipeline')).toBeVisible() // an ancestor of a match keeps its place
  await expect(row(page, 'Summarize last week')).toHaveCount(0)
  await expect(row(page, 'Profile the slow dashboard load')).toHaveCount(0)
  await pop.getByRole('checkbox', { name: 'No colour' }).check()
  await expect(page).toHaveURL(/colors=green(%2C|,)none/)
  await expect(row(page, 'Profile the slow dashboard load')).toBeVisible() // no agent
  await expect(row(page, 'Migrate the settings database')).toBeVisible() // its agent was removed
  await page.keyboard.press('Escape')
  await expect(pop).toBeHidden()
  await page.reload()
  await expect(page.getByRole('button', { name: /Colours · 2/ })).toBeVisible()
  await expect(row(page, 'Summarize last week')).toHaveCount(0)
  await page.getByRole('button', { name: /Colours · 2/ }).click()
  await page.getByRole('dialog', { name: 'Filter by agent colour' }).getByRole('button', { name: 'Any colour' }).click()
  await expect(page).not.toHaveURL(/colors=/)
  await expect(row(page, 'Summarize last week')).toBeVisible()
})
