import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { activeEventSources, announceChange, contract, openApp } from './harness.mjs'
import { large, node, showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const row = (page, title) => page.getByRole('treeitem', { name: new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) })
const UNSAFE = /^(POST|PUT|PATCH|DELETE) /

async function openWork(page, options = {}, hash = '') {
  // A fresh document each time: changing only the hash would keep the previous page running.
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work${hash}`, ...options })
  await expect(page.locator('[role="tree"]')).toBeVisible()
  return log
}

test('the synthetic scenarios use exactly the endpoint’s node shape', () => {
  const fields = Object.keys(contract.all.tasks[0]).sort()
  for (const item of [...showcase().tasks, ...large({ families: 2, width: 3, depth: 2 }).tasks]) expect(Object.keys(item).sort()).toEqual(fields)
  expect(Object.keys(showcase()).sort()).toEqual(Object.keys(contract.all).sort())
})

test('root and child tasks render as one tree from parent_task_id, with sections and guides', async ({ page }) => {
  await openWork(page, { tasks: showcase() })
  await expect(page.locator('.a-sheet-sec')).toHaveText([/Needs you/, /Open/, /History/])
  await expect(row(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-level', '1')
  await expect(row(page, 'Handle quoted fields')).toHaveAttribute('aria-level', '2')
  await expect(row(page, 'Document the CLI flags')).toHaveAttribute('aria-level', '3')
  // A family that only finished stays collapsed under history; the person's question surfaces the whole family.
  await expect(row(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-expanded', 'true')
  for (const title of ['Handle quoted fields', 'JSON command-line interface', 'Document the CLI flags', 'HTML summary report']) await expect(row(page, title)).toBeVisible()
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Waiting for 2 helpers')
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('1 more planned, not started')
  // Planned work is never drawn as a task.
  expect(await page.locator('[role="treeitem"]').count()).toBe(10)
})

test('branches expand and collapse with the mouse and keep the person’s choice across refreshes', async ({ page }) => {
  await openWork(page, { tasks: showcase() })
  const lead = row(page, 'Build the CSV import pipeline')
  await lead.locator('.a-fold').click()
  await expect(lead).toHaveAttribute('aria-expanded', 'false')
  await expect(row(page, 'Handle quoted fields')).toHaveCount(0)
  await expect(lead.locator('.a-attn-dot')).toBeVisible() // the hidden question is still signalled
  await announceChange(page)
  await page.waitForTimeout(300)
  await expect(lead).toHaveAttribute('aria-expanded', 'false')
  await lead.locator('.a-fold').click()
  await expect(row(page, 'Handle quoted fields')).toBeVisible()
})

test('current, last, replaced, removed and missing agents are shown as what they are', async ({ page }) => {
  await openWork(page, { tasks: showcase() })
  await expect(row(page, 'HTML summary report').locator('.a-c-who')).toContainText('Report builder')
  await expect(row(page, 'HTML summary report').locator('.a-c-who')).toContainText('replaced')
  const finished = row(page, 'JSON command-line interface')
  await expect(finished.locator('.a-c-who')).toContainText('Last agent')
  await expect(row(page, 'Migrate the settings database')).toContainText('Waiting for a new owner')
  await expect(row(page, 'Migrate the settings database').locator('.a-avatar--removed')).toHaveCount(1)
  await expect(row(page, 'Profile the slow dashboard load').locator('.a-c-who')).toContainText('No agent')
  await expect(row(page, 'Rename the settings page').locator('.a-c-who')).toContainText('Last agent')
})

test('selecting a task opens the read-only inspector and keeps the task in the address', async ({ page }) => {
  const log = await openWork(page, { tasks: showcase() })
  await row(page, 'Handle quoted fields').click()
  await expect(page).toHaveURL(/#\/work\?task=quoted$/)
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('Quoted fields')
  await expect(inspector).toContainText('Should quoted fields keep their line breaks?')
  await expect(inspector.getByRole('button', { name: 'Send reply' })).toBeVisible()
  await expect(inspector.getByRole('button', { name: 'Open in Classic' })).toHaveCount(0)
  await row(page, 'HTML summary report').click()
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('Report builder')
  await inspector.getByRole('tab', { name: 'Activity' }).click()
  await expect(inspector).toContainText('Agents on this task')
  await page.goBack()
  await expect(page).toHaveURL(/task=quoted$/)
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('Quoted fields')
  await page.goForward()
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('Report builder')
  await inspector.getByRole('button', { name: 'Close Task details' }).click()
  await expect(page).toHaveURL(/#\/work$/)
  expect(log.backend.filter((line) => UNSAFE.test(line) && !line.startsWith('POST /notifications/'))).toEqual([])
  expect(await activeEventSources(page)).toBe(1)
})

test('a task without an agent and a task whose agent was removed are not shown as active', async ({ page }) => {
  await openWork(page, { tasks: showcase() }, '?task=migrate')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector).toContainText('This agent was removed')
  await expect(inspector).toContainText('Waiting for a new owner')
  await page.goto('/#/work?task=backlog')
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('No agent')
  await inspector.getByRole('tab', { name: 'Output' }).click()
  await expect(inspector).toContainText('No agent has worked on this task')
})

test('keyboard: one Tab stop, arrows move, Right/Left open and close, Enter selects', async ({ page }) => {
  await openWork(page, { tasks: showcase() })
  const first = page.locator('[role="treeitem"]').first()
  await first.focus()
  await expect(first).toBeFocused()
  expect(await page.locator('[role="treeitem"][tabindex="0"]').count()).toBe(1)
  await page.keyboard.press('ArrowDown')
  await expect(row(page, 'Handle quoted fields')).toBeFocused()
  await page.keyboard.press('ArrowLeft')
  await expect(row(page, 'Build the CSV import pipeline')).toBeFocused()
  await page.keyboard.press('ArrowLeft')
  await expect(row(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-expanded', 'false')
  await page.keyboard.press('ArrowRight')
  await expect(row(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-expanded', 'true')
  await page.keyboard.press('End')
  await expect(page.locator('[role="treeitem"]').last()).toBeFocused()
  await page.keyboard.press('Home')
  await page.keyboard.press('ArrowDown')
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/task=quoted$/)
  const outline = await row(page, 'Handle quoted fields').evaluate((element) => getComputedStyle(element).boxShadow)
  expect(outline).not.toBe('none')
})

test('filters, project scope and search narrow the list without inventing structure', async ({ page }) => {
  await openWork(page, { tasks: showcase() })
  const now = page.getByRole('group', { name: 'What is happening' })
  await now.getByRole('button', { name: /Need you$/ }).click()
  await expect(page).toHaveURL(/filter=needs/)
  await expect(page.locator('.a-sheet-sec')).toHaveText([/Needs you/])
  // A task scope keeps the tasks that need you and the path to them, not their whole families.
  await expect(row(page, 'Handle quoted fields')).toBeVisible()
  await expect(row(page, 'HTML summary report')).toHaveCount(0)
  await expect(row(page, 'Summarize last week')).toHaveCount(0)
  await now.getByRole('button', { name: /Done$/ }).click()
  await expect(row(page, 'Rename the settings page')).toBeVisible()
  await now.getByRole('button', { name: /Task families$/ }).click()
  await expect(page).not.toHaveURL(/filter=/)
  await page.getByRole('searchbox', { name: 'Find in loaded tasks' }).fill('flags')
  await expect(page.locator('[role="treeitem"]')).toHaveCount(3) // the match and its two loaded ancestors
  await page.getByRole('searchbox', { name: 'Find in loaded tasks' }).fill('nothing like this')
  await expect(page.getByText('Nothing matches.')).toBeVisible()
  await page.getByRole('button', { name: 'Clear filters' }).click()
  await page.getByRole('button', { name: 'All projects' }).click()
  await page.getByRole('menuitem', { name: 'Release planning' }).click()
  await expect(page).toHaveURL(/project=p1/)
  expect(await page.locator('[role="treeitem"]').count()).toBeGreaterThan(0)
})

test('a selected task hidden by a filter stays selected and can be revealed', async ({ page }) => {
  await openWork(page, { tasks: showcase() }, '?task=hebrew&filter=needs')
  await expect(page.getByText('The selected task is hidden by the current view')).toBeVisible()
  await page.getByRole('button', { name: 'Show it' }).click()
  await expect(row(page, 'לתרגם את מסך ההגדרות')).toHaveAttribute('aria-selected', 'true')
  expect(await row(page, 'לתרגם את מסך ההגדרות').locator('.a-t').evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
})

test('pages load on request, parents on other pages are placed, and missing parents are disclosed', async ({ page }) => {
  const pages = contract.paged
  const log = await openWork(page, { tasks: contract.all, taskPages: (url) => {
    const after = url.searchParams.get('after')
    if (!after) return pages[0]
    const index = pages.findIndex((item) => item.next_after === after)
    return pages[index + 1]
  } })
  await expect(page.getByRole('button', { name: 'Load older tasks' })).toBeVisible()
  await expect(page.getByText(/Showing 3 of 12 tasks/)).toBeVisible()
  for (let i = 1; i < pages.length; i += 1) await page.getByRole('button', { name: 'Load older tasks' }).click()
  await expect(page.getByRole('button', { name: 'Load older tasks' })).toHaveCount(0)
  await expect(row(page, 'Stray Task')).toContainText('Its lead no longer exists')
  await expect(row(page, 'Loop A')).toContainText('Its recorded hierarchy is inconsistent')
  // The tree is complete: the flaky test sits under its parent, which sits under the lead.
  await expect(row(page, 'Investigate the flaky test')).toHaveAttribute('aria-level', '3')
  expect(log.backend.filter((line) => line.startsWith('GET /workspace/tasks')).every((line) => line.includes('limit=200'))).toBe(true)
})

test('a selected task that isn’t loaded or no longer exists is explained, never replaced', async ({ page }) => {
  await openWork(page, { tasks: { ...contract.paged[0] } }, '?task=solo')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector).toContainText('This task isn’t in the loaded pages yet')
  await page.goto('about:blank')
  await page.goto('/#/work?task=never-existed')
  await expect(page.locator('[role="tree"]')).toBeVisible()
  await expect(inspector).toContainText('This task isn’t in the loaded pages yet')
  await page.unroute('**/*')
  await openWork(page, { tasks: contract.all }, '?task=never-existed')
  await expect(inspector).toContainText('This task no longer exists')
  expect(await page.locator('[role="treeitem"][aria-selected="true"]').count()).toBe(0)
})

test('live changes refresh the list once per burst, reads never overlap, and the newest state wins', async ({ page }) => {
  let changed = false
  let calls = 0
  let running = 0
  let overlapped = false
  const log = await openWork(page, { tasks: showcase(), taskPages: async () => {
    calls += 1
    running += 1
    if (running > 1) overlapped = true
    const data = showcase()
    if (changed) {
      const quoted = data.tasks.find((item) => item.task_id === 'quoted')
      Object.assign(quoted, { status: 'in_progress', waiting_context: null, agent: { ...quoted.agent, status: 'running', asks_user: false } })
    }
    await new Promise((resolve) => setTimeout(resolve, 150))
    running -= 1
    return data
  } })
  await expect(row(page, 'Handle quoted fields')).toContainText('Asked you a question')
  await page.waitForTimeout(400)
  const before = calls
  changed = true
  for (let i = 0; i < 6; i += 1) { await announceChange(page); await page.waitForTimeout(30) }
  await expect(row(page, 'Handle quoted fields')).toContainText('Working')
  await page.waitForTimeout(600)
  expect(calls - before).toBeLessThanOrEqual(3)
  expect(overlapped).toBe(false)
  expect(await activeEventSources(page)).toBe(1)
  expect(log.external).toEqual([])
})

test('an empty workspace and a backend that stops answering are explained plainly', async ({ page }) => {
  await page.clock.setFixedTime(NOW)
  await openApp(page, { path: '/#/work', tasks: { tasks: [], ancestors: [], ancestors_truncated: false, next_after: null, total: 0, root: null } })
  // A new installation is welcomed with the three first steps; the projects are already there.
  await expect(page.getByRole('heading', { name: 'Welcome to the Control Center' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Open Connections' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add another' })).toBeVisible()
  await expect(page.locator('.a-welcome').getByRole('button', { name: 'New task' })).toBeEnabled()
  await page.unroute('**/*')
  let fail = false
  await openWork(page, { tasks: showcase(), taskPages: () => (fail ? null : showcase()) })
  fail = true
  await announceChange(page)
  await expect(page.getByText('Showing the last list received')).toBeVisible()
  await expect(row(page, 'Build the CSV import pipeline')).toBeVisible()
  fail = false
  await announceChange(page)
  await expect(page.getByText('Showing the last list received')).toHaveCount(0)
})

test('long names, deep hierarchies and wide sibling groups stay inside the layout', async ({ page }) => {
  const data = showcase()
  const lead = data.tasks.find((item) => item.task_id === 'csv')
  lead.title = 'A very long task name '.repeat(12)
  lead.agent.display_name = 'An agent with a remarkably long display name that keeps going'
  let parent = 'backlog'
  for (let i = 0; i < 60; i += 1) { data.tasks.push(node(`deep-${i}`, parent, { title: `Level ${i + 1}` })); parent = `deep-${i}` }
  for (let i = 0; i < 300; i += 1) data.tasks.push(node(`wide-${i}`, 'limits', { title: `Sibling ${i}` }))
  data.total = data.tasks.length
  for (const width of [1440, 1024]) {
    await page.setViewportSize({ width, height: 860 })
    await openWork(page, { tasks: data }, '?task=deep-59')
    await expect(row(page, 'Level 60')).toHaveAttribute('aria-level', '61')
    // Deep levels keep their title in view: indentation stops growing after a few levels.
    const title = await row(page, 'Level 60').locator('.a-t').boundingBox()
    expect(title.width).toBeGreaterThan(40)
    const overflow = await page.evaluate(() => ({ page: document.documentElement.scrollWidth - window.innerWidth,
      outline: document.querySelector('.a-outline').scrollWidth - document.querySelector('.a-outline').clientWidth }))
    expect(overflow).toEqual({ page: 0, outline: 0 })
    await page.unroute('**/*')
  }
})

test('1,000 tasks render and respond within measured bounds, drawing only what is on screen', async ({ page }) => {
  test.setTimeout(60_000)
  const data = large({ families: 50, width: 19 })
  expect(data.tasks.length).toBe(1000)
  await page.clock.setFixedTime(NOW)
  const started = Date.now()
  await openApp(page, { path: '/#/work', tasks: data })
  await expect(page.locator('[role="treeitem"]').first()).toBeVisible({ timeout: 20_000 })
  const firstRender = Date.now() - started
  // A busy workspace starts as one line per family, and only rows near the view are in the page.
  const drawn = await page.locator('[role="treeitem"]').count()
  expect(drawn).toBeGreaterThan(5)
  expect(drawn).toBeLessThan(80)
  await expect(page.locator('[role="treeitem"]').first()).toHaveAttribute('aria-expanded', 'false')
  // The last family is reached by scrolling.
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = sheet.scrollHeight })
  await expect(row(page, 'Family 0 lead task')).toBeVisible()
  const metrics = await page.evaluate(async () => {
    const time = async (action) => {
      const start = performance.now()
      action()
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      return Math.round(performance.now() - start)
    }
    const rows = document.querySelectorAll('[role="treeitem"]')
    rows[0].focus()
    const keyStep = await time(() => rows[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true })))
    const select = await time(() => rows[Math.min(12, rows.length - 1)].click())
    const collapse = await time(() => rows[0].querySelector('.a-fold').click())
    return { keyStep, select, collapse, nodes: document.querySelectorAll('*').length }
  })
  console.log(`1,000-task render: first paint with data ${firstRender} ms (includes page load), ${JSON.stringify(metrics)}`)
  expect(metrics.keyStep).toBeLessThan(500)
  expect(metrics.select).toBeLessThan(1000)
})

test('no serious accessibility violations on Work with the inspector open, in both appearances', async ({ page }) => {
  for (const theme of ['light', 'dark']) {
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await openWork(page, { tasks: showcase() }, '?task=quoted')
    for (const tab of ['Now', 'Activity', 'Output', 'Settings']) {
      await page.getByRole('tab', { name: tab }).click()
      await page.waitForTimeout(150)
      await page.addScriptTag({ path: axePath })
      const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
      const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
      console.log(`axe work ${theme} ${tab}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
      expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    }
    await page.unroute('**/*')
  }
})

