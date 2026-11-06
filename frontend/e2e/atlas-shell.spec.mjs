import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { join } from 'node:path'
import { activeEventSources, openApp } from './harness.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const UNSAFE = /^(POST|PUT|PATCH|DELETE) /

async function heading(page) {
  return page.locator('.atlas h1').innerText()
}

test('the new interface opens by default, with the rail and no classic marker', async ({ page }) => {
  const log = await openApp(page, { path: '/' })
  await expect(page.locator('.atlas h1')).toHaveText('Work')
  await expect(page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Projects' })).toBeVisible()
  // Classic is no longer offered in the everyday interface; Settings keeps it while features move.
  await expect(page.getByRole('button', { name: 'Open the classic interface' })).toHaveCount(0)
  expect(await activeEventSources(page)).toBe(1)
  expect(log.external).toEqual([])
})

test('Back and Forward move between Atlas pages and restore where each page was', async ({ page }) => {
  await openApp(page, { path: '/#/gallery' })
  await expect(page.locator('.atlas h1')).toHaveText('Design gallery')
  await page.locator('.a-main').evaluate((main) => { main.scrollTop = 900 })
  await page.waitForTimeout(50)
  await page.getByRole('navigation', { name: 'Main' }).getByRole('link', { name: 'Settings' }).click()
  await expect(page.locator('.atlas h1')).toHaveText('Settings')
  expect(await page.locator('.a-main').evaluate((main) => main.scrollTop)).toBe(0)
  await expect(page.locator('.atlas h1')).toBeFocused()
  await page.goBack()
  await expect(page.locator('.atlas h1')).toHaveText('Design gallery')
  await expect.poll(() => page.locator('.a-main').evaluate((main) => main.scrollTop)).toBeGreaterThan(850)
  await page.goForward()
  await expect(page.locator('.atlas h1')).toHaveText('Settings')
  await page.goto('/#/does-not-exist')
  await expect(page.locator('.atlas h1')).toHaveText('Page not found')
})

test('appearance persists and Automatic follows the system', async ({ page }) => {
  await openApp(page, { path: '/#/settings' })
  await page.getByRole('radio', { name: 'Dark' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.getByRole('radio', { name: 'Dark' })).toHaveAttribute('aria-checked', 'true')
  await page.getByRole('radio', { name: 'Automatic' }).click()
  await page.emulateMedia({ colorScheme: 'dark' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.emulateMedia({ colorScheme: 'light' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await page.getByRole('radio', { name: 'Automatic' }).press('ArrowRight')
  await expect(page.getByRole('radio', { name: 'Light' })).toBeFocused()
  await expect(page.getByRole('radio', { name: 'Light' })).toHaveAttribute('aria-checked', 'true')
  expect(await page.evaluate(() => localStorage.getItem('control-center.theme'))).toBe('light')
})

test('keyboard focus is visible and the skip link reaches the page content', async ({ page }) => {
  await openApp(page, { path: '/#/work' })
  await expect(page.locator('.atlas h1')).toHaveText('Work')
  await page.keyboard.press('Tab')
  await expect(page.getByRole('button', { name: 'Skip to content' })).toBeFocused()
  await expect(page.getByRole('button', { name: 'Skip to content' })).toBeInViewport()
  await page.keyboard.press('Enter')
  await expect(page.locator('.a-main')).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await page.keyboard.press('Shift+Tab')
  const outline = await page.evaluate(() => {
    const style = getComputedStyle(document.activeElement)
    return { tag: document.activeElement.tagName, style: style.outlineStyle, width: style.outlineWidth }
  })
  expect(outline.style).toBe('solid')
  expect(outline.width).toBe('2px')
})

test('reduced motion removes animation and mixed-direction text keeps its own direction', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await openApp(page, { path: '/#/gallery' })
  // Nothing runs an animation: state marks are still, and transitions are instant.
  await page.waitForTimeout(400)
  expect(await page.evaluate(() => document.getAnimations().filter((item) => item.playState === 'running').length)).toBe(0)
  const directions = await page.locator('.a-bidi-sample p[dir="auto"]').evaluateAll((items) => items.map((item) => getComputedStyle(item).direction))
  expect(directions).toEqual(['rtl', 'ltr', 'rtl'])
})

test('an unreachable backend is reported plainly, never as zero', async ({ page }) => {
  await openApp(page, { backend: 'offline', path: '/#/work' })
  await expect(page.locator('.a-conn')).toContainText('Backend unavailable')
  await expect(page.getByText('The Control Center backend isn’t answering')).toBeVisible()
  await expect(page.getByText('The task list couldn’t be loaded')).toBeVisible()
  expect(await page.locator('[role="treeitem"]').count()).toBe(0)
})

for (const width of [1440, 1180, 1024]) {
  test(`the shell fits ${width}px without overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 860 })
    await openApp(page, { path: '/#/settings' })
    await expect(page.locator('.atlas h1')).toHaveText('Settings')
    const overflow = await page.evaluate(() => ({
      page: document.documentElement.scrollWidth - window.innerWidth,
      topbar: document.querySelector('.a-topbar').scrollWidth - document.querySelector('.a-topbar').clientWidth,
    }))
    expect(overflow).toEqual({ page: 0, topbar: 0 })
  })
}

for (const theme of ['light', 'dark']) {
  test(`no serious accessibility violations in the ${theme} appearance`, async ({ page }) => {
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await openApp(page, { path: '/#/work' })
    const findings = []
    for (const route of ['work', 'settings', 'gallery', 'connections']) {
      await page.goto(`/#/${route}`)
      await expect(page.locator('.atlas h1')).toBeVisible()
      await page.waitForTimeout(150)
      await page.addScriptTag({ path: axePath })
      const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
      for (const violation of result.violations) {
        findings.push(`${route}: ${violation.id} (${violation.impact}) × ${violation.nodes.length} [${violation.nodes.map((node) => `${node.target.join(' ')}: ${(node.any[0]?.message || '').slice(0, 120)}`).join(' / ')}]`)
      }
    }
    console.log(`axe ${theme}: ${findings.length ? findings.join('; ') : 'no violations'}`)
    expect(findings.filter((line) => /\((serious|critical)\)/.test(line))).toEqual([])
  })
}

test('capture review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(120_000)
  await openApp(page)
  await page.screenshot({ path: join(dir, 'm1-00-classic-with-atlas-button.png') })
  const shots = [['work', 'light', 1440], ['work', 'dark', 1440], ['settings', 'light', 1440], ['settings', 'dark', 1440],
    ['gallery', 'light', 1440], ['gallery', 'dark', 1440], ['new', 'light', 1440], ['work', 'light', 1180], ['settings', 'dark', 1180],
    ['work', 'dark', 1024], ['gallery', 'light', 1024]]
  for (const [route, theme, width] of shots) {
    await page.setViewportSize({ width, height: 900 })
    await page.evaluate((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.goto(`/#/${route}`)
    await page.reload()
    await expect(page.locator('.atlas h1')).toBeVisible()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `m1-${route}-${theme}-${width}.png`) })
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.evaluate(() => localStorage.setItem('control-center.theme', 'light'))
  await page.goto('/#/gallery')
  await page.reload()
  await page.getByRole('button', { name: 'Inspector layout' }).click()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(dir, 'm1-gallery-inspector-light-1440.png') })
  await page.getByRole('button', { name: 'Open a dialog' }).click()
  await page.waitForTimeout(300)
  await page.screenshot({ path: join(dir, 'm1-gallery-dialog-light-1440.png') })
})
