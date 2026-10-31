import { test, expect } from '@playwright/test'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

// UAT pass 1: findings from the first Windows walkthrough, reproduced with synthetic data.

const row = (page, title) => page.getByRole('treeitem', { name: new RegExp(`^${title}`) })
// Live updates reported every 50 ms, as while agents write output, for as long as the test runs.
const busy = (page) => page.evaluate(() => {
  window.__busy = setInterval(() => (window.__sources || []).filter((source) => source.readyState !== 2)
    .forEach((source) => source.dispatchEvent(new Event('agent-change'))), 50)
})

test('project names arrive while live updates keep coming: a busy workspace no longer restarts the read forever', async ({ page }) => {
  let reads = 0
  await openApp(page, { path: '/#/work', tasks: showcase(), api: async ({ method, path }) => {
    if (method === 'GET' && path === '/projects') {
      reads += 1
      await new Promise((resolve) => setTimeout(resolve, 600))  // A backend busy with running agents.
    }
    return undefined
  } })
  await busy(page)
  await expect(row(page, 'Build the CSV import pipeline')).toBeVisible()
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning', { timeout: 4000 })
  await expect(row(page, 'Build the CSV import pipeline')).not.toContainText('Loading project')
  // Reads are coalesced: one follow-up per burst, not one per reported change.
  expect(reads).toBeLessThan(12)
})

test('a project lookup that fails says so honestly, and a later success replaces it; names survive refreshes and route changes', async ({ page }) => {
  let failing = true
  await openApp(page, { path: '/#/work', tasks: showcase(), api: ({ method, path }) => (method === 'GET' && path === '/projects' && failing
    ? { status: 503, body: { detail: 'unavailable' } } : undefined) })
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Project name unavailable')
  await expect(row(page, 'Build the CSV import pipeline')).not.toContainText('No project')
  failing = false
  await page.evaluate(() => (window.__sources || []).forEach((source) => source.dispatchEvent(new Event('agent-change'))))
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning')
  // A temporary failure later keeps the names already known.
  failing = true
  await page.evaluate(() => (window.__sources || []).forEach((source) => source.dispatchEvent(new Event('agent-change'))))
  await page.waitForTimeout(500)
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning')
  failing = false
  await page.locator('.a-nav').getByRole('link', { name: 'Projects' }).click()
  await page.getByRole('button', { name: 'Show its work' }).first().click()
  await expect(page).toHaveURL(/project=p1/)
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning')
})

test('changes reported during a slow read are coalesced into one more read, which brings the latest state, and nothing reads after the burst', async ({ page }) => {
  const projects = [{ project_id: 'p1', name: 'Release planning', root_path: 'fixture' }]
  let reads = 0
  await openApp(page, { path: '/#/work', tasks: showcase(), projects, api: async ({ method, path }) => {
    if (method === 'GET' && path === '/projects') {
      reads += 1
      await new Promise((resolve) => setTimeout(resolve, 500))
    }
    return undefined
  } })
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning')
  const before = reads
  // The project is renamed, then twenty changes are reported within a single slow read.
  projects[0].name = 'Release planning (renamed)'
  await page.evaluate(() => new Promise((resolve) => {
    let sent = 0
    const timer = setInterval(() => {
      (window.__sources || []).filter((source) => source.readyState !== 2).forEach((source) => source.dispatchEvent(new Event('agent-change')))
      if (++sent === 20) { clearInterval(timer); resolve() }
    }, 15)
  }))
  await expect(row(page, 'Build the CSV import pipeline')).toContainText('Release planning (renamed)')
  // At most the read in progress and one follow-up, however many changes arrived.
  expect(reads - before).toBeLessThanOrEqual(2)
  const settled = reads
  await page.waitForTimeout(1200)
  expect(reads).toBe(settled)
})

test('a slow answer for the task left behind never replaces the task now open', async ({ page }) => {
  await openApp(page, { path: '/#/task?id=cli', tasks: showcase(), api: async ({ method, path }) => {
    if (method === 'GET' && path === '/tasks/cli') await new Promise((resolve) => setTimeout(resolve, 900))
    return undefined
  } })
  await page.evaluate(() => { window.location.hash = '#/task?id=backlog' })
  await expect(page.locator('#t-request-text')).toContainText('Synthetic description for Profile the slow dashboard load')
  await page.waitForTimeout(1300)
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Profile the slow dashboard load')
  await expect(page.locator('#t-request-text')).not.toContainText('Document the CLI flags')
})

