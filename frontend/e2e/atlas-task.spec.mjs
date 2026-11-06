import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { announceChange, openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'C:\\Work\\release-planning' }]
const posts = (log, path) => log.requests.filter((item) => item.method !== 'GET' && item.path !== '/providers/check' && (!path || item.path === path))
const heading = (page) => page.locator('.atlas h1')

async function open(page, hash, options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/${hash}`, tasks: showcase(), projects: PROJECTS, ...options })
  return log
}

// The "Refactor the parser" variant: a paused task that owns its pause operation.
function withPaused({ owned = true } = {}) {
  const tasks = showcase()
  const limits = tasks.tasks.find((item) => item.task_id === 'limits')
  Object.assign(limits, { status: 'paused', control_intent: 'paused', waiting_context: null, agent: { ...limits.agent, status: 'stopped' },
    assignment: { current: null, last_ended: { ...limits.assignment.current, ended_at: '2026-10-29T11:50:00Z', ended_reason: 'paused' }, count: 1, previous_agent_ids: ['logs'] } })
  const api = ({ method, path }) => (method === 'GET' && path === '/tasks/limits/control-operations'
    ? { body: { operations: owned ? [{ operation_id: 'op-1', root_task_id: 'limits', action: 'pause', status: 'completed', released: 0,
      impact: { affected_task_ids: ['limits'] }, failures: [] }] : [] } } : undefined)
  return { tasks, api }
}

test('Open task from Work keeps the task identity, and Work returns as it was left', async ({ page }) => {
  await open(page, 'work?lens=map&task=cli')
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await inspector.getByRole('button', { name: 'Open the task page (O)' }).click()
  await expect(page).toHaveURL(/#\/task\?id=cli$/)
  await expect(heading(page)).toHaveText('JSON command-line interface')
  await expect(page.getByRole('navigation', { name: 'Where this task sits' })).toContainText('Build the CSV import pipeline')
  // The breadcrumb's Work link returns to the Map with this task selected.
  await page.getByRole('navigation', { name: 'Where this task sits' }).getByRole('button', { name: 'Work' }).click()
  await expect(page).toHaveURL(/lens=map/)
  await expect(page).toHaveURL(/task=cli/)
  // Back and Forward move between the pages.
  await page.goBack()
  await expect(heading(page)).toHaveText('JSON command-line interface')
  await page.goForward()
  await expect(page).toHaveURL(/#\/work\?/)
  await expect(page.getByRole('complementary', { name: 'Task details' })).toContainText('CLI writer')
  // The O key opens the selected task; a list row offers the same on hover.
  await page.locator('body').press('o')
  await expect(page).toHaveURL(/#\/task\?id=cli$/)
  await page.goBack()
  await page.getByRole('radiogroup', { name: 'View as' }).getByRole('radio', { name: 'List' }).click()
  const row = page.getByRole('tree', { name: 'Work hierarchy' }).locator('[data-id="report"]')
  await row.hover()
  await row.locator('.a-row-map[title="Open task"]').click()
  await expect(page).toHaveURL(/#\/task\?id=report$/)
})

test('a task page is about the task: current, earlier, removed and missing agents are shown as what they are', async ({ page }) => {
  await open(page, 'task?id=report')
  const agentCard = page.locator('#t-agent').locator('xpath=ancestor::section[1]')
  await expect(agentCard).toContainText('Report builder')
  await expect(agentCard.locator('.a-hist li')).toHaveCount(2)
  await expect(agentCard.locator('.a-hist li').first()).toContainText('working on it now')
  await expect(agentCard.locator('.a-hist li').nth(1)).toContainText('was removed')
  await expect(page.getByText('1 earlier agent worked on this task; it is the same task.')).toBeVisible()
  // A finished helper names its last agent as such; nobody is presented as working on it.
  await page.goto('/#/task?id=cli')
  await expect(heading(page)).toHaveText('JSON command-line interface')
  await expect(page.locator('.a-tmeta')).toContainText('Last agent: CLI writer')
  await expect(page.locator('#t-agent').locator('xpath=ancestor::section[1]')).toContainText('not working on it now')
  // A task without any agent, and one whose agent was removed.
  await page.goto('/#/task?id=backlog')
  await expect(page.locator('#t-agent').locator('xpath=ancestor::section[1]')).toContainText('Nobody is responsible right now')
  await expect(page.locator('#t-agent').locator('xpath=ancestor::section[1]')).toContainText('No agent has worked on this task yet.')
  await page.goto('/#/task?id=migrate')
  await expect(page.locator('.a-tbanner')).toContainText('Waiting for a new owner')
  await expect(page.locator('.a-tbanner').getByRole('button', { name: 'Open in Work' })).toBeVisible()
})

test('how the work is going: plan in the lead’s own words, planned parts that are not tasks, rounds and activity', async ({ page }) => {
  await open(page, 'task?id=csv', { api: ({ method, path }) => (method === 'GET' && path === '/tasks/csv/orchestration' ? { body: {
    task_id: 'csv', continuation: { phase: 'waiting', mode: 'hybrid', last_error: null, plan_consumed: 0 }, materializations: [],
    decision: { state: 'promoted', revision: 2, decision_key: 'k2', rationale: 'Quoted fields first; the others read them.', accepted_at: null, assignment_id: 'as-lead',
      superseded: null, promotion_rejected: false, source: 'planning', last_rejection: null,
      promotions: [{ proposal_id: 'parser', revision: 2, disposition: 'started', admission_disposition: 'ready', reason: null, child_task_id: 'quoted' },
        { proposal_id: 'xml', revision: 2, disposition: 'blocked', admission_disposition: 'blocked', reason: null, child_task_id: null }] }, wake: { state: 'idle' } } } : undefined) })
  const progress = page.locator('#t-progress').locator('xpath=ancestor::section[1]')
  await expect(progress).toContainText('The lead turned its plan into helper tasks.')
  await expect(progress.locator('.a-quote')).toContainText('In the lead’s words (not checked by the Control Center)')
  await expect(progress.locator('.a-quote')).toContainText('Quoted fields first; the others read them.')
  await expect(progress).toContainText('Planned part parser: Started')
  await expect(progress).toContainText('Planned part xml: Held')
  await expect(progress).toContainText('Rounds with helpers1')
  await expect(progress).toContainText('Waiting for its helpers to finish.')
  await expect(progress.locator('.a-tl-i')).toHaveCount(1)
  // A planned part that started links to its real task; the held one has no task to open.
  await progress.getByRole('button', { name: 'Open its task' }).click()
  await expect(page).toHaveURL(/#\/task\?id=quoted$/)
})

test('who’s on it: the team as a List and a Map from the same model, selection keeps the task identity', async ({ page }) => {
  await open(page, 'task?id=csv')
  const team = page.locator('#t-team').locator('xpath=ancestor::section[1]')
  await expect(team).toContainText('4 helper tasks below it: 1 working, 1 waiting, 2 finished. 1 more planned by the lead, not started (not tasks yet).')
  await expect(team.getByRole('tree', { name: 'Work hierarchy' }).getByRole('treeitem')).toHaveCount(5)
  await team.getByRole('radiogroup', { name: 'Show the team as' }).getByRole('radio', { name: 'Map' }).click()
  const map = team.getByRole('tree', { name: 'Work map' })
  await expect(map.getByRole('treeitem')).toHaveCount(5) // planned work is never drawn as a task
  await map.locator('[data-id="report"]').click()
  await expect(team.locator('.a-team-pick')).toContainText('HTML summary report')
  await team.locator('.a-team-pick').getByRole('button', { name: 'Open task' }).click()
  await expect(page).toHaveURL(/#\/task\?id=report$/)
  await expect(heading(page)).toHaveText('HTML summary report')
  await page.goBack()
  await expect(heading(page)).toHaveText('Build the CSV import pipeline')
  // Double-click opens a task from the map.
  await page.locator('#t-team').locator('xpath=ancestor::section[1]').getByRole('radiogroup', { name: 'Show the team as' }).getByRole('radio', { name: 'Map' }).click()
  await page.locator('.a-tteam [data-id="cli"]').dblclick()
  await expect(page).toHaveURL(/#\/task\?id=cli$/)
})

test('results keep reported, recorded, verified, combined and applied apart, and project apply is not offered', async ({ page }) => {
  await open(page, 'task?id=cli')
  const result = page.locator('#t-result').locator('xpath=ancestor::section[1]')
  await expect(result).toContainText('The helper reported: Completed.')
  await expect(result.locator('.a-quote')).toContainText('The helper’s summary (its own words)')
  await expect(result).toContainText('Not independently verified. The Control Center does not run your tests')
  await expect(result).toContainText('Combined into its lead’s copy')
  await expect(result).toContainText('Applied')
  await result.getByText('Changed files (2)').click()
  await expect(result).toContainText('src/exporter.py')
  await expect(result).not.toContainText('Applied to the project')
  await page.goto('/#/task?id=rename')
  const root = page.locator('#t-result').locator('xpath=ancestor::section[1]')
  await expect(root).toContainText('Applied to the project')
  await expect(root).toContainText('A lead’s own account of its work is in its output')
  // Already applied: nothing to offer.
  await expect(page.getByRole('button', { name: /apply/i })).toHaveCount(0)
})

test('coordination shows the resources and work a task holds, and a stuck claim can be released after confirming', async ({ page }) => {
  let released = 0
  const api = ({ method, path }) => {
    if (method === 'GET' && path === '/tasks/csv/resource-claims') {
      return { body: { claims: released ? [] : [{ claim_id: 'c1', task_id: 'csv', resource_type: 'file', resource_key: 'src/importer.py', scope: 'project', mode: 'exclusive',
        lifetime: 'task', recursive: 0, status: 'active', acquired_at: '2026-10-29T11:30:00Z' }] } }
    }
    if (method === 'GET' && path === '/tasks/csv/work-intents') return { body: { intents: [{ intent_id: 'i1', task_id: 'csv', namespace: 'feature', key: 'CSV import', mode: 'single_owner', status: 'active', holds_authority: 1 }] } }
    if (method === 'GET' && path === '/tasks/csv/deadlocks') return { body: { deadlocks: [{ deadlock_id: 'd1', status: 'open' }] } }
    if (method === 'DELETE' && path === '/tasks/csv/resource-claims/c1') { released += 1; return { body: { status: 'released' } } }
    return undefined
  }
  await open(page, 'task?id=csv', { api })
  const card = page.locator('#t-coord').locator('xpath=ancestor::section[1]')
  await expect(card).toContainText('src/importer.py')
  await expect(card).toContainText('exclusive')
  await expect(card).toContainText('CSV import')
  await expect(card).toContainText('A deadlock involves this task')
  await card.getByRole('button', { name: 'Release src/importer.py' }).click()
  expect(released).toBe(0)
  await card.getByRole('button', { name: 'Release', exact: true }).click()
  await expect.poll(() => released).toBe(1)
  await expect(card).not.toContainText('src/importer.py')
})

test('a finished root task is applied to its project only after its files are reviewed, and the backend’s refusals are shown', async ({ page }) => {
  const tasks = showcase()
  Object.assign(tasks.tasks.find((item) => item.task_id === 'rename'), { integration: null })
  let applied = 0
  let conflict = false
  const preview = { task_id: 'rename', destination_kind: 'project', destination_id: 'p1', changed_paths: ['src/settings/page.tsx', 'docs/settings.md'],
    changed_paths_remaining: 0, destination_changed_paths: ['README.md'], destination_changed_paths_remaining: 0 }
  const api = ({ method, path }) => {
    if (method === 'GET' && path === '/tasks/rename/integration-preview') return { body: preview }
    if (method === 'POST' && path === '/tasks/rename/integrate') {
      applied += 1
      return conflict ? { status: 409, body: { detail: { status: 'conflict', conflict_paths: ['docs/settings.md'], conflict_paths_remaining: 0 } } }
        : { body: { status: 'applied', destination_kind: 'project' } }
    }
    return undefined
  }
  await open(page, 'task?id=rename', { tasks, api })
  conflict = true
  await page.getByRole('button', { name: 'Review and apply…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Apply this task to its project' })
  await expect(dialog).toContainText('src/settings/page.tsx')
  await expect(dialog).toContainText('Files changed in the project meanwhile')
  await expect(dialog).toContainText('README.md')
  expect(applied).toBe(0)
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click()
  await expect(dialog.getByRole('alert')).toContainText('Nothing was applied')
  await expect(dialog).toContainText('Files in conflict')
  await expect(dialog.getByRole('button', { name: 'Apply', exact: true })).toBeDisabled()
  expect(applied).toBe(1)
  await dialog.getByRole('button', { name: 'Not now' }).click()
  conflict = false
  await page.getByRole('button', { name: 'Review and apply…' }).click()
  await dialog.getByRole('button', { name: 'Apply', exact: true }).click()
  await expect(dialog).toBeHidden()
  await expect(page.getByText('Applied. The task’s changes are now in the project folder.')).toBeVisible()
  expect(applied).toBe(2)
})

test('lifecycle controls appear only where the backend allows them, with the reason otherwise', async ({ page }) => {
  await open(page, 'task?id=cli')
  await expect(page.getByText('No lifecycle actions: Finished work can’t be paused, resumed or canceled.')).toBeVisible()
  await expect(page.getByRole('button', { name: /Pause|Cancel task|Resume/ })).toHaveCount(0)
  await page.goto('/#/task?id=csv')
  await expect(page.getByRole('button', { name: 'Pause…' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Cancel task…' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resume…' })).toHaveCount(0)
  // Paused by its own operation: Resume is offered; paused by a task above it: it is explained.
  const owned = withPaused()
  await open(page, 'task?id=limits', owned)
  await expect(page.getByRole('button', { name: 'Resume…' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Pause…' })).toHaveCount(0)
  const notOwned = withPaused({ owned: false })
  await open(page, 'task?id=limits', notOwned)
  await expect(page.getByRole('button', { name: 'Cancel task…' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resume…' })).toHaveCount(0)
})

test('Pause shows the backend’s impact, re-reads it before sending, and sends once', async ({ page }) => {
  const log = await open(page, 'task?id=csv')
  await page.getByRole('button', { name: 'Pause…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Pause this task' })
  await expect(dialog).toContainText('3 tasks will be paused: this one and 2 below it.')
  await expect(dialog).toContainText('2 running agents will be stopped.')
  // Proposed effects read as future effects, never as something that already happened.
  await expect(dialog).toContainText('If you confirm:')
  await expect(dialog).toContainText('Will stop: Atlas lead, Report builder.')
  await expect(dialog).not.toContainText('Stopped:')
  await dialog.getByRole('button', { name: 'Pause', exact: true }).evaluate((button) => { button.click(); button.click() })
  await expect(dialog).toBeHidden()
  await expect(page.getByRole('status').filter({ hasText: 'Paused. Nothing runs until it is resumed.' })).toBeVisible()
  expect(posts(log, '/tasks/csv/pause')).toHaveLength(1)
  expect(posts(log, '/tasks/csv/control-impact').map((item) => item.body)).toEqual([{ action: 'pause' }, { action: 'pause' }])
})

test('a changed impact is shown again instead of being sent, and Cancel needs an explicit acknowledgement', async ({ page }) => {
  let calls = 0
  const log = await open(page, 'task?id=csv', { api: ({ method, path }) => {
    if (method === 'POST' && path === '/tasks/csv/control-impact') {
      calls += 1
      const affected = calls === 1 ? ['csv', 'quoted', 'report'] : ['csv', 'quoted', 'report', 'cli']
      return { body: { target_task_id: 'csv', action: 'cancel', affected_task_ids: affected, descendant_task_ids: ['cli', 'cli-docs', 'quoted', 'report'],
        external_dependent_task_ids: [], workers_to_stop: [], completed_descendants: [], canceled_descendants: [], blocked_task_ids: [], replanning_task_ids: [] } }
    }
    return undefined
  } })
  await page.getByRole('button', { name: 'Cancel task…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Cancel this task' })
  await expect(dialog).toContainText('3 tasks will be canceled')
  const confirm = dialog.getByRole('button', { name: 'Cancel task', exact: true })
  await expect(confirm).toBeDisabled()
  await dialog.getByText('I understand that canceled work can’t be resumed.').click()
  await confirm.click()
  await expect(dialog).toContainText('What this affects changed since it was shown.')
  await expect(dialog).toContainText('4 tasks will be canceled')
  expect(posts(log, '/tasks/csv/cancel')).toHaveLength(0)
  const again = dialog.getByRole('button', { name: 'Cancel task with this new impact' })
  await expect(again).toBeDisabled() // the acknowledgement is asked again
  await dialog.getByText('I understand that canceled work can’t be resumed.').click()
  await again.click()
  await expect(dialog).toBeHidden()
  expect(posts(log, '/tasks/csv/cancel')).toHaveLength(1)
})

test('a lost connection is an unknown outcome and is never retried; a refusal is shown in the backend’s words', async ({ page }) => {
  const log = await open(page, 'task?id=csv', { api: ({ method, path }) => (method === 'POST' && path === '/tasks/csv/pause' ? { abort: true } : undefined) })
  await page.getByRole('button', { name: 'Pause…' }).click()
  await page.getByRole('dialog', { name: 'Pause this task' }).getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'may or may not have been paused' })).toBeVisible()
  await page.waitForTimeout(400)
  expect(posts(log, '/tasks/csv/pause')).toHaveLength(1)
  const paused = withPaused()
  await open(page, 'task?id=limits', { tasks: paused.tasks, api: (entry) => (entry.method === 'POST' && entry.path === '/tasks/limits/resume'
    ? { status: 409, body: { detail: 'A pause operation still has an unsettled worker' } } : paused.api(entry)) })
  await page.getByRole('button', { name: 'Resume…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Resume this task' })
  await expect(dialog).toContainText('This releases the pause for 1 task.')
  await dialog.getByRole('button', { name: 'Resume', exact: true }).click()
  await expect(dialog.getByRole('alert')).toHaveText('Nothing was changed. A pause operation still has an unsettled worker')
})

test('with the backend unreachable or the task state stale, lifecycle actions can’t be started; they return on reconnection', async ({ page }) => {
  let offline = false
  let staleFeed = false
  const log = await open(page, 'task?id=csv', {
    taskPages: (url) => (staleFeed ? null : (() => { const data = showcase(); const root = url.searchParams.get('root')
      const ids = new Set([root, ...data.tasks.filter((item) => item.parent_task_id === root).map((item) => item.task_id)])
      for (let grew = true; grew;) { grew = false; for (const item of data.tasks) if (ids.has(item.parent_task_id) && !ids.has(item.task_id)) { ids.add(item.task_id); grew = true } }
      return { ...data, tasks: data.tasks.filter((item) => ids.has(item.task_id)), ancestors: [] } })()),
    api: ({ method }) => (offline && method === 'GET' ? { abort: true } : undefined) })
  const pause = page.getByRole('button', { name: 'Pause…' })
  await expect(pause).toBeEnabled()
  // The task list can't be re-read: the information stays, the actions wait.
  staleFeed = true
  await announceChange(page)
  await expect(pause).toBeDisabled()
  await expect(page.getByText('The task’s latest state couldn’t be read, so these actions wait until it can.')).toBeVisible()
  await expect(page.locator('#t-request').locator('xpath=ancestor::section[1]')).toContainText('Synthetic description for Build the CSV import pipeline.')
  // The backend stops answering altogether.
  offline = true
  await announceChange(page)
  await expect(page.getByText('The backend isn’t reachable, so nothing can be changed right now.', { exact: false })).toBeVisible()
  await expect(pause).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Cancel task…' })).toBeDisabled()
  // It comes back: the actions return, and nothing was sent meanwhile.
  offline = false
  staleFeed = false
  await announceChange(page)
  await expect(pause).toBeEnabled()
  expect(posts(log)).toEqual([])
})

test('a backend failure after Pause is reported as possibly partial, never as done', async ({ page }) => {
  const log = await open(page, 'task?id=csv', { api: ({ method, path }) => (method === 'POST' && path === '/tasks/csv/pause'
    ? { status: 503, body: { detail: 'Task operation is unavailable. Please retry.' } } : undefined) })
  await page.getByRole('button', { name: 'Pause…' }).click()
  await page.getByRole('dialog', { name: 'Pause this task' }).getByRole('button', { name: 'Pause', exact: true }).click()
  await expect(page.getByRole('status').filter({ hasText: 'The backend couldn’t finish this.' })).toBeVisible()
  await expect(page.getByText('Paused. Nothing runs until it is resumed.')).toHaveCount(0)
  expect(posts(log, '/tasks/csv/pause')).toHaveLength(1)
})

test('live updates refresh the task page in place, a missing task is explained, and a lost backend is shown', async ({ page }) => {
  let changed = false
  let fail = false
  await open(page, 'task?id=quoted', { taskPages: (url) => {
    if (fail) return null
    const data = showcase()
    if (changed) {
      const quoted = data.tasks.find((item) => item.task_id === 'quoted')
      Object.assign(quoted, { status: 'in_progress', waiting_context: null, agent: { ...quoted.agent, status: 'running', asks_user: false } })
    }
    const root = url.searchParams.get('root')
    const ids = new Set(data.tasks.filter((item) => item.task_id === root || item.parent_task_id === root).map((item) => item.task_id))
    return { ...data, tasks: data.tasks.filter((item) => ids.has(item.task_id)), ancestors: data.tasks.filter((item) => item.task_id === 'csv') }
  } })
  await expect(page.locator('.a-tbanner')).toContainText('Asked you a question')
  changed = true
  await announceChange(page)
  await expect(page.locator('.a-tbanner')).toContainText('Working')
  fail = true
  await announceChange(page)
  await expect(page.getByText('Showing the last information received')).toBeVisible()
  await expect(heading(page)).toHaveText('Handle quoted fields and embedded newlines')
  await page.unroute('**/*')
  await open(page, 'task?id=never-existed', { api: ({ method, path }) => (method === 'GET' && path === '/tasks/never-existed' ? { status: 404, body: { detail: 'Task not found' } } : undefined) })
  await expect(heading(page)).toHaveText('Task not found')
  await expect(page.getByText('Nothing else was opened in its place.')).toBeVisible()
})

test('keyboard: the lifecycle dialog opens with Enter, closes with Escape and returns focus; mixed-direction titles keep their direction', async ({ page }) => {
  await open(page, 'task?id=csv')
  const pause = page.getByRole('button', { name: 'Pause…' })
  await pause.focus()
  await page.keyboard.press('Enter')
  const dialog = page.getByRole('dialog', { name: 'Pause this task' })
  await expect(dialog).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(pause).toBeFocused()
  await page.goto('/#/task?id=hebrew')
  await expect(heading(page)).toHaveText('לתרגם את מסך ההגדרות לעברית (Settings page)')
  expect(await heading(page).evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
})

test('the task page fits 1440, 1180 and 1024 in both appearances, with no serious accessibility violations, and honours reduced motion', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  for (const [width, theme] of [[1440, 'light'], [1180, 'dark'], [1024, 'light']]) {
    await page.setViewportSize({ width, height: 900 })
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    await open(page, 'task?id=csv')
    await expect(heading(page)).toHaveText('Build the CSV import pipeline')
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0)
    await page.locator('#t-team').locator('xpath=ancestor::section[1]').getByRole('radiogroup', { name: 'Show the team as' }).getByRole('radio', { name: 'Map' }).click()
    await page.getByRole('button', { name: 'Zoom in' }).click()
    expect(await page.locator('.a-tteam .a-minner').evaluate((element) => getComputedStyle(element).transitionDuration)).toBe('0s')
    await page.addScriptTag({ path: axePath })
    for (const state of ['page', 'dialog']) {
      if (state === 'dialog') {
        await page.getByRole('button', { name: 'Pause…' }).click()
        await expect(page.getByRole('dialog', { name: 'Pause this task' })).toContainText('will be paused')
        await page.waitForTimeout(300)
      }
      const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
      const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
      console.log(`axe task ${width} ${theme} ${state}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
      expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    }
    await page.unroute('**/*')
  }
})

