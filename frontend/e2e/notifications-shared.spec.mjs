import { test, expect } from '@playwright/test'
import { announceChange, openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

// Browser notifications coordinated across Classic view and Atlas (option B). Every page in these
// tests shares one stand-in lock manager (the test origin is not a secure context, so the browser
// offers no Web Locks there); it grants the lock to one page at a time, queues the others, and
// frees a closed page's lock, as the browser does.

const QUOTED = 'Handle quoted fields and embedded newlines'
const beats = (log) => log.requests.filter((item) => item.method === 'POST' && item.path === '/notifications/heartbeat').length
const shown = (page) => page.evaluate(() => window.__shown.map((item) => [item.title, item.options.tag]))

async function sharedLocks(context) {
  const queue = []
  let holder = null
  const grant = () => {
    if (holder || !queue.length) return
    holder = queue.shift()
    holder.resolve()
  }
  const drop = (match) => {
    for (let index = queue.length - 1; index >= 0; index -= 1) if (match(queue[index])) queue.splice(index, 1)
    if (holder && match(holder)) { holder = null; grant() }
  }
  await context.exposeBinding('__lockAcquire', ({ page }, token) => new Promise((resolve) => { queue.push({ token, page, resolve }); grant() }))
  await context.exposeBinding('__lockCancel', (source, token) => drop((entry) => entry.token === token && entry !== holder))
  await context.exposeBinding('__lockRelease', (source, token) => drop((entry) => entry.token === token))
  // Closing a page or navigating it away frees its lock and its waiting requests, as the browser does.
  const watch = (page) => {
    page.on('close', () => drop((entry) => entry.page === page))
    page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) drop((entry) => entry.page === page) })
  }
  context.pages().forEach(watch)
  context.on('page', watch)
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'locks', { configurable: true, value: { request(name, options, callback) {
      const token = Math.random().toString(36).slice(2)
      return new Promise((resolve, reject) => {
        let aborted = false
        options?.signal?.addEventListener('abort', () => {
          if (aborted) return
          aborted = true
          window.__lockCancel(token)
          reject(new DOMException('Aborted', 'AbortError'))
        })
        window.__lockAcquire(token).then(async () => {
          if (aborted) { window.__lockRelease(token); return }
          try { resolve(await callback({ name })) } finally { window.__lockRelease(token) }
        })
      })
    } } })
  })
}

function stubNotifications(target, { permission = 'granted', failing = false } = {}) {
  return target.addInitScript(({ permission, failing }) => {
    window.__shown = []
    window.Notification = class {
      static permission = permission
      static requestPermission() { return Promise.resolve(permission) }
      constructor(title, options) {
        if (failing) throw new Error('The system refused the notification')
        this.title = title
        this.options = options
        window.__shown.push(this)
      }
      close() { this.closed = true }
    }
    localStorage.setItem('ai-agent-control-center.notifications', 'enabled')
  }, { permission, failing })
}

// One scripted workspace that both views read: the showcase Tasks for Atlas, and the same Agent
// as rows of GET /workspace for Classic. `ask()` makes the "Quoted fields" agent ask its question.
function workspace({ classicPages = false } = {}) {
  const state = { asking: false, finished: false }
  const tasks = () => {
    const data = showcase()
    const quoted = data.tasks.find((item) => item.task_id === 'quoted')
    if (!state.asking) {
      quoted.agent = { ...quoted.agent, status: 'running', asks_user: false }
      quoted.waiting_context = null
      quoted.status = 'in_progress'
    }
    return data
  }
  const rows = () => [
    { agent_id: 'quoter', parent_id: 'lead', status: state.asking ? 'waiting' : 'running', task: QUOTED, display_name: 'Quoted fields',
      display_color: 'yellow', agent_type: 'claude', model: 'Sonnet 4.6', sandbox: 'workspace-write', waiting_question: state.asking ? 'Keep line breaks?' : null,
      task_id: 'quoted', task_status: state.asking ? 'waiting' : 'in_progress', plan_revision: null, provider_condition: null, historical_assignment: false,
      autonomous: false, waiting_context: state.asking ? { kind: 'user', label: 'Waiting for you' } : null, removal: null, removed: false, responsibility_awaiting: false },
    { agent_id: 'lead', parent_id: null, status: state.finished ? 'finished' : 'running', task: 'Build the CSV import pipeline', display_name: 'Atlas lead',
      display_color: 'violet', agent_type: 'claude', model: 'Opus 4.8', sandbox: 'workspace-write', waiting_question: null, task_id: 'csv',
      task_status: 'in_progress', plan_revision: 2, provider_condition: null, historical_assignment: false, autonomous: true, waiting_context: null,
      removal: null, removed: false, responsibility_awaiting: false },
  ]
  const api = ({ method, path }) => {
    if (method !== 'GET' || path !== '/workspace') return undefined
    return { body: { agents: rows(), next_after: classicPages ? 1 : null,
      overview: { active_agents: 1, completed_tasks: 0, waiting_tasks: 0, needs_input: state.asking ? 1 : 0, pending_responsibilities: 0 } } }
  }
  return { state, taskPages: () => tasks(), api, ask: () => { state.asking = true }, finish: () => { state.finished = true } }
}

