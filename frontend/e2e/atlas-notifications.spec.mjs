import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { announceChange, openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const posts = (log, path) => log.requests.filter((item) => item.method !== 'GET' && (!path || item.path === path))
const beats = (log) => posts(log, '/notifications/heartbeat').length
const bell = (page) => page.locator('.a-bell')
const panel = (page) => page.getByRole('dialog', { name: 'Notifications' })
const answer = (page) => page.getByRole('complementary', { name: 'Task details' }).getByLabel('Your answer')
const row = (page, title) => page.getByRole('treeitem', { name: new RegExp(`^${title}`) })

const UPDATE = { id: 'update:claude:2.2.0', provider: 'claude', kind: 'update', label: 'Claude Code', installed_version: '2.1.300',
  available_version: '2.2.0', channel: 'latest', manual: false, checked_at: '2026-10-29T11:00:00Z', stale: false }

async function open(page, hash, options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  return openApp(page, { path: `/#/${hash}`, tasks: showcase(), ...options })
}

// The browser's notification API as a person who allowed (or blocked) it would have it. The test
// origin is not a secure context, so the real API is stood in for; shown notifications are kept.
function stubNotifications(page, { permission = 'granted', enabled = true } = {}) {
  return page.addInitScript(({ permission, enabled }) => {
    window.__shown = []
    window.Notification = class {
      static permission = permission
      static requestPermission() { return Promise.resolve(permission) }
      constructor(title, options) { this.title = title; this.options = options; window.__shown.push(this) }
      close() { this.closed = true }
    }
    localStorage.setItem('ai-agent-control-center.notifications', enabled ? 'enabled' : 'disabled')
  }, { permission, enabled })
}

// The showcase with its question not asked yet; `ask()` makes the agent ask it.
function scripted() {
  let asking = false
  const data = () => {
    const tasks = showcase()
    const quoted = tasks.tasks.find((item) => item.task_id === 'quoted')
    if (!asking) {
      quoted.agent = { ...quoted.agent, status: 'running', asks_user: false }
      quoted.waiting_context = null
      quoted.status = 'in_progress'
    }
    return tasks
  }
  return { taskPages: () => data(), ask: () => { asking = true } }
}

test('the bell lists what needs you now, opens with the keyboard, and Escape returns focus to it', async ({ page }) => {
  await open(page, 'settings')
  await expect(bell(page)).toHaveAccessibleName('Notifications: 1 waiting for your reply, 1 other task needs you')
  await bell(page).focus()
  await page.keyboard.press('Enter')
  await expect(panel(page)).toBeVisible()
  await expect(panel(page).getByRole('heading', { name: 'Notifications' })).toBeFocused()
  const needs = panel(page).getByRole('region', { name: 'Needs you' })
  await expect(needs.getByRole('listitem')).toHaveCount(2)
  await expect(needs.getByRole('listitem').first()).toContainText('Handle quoted fields and embedded newlines')
  await expect(needs.getByRole('listitem').first()).toContainText('Waiting for your reply · Quoted fields')
  await expect(needs.getByRole('listitem').nth(1)).toContainText('Waiting for a new owner')
  await expect(panel(page).getByRole('region', { name: 'Tool updates' })).toContainText('No updates to review.')
  await page.keyboard.press('Escape')
  await expect(panel(page)).toHaveCount(0)
  await expect(bell(page)).toBeFocused()
  // Reply from the panel opens the task in Work with the cursor in its answer.
  await bell(page).click()
  await panel(page).getByRole('button', { name: 'Reply' }).click()
  await expect(page).toHaveURL(/#\/work\?task=quoted$/)
  await expect(answer(page)).toBeFocused()
})

test('provider updates: review opens setup without installing; remind later and dismiss are per version', async ({ page }) => {
  let notices = [UPDATE, { ...UPDATE, id: 'update:codex:0.9.0', provider: 'codex', label: 'Codex', available_version: '0.9.0', installed_version: '0.8.1', stale: true }]
  const log = await open(page, 'work', { notices: () => notices, api: ({ method, path, body }) => {
    if (method === 'POST' && path === '/providers/claude/notifications/dismiss') { notices = notices.filter((item) => item.provider !== 'claude' || item.available_version !== body.version); return { body: notices } }
    if (method === 'POST' && path === '/providers/codex/notifications/dismiss') return { status: 422, body: { detail: 'Invalid version' } }
    return undefined
  } })
  await expect(bell(page)).toHaveAccessibleName(/2 tool updates available/)
  await bell(page).click()
  const updates = panel(page).getByRole('region', { name: 'Tool updates' })
  await expect(updates).toContainText('Claude Code 2.2.0 is available (installed 2.1.300, latest channel).')
  await expect(updates).toContainText('updating is optional and reviewed first')
  await expect(updates.getByRole('listitem').nth(1)).toContainText('may be out of date')
  await updates.getByRole('listitem').first().getByRole('button', { name: 'Remind me later' }).click()
  await expect.poll(() => posts(log, '/providers/claude/notifications/dismiss').map((item) => item.body)).toEqual([{ version: '2.2.0', mode: 'remind_later' }])
  await expect(updates.getByRole('listitem')).toHaveCount(1)
  await expect(updates).toContainText('Claude Code: reminding you again tomorrow.')
  // A refusal is shown in the backend's words and the notice stays.
  await updates.getByRole('button', { name: 'Dismiss' }).click()
  await expect(updates).toContainText('Nothing was changed. Invalid version')
  await expect(updates.getByRole('listitem')).toHaveCount(1)
  // Review update opens that tool's setup in Connections and installs nothing.
  await updates.getByRole('button', { name: 'Review update' }).click()
  await expect(page).toHaveURL(/#\/connections\?provider=codex&section=setup$/)
  expect(posts(log).filter((item) => item.path.includes('/operations'))).toEqual([])
})

test('a lost connection while dismissing is an unknown outcome, and unreadable notices are said to be', async ({ page }) => {
  const log = await open(page, 'work', { notices: [UPDATE], api: ({ method, path }) => (method === 'POST' && path.endsWith('/notifications/dismiss') ? { abort: true } : undefined) })
  await bell(page).click()
  await panel(page).getByRole('button', { name: 'Dismiss' }).click()
  await expect(panel(page)).toContainText('It may or may not have been saved. The notices are read again.')
  expect(posts(log, '/providers/claude/notifications/dismiss')).toHaveLength(1)
  await open(page, 'work', { notices: null })
  await bell(page).click()
  await expect(panel(page).getByRole('region', { name: 'Tool updates' })).toContainText('Update notices couldn’t be read.')
})

test('"need you" goes to the next question, keeping the Work view unless it would hide it', async ({ page }) => {
  await open(page, 'settings')
  await expect(page.locator('.a-bell')).toHaveAccessibleName(/1 waiting for your reply/)
  await expect(page.locator('.a-bell')).not.toHaveAccessibleName(/isn’t known yet/)
  await page.getByRole('button', { name: '1 needs you. Go to the next one' }).click()
  await expect(page).toHaveURL(/#\/work\?task=quoted$/)
  await expect(answer(page)).toBeFocused()
  // From a Work view that hides it (the Done filter), the filter is cleared and nothing else.
  await open(page, 'work?filter=done&lens=map')
  await expect(page.locator('.a-mappane')).toBeVisible()  // The task list has been read.
  await page.getByRole('button', { name: '1 needs you. Go to the next one' }).click()
  await expect(page).toHaveURL(/#\/work\?lens=map&task=quoted$/)
})

test('Work moves between the tasks that need you in the List’s order, in both views, with A and Shift A', async ({ page }) => {
  await open(page, 'work')
  const bar = page.getByRole('navigation', { name: 'Tasks that need you' })
  await expect(bar).toContainText('2 tasks need you')
  await bar.getByRole('button', { name: 'Go to the first task that needs you' }).click()
  await expect(page).toHaveURL(/task=quoted/)
  await expect(bar).toContainText('1 of 2 that need you · Waiting for your reply')
  await bar.getByRole('button', { name: 'Next task that needs you' }).click()
  await expect(page).toHaveURL(/task=migrate/)
  await expect(bar).toContainText('2 of 2 that need you · Waiting for a new owner')
  await expect(bar.getByRole('button', { name: 'Reply' })).toHaveCount(0)
  await page.locator('.a-whead').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('a')
  await expect(page).toHaveURL(/task=quoted/)
  await page.keyboard.press('Shift+A')
  await expect(page).toHaveURL(/task=migrate/)
  // Back returns to the previous one: each move is a navigation.
  await page.goBack()
  await expect(page).toHaveURL(/task=quoted/)
  // The Map follows the same selection and the same order.
  await page.keyboard.press('m')
  await expect(page).toHaveURL(/lens=map/)
  await page.keyboard.press('a')
  await expect(page).toHaveURL(/lens=map.*task=migrate|task=migrate.*lens=map/)
  await expect(page.locator('.a-mnode.is-selected, [data-id="migrate"][aria-selected="true"]').first()).toBeVisible()
  // A filter that shows no task that needs you hides the bar.
  await open(page, 'work?filter=done')
  await expect(page.getByRole('navigation', { name: 'Tasks that need you' })).toHaveCount(0)
})

test('reply from a row or with R puts the cursor in the answer, and a draft survives moving away and back', async ({ page }) => {
  await open(page, 'work')
  await row(page, 'Handle quoted fields').hover()
  await row(page, 'Handle quoted fields').locator('.a-row-reply').click()
  await expect(page).toHaveURL(/task=quoted/)
  await expect(answer(page)).toBeFocused()
  await answer(page).fill('Keep them; escape inside quotes.')
  // Only rows waiting for a reply have it.
  await row(page, 'Migrate the settings database').hover()
  await expect(row(page, 'Migrate the settings database').locator('.a-row-reply')).toHaveCount(0)
  await row(page, 'Migrate the settings database').click()
  await expect(page).toHaveURL(/task=migrate/)
  await row(page, 'Handle quoted fields').click()
  await expect(answer(page)).toHaveValue('Keep them; escape inside quotes.')
  await page.getByRole('complementary', { name: 'Task details' }).getByRole('tab', { name: 'Activity' }).click()
  await page.locator('.a-whead').click({ position: { x: 5, y: 5 } })
  await page.keyboard.press('r')
  await expect(answer(page)).toBeFocused()
  await expect(answer(page)).toHaveValue('Keep them; escape inside quotes.')
  // Typing in the answer is never interrupted by a refresh.
  await answer(page).press('End')
  await page.keyboard.type(' Thanks')
  await announceChange(page)
  await page.waitForTimeout(300)
  await expect(answer(page)).toBeFocused()
  await expect(answer(page)).toHaveValue('Keep them; escape inside quotes. Thanks')
})

test('a reply whose outcome is unknown keeps the text and says so; a second send is one request', async ({ page }) => {
  let count = 0
  const log = await open(page, 'work?task=quoted', { api: ({ method, path }) => {
    if (method === 'POST' && path === '/agents/quoter/reply') { count += 1; return count === 1 ? { abort: true } : { body: { status: 'running' } } }
    return undefined
  } })
  await answer(page).fill('Keep them.')
  await page.getByRole('button', { name: 'Send reply' }).click()
  await expect(page.getByRole('complementary', { name: 'Task details' })).toContainText('The reply may or may not have reached the agent. Your text is kept')
  await expect(answer(page)).toHaveValue('Keep them.')
  expect(posts(log, '/agents/quoter/reply')).toHaveLength(1)
})

test('when not every task is loaded, the counts say what they cover and where the rest may be', async ({ page }) => {
  const all = showcase()
  const first = { ...all, tasks: all.tasks.filter((item) => !['csv', 'quoted', 'cli', 'cli-docs', 'report'].includes(item.task_id)), next_after: 'older', total: 11 }
  await open(page, 'work', { taskPages: (url) => (url.searchParams.get('after') ? { ...all, tasks: all.tasks.filter((item) => ['csv', 'quoted', 'cli', 'cli-docs', 'report'].includes(item.task_id)), next_after: null } : first) })
  const bar = page.getByRole('navigation', { name: 'Tasks that need you' })
  await expect(bar).toContainText('1 task needs you among loaded tasks')
  await expect(bar).toContainText('1 more waiting for a reply may be on older pages')
  await bell(page).click()
  await expect(panel(page)).toContainText('The backend counts 1 waiting for a reply; 1 may be on older pages that aren’t loaded.')
  await page.keyboard.press('Escape')
  await bar.getByRole('button', { name: 'Look on older pages' }).click()
  await expect(bar).toContainText('2 tasks need you')
  await expect(bar).not.toContainText('older pages')
})

test('backend unavailable: the panel says so, keeps the last answer and offers no update actions', async ({ page }) => {
  let online = true
  await open(page, 'work', { notices: () => (online ? [UPDATE] : null), api: () => (online ? undefined : { status: 503, body: { detail: 'unavailable' } }) })
  await expect(bell(page)).toHaveAccessibleName(/1 tool update available/)
  online = false
  await announceChange(page)
  await expect(page.locator('.a-conn')).toContainText('Backend unavailable')
  await bell(page).click()
  await expect(panel(page)).toContainText('The backend isn’t answering.')
  await expect(panel(page).getByRole('button', { name: 'Dismiss' })).toBeDisabled()
  await expect(panel(page).getByRole('button', { name: 'Remind me later' })).toBeDisabled()
  await expect(panel(page)).toContainText('These are from the last answer; the latest read failed.')
  await expect(panel(page)).toContainText('From the last task list received.')
  // Unreachable from the start: nothing is claimed about what needs you.
  await open(page, 'work', { backend: 'offline' })
  await expect(bell(page)).toHaveAccessibleName('Notifications: what needs you isn’t known yet')
  await bell(page).click()
  await expect(panel(page)).toContainText('Not known yet: the task list hasn’t been read because the backend isn’t answering.')
  await expect(panel(page)).not.toContainText('Nothing needs you')
})

test('browser notifications: one per transition, tagged, with the heartbeat, and a click opens the task', async ({ page }) => {
  await stubNotifications(page)
  await stubLocks(page, true)
  const script = scripted()
  const log = await open(page, 'settings?section=notifications', { taskPages: script.taskPages })
  await expect(page.getByText('As browser notifications from this page')).toBeVisible()
  await expect.poll(() => beats(log)).toBeGreaterThan(0)
  expect(await page.evaluate(() => window.__shown.length)).toBe(0)  // Nothing already true is replayed.
  script.ask()
  await announceChange(page)
  await expect.poll(() => page.evaluate(() => window.__shown.map((item) => [item.title, item.options.body, item.options.tag])))
    .toEqual([['Agent needs your input', 'Handle quoted fields and embedded newlines', 'control-center:quoter:waiting:user']])
  await expect(page.locator('.a-app > [role="status"][aria-live="polite"]')).toHaveText('Quoted fields on “Handle quoted fields and embedded newlines” needs your reply.')
  // The same state reported again (a repeated event, a reconnect) shows nothing new.
  await announceChange(page)
  await page.waitForTimeout(400)
  expect(await page.evaluate(() => window.__shown.length)).toBe(1)
  await page.evaluate(() => window.__shown[0].onclick())
  await expect(page).toHaveURL(/#\/work\?task=quoted$/)
  expect(await page.evaluate(() => window.__shown[0].closed)).toBe(true)
})

test('no browser notification and no heartbeat when the preference is off, the browser blocks them, or not every task is loaded', async ({ page }) => {
  for (const [label, options, extra] of [['off', { enabled: false }, {}], ['blocked', { permission: 'denied' }, {}],
    ['partial', {}, { taskPages: (url) => (url.searchParams.get('after') ? { ...showcase(), next_after: null } : { ...showcase(), next_after: 'older', total: 30 }) }]]) {
    await stubNotifications(page, options)
    await stubLocks(page, true)
    const script = scripted()
    const log = await open(page, 'settings?section=notifications', { taskPages: script.taskPages, ...extra })
    await expect(page.locator('.atlas h1')).toHaveText('Settings')
    if (label === 'partial') await expect(page.getByText('Not every task is loaded here. Windows notifications are the fallback when available.', { exact: false })).toBeVisible()
    script.ask()
    await announceChange(page)
    await page.waitForTimeout(2500)
    expect(beats(log), label).toBe(0)
    expect(await page.evaluate(() => window.__shown.length), label).toBe(0)
    await page.unroute('**/*')
  }
})

// The test origin is not a secure context, so the browser offers no Web Locks there; a stand-in
// lock manager grants the lock at once (`held`) or only when `window.__grant()` is called, the
// way a second tab waits until the first one closes.
function stubLocks(page, held) {
  return page.addInitScript((held) => {
    Object.defineProperty(navigator, 'locks', { configurable: true, value: { request(name, options, callback) {
      return new Promise((resolve) => {
        const run = () => resolve(callback({ name }))
        if (held) run()
        else window.__grant = run
      })
    } } })
  }, held)
}

test('with two Atlas tabs open, only the one holding the lock shows browser notifications and sends the heartbeat', async ({ page, context }) => {
  await stubNotifications(page)
  await stubLocks(page, true)
  const first = await open(page, 'settings?section=notifications')
  await expect.poll(() => beats(first)).toBeGreaterThan(0)
  const other = await context.newPage()
  await stubNotifications(other)
  await stubLocks(other, false)
  const second = await open(other, 'settings?section=notifications')
  await expect(other.getByText('Another Atlas tab shows them.')).toBeVisible()
  await other.waitForTimeout(2500)
  expect(beats(second)).toBe(0)
  // When the first tab closes, the browser hands the lock to the other one.
  await page.close()
  await other.evaluate(() => window.__grant())
  await expect(other.getByText('As browser notifications from this page')).toBeVisible()
  await expect.poll(() => beats(second), { timeout: 8000 }).toBeGreaterThan(0)
})

test('without Web Locks no tab shows browser notifications or sends the heartbeat; the wording promises nothing', async ({ page }) => {
  await stubNotifications(page)
  const script = scripted()
  const log = await open(page, 'settings?section=notifications', { taskPages: script.taskPages })
  await expect(page.getByText('This browser can’t coordinate Atlas tabs, so none shows them. Windows notifications are the fallback when available.', { exact: false })).toBeVisible()
  await expect(page.locator('#atlas-settings-notifications')).not.toContainText(/Windows notifications are used|shown as Windows notifications/)
  script.ask()
  await announceChange(page)
  await page.waitForTimeout(2500)
  expect(beats(log)).toBe(0)
  expect(await page.evaluate(() => window.__shown.length)).toBe(0)
  await bell(page).click()
  await expect(panel(page)).toContainText('Notifications: On. This browser can’t coordinate Atlas tabs')
})

test('when the browser fails to show one, the heartbeat stops at once so the backend can fall back', async ({ page }) => {
  await stubNotifications(page)
  await stubLocks(page, true)
  await page.addInitScript(() => {
    const Real = window.Notification
    window.Notification = class extends Real { constructor(title, options) { super(title, options); if (window.__failNext) throw new Error('blocked by the system') } }
  })
  const script = scripted()
  const log = await open(page, 'settings?section=notifications', { taskPages: script.taskPages })
  await expect.poll(() => beats(log)).toBeGreaterThan(0)
  await page.evaluate(() => { window.__failNext = true })
  script.ask()
  await announceChange(page)
  await expect(page.getByText('This browser couldn’t show one here. Windows notifications are the fallback when available.', { exact: false })).toBeVisible()
  const after = beats(log)
  await page.waitForTimeout(2500)
  expect(beats(log)).toBe(after)
  // Turning notifications off and on again is a new chance to show them here.
  await page.getByRole('switch', { name: 'Control Center notifications' }).click()
  await page.getByRole('switch', { name: 'Control Center notifications' }).click()
  await expect(page.getByText('As browser notifications from this page')).toBeVisible()
})

test('backend unavailable: no heartbeat and no promise of a Windows notification', async ({ page }) => {
  await stubNotifications(page)
  await stubLocks(page, true)
  let online = true
  const log = await open(page, 'work', { api: () => (online ? undefined : { status: 503, body: { detail: 'unavailable' } }) })
  await expect.poll(() => beats(log)).toBeGreaterThan(0)
  online = false
  await announceChange(page)
  await expect(page.locator('.a-conn')).toContainText('Backend unavailable')
  await bell(page).click()
  await expect(panel(page)).toContainText('Notifications: On. The backend isn’t answering, so nothing can be sent until it returns.')
  await expect(panel(page)).not.toContainText('Windows notification')
})

test('the panel respects reduced motion, keeps mixed-direction titles apart and passes axe in both themes', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' })
  for (const theme of ['light', 'dark']) {
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    const tasks = showcase()
    tasks.tasks.find((item) => item.task_id === 'quoted').title = 'לתרגם את השאלה (quoted fields)'
    await open(page, 'work', { tasks, notices: [UPDATE] })
    await bell(page).click()
    await expect(panel(page)).toBeVisible()
    expect(await panel(page).evaluate((element) => getComputedStyle(element).animationName)).toBe('none')
    const title = panel(page).getByText('לתרגם את השאלה (quoted fields)')
    expect(await title.evaluate((element) => getComputedStyle(element).direction)).toBe('rtl')
    await page.addScriptTag({ path: axePath })
    const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
    console.log(`axe notifications ${theme}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
    expect(result.violations.filter((item) => ['serious', 'critical'].includes(item.impact)).map((item) => item.id)).toEqual([])
    await page.unroute('**/*')
  }
})