test('picking a task near the top from the list beside the map brings it back without a large empty area above the graph', async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 })
  // The camera starts far down the graph, on the last family.
  await openApp(page, { path: '/#/work?lens=map&side=1&task=rename', tasks: showcase() })
  const graphTop = () => page.evaluate(() => {
    const pane = document.querySelector('.a-mappane')?.getBoundingClientRect()
    if (!pane) return Number.NaN
    return Math.min(...[...document.querySelectorAll('.a-mzone, .a-mband')].map((item) => item.getBoundingClientRect().top)) - pane.top
  })
  await expect.poll(graphTop).toBeLessThan(0)
  await page.locator('[data-side-list]').getByRole('treeitem', { name: /^Build the CSV import pipeline/ }).click()
  await expect(page).toHaveURL(/task=csv/)
  // Before the fix the selected lead was centred, leaving about a third of the pane (some 350 px)
  // empty above the graph. Now the graph's top comes to the usual top padding.
  await expect.poll(graphTop).toBeGreaterThan(-40)
  await page.waitForTimeout(400)
  expect(await graphTop()).toBeLessThan(80)
  await expect(page.locator('.a-mappane').getByText('Build the CSV import pipeline').first()).toBeInViewport()
})

// A not-started task that was worked on before (its agent stopped), with a long request.
const LONG = Array.from({ length: 40 }, (_, index) => `${index + 1}. Step ${index + 1}: check the importer handles case ${index + 1} and write a test.`).join('\n')
function withHistory() {
  const data = showcase()
  const backlog = data.tasks.find((item) => item.task_id === 'backlog')
  backlog.assignment = { current: null, last_ended: { assignment_id: 'as-old', agent_id: 'old', assigned_at: '2026-10-29T10:00:00.000Z',
    ended_at: '2026-10-29T10:30:00.000Z', ended_reason: 'stopped' }, count: 1, previous_agent_ids: ['old'] }
  backlog.agent = { agent_id: 'old', role: 'last', status: 'stopped', display_name: 'Profiler', display_color: 'blue', agent_type: 'claude', model: 'Sonnet 4.6',
    effort: 'medium', asks_user: false, provider_condition: null, autonomous: false, removed: false, removal: null }
  backlog.description = LONG
  return data
}

test('open work is called "Open", and a task whose agent stopped reads "Not running", in the List, the Map and its page', async ({ page }) => {
  await openApp(page, { path: '/#/work', tasks: withHistory() })
  await expect(page.locator('.a-ol-sec')).toHaveText([/Needs your attention/, /Open/, /Completed and history/])
  await expect(page.getByRole('radio', { name: /^Open/ })).toBeVisible()
  await expect(row(page, 'Profile the slow dashboard load')).toContainText('Not running')
  await expect(row(page, 'Profile the slow dashboard load')).not.toContainText('Not started')
  await page.keyboard.press('m')
  await expect(page.locator('[data-id="backlog"]')).toContainText('Not running')
  await page.goto('about:blank')
  await openApp(page, { path: '/#/task?id=backlog', tasks: withHistory() })
  await expect(page.locator('.a-tbanner')).toContainText('Not running')
  // The backend allows pausing work that isn't running; the preview says no agent will stop.
  await page.getByRole('button', { name: 'Pause…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Pause this task' })
  await expect(dialog).toContainText('keeps this task and the unfinished tasks below it from being started')
  await expect(dialog).toContainText('No agent is running here now, so none is stopped.')
  await dialog.getByRole('button', { name: 'Keep it as it is' }).click()
})

test('a long request starts folded, keeps every line in the page, and opens with the keyboard', async ({ page }) => {
  await openApp(page, { path: '/#/task?id=backlog', tasks: withHistory() })
  const text = page.locator('#t-request-text')
  await expect(text).toHaveText(LONG, { useInnerText: false })
  const box = await text.boundingBox()
  expect(box.height).toBeLessThan(260)
  const toggle = page.getByRole('button', { name: /Show the whole request \(40 lines/ })
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await toggle.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: 'Show less' })).toHaveAttribute('aria-expanded', 'true')
  expect((await text.boundingBox()).height).toBeGreaterThan(600)
  // Jump straight to the result from the top of the page.
  await page.getByRole('navigation', { name: 'On this page' }).getByRole('button', { name: 'Result' }).click()
  await expect(page.locator('#t-result')).toBeFocused()
})