test('a task with 600 helpers and deep chains stays responsive (measured)', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  const tasks = large({ families: 1, width: 600, depth: 8 })
  const started = Date.now()
  await open(page, 'task?id=r000', { tasks })
  await expect(heading(page)).toHaveText('Family 0 lead task with a reasonably long descriptive name')
  await expect(page.locator('#t-team').locator('xpath=ancestor::section[1]')).toContainText('helper tasks below it')
  const pageReady = Date.now() - started
  const team = page.locator('#t-team').locator('xpath=ancestor::section[1]')
  const mapStart = Date.now()
  await team.getByRole('radiogroup', { name: 'Show the team as' }).getByRole('radio', { name: 'Map' }).click()
  await expect(team.getByRole('tree', { name: 'Work map' }).getByRole('treeitem').first()).toBeVisible()
  const mapReady = Date.now() - mapStart
  const drawn = await team.getByRole('tree', { name: 'Work map' }).getByRole('treeitem').count()
  const navStart = Date.now()
  await page.goto('/#/task?id=r000-c5-d7')
  await expect(heading(page)).toHaveText('Level 9 under r000-c5')
  const deepReady = Date.now() - navStart
  // Its nine loaded ancestors and the task itself.
  await expect(page.getByRole('navigation', { name: 'Where this task sits' }).locator('.a-crumb')).toHaveCount(10)
  console.log(`task page with 1,000+ tasks below: page ${pageReady} ms, team map ${mapReady} ms (${drawn} nodes drawn), deep task ${deepReady} ms`)
  expect(pageReady).toBeLessThan(6000)
  expect(mapReady).toBeLessThan(4000)
})
