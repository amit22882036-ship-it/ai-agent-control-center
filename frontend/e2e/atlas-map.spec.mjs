import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { announceChange, contract, openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }, { project_id: 'p2', name: 'Docs site', root_path: 'C:\\Work\\docs-site' }]
const esc = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const mapTree = (page) => page.getByRole('tree', { name: 'Work map' })
const listTree = (page) => page.getByRole('tree', { name: /^Work hierarchy/ })
const node = (page, title) => mapTree(page).getByRole('treeitem', { name: new RegExp(`^${esc(title)}`) })
const row = (page, title) => listTree(page).getByRole('treeitem', { name: new RegExp(`^${esc(title)}`) })
const hashParams = (page) => new URL(page.url().replace('#/', '')).searchParams
const transform = (page) => page.locator('.a-minner').evaluate((element) => element.style.transform)

async function openWork(page, hash = '', options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work${hash}`, tasks: showcase(), projects: PROJECTS, ...options })
  await expect(page.locator('[role="tree"]').first()).toBeVisible()
  return log
}

test('List and Map are two views of the same tasks: switching keeps the selection, the address and Back/Forward', async ({ page }) => {
  await openWork(page, '?task=quoted')
  await expect(row(page, 'Handle quoted fields')).toHaveAttribute('aria-selected', 'true')
  const view = page.getByRole('radiogroup', { name: 'View as' })
  await view.getByRole('radio', { name: 'Map' }).click()
  await expect(page).toHaveURL(/lens=map/)
  await expect(page).toHaveURL(/task=quoted/)
  await expect(node(page, 'Handle quoted fields')).toHaveAttribute('aria-selected', 'true')
  // The same tasks, the same open branches: one node per list row.
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(10)
  // The removed agent and the task without an agent are shown as what they are.
  await expect(node(page, 'Migrate the settings database')).toHaveAccessibleName(/Last agent: Migrator, removed/)
  await expect(node(page, 'Profile the slow dashboard load')).toHaveAccessibleName(/No agent/)
  await expect(node(page, 'JSON command-line interface')).toHaveAccessibleName(/Last agent: CLI writer/)
  // Levels follow parent_task_id: the lead, its helper, and the helper's own helper.
  await expect(node(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-level', '1')
  await expect(node(page, 'JSON command-line interface')).toHaveAttribute('aria-level', '2')
  await expect(node(page, 'Document the CLI flags')).toHaveAttribute('aria-level', '3')
  // A node click selects in both views.
  await node(page, 'HTML summary report').click()
  await expect(page).toHaveURL(/task=report/)
  await expect(page.getByRole('complementary', { name: 'Task details' })).toContainText('Report builder')
  // Keyboard: L returns to the List with the same selection revealed; Back returns to the Map.
  await page.locator('body').press('l')
  await expect(page).not.toHaveURL(/lens=map/)
  await expect(row(page, 'HTML summary report')).toHaveAttribute('aria-selected', 'true')
  await page.goBack()
  await expect(page).toHaveURL(/lens=map/)
  await expect(node(page, 'HTML summary report')).toHaveAttribute('aria-selected', 'true')
  await page.goForward()
  await expect(listTree(page)).toBeVisible()
  await page.locator('body').press('m')
  await expect(mapTree(page)).toBeVisible()
})

test('the map follows parent_task_id only and shows partial hierarchies honestly', async ({ page }) => {
  const pages = contract.paged
  await openWork(page, '?lens=map', { tasks: contract.all, taskPages: (url) => {
    const after = url.searchParams.get('after')
    if (!after) return pages[0]
    return pages[pages.findIndex((item) => item.next_after === after) + 1]
  } })
  await expect(page.locator('.a-mpartial')).toContainText(/Showing 3 of 12 tasks/)
  for (let i = 1; i < pages.length; i += 1) await page.locator('.a-mpartial').getByRole('button', { name: 'Load older tasks' }).click()
  await expect(page.locator('.a-mpartial')).toHaveCount(0)
  // A missing parent and a cycle get no invented edge, and say why.
  await expect(node(page, 'Stray Task')).toHaveAccessibleName(/Its lead no longer exists/)
  await expect(node(page, 'Loop A')).toHaveAccessibleName(/Its recorded hierarchy is inconsistent/)
  await expect(node(page, 'Stray Task')).toHaveAttribute('aria-level', '1')
  await expect(node(page, 'Investigate the flaky test')).toHaveAttribute('aria-level', '3')
  // Planned, unmaterialized work is a note on its lead, never a node.
  await expect(node(page, 'Ship the release')).toHaveAccessibleName(/2 planned/)
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(12)
})

test('keyboard: arrows follow the drawing, Enter selects, Space opens and closes, shared with the List', async ({ page }) => {
  await openWork(page, '?lens=map&task=cli')
  const cli = node(page, 'JSON command-line interface')
  await cli.focus()
  await page.keyboard.press('ArrowUp')
  await expect(node(page, 'Build the CSV import pipeline')).toBeFocused()
  await page.keyboard.press('ArrowDown')
  await expect(node(page, 'Handle quoted fields')).toBeFocused()
  await page.keyboard.press('ArrowRight')
  await expect(cli).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/task=cli/)
  // Space closes this branch on the map; the List shows the same closed branch.
  await page.keyboard.press(' ')
  await expect(node(page, 'Document the CLI flags')).toHaveCount(0)
  await expect(cli).toHaveAttribute('aria-expanded', 'false')
  await expect(cli).toHaveAccessibleName(/1 task hidden below/)
  await page.keyboard.press('ArrowDown') // opens it again on the way down
  await expect(node(page, 'Document the CLI flags')).toBeFocused()
  await page.keyboard.press('ArrowUp')
  await page.keyboard.press(' ')
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'List' }).click()
  await expect(row(page, 'JSON command-line interface')).toHaveAttribute('aria-expanded', 'false')
  // Plus and minus zoom from the map.
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'Map' }).click()
  const before = await page.locator('.a-mctl .a-pct').textContent()
  await node(page, 'JSON command-line interface').focus()
  await page.keyboard.press('-')
  await expect(page.locator('.a-mctl .a-pct')).not.toHaveText(before)
})

test('focus a branch from the details, with F, and leave it with Escape; both views show only that branch', async ({ page }) => {
  await openWork(page, '?lens=map&task=cli')
  await page.getByRole('complementary', { name: 'Task details' }).getByRole('button', { name: 'Focus this branch (F)' }).click()
  await expect(page).toHaveURL(/focus=cli/)
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(2)
  await expect(page.getByRole('navigation', { name: 'Focused branch' })).toContainText('Build the CSV import pipeline')
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'List' }).click()
  await expect(listTree(page).getByRole('treeitem')).toHaveCount(2)
  // Widen to the lead from the breadcrumb, then leave with Escape.
  await page.getByRole('navigation', { name: 'Focused branch' }).getByRole('button', { name: 'Build the CSV import pipeline' }).click()
  await expect(page).toHaveURL(/focus=csv/)
  await expect(listTree(page).getByRole('treeitem')).toHaveCount(5)
  await page.locator('body').press('Escape')
  await expect(page).not.toHaveURL(/focus=/)
  await expect(listTree(page).getByRole('treeitem')).toHaveCount(10)
  // F focuses the selected branch; choosing a task outside it leaves the focus rather than hiding the selection.
  await row(page, 'JSON command-line interface').click()
  await page.locator('body').press('f')
  await expect(page).toHaveURL(/focus=cli/)
  await page.getByRole('group', { name: 'Lead and helpers' }).getByRole('button', { name: /^Its lead: Build the CSV import pipeline/ }).click()
  await expect(page).toHaveURL(/task=csv/)
  await expect(page).not.toHaveURL(/focus=/)
  await expect(row(page, 'Build the CSV import pipeline')).toHaveAttribute('aria-selected', 'true')
})

test('status, project, colour and search filters narrow the map exactly as they narrow the list', async ({ page }) => {
  await openWork(page, '?lens=map')
  const now = page.getByRole('group', { name: 'What is happening' })
  await now.getByRole('button', { name: /Need you$/ }).click()
  // The tasks that need you and the path to them: the CSV lead, its question, and the migration.
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(3)
  await now.getByRole('button', { name: /Task families$/ }).click()
  await page.getByRole('button', { name: /Colour · Any/ }).click()
  await page.getByRole('dialog', { name: 'Filter by agent colour' }).getByRole('checkbox', { name: 'Green' }).check()
  await page.keyboard.press('Escape')
  await expect(page).toHaveURL(/colors=green/)
  await expect(node(page, 'Document the CLI flags')).toBeVisible()
  await expect(node(page, 'Summarize last week')).toHaveCount(0)
  // Ancestors kept for context are drawn quieter than the matches.
  await expect(node(page, 'Build the CSV import pipeline')).toHaveClass(/is-dim/)
  await expect(node(page, 'Document the CLI flags')).not.toHaveClass(/is-dim/)
  await page.getByRole('button', { name: /Colour · Green/ }).click()
  await page.getByRole('dialog', { name: 'Filter by agent colour' }).getByRole('button', { name: 'Any colour' }).click()
  await page.getByRole('searchbox', { name: 'Find in loaded tasks' }).fill('report')
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(2)
  await page.getByRole('searchbox', { name: 'Find in loaded tasks' }).fill('')
  await page.getByRole('button', { name: 'All projects' }).click()
  await page.getByRole('menuitem', { name: 'Docs site' }).click()
  await expect(page).toHaveURL(/project=p2/)
  await expect(page.locator('.a-mappane')).toContainText('No loaded tasks in Docs site.')
})

test('camera: zoom, fit, show everything, the overview and dragging, without selecting by accident', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openWork(page, '?lens=map', { tasks: large({ families: 12, width: 5 }) })
  const pct = page.locator('.a-mctl .a-pct')
  const start = await pct.textContent()
  await page.getByRole('button', { name: 'Zoom in' }).click()
  await expect(pct).not.toHaveText(start)
  await page.getByRole('button', { name: 'Show everything' }).click()
  await expect.poll(async () => parseInt(await pct.textContent(), 10)).toBeLessThan(70)
  await page.getByRole('button', { name: /Fit the selected task’s family/ }).click()
  await expect.poll(async () => parseInt(await pct.textContent(), 10)).toBeGreaterThanOrEqual(70)
  // Dragging the background pans and selects nothing.
  const before = await transform(page)
  const pane = await page.locator('.a-mappane').boundingBox()
  await page.mouse.move(pane.x + pane.width / 2, pane.y + pane.height - 140)
  await page.mouse.down()
  await page.mouse.move(pane.x + pane.width / 2 - 220, pane.y + pane.height - 180, { steps: 8 })
  await page.mouse.up()
  expect(await transform(page)).not.toBe(before)
  expect(hashParams(page).get('task')).toBeNull()
  // Wheel zooms around the pointer.
  const zoomed = await pct.textContent()
  await page.mouse.move(pane.x + 500, pane.y + 300)
  await page.mouse.wheel(0, -400)
  await expect(pct).not.toHaveText(zoomed)
  // The overview moves the camera.
  await page.getByRole('button', { name: 'Zoom in' }).click()
  const mini = page.locator('.a-mini')
  await expect(mini).toBeVisible()
  const atMini = await transform(page)
  const box = await mini.boundingBox()
  await page.mouse.click(box.x + box.width - 12, box.y + box.height - 12)
  await expect.poll(() => transform(page)).not.toBe(atMini)
})

test('the list beside the map: same selection, same branches, highlighted together', async ({ page }) => {
  await openWork(page, '?lens=map&task=quoted')
  await page.getByRole('button', { name: 'Show list' }).click()
  await expect(page).toHaveURL(/side=1/)
  await expect(listTree(page)).toBeVisible()
  await expect(row(page, 'Handle quoted fields')).toHaveAttribute('aria-selected', 'true')
  await row(page, 'HTML summary report').click()
  await expect(node(page, 'HTML summary report')).toHaveAttribute('aria-selected', 'true')
  await row(page, 'HTML summary report').hover()
  await expect(node(page, 'HTML summary report')).toHaveClass(/is-hl/)
  await page.getByRole('button', { name: 'Hide list' }).click()
  await expect(page).not.toHaveURL(/side=1/)
  await expect(listTree(page)).toHaveCount(0)
})

test('“Show in map” from a list row and from the details panel opens the Map at that task', async ({ page }) => {
  await openWork(page, '?task=cli-docs')
  await page.getByRole('complementary', { name: 'Task details' }).getByRole('button', { name: 'Show this task in the map' }).click()
  await expect(page).toHaveURL(/lens=map/)
  await expect(node(page, 'Document the CLI flags')).toHaveAttribute('aria-selected', 'true')
  await expect(node(page, 'Document the CLI flags')).toBeInViewport()
  // The family picture: real relationships, and choosing one selects it in the Map.
  const family = page.getByRole('group', { name: 'Lead and helpers' })
  await family.getByRole('button', { name: /^Its lead: JSON command-line interface/ }).click()
  await expect(node(page, 'JSON command-line interface')).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('complementary', { name: 'Task details' })).toContainText('Level 2 · helper 2 of 3 under its lead')
})

test('Projects show a picture of each project’s hierarchy and open its Map', async ({ page }) => {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  await openApp(page, { path: '/#/projects', tasks: showcase(), projects: PROJECTS })
  const release = page.locator('.a-plate').filter({ hasText: 'Release planning' })
  await expect(release.locator('.a-pv-svg circle')).toHaveCount(10) // every loaded task of its six families, as a dot
  await expect(release.locator('.a-pv-svg path')).toHaveCount(4) // the four parent_task_id links among them
  await expect(page.locator('.a-plate').filter({ hasText: 'Docs site' })).toContainText('No tasks yet')
  await release.getByRole('button', { name: /^Open Release planning in the map/ }).click()
  await expect(page).toHaveURL(/#\/work\?lens=map&project=p1$/)
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(10)
  await page.goBack()
  await page.locator('.a-plate').filter({ hasText: 'Release planning' }).getByRole('button', { name: 'Open map' }).click()
  await expect(page).toHaveURL(/lens=map&project=p1/)
})

test('a project with a very large family gets a bounded picture that says what it leaves out, and precise counts', async ({ page }) => {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const started = Date.now()
  await openApp(page, { path: '/#/projects', tasks: large({ families: 2, width: 600 }), projects: PROJECTS })
  const card = page.locator('.a-plate').filter({ hasText: 'Release planning' })
  await expect(card.locator('.a-pv-svg')).toBeVisible()
  const elapsed = Date.now() - started
  const shapes = await card.locator('.a-pv-svg .a-pv-n').count()
  expect(shapes).toBeLessThanOrEqual(36)
  await expect(card.locator('.a-pthumb-note')).toHaveText(`${shapes} of 1202 tasks shown`)
  await expect(card.locator('.a-pv-more').first()).toContainText('+')
  await expect(card.getByRole('button', { name: /^Open Release planning in the map/ })).toHaveAccessibleName(new RegExp(`shows ${shapes} of 1202 tasks in 2 of 2 task families`))
  // Counts are of tasks: 2 leads working, and every fourth helper running (150 per family), out of 1202 tasks.
  await expect(card.locator('.a-pstats')).toContainText('302open tasks')
  await expect(card.locator('.a-pstats')).toContainText('302agents working')
  await expect(card).toContainText('1202 tasks in 2 families')
  console.log(`projects page with 1,202 tasks: card picture ready in ${elapsed} ms, ${shapes} shapes`)
})

test('live updates refresh the map in place without moving the camera, and a lost backend is explained', async ({ page }) => {
  let fail = false
  let changed = false
  await openWork(page, '?lens=map&task=quoted', { taskPages: () => {
    if (fail) return null
    const data = showcase()
    if (changed) {
      const quoted = data.tasks.find((item) => item.task_id === 'quoted')
      Object.assign(quoted, { status: 'in_progress', waiting_context: null, agent: { ...quoted.agent, status: 'running', asks_user: false } })
    }
    return data
  } })
  await expect(node(page, 'Handle quoted fields')).toHaveAccessibleName(/Waiting for you/)
  await page.waitForTimeout(700)
  const before = await node(page, 'Handle quoted fields').boundingBox()
  changed = true
  await announceChange(page)
  await expect(node(page, 'Handle quoted fields')).toHaveAccessibleName(/Working/)
  // Its family moved from "Needs your attention" to "Open", yet the selected task stays put on screen.
  await page.waitForTimeout(300)
  const after = await node(page, 'Handle quoted fields').boundingBox()
  expect(Math.abs(after.x - before.x)).toBeLessThan(1)
  expect(Math.abs(after.y - before.y)).toBeLessThan(1)
  fail = true
  await announceChange(page)
  await expect(page.getByText('Showing the last list received')).toBeVisible()
  await expect(node(page, 'Handle quoted fields')).toBeVisible()
  fail = false
  await announceChange(page)
  await expect(page.getByText('Showing the last list received')).toHaveCount(0)
})

test('reduced motion: switching views and moving the camera happen at once, without travelling avatars', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openWork(page, '?task=quoted')
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'Map' }).click()
  await expect(mapTree(page)).toBeVisible()
  expect(await page.locator('.a-ghosts').count()).toBe(0)
  await page.getByRole('button', { name: 'Zoom in' }).click()
  expect(await page.locator('.a-minner').evaluate((element) => getComputedStyle(element).transitionDuration)).toBe('0s')
  expect(await page.locator('.a-edges .a-e--work').evaluate((element) => getComputedStyle(element).animationName)).toBe('none')
})

test('with motion allowed, the avatars travel between the List and the Map', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'no-preference' })
  await openWork(page, '?task=quoted')
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'Map' }).click()
  await expect(page.locator('.a-ghosts .a-ghost').first()).toBeAttached()
  await expect(page.locator('.a-ghosts')).toHaveCount(0, { timeout: 2000 })
})

test('mixed-direction titles keep their direction in the map', async ({ page }) => {
  await openWork(page, '?lens=map')
  const hebrew = node(page, 'לתרגם את מסך ההגדרות')
  expect(await hebrew.locator('.a-gt').evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
  expect(await node(page, 'Build the CSV import pipeline').locator('.a-gt').evaluate((element) => getComputedStyle(element).direction)).toBe('ltr')
})

test('the map fits 1440, 1180 and 1024 with the list beside it and details open, with no serious accessibility violations', async ({ page }) => {
  for (const [width, theme] of [[1440, 'light'], [1180, 'dark'], [1024, 'light']]) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await openWork(page, '?lens=map&side=1&task=quoted')
    await expect(node(page, 'Handle quoted fields')).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0)
    // The header's view switch stays reachable, and the selected task sits in the open part of the map.
    await expect(page.getByRole('radiogroup', { name: 'View as' })).toBeInViewport()
    const open = await page.evaluate(() => {
      const pane = document.querySelector('.a-mappane').getBoundingClientRect()
      const side = document.querySelector('[data-side-list]')?.getBoundingClientRect()
      const insp = document.querySelector('.a-work > .a-inspector').getBoundingClientRect()
      const target = document.querySelector('.a-mappane [data-id="quoted"]').getBoundingClientRect()
      return { left: side ? side.right : pane.left, right: Math.min(pane.right, insp.left), x: target.left + target.width / 2 }
    })
    expect(open.x).toBeGreaterThan(open.left)
    expect(open.x).toBeLessThan(open.right)
    await page.waitForTimeout(500) // let the camera settle before scanning
    await page.addScriptTag({ path: axePath })
    const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
    const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
    console.log(`axe map ${width} ${theme}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
    expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    await page.unroute('**/*')
  }
})