test('the project menu lists every registered project, and an unavailable list is said plainly', async ({ page }) => {
  const projects = [{ project_id: 'p1', name: 'Release planning' }, { project_id: 'p2', name: 'Docs site' }]
  await openWork(page, { tasks: showcase(), projects })
  await page.getByRole('button', { name: 'All projects' }).click()
  // In the person's own order (the backend's), not by name.
  await expect(page.getByRole('menuitem')).toHaveText(['All projects', 'Release planning', 'Docs site'])
  await page.getByRole('menuitem', { name: 'Docs site' }).click() // registered, but no loaded task uses it
  await expect(page).toHaveURL(/project=p2/)
  await expect(page.getByText('No loaded tasks in Docs site.')).toBeVisible()
  await page.unroute('**/*')
  await openWork(page, { tasks: showcase(), projects: null })
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Project name unavailable')
  await page.getByRole('button', { name: 'All projects' }).click()
  await expect(page.getByRole('menuitem', { name: 'Projects couldn’t be loaded' })).toBeDisabled()
})

test('section counts say when they cover only the loaded tasks', async ({ page }) => {
  const pages = contract.paged
  await openWork(page, { tasks: contract.all, taskPages: (url) => {
    const after = url.searchParams.get('after')
    return after ? pages[pages.findIndex((item) => item.next_after === after) + 1] : pages[0]
  } })
  const note = page.locator('#work-count-note')
  await expect(note).toHaveText('Counts cover the 3 loaded tasks of 12')
  const now = page.getByRole('group', { name: 'What is happening' })
  await expect(now).toHaveAttribute('aria-describedby', 'work-count-note')
  await expect(now.getByRole('button', { name: /Need(s)? you$/ })).toHaveAttribute('title', /among the loaded tasks/)
  for (let i = 1; i < pages.length; i += 1) await page.getByRole('button', { name: 'Load older tasks' }).click()
  await expect(page.getByRole('button', { name: 'Load older tasks' })).toHaveCount(0)
  await expect(note).toHaveCount(0)
  await expect(now.getByRole('button', { name: /Need(s)? you$/ })).not.toHaveAttribute('title', /loaded/)
})

