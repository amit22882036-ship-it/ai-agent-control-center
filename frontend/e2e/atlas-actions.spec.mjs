import { test, expect } from '@playwright/test'
import { agentDetail, announceChange, openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const NOW = new Date('2026-10-29T12:05:00.000Z')
const posts = (log, path) => log.requests.filter((item) => item.method !== 'GET' && (!path || item.path === path))

async function openTask(page, taskId, options = {}) {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work?task=${taskId}`, tasks: showcase(), ...options })
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector).toBeVisible()
  return { log, inspector }
}

test('reply goes to the current agent with the answer, once, and the task list is read again', async ({ page }) => {
  let release
  const { log, inspector } = await openTask(page, 'quoted', { api: async ({ method, path }) => {
    if (method === 'POST' && path === '/agents/quoter/reply') { await new Promise((resolve) => { release = resolve }); return { body: { status: 'running' } } }
    return undefined
  } })
  await expect(inspector).toContainText('Should quoted fields keep their line breaks?')
  await inspector.getByLabel('Your answer').fill('Keep them; escape inside quotes.')
  const send = inspector.getByRole('button', { name: 'Send reply' })
  await send.click()
  // A second click while sending sends nothing (the button is disabled and the runner allows one action).
  await inspector.locator('.a-qcard--form button[type="submit"]').click({ force: true, timeout: 2000 }).catch(() => {})
  await expect(inspector.getByRole('button', { name: 'Sending…' })).toBeDisabled()
  await expect.poll(() => typeof release).toBe('function') // the request has reached the backend
  const readsBefore = log.requests.filter((item) => item.path === '/workspace/tasks').length
  release()
  await expect(inspector.getByRole('status').filter({ hasText: 'Reply sent.' })).toBeVisible()
  expect(posts(log, '/agents/quoter/reply')).toHaveLength(1)
  expect(posts(log, '/agents/quoter/reply')[0].body).toEqual({ answer: 'Keep them; escape inside quotes.' })
  await expect.poll(() => log.requests.filter((item) => item.path === '/workspace/tasks').length).toBeGreaterThan(readsBefore)
  await expect(inspector.getByLabel('Your answer')).toHaveValue('')
})

test('a one-time model override is sent with the reply and never changes the defaults', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'quoted')
  await inspector.getByLabel('Your answer').fill('Use the faster model for this one.')
  await inspector.getByText('Use a different model or effort for this instruction only').click()
  await inspector.getByLabel('Model for this instruction').selectOption('Haiku 4.5')
  await inspector.locator('.a-qcard--form').getByLabel('Effort', { exact: true }).selectOption('low')
  await inspector.getByRole('button', { name: 'Send reply' }).click()
  await expect.poll(() => posts(log, '/agents/quoter/reply').length).toBe(1)
  expect(posts(log, '/agents/quoter/reply')[0].body).toEqual({ answer: 'Use the faster model for this one.', model_override: true, model: 'Haiku 4.5',
    effort_override: true, effort: 'low', confirm_new_session: false, attempt_unverified: false })
  expect(posts(log, '/agents/quoter/defaults')).toHaveLength(0)
})

test('decide for me, and delegating future decisions, address the asking agent', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'quoted')
  await inspector.getByRole('button', { name: 'Decide for me' }).click()
  await expect(inspector.getByText('The agent decides this one itself and continues.')).toBeVisible()
  await inspector.getByText('Delegate future decisions').click()
  await inspector.getByRole('button', { name: 'Decide similar automatically' }).click()
  await expect(inspector.getByText('Similar decisions are now made automatically for this agent.')).toBeVisible()
  await inspector.getByRole('button', { name: 'Always decide for this agent' }).click()
  await expect(inspector.getByText('Always decide is on for this agent.')).toBeVisible()
  expect(posts(log).map((item) => item.path)).toEqual(['/agents/quoter/decide', '/agents/quoter/decide-similar', '/agents/quoter/decide-always'])
})

test('saved decision policies can be turned off in Settings', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'csv', { agents: { lead: { similar_decisions_enabled: true, always_decide_enabled: true } } })
  await inspector.getByRole('tab', { name: 'Settings' }).click()
  await expect(inspector).toContainText('Always decide for this agent: On')
  await expect(inspector).toContainText('Auto-decide similar questions: Saved (Always Decide is active)')
  await inspector.getByRole('button', { name: 'Turn off' }).first().click()
  await expect(inspector.getByText('Always decide is off.')).toBeVisible()
  expect(posts(log).map((item) => item.path)).toEqual(['/agents/lead/decide-always/disable'])
})

test('a backend refusal is shown in its own words and nothing is assumed', async ({ page }) => {
  const { inspector } = await openTask(page, 'quoted', { api: ({ method, path }) => method === 'POST' && path === '/agents/quoter/reply'
    ? { status: 409, body: { detail: 'Agent is no longer waiting for this answer.' } } : undefined })
  await inspector.getByLabel('Your answer').fill('Keep them.')
  await inspector.getByRole('button', { name: 'Send reply' }).click()
  await expect(inspector.getByRole('alert')).toHaveText('Agent is no longer waiting for this answer.')
  await expect(inspector.getByLabel('Your answer')).toHaveValue('Keep them.') // the draft is kept
})

test('redirect sends a correction to a working agent', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'report')
  await inspector.getByText('Send a correction to this agent').click()
  await inspector.getByLabel('Correction').fill('Render the totals as a table.')
  await inspector.getByRole('button', { name: 'Send correction' }).click()
  await expect(inspector.getByText('Redirect sent.', { exact: false })).toBeVisible()
  expect(posts(log)).toEqual([expect.objectContaining({ path: '/agents/rep-2/redirect', body: { instruction: 'Render the totals as a table.' } })])
})

test('stop asks first, says it does not pause or cancel the task, and Cancel sends nothing', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'report')
  await inspector.getByRole('button', { name: 'Stop agent' }).click()
  const dialog = page.getByRole('dialog', { name: 'Stop Report builder?' })
  await expect(dialog).toContainText('it does not pause or cancel the task')
  await dialog.getByRole('button', { name: 'Cancel' }).click()
  expect(posts(log)).toHaveLength(0)
  await inspector.getByRole('button', { name: 'Stop agent' }).click()
  await page.getByRole('dialog', { name: 'Stop Report builder?' }).getByRole('button', { name: 'Stop agent' }).click()
  await expect(inspector.getByText('Report builder was stopped.')).toBeVisible()
  expect(posts(log).map((item) => item.path)).toEqual(['/agents/rep-2/stop'])
})

test('stop branch follows agent lineage and reports partial success by name', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'csv', {
    agents: { lead: { child_ids: ['quoter', 'rep-2'] } },
    api: ({ method, path }) => method === 'POST' && path === '/agents/lead/stop-branch'
      ? { body: { ok: false, failures: [{ agent_id: 'quoter', error: 'Stop was not confirmed' }] } } : undefined,
  })
  await inspector.getByRole('button', { name: 'Stop branch' }).click()
  const dialog = page.getByRole('dialog', { name: 'Stop Atlas lead and its branch?' })
  await expect(dialog).toContainText('following the agents’ own lineage')
  await expect(dialog).toContainText('It started 2 agents directly')
  await dialog.getByRole('button', { name: 'Stop branch' }).click()
  const alert = inspector.getByRole('alert')
  await expect(alert).toContainText('Branch stopping was only partially successful.')
  await expect(alert).toContainText('Quoted fields: Stop was not confirmed')
  expect(posts(log).map((item) => item.path)).toEqual(['/agents/lead/stop-branch'])
})

test('a provider wait resumes only on an explicit retry', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'limits', { agents: { logs: {
    provider_condition: { kind: 'usage_limited', label: 'Waiting for provider usage', provider: 'claude', blocking: [{ label: '5-hour limit', reset_at: '2026-10-29T14:30:00Z' }] },
    resume: { available: true, mode: 'explicit_retry', reason: 'The provider reported a limit; retrying may hit it again.', blocking: [] } } } })
  await expect(inspector).toContainText('Nothing resumes automatically, not even after a reset time.')
  const resume = inspector.getByRole('button', { name: 'Resume task' })
  await expect(resume).toBeDisabled()
  await inspector.getByText('Retry explicitly: availability is not guaranteed').click()
  await resume.click()
  await expect(inspector.getByText('Resume requested.', { exact: false })).toBeVisible()
  await inspector.getByRole('button', { name: 'Refresh usage' }).click()
  await expect.poll(() => posts(log).map((item) => item.path)).toEqual(['/agents/logs/resume', '/providers/claude/usage/refresh'])
  expect(posts(log)[0].body).toEqual({ explicit_retry: true })
})

test('switching tool or model while the agent waits sends the classic payload', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'quoted')
  await inspector.getByText('Switch tool or model').click()
  await inspector.getByLabel('Provider', { exact: true }).selectOption('codex')
  await inspector.getByLabel('Model', { exact: true }).selectOption('GPT-5.5')
  await inspector.getByLabel('Access').selectOption('read-only')
  await inspector.getByRole('button', { name: 'Change', exact: true }).click()
  await expect(inspector.getByText('Provider and model changed.', { exact: false })).toBeVisible()
  expect(posts(log)).toEqual([expect.objectContaining({ path: '/agents/quoter/provider', body: { provider: 'codex', model: 'GPT-5.5', sandbox: 'read-only' } })])
})

test('nothing is sent to an agent that replaced the one the answer was written for', async ({ page }) => {
  let replaced = false
  const swapped = () => {
    const data = showcase()
    const quoted = data.tasks.find((item) => item.task_id === 'quoted')
    quoted.assignment = { ...quoted.assignment, current: { assignment_id: 'as-new', agent_id: 'quoter-2', assigned_at: quoted.assignment.current.assigned_at } }
    quoted.agent = { ...quoted.agent, agent_id: 'quoter-2', display_name: 'Second reader' }
    return { data, quoted }
  }
  const { log, inspector } = await openTask(page, 'quoted', {
    taskPages: () => (replaced ? swapped().data : showcase()),
    api: ({ method, path }) => (method === 'GET' && path === '/agents/quoter-2' ? { body: agentDetail(swapped().quoted) } : undefined),
  })
  await inspector.getByLabel('Your answer').fill('Keep them.')
  replaced = true
  await announceChange(page)
  await expect(inspector.getByRole('heading', { level: 2 })).toHaveText('Second reader')
  await inspector.getByRole('button', { name: 'Send reply' }).click()
  await expect(inspector.getByRole('alert')).toContainText('This task’s agent changed while you were writing. Nothing was sent.')
  expect(posts(log)).toHaveLength(0)
  await inspector.getByRole('button', { name: 'Send reply' }).click()
  await expect.poll(() => posts(log).map((item) => item.path)).toEqual(['/agents/quoter-2/reply'])
})

test('a task without a current agent offers no agent actions', async ({ page }) => {
  for (const taskId of ['cli', 'migrate', 'backlog']) {
    const { inspector } = await openTask(page, taskId)
    for (const name of ['Stop agent', 'Stop branch', 'Send reply', 'Decide for me', 'Resume task']) {
      await expect(inspector.getByRole('button', { name })).toHaveCount(0)
    }
    await page.unroute('**/*')
  }
})