async function classic(page, ws) {
  const log = await openApp(page, { path: '/', taskPages: ws.taskPages, api: ws.api })
  await expect(page.getByRole('heading', { level: 1, name: 'Agent Workspace' })).toBeVisible()
  return log
}
async function atlas(page, ws, hash = 'settings?section=notifications') {
  const log = await openApp(page, { path: `/#/${hash}`, taskPages: ws.taskPages, api: ws.api })
  await expect(page.locator('.atlas h1').first()).toBeAttached()
  return log
}
const classicStatus = async (page) => {
  await page.getByRole('button', { name: /^Notifications/ }).click()
  const text = await page.locator('.notification-status').innerText()
  await page.keyboard.press('Escape')
  return text
}
const change = async (...pages) => { for (const page of pages) await announceChange(page) }

test('Classic alone: one notification per transition with the shared tag, and the heartbeat only after a fresh complete read', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(page)
  const ws = workspace()
  const log = await classic(page, ws)
  await claimedIn(page, log)
  ws.ask()
  ws.finish()
  await change(page)
  await expect.poll(() => shown(page)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user'], ['Agent finished', 'control-center:lead:finished']])
  await change(page)
  await page.waitForTimeout(400)
  expect(await shown(page)).toHaveLength(2)  // The same state read again shows nothing new.
  expect(await classicStatus(page)).toBe('Control Center notifications: On')
})

test('Classic with agents on unloaded pages never claims events: no heartbeat, Windows stays the fallback', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(page)
  const ws = workspace({ classicPages: true })
  const log = await classic(page, ws)
  await expect(page.getByRole('button', { name: /Load more agents/ })).toBeVisible()
  ws.ask()
  await change(page)
  await page.waitForTimeout(2600)
  expect(beats(log)).toBe(0)
  expect(await shown(page)).toEqual([])
  expect(await classicStatus(page)).toBe('Control Center notifications: On. Windows notifications are the fallback when available.')
})

test('Classic and Atlas together: one page claims the event, the other shows nothing and sends no heartbeat', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const classicLog = await classic(page, ws)
  await claimedIn(page, classicLog)
  const other = await context.newPage()
  const atlasLog = await atlas(other, ws)
  await expect(other.getByText('Another Control Center tab in this browser shows them.', { exact: false }).first()).toBeVisible()
  ws.ask()
  await change(page, other)
  await expect.poll(() => shown(page)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user']])
  await other.waitForTimeout(2600)
  expect(await shown(other)).toEqual([])
  expect(beats(atlasLog)).toBe(0)
  // Atlas still announces the question in its own page for screen readers, without a browser notification.
  await expect(other.locator('.a-app > [role="status"][aria-live="polite"]')).toContainText('needs your reply')
})

test('when the Classic tab holding the lock closes, Atlas takes over after its next read', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const classicLog = await classic(page, ws)
  await claimedIn(page, classicLog)
  const other = await context.newPage()
  const atlasLog = await atlas(other, ws)
  await expect(other.getByText('Another Control Center tab in this browser shows them.', { exact: false }).first()).toBeVisible()
  await page.close()
  await expect(other.getByText('As browser notifications from this page')).toBeVisible()
  ws.ask()
  await change(other)
  await expect.poll(() => shown(other)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user']])
  await expect.poll(() => beats(atlasLog)).toBeGreaterThan(0)
})

test('two Classic tabs: only the first shows it; the second takes over when the first closes', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const firstLog = await classic(page, ws)
  await claimedIn(page, firstLog)
  const other = await context.newPage()
  const secondLog = await classic(other, ws)
  expect(await classicStatus(other)).toBe('Control Center notifications: On. Another Control Center tab in this browser shows them.')
  ws.ask()
  await change(page, other)
  await expect.poll(() => shown(page)).toHaveLength(1)
  await other.waitForTimeout(2600)
  expect(await shown(other)).toEqual([])
  expect(beats(secondLog)).toBe(0)
  await page.close()
  await change(other)
  await expect.poll(() => beats(secondLog)).toBeGreaterThan(0)
})

