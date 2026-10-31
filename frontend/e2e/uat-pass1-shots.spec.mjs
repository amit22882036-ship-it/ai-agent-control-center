import { test, expect } from '@playwright/test'
import { join } from 'node:path'
import { openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const LONG = Array.from({ length: 40 }, (_, index) => `${index + 1}. Step ${index + 1}: check the importer handles case ${index + 1} and write a test.`).join('\n')

function workspace() {
  const data = showcase()
  const backlog = data.tasks.find((item) => item.task_id === 'backlog')
  backlog.assignment = { current: null, last_ended: { assignment_id: 'as-old', agent_id: 'old', assigned_at: '2026-10-29T10:00:00.000Z',
    ended_at: '2026-10-29T10:30:00.000Z', ended_reason: 'stopped' }, count: 1, previous_agent_ids: ['old'] }
  backlog.agent = { agent_id: 'old', role: 'last', status: 'stopped', display_name: 'Profiler', display_color: 'blue', agent_type: 'claude', model: 'Sonnet 4.6',
    effort: 'medium', asks_user: false, provider_condition: null, autonomous: false, removed: false, removal: null }
  backlog.description = LONG
  const extra = large({ families: 24, width: 3 })
  for (const item of extra.tasks) if (!item.parent_task_id) item.status = Number(item.task_id.slice(1)) % 3 ? 'completed' : item.status
  data.tasks.push(...extra.tasks)
  data.total = data.tasks.length
  return data
}

test('capture UAT pass 1 screenshots', async ({ page }) => {
  const dir = process.env.ATLAS_SCREENSHOTS
  test.skip(!dir, 'Set ATLAS_SCREENSHOTS to a folder to capture screenshots.')
  test.setTimeout(300_000)
  const shot = async (name, hash, theme, { width = 1440, height = 900, prepare = null } = {}) => {
    await page.goto('about:blank')
    await page.setViewportSize({ width, height })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await page.clock.setFixedTime(NOW)
    await openApp(page, { path: `/#/${hash}`, tasks: workspace() })
    await expect(page.locator('.atlas h1').first()).toBeAttached()
    await page.waitForTimeout(900)
    if (prepare) await prepare()
    await page.waitForTimeout(400)
    await page.screenshot({ path: join(dir, `${name}.png`) })
    await page.unroute('**/*')
  }
  for (const theme of ['light', 'dark']) {
    await shot(`p1-${theme}-01-work-list`, 'work', theme)
    await shot(`p1-${theme}-02-project-map`, 'work?lens=map&project=p1&focus=csv', theme)
    await shot(`p1-${theme}-03-global-map`, 'work?lens=map', theme)
    await shot(`p1-${theme}-04-map-side-list`, 'work?lens=map&side=1', theme)
    await shot(`p1-${theme}-05-not-running-task`, 'task?id=backlog', theme)
    await shot(`p1-${theme}-06-pause-preview`, 'task?id=backlog', theme, { prepare: async () => {
      await page.getByRole('button', { name: 'Pause…' }).click()
      await expect(page.getByRole('dialog', { name: 'Pause this task' })).toContainText('No agent is running here now')
    } })
    await shot(`p1-${theme}-07-connections-1920`, 'connections?provider=claude', theme, { width: 1920, height: 1080 })
    await shot(`p1-${theme}-08-settings-notifications-1920`, 'settings?section=notifications', theme, { width: 1920, height: 1080 })
  }
  await shot('p1-light-09-long-request-open', 'task?id=backlog', 'light', { prepare: async () => {
    await page.getByRole('button', { name: /Show the whole request/ }).click()
  } })
  await shot('p1-light-10-work-1024', 'work?task=backlog', 'light', { width: 1024, height: 768 })
})
