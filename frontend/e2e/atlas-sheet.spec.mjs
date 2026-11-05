import { test, expect } from '@playwright/test'
import { announceChange, openApp } from './harness.mjs'
import { BUSY_PROJECTS, busy, node, showcase } from './scenarios.mjs'

// The Work sheet at scale: the Now strip, marked paths, history by day, Go to, and live changes
// that never move what the person is looking at. Synthetic busy workspaces only.
const NOW = new Date('2026-10-29T12:05:00.000Z')
const projects = BUSY_PROJECTS.map((project, index) => ({ ...project, sort_order: index }))
const row = (page, title) => page.getByRole('treeitem', { name: new RegExp(`^${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}`) })
const strip = (page) => page.getByRole('group', { name: 'What is happening' })

async function openWork(page, options = {}, hash = '') {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work${hash}`, projects, ...options })
  await expect(page.locator('[role="tree"]')).toBeVisible()
  return log
}

test('a busy workspace opens as one line per family, with what needs you marked and history folded away', async ({ page }) => {
  const data = busy({ families: 240 })
  await openWork(page, { tasks: data })
  // The strip counts every loaded task by state.
  for (const label of ['Need you', 'Working', 'Waiting', 'On hold', 'Done']) await expect(strip(page).getByRole('button', { name: new RegExp(`\\d+ ${label}$`) })).toBeVisible()
  // Only rows near the view exist: the first heading is drawn, the others as they are reached.
  await expect(page.locator('.a-sheet-sec').first()).toHaveText(/^Needs you/)
  expect(await page.locator('.a-sheet [role="treeitem"]').count()).toBeLessThan(80)
  // Families that need you show only the path to what needs you, and say what is not shown.
  const marked = page.locator('.a-sheet .a-row.is-marked').first()
  await expect(marked.locator('.a-mark')).toHaveCount(1)
  await expect(page.locator('.a-sheet-more').first()).toContainText(/more tasks? in this branch/)
  // History is offered, not listed, until asked for, and then grouped by day.
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = sheet.scrollHeight })
  const reveal = page.locator('.a-sheet-reveal button')
  await expect(reveal).toContainText(/task families in history/)
  await reveal.click()
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = sheet.scrollHeight })
  await expect(page.locator('.a-sheet-day').first()).toBeVisible()
  // The choice is remembered.
  await openWork(page, { tasks: data })
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = sheet.scrollHeight })
  await expect(page.locator('.a-sheet-day').first()).toBeVisible()
})

test('a scope from the strip narrows to the tasks in that state and the path to them, and pressing it again returns to all work', async ({ page }) => {
  await openWork(page, { tasks: busy({ families: 120 }) })
  await strip(page).getByRole('button', { name: /Working$/ }).click()
  await expect(page).toHaveURL(/filter=working/)
  await expect(strip(page).getByRole('button', { name: /Working$/ })).toHaveAttribute('aria-pressed', 'true')
  const rows = page.locator('.a-sheet [role="treeitem"]')
  const states = await rows.evaluateAll((items) => items.map((item) => item.querySelector('.a-c-now .a-l')?.textContent))
  expect(states.length).toBeGreaterThan(3)
  expect(states.some((state) => /Working|Reviewing/.test(state))).toBe(true)
  await strip(page).getByRole('button', { name: /Working$/ }).click()
  await expect(page).not.toHaveURL(/filter=/)
})

test('a branch pruned to what needs you opens fully on request, and closes again with the keyboard', async ({ page }) => {
  await openWork(page, { tasks: busy({ families: 120 }) })
  const more = page.locator('.a-sheet-more').first()
  const id = (await more.getAttribute('data-key')).replace(/^more:/, '')
  const hidden = Number((await more.textContent()).match(/\d+/)[0])
  expect(hidden).toBeGreaterThan(0)
  const shown = await page.locator(`.a-sheet [data-id^="${id}-"]`).count()
  await more.locator('button').click()
  await expect(page.locator(`.a-sheet [data-key="more:${id}"]`)).toHaveCount(0)
  await expect.poll(() => page.locator(`.a-sheet [data-id^="${id}-"]`).count()).toBeGreaterThan(shown)
  // Left closes the branch again from the keyboard.
  await page.locator(`.a-sheet [data-id="${id}"]`).focus()
  await page.keyboard.press('ArrowLeft')
  await expect(page.locator(`.a-sheet [data-id="${id}"]`)).toHaveAttribute('aria-expanded', 'false')
})

test('Go to finds a task by name from any page and opens it in Work; it remembers what was opened', async ({ page }) => {
  await openWork(page, { tasks: showcase() }, '')
  await page.goto('http://atlas.preview/#/settings')
  await page.keyboard.press('Control+k')
  const dialog = page.getByRole('dialog', { name: 'Go to' })
  await expect(dialog).toBeVisible()
  const field = dialog.getByRole('combobox', { name: 'Go to a task, project or page' })
  await expect(field).toBeFocused()
  await field.fill('quoted')
  await expect(dialog.getByRole('option').first()).toContainText('Handle quoted fields and embedded newlines')
  await page.keyboard.press('Enter')
  await expect(dialog).toBeHidden()
  await expect(page).toHaveURL(/#\/work.*task=quoted/)
  await expect(row(page, 'Handle quoted fields')).toHaveAttribute('aria-selected', 'true')
  // "/" opens it too; the recently opened task is offered first, and pages are listed.
  await page.locator('.a-whead').click({ position: { x: 5, y: 40 } })
  await page.keyboard.press('/')
  await expect(dialog.getByRole('group', { name: 'Recently opened' })).toContainText('Handle quoted fields')
  await expect(dialog.getByRole('group', { name: 'Pages' })).toContainText('Connections')
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  // From the title bar as well.
  await page.getByRole('button', { name: /Go to/ }).first().click()
  await expect(dialog).toBeVisible()
  await field.fill('connections')
  await page.keyboard.press('Enter')
  await expect(page).toHaveURL(/#\/connections/)
})

test('a live change never moves the selected row or the scroll position, even when new work arrives above it', async ({ page }) => {
  const data = busy({ families: 300 })
  let extra = []
  await openWork(page, { tasks: data, taskPages: () => ({ ...data, tasks: [...extra, ...data.tasks], total: data.tasks.length + extra.length }) })
  // Scroll well down and select a row there.
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = 2400 })
  await page.waitForTimeout(150)
  const id = await page.locator('.a-sheet .a-row--root').nth(8).getAttribute('data-id')
  await page.locator(`.a-sheet [data-id="${id}"]`).click()
  await expect(page).toHaveURL(new RegExp(`task=${id}`))
  await page.waitForTimeout(150)
  const before = await page.locator(`.a-sheet [data-id="${id}"]`).evaluate((element) => element.getBoundingClientRect().top)
  const scrolled = await page.evaluate(() => document.querySelector('.a-sheet').scrollTop)
  // Three new families that need you appear at the very top of the list.
  extra = [0, 1, 2].map((index) => node(`new-${index}`, null, { title: `Brand new family ${index}`, project_id: 'p1', created_at: '2026-10-29T12:04:00.000Z',
    updated_at: '2026-10-29T12:04:00.000Z', status: 'in_progress', responsibility_awaiting: true }))
  await announceChange(page)
  await expect.poll(() => page.evaluate(() => document.querySelector('.a-sheet').scrollTop)).toBeGreaterThan(scrolled + 100)
  const after = await page.locator(`.a-sheet [data-id="${id}"]`).evaluate((element) => element.getBoundingClientRect().top)
  expect(Math.abs(after - before)).toBeLessThan(3)
  await expect(page.locator(`.a-sheet [data-id="${id}"]`)).toHaveAttribute('aria-selected', 'true')
  await page.locator('.a-sheet').evaluate((sheet) => { sheet.scrollTop = 0 })
  await expect(row(page, 'Brand new family 2')).toBeVisible()
})

test('changes since you last looked are marked, can be shown on their own, and cleared', async ({ page }) => {
  const data = showcase()
  await page.addInitScript(() => localStorage.setItem('control-center.atlas.seen', JSON.stringify({ since: '2026-10-29T01:00:00.000Z', opened: {} })))
  await openWork(page, { tasks: data })
  const changed = strip(page).getByRole('button', { name: /Changed$/ })
  await expect(changed).toBeVisible()
  await changed.click()
  await expect(page).toHaveURL(/filter=changed/)
  expect(await page.locator('.a-sheet .a-changed').count()).toBeGreaterThan(0)
  await strip(page).getByRole('button', { name: 'Mark all seen' }).click()
  await expect(strip(page).getByRole('button', { name: /Changed$/ })).toHaveCount(0)
  await expect(page).not.toHaveURL(/filter=changed/)
})

test('the sheet keeps one Tab stop and moves by page with the keyboard', async ({ page }) => {
  await openWork(page, { tasks: busy({ families: 200 }) })
  const first = page.locator('.a-sheet [role="treeitem"][tabindex="0"]')
  await expect(first).toHaveCount(1)
  await first.focus()
  await page.keyboard.press('PageDown')
  await page.keyboard.press('PageDown')
  const focused = await page.evaluate(() => document.activeElement?.getAttribute('aria-level'))
  expect(focused).not.toBeNull()
  expect(await page.evaluate(() => document.querySelector('.a-sheet').scrollTop)).toBeGreaterThan(200)
  await page.keyboard.press('End')
  await page.keyboard.press('Home')
  expect(await page.evaluate(() => document.querySelector('.a-sheet').scrollTop)).toBeLessThan(80)
})

test('reduced motion: the grease mark is already drawn and rows do not glide', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openWork(page, { tasks: showcase() })
  const path = page.locator('.a-sheet .a-mark path').first()
  expect(await path.evaluate((element) => getComputedStyle(element).animationName)).toBe('none')
  expect(await path.evaluate((element) => getComputedStyle(element).strokeDashoffset)).toMatch(/^0(px)?$/)
})
