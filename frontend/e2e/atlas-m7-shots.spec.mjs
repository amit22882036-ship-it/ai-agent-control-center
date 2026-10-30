import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }]
// A lead with a recorded plan, two rounds and a held proposal (the same shapes the real endpoints return).
const LEAD_DETAIL = ({ method, path }) => {
  if (method === 'GET' && path === '/tasks/csv/orchestration') {
    return { body: { task_id: 'csv', continuation: { phase: 'waiting', mode: 'hybrid', last_error: null, plan_consumed: 0 }, materializations: [],
      decision: { state: 'promoted', revision: 2, decision_key: 'k2', rationale: 'Parsing, the JSON CLI and the HTML report are independent; quoted fields go first because the others read them.',
        accepted_at: '2026-10-29T11:02:00Z', assignment_id: 'as-lead', superseded: null, promotion_rejected: false, source: 'planning', last_rejection: null,
        promotions: [{ proposal_id: 'parser', revision: 2, disposition: 'started', admission_disposition: 'ready', reason: null, child_task_id: 'quoted' },
          { proposal_id: 'cli', revision: 2, disposition: 'acknowledged', admission_disposition: 'ready', reason: null, child_task_id: 'cli' },
          { proposal_id: 'xml', revision: 2, disposition: 'blocked', admission_disposition: 'blocked', reason: null, child_task_id: null }] }, wake: { state: 'idle' } } }
  }
  return undefined
}

test('capture M7 review screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(240_000)
  const shot = async (name, hash, theme, { width = 1440, height = 900, tasks = showcase(), prepare = null, api = LEAD_DETAIL, full = false } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/${hash}`, tasks, projects: PROJECTS, api })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(600)
    if (prepare) await prepare()
    await page.waitForTimeout(600)
    await page.screenshot({ path: join(dir, `${name}.png`), fullPage: full })
    await page.unroute('**/*')
  }
  await shot('m7-01-task-lead-light', 'task?id=csv', 'light', { height: 1500 })
  await shot('m7-02-task-lead-team-map-light', 'task?id=csv', 'light', { prepare: async () => {
    await page.getByRole('radiogroup', { name: 'Show the team as' }).getByRole('radio', { name: 'Map' }).click()
    await page.locator('#t-team').scrollIntoViewIfNeeded()
  } })
  await shot('m7-03-task-helper-result-light', 'task?id=cli', 'light', { height: 1400 })
  await shot('m7-04-task-root-applied-dark', 'task?id=rename', 'dark', { height: 1300 })
  await shot('m7-05-task-question-dark', 'task?id=quoted', 'dark')
  await shot('m7-06-task-removed-owner-light', 'task?id=migrate', 'light', { height: 1200 })
  await shot('m7-07-pause-impact-light', 'task?id=csv', 'light', { prepare: async () => page.getByRole('button', { name: 'Pause…' }).click() })
  await shot('m7-08-cancel-impact-dark', 'task?id=csv', 'dark', { prepare: async () => page.getByRole('button', { name: 'Cancel task…' }).click() })
  await shot('m7-09-task-1180-light', 'task?id=csv', 'light', { width: 1180 })
  await shot('m7-10-task-1024-dark', 'task?id=report', 'dark', { width: 1024 })
  await shot('m7-11-task-deep-light', 'task?id=r001-c0-d2', 'light', { tasks: large({ families: 3, width: 6, depth: 6 }) })
  await shot('m7-12-work-open-task-light', 'work?task=cli', 'light', { prepare: async () => page.locator('[data-id="cli"]').first().hover() })
})
