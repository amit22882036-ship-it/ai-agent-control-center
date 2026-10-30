import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }, { project_id: 'p2', name: 'Docs site', root_path: 'C:\\Work\\docs-site' }]

test('capture M5 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(180_000)
  const shot = async (name, hash, theme, prepare, width = 1440) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/${hash}`, tasks: showcase(), projects: PROJECTS })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(400)
    if (prepare) await prepare()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  const fill = async () => {
    await page.getByLabel('What should be done?').fill('Fix the flaky timeout in test_exporter.py and explain the cause.')
    await page.getByRole('radio', { name: /Claude Code/ }).check()
    await page.getByLabel('Model', { exact: true }).selectOption('Opus 4.8')
    await page.getByRole('switch', { name: 'Let the lead bring in helpers' }).click()
    await page.getByRole('radio', { name: /Also allow automatic choice/ }).check()
    await page.getByRole('checkbox', { name: /Codex/ }).check()
  }
  await shot('m5-new-empty-light', 'new', 'light', null)
  await shot('m5-new-filled-light', 'new?project=p2', 'light', fill)
  await shot('m5-new-filled-dark', 'new?project=p2', 'dark', fill)
  await shot('m5-new-1180-light', 'new?project=p2', 'light', fill, 1180)
  await shot('m5-new-1024-dark', 'new?project=p2', 'dark', fill, 1024)
  await shot('m5-new-project-picker-light', 'new', 'light', async () => page.getByRole('button', { name: /Project/ }).first().click())
  await shot('m5-add-project-dark', 'new', 'dark', async () => {
    await page.getByRole('button', { name: /Project/ }).first().click()
    await page.getByRole('button', { name: /Add a project…/ }).click()
  })
  await shot('m5-projects-light', 'projects', 'light', null)
  await shot('m5-projects-dark', 'projects', 'dark', null)
  await shot('m5-add-helper-light', 'work?task=report', 'light', async () => {
    await page.getByRole('complementary', { name: 'Task details' }).getByRole('button', { name: 'Add a helper…' }).click()
    await page.getByRole('dialog').getByLabel('What should the helper do?').fill('Add a totals row to the summary')
  })
  await shot('m5-start-again-dark', 'work?task=backlog', 'dark', async () => {
    await page.getByRole('complementary', { name: 'Task details' }).getByRole('button', { name: 'Start a new agent…' }).click()
  })
  await shot('m5-colour-filter-light', 'work?colors=green,none', 'light', async () => page.getByRole('button', { name: /Colours · 2/ }).click())
})