test('the task panel no longer offers Classic; Settings keeps it as a temporary fallback', async ({ page }) => {
  await openWork(page, { tasks: showcase() }, '?task=quoted')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector.getByRole('button', { name: 'Open in Classic' })).toHaveCount(0)
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('button', { name: 'Open Classic interface' }).click()
  await expect(page.getByRole('heading', { level: 1, name: 'Agent Workspace' })).toBeVisible()
})

test('capture M3 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(120_000)
  const shots = [
    ['m3-work-list-light-1440', 'light', 1440, ''],
    ['m3-work-list-dark-1440', 'dark', 1440, ''],
    ['m3-selected-task-inspector-light-1440', 'light', 1440, '?task=csv'],
    ['m3-multi-level-hierarchy-dark-1440', 'dark', 1440, '?task=cli-docs'],
    ['m3-awaiting-input-light-1440', 'light', 1440, '?task=quoted'],
    ['m3-replaced-agent-light-1440', 'light', 1440, '?task=report'],
    ['m3-removed-agent-dark-1440', 'dark', 1440, '?task=migrate'],
    ['m3-narrow-1024-light', 'light', 1024, '?task=quoted'],
    ['m3-narrow-1180-dark', 'dark', 1180, ''],
  ]
  for (const [name, theme, width, hash] of shots) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await openWork(page, { tasks: showcase() }, hash)
    await page.waitForTimeout(500)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
})
