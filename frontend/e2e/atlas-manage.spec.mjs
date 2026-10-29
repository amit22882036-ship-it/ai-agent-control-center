import { test, expect } from '@playwright/test'
import { createRequire } from 'node:module'
import { openApp } from './harness.mjs'
import { showcase } from './scenarios.mjs'

const require = createRequire(import.meta.url)
const axePath = require.resolve('axe-core/axe.min.js')
const NOW = new Date('2026-10-29T12:05:00.000Z')
const posts = (log) => log.requests.filter((item) => item.method !== 'GET')

async function openTask(page, taskId, options = {}, tab = 'Settings') {
  await page.goto('about:blank')
  await page.clock.setFixedTime(NOW)
  const log = await openApp(page, { path: `/#/work?task=${taskId}`, tasks: showcase(), ...options })
  const inspector = page.getByRole('complementary', { name: 'Task details' })
  await expect(inspector).toBeVisible()
  if (tab) await inspector.getByRole('tab', { name: tab }).click()
  return { log, inspector }
}

const PREVIEW = (token) => ({ mode: 'single', agent: { status: 'running' }, members: [{ agent_id: 'rep-2', status: 'running', unresolved_executions: 0 }],
  descendants: [], options: { single: { available: true, reasons: [] } }, plan_token: token,
  consequences: ['stop_running_worker', 'task_awaits_responsibility_transfer', 'identity_retired_permanently'] })

test('defaults, name and colour are saved for the current agent with the classic payloads', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'report')
  await inspector.getByText('Change this agent’s defaults').click()
  await inspector.getByLabel('Default model').selectOption('GPT-5.5')
  await inspector.getByRole('button', { name: 'Save defaults' }).click()
  await expect(inspector.getByText('Defaults saved.', { exact: false })).toBeVisible()
  await inspector.getByRole('button', { name: 'Rename' }).click()
  await inspector.getByLabel('Agent name').fill('Report writer')
  await inspector.getByRole('button', { name: 'Save name' }).click()
  await expect(inspector.getByText('Renamed to', { exact: false })).toBeVisible()
  await inspector.getByRole('radio', { name: 'Green' }).click() // the label shown is the backend's, read again after saving
  await expect(inspector.getByText('Colour label set to green.')).toBeVisible()
  expect(posts(log).map((item) => [item.path, item.body])).toEqual([
    ['/agents/rep-2/defaults', { model_set: true, model: 'GPT-5.5', effort_set: true, effort: null, attempt_unverified: false }],
    ['/agents/rep-2/rename', { display_name: 'Report writer' }],
    ['/agents/rep-2/color', { display_color: 'green' }],
  ])
})

test('the last agent of a finished task can be relabelled, clearly as history, and nothing else is offered', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'cli', { api: ({ path }) => path === '/agents/cli-2/name-history'
    ? { body: { history: [{ name: 'Codex agent', changed_at: '2026-10-29T11:00:00Z' }, { name: 'CLI writer', changed_at: '2026-10-29T11:05:00Z' }] } } : undefined })
  await expect(inspector).toContainText('Name and colour of its last agent')
  await expect(inspector).toContainText('Other controls apply only to a task’s current agent.')
  await expect(inspector.getByRole('button', { name: 'Save defaults' })).toHaveCount(0)
  await expect(inspector.getByRole('button', { name: 'Review removal…' })).toHaveCount(0)
  await inspector.getByText('Name history', { exact: true }).click()
  await expect(inspector.getByText('Codex agent')).toBeVisible()
  expect(posts(log)).toHaveLength(0)
})

test('removal is reviewed first, refuses a changed impact, and needs a fresh confirmation', async ({ page }) => {
  let previews = 0
  let removes = 0
  const { log, inspector } = await openTask(page, 'report', { api: ({ method, path }) => {
    if (method === 'POST' && path === '/agents/rep-2/removal-preview') return { body: PREVIEW(`token-${++previews}`) }
    if (method === 'POST' && path === '/agents/rep-2/remove') {
      removes += 1
      return removes === 1 ? { status: 409, body: { detail: { code: 'stale_preview', message: 'The impact changed since it was reviewed.', preview: PREVIEW('token-2') } } }
        : { body: { phase: 'admitted' } }
    }
    return undefined
  } })
  await inspector.getByRole('button', { name: 'Review removal…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Remove Report builder?' })
  await expect(dialog).toContainText('never deletes tasks, results, workspaces or history')
  await dialog.getByRole('button', { name: 'Review impact' }).click()
  await expect(dialog).toContainText('Its running work is stopped and its end verified before removal completes.')
  await expect(dialog).toContainText('Its task is kept and waits until you choose a new owner.')
  const remove = dialog.getByRole('button', { name: 'Remove this agent' })
  await expect(remove).toBeDisabled()
  await dialog.getByText('I understand this agent identity is retired permanently.').click()
  await remove.click()
  await expect(dialog.getByRole('alert')).toContainText('The impact changed since you reviewed it.')
  await expect(remove).toBeDisabled() // a changed impact needs a fresh confirmation
  await dialog.getByText('I understand this agent identity is retired permanently.').click()
  await remove.click()
  await expect(dialog).toBeHidden()
  await expect(inspector.getByText('Removal admitted and in progress.')).toBeVisible()
  const sent = posts(log).filter((item) => item.path === '/agents/rep-2/remove').map((item) => item.body)
  expect(sent.map((body) => body.plan_token)).toEqual(['token-1', 'token-2'])
  expect(sent[0].request_key).not.toEqual(sent[1].request_key)
  expect(sent[1]).toEqual(expect.objectContaining({ mode: 'single', task_disposition: 'preserve', reason: null }))
})

