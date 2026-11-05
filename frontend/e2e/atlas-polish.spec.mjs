import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const heading = (page) => page.locator('.atlas h1').first()

async function open(page, hash, options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  return openApp(page, { path: `/#/${hash}`, tasks: showcase(), ...options })
}

// Every control that can take focus and is drawn must be reachable: inside the window, or inside a
// region that scrolls sideways (the navigation row, the Map). Returns those that are not.
function unreachable(page) {
  return page.evaluate(() => {
    const out = []
    for (const element of document.querySelectorAll('.atlas a[href], .atlas button, .atlas input, .atlas select, .atlas textarea, .atlas [tabindex="0"]')) {
      const box = element.getBoundingClientRect()
      if (!box.width || !box.height || getComputedStyle(element).visibility === 'hidden') continue
      if (element.closest('.a-mappane, .a-inspector-resize')) continue
      if (box.right <= window.innerWidth + 1 && box.left >= -1) continue
      let parent = element.parentElement
      let scrolls = false
      while (parent) {
        const style = getComputedStyle(parent)
        if (['auto', 'scroll'].includes(style.overflowX) && parent.scrollWidth > parent.clientWidth) { scrolls = true; break }
        parent = parent.parentElement
      }
      if (!scrolls) out.push((element.getAttribute('aria-label') || element.textContent || '').trim().slice(0, 40))
    }
    return out
  })
}

// Widths stand for window sizes and for text zoom on a 1280-pixel window: 640 is 200 %, 512 is
// 250 % and 320 is 400 % (the WCAG reflow width). Browser zoom changes the CSS width the same way.
const WIDTHS = [1600, 1440, 1180, 1024, 820, 640, 512, 320]
const SCREENS = ['work', 'work?task=quoted', 'work?lens=map&task=quoted', 'new', 'projects', 'task?id=csv', 'connections', 'connections?provider=claude', 'settings']

test('every screen reflows from 1600 pixels down to 320 (400 % text) without sideways scrolling or unreachable controls', async ({ page }) => {
  test.setTimeout(240_000)
  const problems = []
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: width < 700 ? 720 : 900 })
    for (const screen of SCREENS) {
      await open(page, screen)
      await expect(heading(page)).toBeAttached()
      await page.waitForTimeout(250)
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)
      const lost = await unreachable(page)
      if (overflow > 0 || lost.length) problems.push(`${width} ${screen}: overflow ${overflow}, unreachable ${lost.join(' | ')}`)
      await page.unroute('**/*')
    }
  }
  expect(problems).toEqual([])
})

test('at 250 % text the top bar keeps every action with its name, the bell panel fits and New task stacks', async ({ page }) => {
  await page.setViewportSize({ width: 512, height: 800 })
  await open(page, 'work')
  for (const name of ['New task', /^Notifications/, '1 needs you. Go to the next one']) {
    await expect(page.locator('.a-topbar').getByRole(typeof name === 'string' && name === 'New task' ? 'link' : 'button', { name }).first()).toBeVisible()
  }
  await expect(page.locator('.a-topbar').getByRole('link', { name: 'Control Center' })).toBeVisible()
  await page.locator('.a-bell').click()
  const box = await page.getByRole('dialog', { name: 'Notifications' }).boundingBox()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(512)
  await page.keyboard.press('Escape')
  await open(page, 'new')
  const write = await page.locator('.a-write').boundingBox()
  const side = await page.locator('.a-nt-side').boundingBox()
  expect(side.y).toBeGreaterThanOrEqual(write.y + write.height - 1)  // The summary follows the form; it never covers it.
})