test('the lead’s explanation of its plan and a helper’s summary are never cut: long ones fold and open in full', async ({ page }) => {
  const rationale = Array.from({ length: 30 }, (_, index) => `Reason ${index + 1}: the importer parts depend on each other in this order, so this part goes now.`).join('\n')
  const summary = Array.from({ length: 25 }, (_, index) => `Finding ${index + 1}: the flag is documented with an example and a note about its default.`).join('\n')
  const data = showcase()
  data.tasks.find((item) => item.task_id === 'cli').delegation.result.summary = summary
  await openApp(page, { path: '/#/task?id=csv', tasks: data, api: ({ method, path }) => (method === 'GET' && path === '/tasks/csv/orchestration' ? { body: {
    task_id: 'csv', continuation: { phase: 'waiting', mode: 'hybrid', last_error: null, plan_consumed: 0 }, materializations: [],
    decision: { state: 'promoted', revision: 2, decision_key: 'k2', rationale, accepted_at: null, assignment_id: 'as-lead', superseded: null,
      promotion_rejected: false, source: 'planning', last_rejection: null, promotions: [] }, wake: { state: 'idle' } } } : undefined) })
  const explanation = page.locator('#t-plan-rationale')
  await expect(explanation).toHaveClass(/a-request-folded/)
  expect(await explanation.textContent()).toBe(rationale)  // Every character is in the page.
  const more = page.getByRole('button', { name: `Show the whole explanation (30 lines, ${rationale.length.toLocaleString('en-US')} characters)` })
  await expect(more).toHaveAttribute('aria-expanded', 'false')
  await more.focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('[aria-controls="t-plan-rationale"]')).toHaveAttribute('aria-expanded', 'true')
  await expect(explanation).not.toHaveClass(/a-request-folded/)

  await page.evaluate(() => { window.location.hash = '#/task?id=cli' })
  const helper = page.locator('#t-helper-summary')
  await expect(helper).toHaveClass(/a-request-folded/)
  expect(await helper.textContent()).toBe(summary)
  await page.getByRole('button', { name: /^Show the whole summary \(25 lines/ }).click()
  await expect(helper).not.toHaveClass(/a-request-folded/)
  await expect(page.getByRole('button', { name: 'Show less' })).toBeVisible()
})

test('the global map keeps finished families visible but quieter, and its tools no longer cover the first band', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await openApp(page, { path: '/#/work?lens=map', tasks: showcase() })
  await expect(page.locator('.a-mappane')).toBeVisible()
  await expect(page.locator('[data-id="rename"]')).toHaveClass(/is-historic/)
  await expect(page.locator('[data-id="csv"]')).not.toHaveClass(/is-historic/)
  const band = await page.locator('.a-mband').first().boundingBox()
  const tools = await page.locator('.a-mtool').boundingBox()
  const overlaps = band.x < tools.x + tools.width && tools.x < band.x + band.width && band.y < tools.y + tools.height && tools.y < band.y + band.height
  expect(overlaps).toBe(false)
  await page.locator('[data-id="rename"]').click()
  await expect(page).toHaveURL(/task=rename/)
  await page.mouse.move(5, 5)
  await expect.poll(() => page.locator('[data-id="rename"]').evaluate((element) => getComputedStyle(element).opacity)).toBe('1')
})

test('Settings keeps the notification preference up front and the coordination rules one click away', async ({ page }) => {
  await openApp(page, { path: '/#/settings?section=notifications', tasks: showcase() })
  const section = page.locator('#atlas-settings-notifications')
  await expect(section.getByRole('switch', { name: 'Control Center notifications' })).toBeVisible()
  const more = section.locator('details.a-settings-more')
  await expect(more).not.toHaveAttribute('open', '')
  await expect(section.getByText('One at a time')).toBeHidden()
  await more.locator('summary').focus()
  await page.keyboard.press('Enter')
  await expect(section.getByText('One at a time')).toBeVisible()
  await expect(section).toContainText('Another browser or browser profile open at the same time is not coordinated')
})