test('a blocked removal says what is pending and retries only its safe steps', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'report', {
    agents: { 'rep-2': { removal: { state: 'blocked', removal_id: 'rm1', mode: 'single', failure: 'stop_not_confirmed', removed_at: null } } },
    api: ({ method, path }) => (method === 'GET' && path === '/agent-removals/rm1'
      ? { body: { mode: 'single', members: [{ agent_id: 'rep-2', phase: 'recovery_required', failure: 'stop_not_confirmed' }] } } : undefined),
  }, 'Now')
  await expect(inspector.getByRole('heading', { name: 'Removal blocked' })).toBeVisible()
  await expect(inspector).toContainText('Its process could not be confirmed stopped.')
  await inspector.getByRole('button', { name: 'Retry safe removal steps' }).click()
  await expect(inspector.getByText('Removal retried.')).toBeVisible()
  expect(posts(log).map((item) => item.path)).toEqual(['/agent-removals/rm1/retry'])
})

test('recovery stays available for a task’s last agent, while execution actions do not', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'cli', {
    agents: { 'cli-2': { removal: { state: 'blocked', removal_id: 'rm9', mode: 'single', failure: 'execution_unresolved', removed_at: null } } },
    api: ({ method, path }) => (method === 'GET' && path === '/agent-removals/rm9'
      ? { body: { mode: 'single', members: [{ agent_id: 'cli-2', phase: 'recovery_required', failure: 'execution_unresolved' }] } } : undefined),
  }, 'Now')
  await expect(inspector.getByRole('heading', { name: 'Removal blocked' })).toBeVisible()
  await expect(inspector.getByRole('button', { name: 'Stop agent' })).toHaveCount(0)
  await inspector.getByRole('button', { name: 'Retry safe removal steps' }).click()
  await expect(inspector.getByText('Removal retried.')).toBeVisible()
  expect(posts(log).map((item) => item.path)).toEqual(['/agent-removals/rm9/retry'])
})

test('a task whose agent was removed gets a new owner only after an explicit confirmation', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'migrate', { api: ({ method, path }) => (method === 'GET' && path === '/tasks/migrate/responsibility'
    ? { body: { awaiting_transfer: true, transferable: true, task_status: 'in_progress', removed_owner: { assignment_id: 'as-mig' }, reasons: [], transfers: [] } } : undefined) }, 'Now')
  await expect(inspector).toContainText('The task waits for you to choose a new owner.')
  await inspector.getByText('Choose a new owner', { exact: true }).click()
  await inspector.getByLabel('New agent', { exact: true }).selectOption('codex')
  const assign = inspector.getByRole('button', { name: 'Assign new owner' })
  await expect(assign).toBeDisabled()
  await inspector.getByText('Start a new agent that takes responsibility for this task.').click()
  await assign.click()
  await expect(inspector.getByText('A new owner took over this task.')).toBeVisible()
  const [sent] = posts(log)
  expect(sent.path).toBe('/tasks/migrate/responsibility/transfer')
  expect(sent.body).toEqual(expect.objectContaining({ expected_owner_assignment_id: 'as-mig', agent_type: 'codex', sandbox: 'read-only', reason: null }))
})

test('an unresolved run is shown, diagnostics can be refreshed and release needs an attested statement', async ({ page }) => {
  const execution = { execution_id: 'e1', provider: 'codex', state: 'uncertain', created_at: '2026-10-29T11:00:00Z', uncertainty: 'unsupervised',
    diagnostic: { observed_at: '2026-10-29T12:04:00Z', status: 'no_candidates', sources: {}, candidates: [] },
    attestation_offer: { eligible: true, reviewed_diagnostic_id: 'd1', review_window_seconds: 600, statement: 'I checked that no process of this run is still running.' } }
  const { log, inspector } = await openTask(page, 'report', { api: ({ method, path }) => {
    if (method === 'GET' && path === '/executions/unresolved') return { body: { executions: [execution] } }
    if (method === 'POST' && path === '/executions/e1/attestation') return { body: { outcome: 'settled_by_attestation' } }
    return undefined
  } }, 'Now')
  await expect(inspector.getByRole('heading', { name: /Process ownership · 1 unresolved/ })).toBeVisible()
  await inspector.getByRole('button', { name: 'Refresh diagnostics' }).click()
  await expect(inspector.getByText('Diagnostics refreshed.')).toBeVisible()
  await inspector.getByText('I verified it myself').click()
  const release = inspector.getByRole('button', { name: 'Release on my responsibility' })
  await expect(release).toBeDisabled()
  await inspector.getByText('I have read this statement and accept it.').click()
  await release.click()
  await expect(inspector.getByText('Released on your attestation.', { exact: false })).toBeVisible()
  const sent = posts(log)
  expect(sent.map((item) => item.path)).toEqual(['/executions/e1/diagnostics', '/executions/e1/attestation'])
  expect(sent[1].body).toEqual(expect.objectContaining({ reviewed_diagnostic_id: 'd1', statement: 'I checked that no process of this run is still running.', reason: null }))
})