test('switching the same tab from Classic to Atlas hands over the lock without leaving it held', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(page)
  const ws = workspace()
  const log = await classic(page, ws)
  await claimedIn(page, log)
  await page.getByRole('button', { name: 'Atlas preview' }).click()
  await expect(page.locator('.atlas h1')).toHaveText('Work')
  await page.locator('.a-nav').getByRole('link', { name: 'Settings' }).click()
  await expect(page.getByText('As browser notifications from this page')).toBeVisible()
  ws.ask()
  await change(page)
  await expect.poll(() => shown(page)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user']])
})

test('initial loading, a lost backend and a refused notification never leave a suppressing heartbeat', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(page)
  let online = true
  let slow = true
  const ws = workspace()
  const log = await openApp(page, { path: '/', taskPages: ws.taskPages, api: async (entry) => {
    if (!online && entry.path !== '/security/csrf-token') return { status: 503, body: { detail: 'unavailable' } }
    if (slow && entry.method === 'GET' && entry.path === '/workspace') { await new Promise((resolve) => setTimeout(resolve, 2500)); slow = false }
    return ws.api(entry)
  } })
  // Before the first read has answered, nothing is claimed.
  await page.waitForTimeout(2000)
  expect(beats(log)).toBe(0)
  await claimedIn(page, log)
  // The backend stops answering: the failed read ends the claim.
  online = false
  await change(page)
  await page.waitForTimeout(500)
  const atLoss = beats(log)
  await page.waitForTimeout(4600)
  expect(beats(log)).toBe(atLoss)
})

test('a page whose browser refuses notifications stops claiming them at once', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(page, { failing: true })
  const ws = workspace()
  const log = await classic(page, ws)
  await claimedIn(page, log)
  ws.ask()
  await change(page)
  await page.waitForTimeout(600)
  const atFailure = beats(log)
  await page.waitForTimeout(4200)
  expect(beats(log)).toBe(atFailure)
  expect(await classicStatus(page)).toBe('Control Center notifications: On. Windows notifications are the fallback when available.')
})

test('without Web Locks, or with notifications blocked, neither view shows them or sends a heartbeat', async ({ context, page }) => {
  await stubNotifications(page)
  const ws = workspace()
  const log = await classic(page, ws)
  ws.ask()
  await change(page)
  await page.waitForTimeout(2600)
  expect(beats(log)).toBe(0)
  expect(await shown(page)).toEqual([])
  expect(await classicStatus(page)).toBe('Control Center notifications: On. Windows notifications are the fallback when available.')
  const blocked = await context.newPage()
  await stubNotifications(blocked, { permission: 'denied' })
  const blockedLog = await atlas(blocked, workspace())
  await blocked.waitForTimeout(2600)
  expect(beats(blockedLog)).toBe(0)
})

// Stale leadership (a page that holds the lock but whose last read is old). The browser clock is
// moved forward past LEADERSHIP_FRESH_MS (60 seconds) instead of waiting.
const STALE_MS = 61_000
const claimedIn = async (page, log) => {
  await expect.poll(async () => { await announceChange(page).catch(() => {}); return beats(log) }, { timeout: 10_000 }).toBeGreaterThan(0)
}

