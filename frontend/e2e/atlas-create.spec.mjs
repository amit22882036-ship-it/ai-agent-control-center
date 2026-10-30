import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }, { project_id: 'p2', name: 'Docs site', root_path: 'C:\\Work\\docs-site' }]
const posts = (log, path) => log.requests.filter((item) => item.method !== 'GET' && (!path || item.path === path))

async function openNew(page, options = {}, hash = '#/new') {
  await page.goto('about:blank')
  const log = await openApp(page, { path: `/${hash}`, tasks: showcase(), projects: PROJECTS, ...options })
  await expect(page.locator('.atlas h1')).toHaveText('New task')
  return log
}
async function chooseProject(page, name) {
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByRole('dialog', { name: 'Choose a project' }).getByRole('button', { name: new RegExp(name) }).click()
}

test('a new task is started with the classic payload and opens in Work', async ({ page }) => {
  const log = await openNew(page, { api: ({ method, path }) => (method === 'POST' && path === '/agents/start' ? { body: { agent_id: 'quoter', status: 'running' } } : undefined) })
  const start = page.getByRole('button', { name: 'Start task' })
  await expect(start).toBeDisabled()
  await expect(page.locator('#nt-why')).toHaveText('Describe the task to start.')
  await page.getByLabel('What should be done?').fill('Fix the flaky timeout in test_exporter.py.')
  await expect(page.locator('#nt-why')).toHaveText('Choose a project to start.')
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByLabel('Find a project').fill('docs')
  await expect(page.getByRole('dialog', { name: 'Choose a project' }).getByRole('button', { name: /Release planning/ })).toHaveCount(0)
  await page.getByRole('dialog', { name: 'Choose a project' }).getByRole('button', { name: /Docs site/ }).click()
  await page.getByRole('radio', { name: /Claude Code/ }).check()
  await page.getByLabel('Model', { exact: true }).selectOption('Opus 4.8')
  await page.getByRole('radio', { name: 'High' }).check()
  await page.getByRole('radio', { name: 'Can edit' }).check()
  const helpers = page.getByRole('switch', { name: 'Let the lead bring in helpers' })
  await expect(helpers).toHaveAttribute('aria-checked', 'false') // delegation is a conscious choice
  await helpers.click()
  await expect(helpers).toHaveAttribute('aria-checked', 'true')
  await page.getByRole('radio', { name: /Also allow automatic choice/ }).check()
  await page.getByRole('checkbox', { name: /Codex/ }).check()
  await expect(page.locator('.a-summary')).toContainText('Claude Code (Opus 4.8, high effort) leads this task in a private copy of Docs site and may edit files there.')
  await expect(page.locator('.a-summary')).toContainText('may pick Claude Code or Codex, only if allowed, ready and suited')
  await start.click()
  await expect(page).toHaveURL(/#\/work\?task=quoted$/)
  expect(posts(log)).toEqual(expect.arrayContaining([expect.objectContaining({ path: '/agents/start', body: { project_id: 'p2', task: 'Fix the flaky timeout in test_exporter.py.',
    agent_type: 'claude', sandbox: 'workspace-write', autonomous: true, model: 'Opus 4.8', effort: 'high', auto_allocation_providers: ['claude', 'codex'] } })]))
  // Permissions were read again right before the request.
  const order = log.requests.map((item) => `${item.method} ${item.path}`)
  expect(order.lastIndexOf('GET /provider-policy')).toBeLessThan(order.indexOf('POST /agents/start'))
})

test('helpers are only offered for a Claude Code or Codex lead, and Mock sends no model', async ({ page }) => {
  const log = await openNew(page)
  await page.getByLabel('What should be done?').fill('Try it out')
  await chooseProject(page, 'Release planning')
  await page.getByRole('radio', { name: /Gemini CLI/ }).check()
  await expect(page.getByRole('switch', { name: 'Let the lead bring in helpers' })).toBeDisabled()
  await expect(page.getByText('Only Claude Code and Codex can lead helpers.').first()).toBeVisible()
  await page.getByRole('radio', { name: /Practice \(Mock\)/ }).check()
  await expect(page.locator('.a-summary')).toContainText('Mock simulates one agent in Release planning.')
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect.poll(() => posts(log, '/agents/start').length).toBe(1)
  expect(posts(log, '/agents/start')[0].body).toEqual({ project_id: 'p1', task: 'Try it out', agent_type: 'mock', sandbox: 'read-only', autonomous: false })
})

test('helpers start off for a Claude Code lead, so a task is started alone unless they are switched on', async ({ page }) => {
  const log = await openNew(page)
  await page.getByLabel('What should be done?').fill('Fix the parser')
  await chooseProject(page, 'Release planning')
  await page.getByRole('radio', { name: /Claude Code/ }).check()
  const helpers = page.getByRole('switch', { name: 'Let the lead bring in helpers' })
  await expect(helpers).toBeEnabled()
  await expect(helpers).toHaveAttribute('aria-checked', 'false')
  await expect(page.getByText('The lead does all the work itself. No helper creation is authorized.')).toBeVisible()
  await expect(page.getByRole('radio', { name: /Follow the lead’s choices/ })).toHaveCount(0)
  await expect(page.locator('.a-summary')).toContainText('It works alone. No helper creation is authorized for this task.')
  // Switching on shows both real policies, explicit choice first.
  await helpers.click()
  await expect(page.getByRole('radio', { name: /Follow the lead’s choices/ })).toBeChecked()
  await expect(page.getByRole('radio', { name: /Also allow automatic choice/ })).not.toBeChecked()
  await helpers.click()
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect.poll(() => posts(log, '/agents/start').length).toBe(1)
  expect(posts(log, '/agents/start')[0].body).toEqual({ project_id: 'p1', task: 'Fix the parser', agent_type: 'claude', sandbox: 'read-only', autonomous: false })
})

test('a provider that is not allowed cannot be chosen, and an unverified model needs an explicit attempt', async ({ page }) => {
  await openNew(page, { api: ({ method, path }) => {
    if (method === 'GET' && path === '/provider-policy') return { body: { allowed: ['claude'], default_provider: null } }
    if (method === 'GET' && path === '/providers/claude/models') return { body: { provider: 'claude', entitlement_verified: true, refresh: { fresh: true, last_success_at: '2026-10-29T11:00:00Z' },
      models: [{ model_id: 'Preview X', display_name: 'Preview X', selectable: false, requires_attempt: true, status: 'unverified', status_label: 'Unverified', explanation: 'Not listed for this account.' }] } }
    return undefined
  } })
  await expect(page.getByRole('radio', { name: /Codex/ })).toBeDisabled()
  await expect(page.getByText('Codex is not in your allowed providers.', { exact: false }).first()).toBeVisible()
  await page.getByLabel('What should be done?').fill('Investigate')
  await chooseProject(page, 'Release planning')
  await page.getByRole('radio', { name: /Claude Code/ }).check()
  await page.getByLabel('Model', { exact: true }).selectOption('Preview X')
  await expect(page.locator('#nt-why')).toContainText('Confirm the explicit attempt')
  await page.getByText('Try this model explicitly.', { exact: false }).click()
  await expect(page.getByRole('button', { name: 'Start task' })).toBeEnabled()
})

test('a dropped connection is reported as an unknown outcome, a double click sends once, and a retry must be explicit', async ({ page }) => {
  const log = await openNew(page, { api: ({ method, path }) => (method === 'POST' && path === '/agents/start' ? { abort: true } : undefined) })
  await page.getByLabel('What should be done?').fill('Fix it')
  await chooseProject(page, 'Release planning')
  const start = page.getByRole('button', { name: 'Start task' })
  // Two clicks in the same moment, before React re-renders the disabled state.
  await start.evaluate((button) => { button.click(); button.click() })
  await expect(page.getByRole('status').filter({ hasText: 'may or may not have started' })).toBeVisible()
  expect(posts(log, '/agents/start')).toHaveLength(1)
  await expect(page.getByLabel('What should be done?')).toHaveValue('Fix it') // the draft is kept
  await expect(start).toBeDisabled()
  await expect(page.locator('#nt-why')).toHaveText('The last start may have gone through. Look in Work first, or choose Start it anyway.')
  await page.keyboard.press('Control+Enter')
  expect(posts(log, '/agents/start')).toHaveLength(1)
  await page.getByRole('button', { name: 'Start it anyway' }).click()
  await expect.poll(() => posts(log, '/agents/start').length).toBe(2)
})

test('a backend refusal is shown, and a start that cannot be opened is reported honestly', async ({ page }) => {
  let refuse = true
  await openNew(page, { api: ({ method, path }) => {
    if (method === 'POST' && path === '/agents/start') return refuse ? { status: 409, body: { detail: 'Claude Code is usage limited. Resume after the reset.' } } : { body: { agent_id: 'unknown-agent' } }
    return undefined
  } })
  await page.getByLabel('What should be done?').fill('Fix it')
  await chooseProject(page, 'Release planning')
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect(page.getByRole('alert')).toHaveText('Claude Code is usage limited. Resume after the reset.')
  refuse = false
  await page.getByRole('button', { name: 'Start task' }).click()
  await expect(page.getByText('The task started, but it could not be opened automatically.', { exact: false })).toBeVisible()
})

test('a project is registered by its path, and backend validation errors are shown', async ({ page }) => {
  let fail = true
  const log = await openNew(page, { api: ({ method, path }) => (method === 'POST' && path === '/projects'
    ? (fail ? { status: 409, body: { detail: 'The folder is not a Git repository with a commit.' } } : { body: { project_id: 'p3', name: 'Payments' } }) : undefined) })
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByRole('button', { name: /Add a project…/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Add a project' })
  await expect(dialog).toContainText('Browsing for a folder is not available yet.')
  await dialog.getByLabel('Project name').fill('Payments')
  await dialog.getByLabel('Folder path').fill('C:\\Work\\payments')
  await dialog.getByRole('button', { name: 'Add project' }).click()
  await expect(dialog.getByRole('alert')).toHaveText('The folder is not a Git repository with a commit.')
  fail = false
  await dialog.getByRole('button', { name: 'Add project' }).click()
  await expect(dialog).toBeHidden()
  expect(posts(log, '/projects').map((item) => item.body)).toEqual([{ name: 'Payments', root_path: 'C:\\Work\\payments' }, { name: 'Payments', root_path: 'C:\\Work\\payments' }])
})

test('a registration whose outcome is unknown keeps the path, is never sent again by itself, and can be checked first', async ({ page }) => {
  let registered = false
  const log = await openNew(page, { api: ({ method, path }) => {
    if (method === 'POST' && path === '/projects') return { abort: true }
    // The second read of the list shows the folder: the backend had registered it after all.
    if (method === 'GET' && path === '/projects' && registered) return { body: { projects: [...PROJECTS, { project_id: 'p3', name: 'Payments', root_path: 'C:\\Work\\payments' }] } }
    return undefined
  } })
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByRole('button', { name: /Add a project…/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Add a project' })
  await dialog.getByLabel('Project name').fill('Payments')
  await dialog.getByLabel('Folder path').fill('C:/Work/payments/')
  await dialog.getByRole('button', { name: 'Add project' }).click()
  await expect(dialog.getByRole('status')).toContainText('it is unknown whether this folder was added')
  await expect(dialog).not.toContainText('Nothing was registered')
  await expect(dialog.getByLabel('Folder path')).toHaveValue('C:/Work/payments/')
  await expect(dialog.getByRole('button', { name: 'Add project' })).toBeDisabled()
  expect(posts(log, '/projects')).toHaveLength(1)
  registered = true
  await dialog.getByRole('button', { name: 'Check the projects list' }).click()
  await expect(dialog.getByRole('status')).toContainText('This folder is registered as Payments.')
  expect(posts(log, '/projects')).toHaveLength(1) // checking only reads
  await dialog.getByRole('button', { name: 'Use this project' }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('button', { name: /Payments/ })).toBeVisible()
})

test('after an unknown registration, a list without the folder says so and adding again stays a deliberate step', async ({ page }) => {
  const log = await openNew(page, { api: ({ method, path }) => (method === 'POST' && path === '/projects' ? { abort: true } : undefined) })
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByRole('button', { name: /Add a project…/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Add a project' })
  await dialog.getByLabel('Project name').fill('Payments')
  await dialog.getByLabel('Folder path').fill('C:\\Work\\payments')
  await dialog.getByRole('button', { name: 'Add project' }).click()
  await dialog.getByRole('button', { name: 'Check the projects list' }).click()
  await expect(dialog.getByRole('status')).toContainText('No registered project has this path, so it was not added.')
  await expect(dialog.getByRole('button', { name: 'Add project' })).toBeEnabled()
  expect(posts(log, '/projects')).toHaveLength(1)
  await dialog.getByRole('button', { name: 'Add project' }).click()
  await expect.poll(() => posts(log, '/projects').length).toBe(2)
})

test('Ctrl+Enter starts only from the New Task page, never through an open dialog, popover or the header', async ({ page }) => {
  const log = await openNew(page)
  const text = page.getByLabel('What should be done?')
  await text.fill('Fix the parser')
  await chooseProject(page, 'Release planning')
  // The project popover is open: the shortcut does nothing.
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByLabel('Find a project').press('Control+Enter')
  await page.keyboard.press('Escape')
  // The registration dialog is open, focus in its path field: nothing is started.
  await page.getByRole('button', { name: /Project/ }).first().click()
  await page.getByRole('button', { name: /Add a project…/ }).click()
  const dialog = page.getByRole('dialog', { name: 'Add a project' })
  await dialog.getByLabel('Folder path').fill('C:\\Work\\x')
  await dialog.getByLabel('Folder path').press('Control+Enter')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  await expect(dialog).toBeHidden()
  // Focus in the app header: nothing is started.
  await page.getByRole('button', { name: 'Classic view' }).focus()
  await page.keyboard.press('Control+Enter')
  await page.waitForTimeout(300)
  expect(posts(log, '/agents/start')).toHaveLength(0)
  expect(posts(log, '/projects')).toHaveLength(0)
  // From the description itself it starts, once, even when pressed twice quickly.
  await text.focus()
  await page.keyboard.press('Control+Enter')
  await page.keyboard.press('Control+Enter')
  await expect.poll(() => posts(log, '/agents/start').length).toBe(1)
  await page.waitForTimeout(300)
  expect(posts(log, '/agents/start')).toHaveLength(1)
})

test('Projects lists, searches and opens registered projects', async ({ page }) => {
  await page.goto('about:blank')
  await openApp(page, { path: '/#/projects', tasks: showcase(), projects: PROJECTS })
  await expect(page.locator('.atlas h1')).toHaveText('Projects')
  await expect(page.locator('.a-pcard h2')).toHaveText(['Docs site', 'Release planning', 'Add a project'])
  await page.getByLabel('Find a project').fill('release')
  await expect(page.locator('.a-pcard h2')).toHaveText(['Release planning', 'Add a project'])
  await page.getByRole('button', { name: 'Show its work' }).click()
  await expect(page).toHaveURL(/#\/work\?project=p1$/)
  await page.goBack()
  await page.getByLabel('Find a project').fill('')
  await page.getByRole('button', { name: 'New task here' }).first().click()
  await expect(page).toHaveURL(/#\/new\?project=p2$/)
  await expect(page.getByRole('button', { name: /Docs site/ })).toBeVisible()
})

test('New task fits 1440, 1180 and 1024 without overflow, keeps keyboard access, and has no serious accessibility violations', async ({ page }) => {
  for (const [width, theme] of [[1440, 'light'], [1180, 'dark'], [1024, 'light']]) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await openNew(page)
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
    expect(overflow).toBe(0)
    // The "Before you start" picture keeps its size at every width (a shared class once collapsed it).
    expect(await page.locator('.a-ntpic-svg').evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(40)
    await page.getByLabel('What should be done?').fill('לבדוק את הייבוא (CSV)')
    expect(await page.getByLabel('What should be done?').evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
    await page.getByRole('radio', { name: /Codex/ }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page.getByRole('radio', { name: /Gemini CLI/ })).toBeChecked()
    await page.addScriptTag({ path: axePath })
    const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
    const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
    console.log(`axe new task ${width} ${theme}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
    expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    await page.unroute('**/*')
  }
})
