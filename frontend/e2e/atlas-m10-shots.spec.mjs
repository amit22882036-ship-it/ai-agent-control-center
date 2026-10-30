import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')

test('capture M10 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(400_000)
  const shot = async (name, hash, theme, { width = 1440, height = 900, prepare = null } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/${hash}`, tasks: showcase() })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(700)
    if (prepare) await prepare()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  const bell = async () => { await page.locator('.a-bell').click() }
  for (const theme of ['light', 'dark']) {
    await shot(`m10-${theme}-01-work-list`, 'work?task=quoted', theme)
    await shot(`m10-${theme}-02-work-map`, 'work?lens=map&task=quoted', theme)
    await shot(`m10-${theme}-03-new-task`, 'new', theme)
    await shot(`m10-${theme}-04-projects`, 'projects', theme)
    await shot(`m10-${theme}-05-task-page`, 'task?id=csv', theme)
    await shot(`m10-${theme}-06-connections`, 'connections', theme)
    await shot(`m10-${theme}-07-connections-gemini`, 'connections?provider=gemini', theme)
    await shot(`m10-${theme}-08-settings`, 'settings', theme)
    await shot(`m10-${theme}-09-notifications`, 'work', theme, { prepare: bell })
  }
  await shot('m10-resp-1600-work', 'work?lens=map&side=1&task=quoted', 'light', { width: 1600, height: 960 })
  await shot('m10-resp-1024-work', 'work?task=quoted', 'light', { width: 1024, height: 768 })
  await shot('m10-resp-820-work', 'work?task=quoted', 'light', { width: 820, height: 900 })
  await shot('m10-zoom-200-new-task', 'new', 'light', { width: 640, height: 900 })
  await shot('m10-zoom-200-connections', 'connections?provider=claude', 'dark', { width: 640, height: 900 })
  await shot('m10-zoom-250-task-page', 'task?id=csv', 'light', { width: 512, height: 860 })
  await shot('m10-zoom-250-notifications', 'work', 'light', { width: 512, height: 860, prepare: bell })
  await shot('m10-zoom-400-work', 'work', 'light', { width: 320, height: 720 })
})