test('N opens New task from any page, but never while typing, with a menu or panel open, or with a modifier', async ({ page }) => {
  for (const start of ['work', 'settings', 'connections?provider=codex', 'task?id=csv']) {
    await open(page, start)
    await expect(heading(page)).toBeAttached()
    await page.locator('body').press('n')
    await expect(page).toHaveURL(/#\/new$/)
  }
  // Back returns to where N was pressed.
  await page.goBack()
  await expect(page).toHaveURL(/#\/task\?id=csv$/)
  await open(page, 'work')
  await page.getByLabel('Find in loaded tasks').fill('n')
  await page.getByLabel('Find in loaded tasks').press('n')
  await expect(page).toHaveURL(/#\/work$/)
  await expect(page.getByLabel('Find in loaded tasks')).toHaveValue('nn')
  await page.getByRole('button', { name: 'View options' }).click()
  await page.keyboard.press('n')
  await expect(page).toHaveURL(/#\/work$/)
  await page.keyboard.press('Escape')
  await page.locator('.a-bell').click()
  await page.keyboard.press('n')
  await expect(page).toHaveURL(/#\/work$/)
  await page.keyboard.press('Escape')
  await page.locator('body').press('Control+n').catch(() => {})
  await expect(page).toHaveURL(/#\/work$/)
  // On New task itself, N types into the description.
  await open(page, 'new')
  await page.locator('#nt-text').fill('Plan the')
  await page.locator('#nt-text').press('n')
  await expect(page.locator('#nt-text')).toHaveValue('Plan then')
  await expect(page.locator('.a-topbar').getByRole('link', { name: 'New task' })).toHaveAttribute('aria-keyshortcuts', 'N')
})

test('closing a popover returns focus to the control that opened it', async ({ page }) => {
  await open(page, 'work')
  const colour = page.getByRole('button', { name: /Colour/ })
  await colour.click()
  await expect(page.getByRole('dialog', { name: 'Filter by agent colour' })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Filter by agent colour' }).getByRole('button', { name: 'Any colour' })).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Filter by agent colour' })).toHaveCount(0)
  await expect(colour).toBeFocused()
  await open(page, 'new')
  const picker = page.getByRole('button', { name: /Project/ }).first()
  await picker.click()
  await expect(page.getByLabel('Find a project')).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(picker).toBeFocused()
  await picker.click()
  await page.getByRole('dialog', { name: 'Choose a project' }).getByRole('button', { name: /Release planning/ }).click()
  await expect(picker).toBeFocused()
  await expect(picker).toContainText('Release planning')
})

test('an unfinished New task is kept when leaving the page and is gone once it starts', async ({ page }) => {
  await open(page, 'new')
  await page.locator('#nt-text').fill('Translate the settings page into Hebrew (לעברית)')
  await page.getByRole('button', { name: 'Back to Work' }).click()
  await expect(page).toHaveURL(/#\/work$/)
  await page.locator('body').press('n')
  await expect(page.locator('#nt-text')).toHaveValue('Translate the settings page into Hebrew (לעברית)')
  await expect(page.getByRole('status').filter({ hasText: 'Your unfinished task is as you left it.' })).toBeVisible()
  await page.getByRole('button', { name: 'Start over' }).click()
  await expect(page.locator('#nt-text')).toHaveValue('')
})

test('the keyboard reference matches the shortcuts the pages implement', async ({ page }) => {
  await open(page, 'settings?section=keyboard')
  const keys = page.locator('#atlas-settings-keyboard')
  for (const [action, combo] of [['New task', 'N'], ['Show the List', 'L'], ['Show the Map', 'M'], ['Focus on the selected task’s branch', 'F'],
    ['Open the selected task’s page', 'O'], ['Next or previous task that needs you', 'A · Shift A'], ['Reply to the selected task’s question', 'R']]) {
    await expect(keys.locator('div').filter({ hasText: action }).filter({ has: page.locator('kbd', { hasText: combo }) }).first()).toBeVisible()
  }
  // Each listed Work key does what the reference says.
  await open(page, 'work?task=quoted')
  await page.locator('.a-whead').click({ position: { x: 4, y: 40 } })
  for (const [press, url] of [['m', /lens=map/], ['l', /^(?!.*lens=map)/], ['f', /focus=quoted/], ['Escape', /^(?!.*focus=)/], ['a', /task=migrate/], ['o', /#\/task\?id=migrate/]]) {
    await page.locator('.a-whead').click({ position: { x: 4, y: 40 } })
    await page.keyboard.press(press)
    await expect(page).toHaveURL(url)
  }
})

test('state survives moving between screens: project, selection in both views, panel width and appearance', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('control-center.atlas.inspector-width', '460'))
  await open(page, 'work?project=p1&task=quoted')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector).toBeVisible()
  expect(await inspector.evaluate((element) => element.getBoundingClientRect().width)).toBe(460)
  await page.locator('.a-whead').click({ position: { x: 4, y: 40 } })
  await page.keyboard.press('m')
  await expect(page).toHaveURL(/lens=map/)
  await expect(page).toHaveURL(/project=p1/)
  await expect(page).toHaveURL(/task=quoted/)
  await expect(inspector).toBeVisible()
  await inspector.getByRole('button', { name: 'Open task' }).click()
  await expect(heading(page)).toContainText('Handle quoted fields')
  await page.goBack()
  await expect(page).toHaveURL(/project=p1/)
  await expect(page).toHaveURL(/lens=map/)
  await expect(page).toHaveURL(/task=quoted/)
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Settings' }).click()
  await page.getByRole('radio', { name: /Dark/ }).click()
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.goBack()
  await expect(page).toHaveURL(/#\/work\?.*task=quoted/)
})

test('reduced motion is honoured everywhere: no travelling avatars, no animated panels', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await open(page, 'work?task=quoted')
  await page.locator('.a-whead').click({ position: { x: 4, y: 40 } })
  await page.keyboard.press('m')
  await page.waitForTimeout(50)
  expect(await page.locator('.a-ghosts').count()).toBe(0)
  await page.locator('.a-bell').click()
  expect(await page.getByRole('dialog', { name: 'Notifications' }).evaluate((element) => getComputedStyle(element).animationName)).toBe('none')
  await page.keyboard.press('Escape')
  await page.getByRole('button', { name: 'View options' }).click()
  expect(await page.getByRole('menu', { name: 'View options' }).evaluate((element) => getComputedStyle(element).animationName)).toBe('none')
})

test('axe finds no serious problem on the main screens at 200 % text in both themes', async ({ page }) => {
  test.setTimeout(180_000)
  await page.setViewportSize({ width: 640, height: 800 })
  for (const theme of ['light', 'dark']) {
    for (const screen of ['work?task=quoted', 'new', 'projects', 'task?id=csv', 'connections?provider=gemini', 'settings']) {
      await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
      await open(page, screen)
      await expect(heading(page)).toBeAttached()
      await page.waitForTimeout(300)
      await page.addScriptTag({ path: axePath })
      const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
      const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
      console.log(`axe 640 ${theme} ${screen}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
      expect(serious.map((item) => `${screen} ${item.id}: ${item.nodes.map((node) => node.target.join(' ')).join(' / ')}`)).toEqual([])
      await page.unroute('**/*')
    }
  }
})