test('at 150 % browser zoom (1440 × 900 shown as 960 × 600), the map, its controls and the details stay usable', async ({ page }) => {
  await page.setViewportSize({ width: 960, height: 600 })
  await openWork(page, '?lens=map&task=quoted')
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0)
  await expect(page.getByRole('radiogroup', { name: 'View as' })).toBeInViewport()
  await expect(page.getByRole('button', { name: 'Zoom in' })).toBeInViewport()
  await expect(node(page, 'Handle quoted fields')).toBeInViewport()
  await page.getByRole('button', { name: 'Show list' }).click()
  await row(page, 'HTML summary report').click()
  await expect(page.getByRole('complementary', { name: 'Task details' })).toContainText('Report builder')
  await expect(node(page, 'HTML summary report')).toHaveAttribute('aria-selected', 'true')
})

test('Projects with previews have no serious accessibility violations in both appearances', async ({ page }) => {
  for (const theme of ['light', 'dark']) {
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.goto('about:blank')
    await openApp(page, { path: '/#/projects', tasks: showcase(), projects: PROJECTS })
    await expect(page.locator('.a-pv-svg').first()).toBeVisible()
    await page.addScriptTag({ path: axePath })
    const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
    const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
    console.log(`axe projects ${theme}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
    expect(serious.map((item) => item.id)).toEqual([])
    await page.unroute('**/*')
  }
})

test('large hierarchies: 1,000 tasks, deep chains and rapid selection stay responsive (measured)', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  const tasks = large({ families: 50, width: 19 })
  await openWork(page, '', { tasks })
  const radio = page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'Map' })
  const started = Date.now()
  await radio.click()
  await expect(mapTree(page).getByRole('treeitem').first()).toBeVisible()
  const mapOpen = Date.now() - started
  const drawn = await mapTree(page).getByRole('treeitem').count()
  const domNodes = await page.evaluate(() => document.querySelectorAll('.a-mappane *').length)
  // Selection of a visible node.
  // Tasks drawn inside the visible part of the map (some are drawn just outside it, ready for panning).
  const visibleIds = () => mapTree(page).getByRole('treeitem').evaluateAll((elements) => {
    const pane = document.querySelector('.a-mappane').getBoundingClientRect()
    return elements.filter((element) => {
      const r = element.getBoundingClientRect()
      return r.left > pane.left + 20 && r.right < pane.right - 220 && r.top > pane.top + 70 && r.bottom < pane.bottom - 70
    }).map((element) => element.dataset.id)
  })
  const firstId = (await visibleIds())[3]
  const first = mapTree(page).locator(`[data-id="${firstId}"]`)
  const selectStart = Date.now()
  await first.click()
  await expect(first).toHaveAttribute('aria-selected', 'true')
  const selectTime = Date.now() - selectStart
  // Rapid selection: the last click wins.
  const ids = (await visibleIds()).slice(4, 12)
  for (const id of ids) await mapTree(page).locator(`[data-id="${id}"]`).click({ delay: 0 })
  const lastId = ids[ids.length - 1]
  await expect(page).toHaveURL(new RegExp(`task=${lastId}`))
  // Pan and zoom frames.
  const frames = await page.evaluate(async () => {
    const pane = document.querySelector('.a-mappane')
    const rect = pane.getBoundingClientRect()
    const times = []
    for (let i = 0; i < 20; i += 1) {
      const t = performance.now()
      pane.dispatchEvent(new WheelEvent('wheel', { deltaY: i % 2 ? 60 : -60, clientX: rect.left + 400, clientY: rect.top + 300, bubbles: true, cancelable: true }))
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      times.push(performance.now() - t)
    }
    times.sort((a, b) => a - b)
    return { median: times[10], worst: times[19] }
  })
  // Expand everything, then collapse to families.
  await page.getByRole('button', { name: 'View options' }).click()
  const collapseStart = Date.now()
  await page.getByRole('menuitem', { name: 'Collapse to task families' }).click()
  await expect(mapTree(page).getByRole('treeitem')).toHaveCount(50)
  const collapseTime = Date.now() - collapseStart
  console.log(`map 1000 tasks: open ${mapOpen} ms, drawn ${drawn} nodes (${domNodes} DOM elements), select ${selectTime} ms, `
    + `wheel frame median ${frames.median.toFixed(1)} ms worst ${frames.worst.toFixed(1)} ms, collapse ${collapseTime} ms`)
  expect(drawn).toBeLessThan(1000) // only what is near the view is drawn
  expect(mapOpen).toBeLessThan(4000)
  expect(selectTime).toBeLessThan(1500)
  expect(frames.median).toBeLessThan(50)
  // Deep chains.
  await page.unroute('**/*')
  await openWork(page, '?lens=map&task=r000-c0-d29', { tasks: large({ families: 4, width: 6, depth: 30 }) })
  await expect(node(page, 'Level 31 under r000-c0')).toHaveAttribute('aria-selected', 'true')
  await expect(node(page, 'Level 31 under r000-c0')).toHaveAttribute('aria-level', '32')
  await expect(node(page, 'Level 31 under r000-c0')).toBeInViewport()
})

test('the map can be drawn left to right; the choice is remembered and the arrows turn with it', async ({ page }) => {
  await openWork(page, '?lens=map&task=csv')
  const box = async (title) => node(page, title).boundingBox()
  const lead = await box('Build the CSV import pipeline')
  const helper = await box('HTML summary report')
  expect(helper.y).toBeGreaterThan(lead.y + lead.height - 1) // Top to bottom by default.
  await page.getByRole('button', { name: 'Draw the map left to right' }).click()
  await expect(page.getByRole('button', { name: 'Draw the map top to bottom' })).toBeVisible()
  const lead2 = await box('Build the CSV import pipeline')
  const helper2 = await box('HTML summary report')
  expect(helper2.x).toBeGreaterThan(lead2.x + lead2.width - 1)
  expect(await page.evaluate(() => localStorage.getItem('control-center.atlas.map-direction'))).toBe('right')
  await node(page, 'Build the CSV import pipeline').focus()
  await page.keyboard.press('ArrowRight')
  await expect(page.locator(':focus')).not.toHaveAttribute('data-id', 'csv')
  await page.keyboard.press('ArrowLeft')
  await expect(page.locator(':focus')).toHaveAttribute('data-id', 'csv')
  await page.reload()
  await expect(page.getByRole('button', { name: 'Draw the map top to bottom' })).toBeVisible()
})