test('a stale Classic leader gives the lock up, a fresh Atlas takes it, and a new event is shown once', async ({ context, page }) => {
  // Both pages exist before the clock is installed, so both follow it.
  const other = await context.newPage()
  await context.clock.install()
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const classicLog = await classic(page, ws)
  await claimedIn(page, classicLog)
  const atlasLog = await atlas(other, ws)
  await expect(other.getByText('Another Control Center tab in this browser shows them.', { exact: false }).first()).toBeVisible()
  await page.waitForTimeout(800)
  // Classic gets no further reads (no reported change reaches it) and no error either.
  await context.clock.fastForward(STALE_MS)
  await announceChange(other)  // Atlas reads afresh.
  await expect(other.getByText('As browser notifications from this page')).toBeVisible()
  await claimedIn(other, atlasLog)
  const classicBeats = beats(classicLog)
  await other.waitForTimeout(2500)
  expect(beats(classicLog)).toBe(classicBeats)  // Only Atlas sends a fresh heartbeat.
  expect(await classicStatus(page)).toBe('Control Center notifications: On. Windows notifications are the fallback when available.')
  ws.ask()
  await change(other, page)
  await expect.poll(() => shown(other)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user']])
  await page.waitForTimeout(800)
  expect(await shown(page)).toEqual([])  // Classic read it fresh but did not hold the lock: not announced twice.
})

test('a stale Atlas leader gives the lock up to a fresh Classic tab', async ({ context, page }) => {
  // Both pages exist before the clock is installed, so both follow it.
  const other = await context.newPage()
  await context.clock.install()
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const atlasLog = await atlas(page, ws)
  await claimedIn(page, atlasLog)
  const classicLog = await classic(other, ws)
  expect(await classicStatus(other)).toBe('Control Center notifications: On. Another Control Center tab in this browser shows them.')
  await page.waitForTimeout(800)
  await context.clock.fastForward(STALE_MS)
  await claimedIn(other, classicLog)
  await expect(page.getByText('The task list isn’t current here.', { exact: false })).toBeVisible()
  const atlasBeats = beats(atlasLog)
  ws.ask()
  await change(other, page)
  await expect.poll(() => shown(other)).toEqual([['Agent needs your input', 'control-center:quoter:waiting:user']])
  await page.waitForTimeout(800)
  expect(await shown(page)).toEqual([])
  expect(beats(atlasLog)).toBe(atlasBeats)
})

test('a read before expiry keeps the lock; after expiry the next read makes the page eligible again', async ({ context, page }) => {
  // Both pages exist before the clock is installed, so both follow it.
  const other = await context.newPage()
  await context.clock.install()
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const log = await atlas(page, ws)
  await claimedIn(page, log)
  await classic(other, ws)
  await other.waitForTimeout(800)  // Its opening reads have all been answered before time moves on.
  // A read 30 seconds in renews the leader; 40 seconds later (70 after the first read) it still leads.
  await context.clock.fastForward(30_000)
  const reads = () => log.requests.filter((item) => item.method === 'GET' && item.path === '/workspace/tasks').length
  const readsBefore = await reads()
  await announceChange(page)
  await expect.poll(reads).toBeGreaterThan(readsBefore)  // The renewing read has arrived.
  await context.clock.fastForward(40_000)
  await expect(page.getByText('As browser notifications from this page')).toBeVisible()
  // Classic, unread for 70 seconds, is no longer eligible: it neither leads nor waits for the lock.
  await expect.poll(() => classicStatus(other), { timeout: 10_000 }).toBe('Control Center notifications: On. Windows notifications are the fallback when available.')
  // Alone, an expired page asks for nothing until it reads again, then leads again.
  await other.close()
  await context.clock.fastForward(STALE_MS)
  await expect(page.getByText('The task list isn’t current here.', { exact: false })).toBeVisible()
  const before = beats(log)
  await page.waitForTimeout(2500)
  expect(beats(log)).toBe(before)
  await claimedIn(page, log)
  await expect(page.getByText('As browser notifications from this page')).toBeVisible()
})

test('a page that leaves while still waiting for the lock leaves nothing behind', async ({ context, page }) => {
  await sharedLocks(context)
  await stubNotifications(context)
  const ws = workspace()
  const firstLog = await classic(page, ws)
  await claimedIn(page, firstLog)
  const waiting = await context.newPage()
  await atlas(waiting, ws)
  await expect(waiting.getByText('Another Control Center tab in this browser shows them.', { exact: false }).first()).toBeVisible()
  await waiting.goto('about:blank')  // Unmounts with its lock request outstanding.
  await page.close()
  const third = await context.newPage()
  const thirdLog = await classic(third, ws)
  await claimedIn(third, thirdLog)  // The lock went to the new page, not to the one that left.
  expect(await classicStatus(third)).toBe('Control Center notifications: On')
})

// The backend keeps one notification preference for every browser. Two browsers (separate
// contexts here: separate storage, permission and locks, like Chrome and Edge).
test('a browser where notifications were never turned on does not switch them off for another browser', async ({ browser }) => {
  const prefs = (log) => log.requests.filter((item) => item.method === 'POST' && item.path === '/notifications/preferences').map((item) => item.body.enabled)
  const edge = await browser.newContext()
  const chrome = await browser.newContext()
  try {
    const on = await edge.newPage()
    await stubNotifications(on)
    const off = await chrome.newPage()
    // Chrome: permission granted, but the preference was never turned on in this browser.
    await off.addInitScript(() => {
      window.__shown = []
      window.Notification = class { static permission = 'granted'; static requestPermission() { return Promise.resolve('granted') } }
    })
    const ws = workspace()
    const onLog = await classic(on, ws)
    const offLog = await classic(off, ws)
    await off.waitForTimeout(2600)
    expect(prefs(offLog)).toEqual([])  // Nothing sent for an untouched "off".
    expect(prefs(onLog).every(Boolean)).toBe(true)
    // Switching it off explicitly in Chrome is told to the backend once, not every two seconds.
    await off.getByRole('button', { name: /^Notifications/ }).click()
    await off.getByRole('button', { name: 'Enable notifications' }).click()
    await off.getByRole('button', { name: 'Disable notifications' }).click()
    await off.keyboard.press('Escape')
    await off.waitForTimeout(4500)
    expect(prefs(offLog).filter((value) => value === false)).toHaveLength(1)
  } finally {
    await edge.close()
    await chrome.close()
  }
})