test('Auto allocation and provider permissions are edited as drafts and saved explicitly', async ({ page }) => {
  const { log, inspector } = await openTask(page, 'csv', { api: ({ method, path }) => {
    if (method === 'GET' && path === '/tasks/csv/auto-allocation') return { body: { policy: null, inherited: false, own_revision: null, categories: ['general', 'coding'], children: [] } }
    if (method === 'GET' && path === '/tasks/csv/provider-restrictions') return { body: { task_id: 'csv', own: null, inherited: ['codex', 'claude'], effective: ['codex', 'claude'], sources: [] } }
    return undefined
  } })
  await inspector.getByRole('button', { name: 'Set Auto for delegated children' }).click()
  await inspector.getByText('Auto: best suitable provider and model for delegated children').click()
  await inspector.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(inspector.getByText('Auto policy saved.')).toBeVisible()
  await inspector.getByRole('button', { name: 'Restrict providers for this task' }).click()
  await expect(inspector.getByRole('checkbox', { name: /Gemini CLI \(not allowed above this task\)/ })).toBeDisabled()
  await inspector.getByRole('checkbox', { name: 'Claude Code' }).uncheck()
  await inspector.getByRole('button', { name: 'Save restriction' }).click()
  await expect(inspector.getByText('Restriction saved.')).toBeVisible()
  expect(posts(log).map((item) => [item.method, item.path, item.body])).toEqual([
    ['PUT', '/tasks/csv/auto-allocation', { enabled: true, providers: ['claude'], models: {}, preferences: [], profile: {}, expected_revision: null }],
    ['PUT', '/tasks/csv/provider-restrictions', { providers: ['codex'], expected_revision: null }],
  ])
})

test('output pages back through earlier lines', async ({ page }) => {
  const { inspector } = await openTask(page, 'report', { api: ({ path, search }) => {
    if (path !== '/agents/rep-2/output') return undefined
    const before = new URLSearchParams(search).get('before')
    return { body: before ? { agent_id: 'rep-2', items: [{ seq: 0, text: 'Earliest line' }], has_older: false, has_newer: true }
      : { agent_id: 'rep-2', items: [{ seq: 1, text: 'Reading the data' }, { seq: 2, text: 'Rendering totals' }], has_older: true, has_newer: false } }
  } }, 'Output')
  await expect(inspector).toContainText('Rendering totals')
  await inspector.getByRole('button', { name: 'Show earlier lines' }).click()
  await expect(inspector).toContainText('Earliest line')
})

test('no serious accessibility violations in agent controls and dialogs', async ({ page }) => {
  for (const theme of ['light', 'dark']) {
    await page.addInitScript((value) => localStorage.setItem('control-center.theme', value), theme)
    const { inspector } = await openTask(page, 'quoted', {}, null)
    const scan = async (label) => {
      await page.addScriptTag({ path: axePath })
      const result = await page.evaluate(async () => window.axe.run(document, { resultTypes: ['violations'] }))
      const serious = result.violations.filter((item) => ['serious', 'critical'].includes(item.impact))
      console.log(`axe ${theme} ${label}: ${result.violations.map((item) => `${item.id} (${item.impact})`).join(', ') || 'no violations'}`)
      expect(serious.map((item) => `${item.id}: ${item.nodes.map((n) => n.target.join(' ')).join(' / ')}`)).toEqual([])
    }
    await inspector.getByText('Delegate future decisions').click()
    await inspector.getByText('Switch tool or model').click()
    await scan('question')
    await inspector.getByRole('tab', { name: 'Settings' }).click()
    await inspector.getByText('Change this agent’s defaults').click()
    await scan('settings')
    await inspector.getByRole('button', { name: 'Stop agent' }).click()
    await expect(page.getByRole('dialog')).toBeVisible()
    await page.waitForTimeout(400) // let the dialog finish appearing before measuring colours
    await scan('stop dialog')
    await page.keyboard.press('Escape')
    await page.unroute('**/*')
  }
})
