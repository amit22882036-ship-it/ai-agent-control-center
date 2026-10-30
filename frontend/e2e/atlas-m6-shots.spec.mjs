import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }, { project_id: 'p2', name: 'Docs site', root_path: 'C:\\Work\\docs-site' }]

test('capture M6 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(240_000)
  const shot = async (name, hash, theme, { width = 1440, tasks = showcase(), prepare = null, clip = null } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/${hash}`, tasks, projects: PROJECTS })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(500)
    if (prepare) await prepare()
    await page.waitForTimeout(700)
    await page.screenshot({ path: join(dir, `${name}.png`), ...(clip ? { clip } : {}) })
    await page.unroute('**/*')
  }
  await shot('m6-01-work-list-light', 'work?task=quoted', 'light')
  await shot('m6-02-work-map-light', 'work?lens=map', 'light')
  await shot('m6-03-map-selected-inspector-light', 'work?lens=map&task=quoted', 'light')
  await shot('m6-04-map-with-list-light', 'work?lens=map&side=1&task=report', 'light')
  await shot('m6-05-map-dark', 'work?lens=map&task=cli', 'dark')
  await shot('m6-06-map-deep-dark', 'work?lens=map&task=r002-c0-d3', 'dark', { tasks: large({ families: 6, width: 7, depth: 5 }) })
  await shot('m6-07-map-many-families-light', 'work?lens=map', 'light', { tasks: large({ families: 30, width: 4 }),
    prepare: async () => page.getByRole('button', { name: 'Show everything' }).click() })
  await shot('m6-08-projects-light', 'projects', 'light')
  await shot('m6-09-project-map-light', 'work?lens=map&project=p1', 'light')
  await shot('m6-10-family-context-light', 'work?task=cli', 'light', { prepare: async () => {
    await page.getByRole('group', { name: 'Lead and helpers' }).scrollIntoViewIfNeeded()
  } })
  await shot('m6-15-list-map-controls-light', 'work?lens=map&side=1&task=report', 'light', { clip: { x: 0, y: 56, width: 1440, height: 110 } })
  await shot('m6-16-map-search-dim-dark', 'work?lens=map&task=report', 'dark', { prepare: async () => {
    await page.getByRole('searchbox', { name: 'Find in loaded tasks' }).fill('report')
  } })
  await shot('m6-11-map-focus-light', 'work?lens=map&focus=csv&task=cli', 'light')
  await shot('m6-12-map-1180-light', 'work?lens=map&side=1&task=quoted', 'light', { width: 1180 })
  await shot('m6-13-map-1024-dark', 'work?lens=map&task=quoted', 'dark', { width: 1024 })
  await shot('m6-14-projects-dark', 'projects', 'dark')
})
