// Test harness: serves the production build and a deterministic fixture backend through request
// routing, so no server process is started and no real provider can run.
import { readFile } from 'node:fs/promises'
import { extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const workspace = JSON.parse(await readFile(new URL('../tests/fixtures/workspace-sample.json', import.meta.url), 'utf8'))
// Real GET /workspace/tasks responses recorded from an isolated backend (see tests/fixtures/README.md).
export const contract = JSON.parse(await readFile(new URL('../tests/fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.json': 'application/json' }

// A stand-in EventSource that opens and stays open. It counts connections so tests can prove
// there is never more than one.
function fakeEventSource() {
  window.__events = { created: 0, closed: 0 }
  class FakeEventSource extends EventTarget {
    constructor(url) {
      super()
      this.url = url
      this.readyState = 0
      window.__events.created++
      ;(window.__sources ||= []).push(this)
      setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen?.(new Event('open')) } }, 20)
    }
    close() { if (this.readyState !== 2) { this.readyState = 2; window.__events.closed++ } }
  }
  window.EventSource = FakeEventSource
}

export const QUESTION = 'Should quoted fields keep their line breaks?'
export const PROJECTS = [{ project_id: 'p1', name: 'Release planning', root_path: 'fixture' }]

// GET /agents/{id}?include_output=false for an agent that appears on a task node, shaped like the
// backend's agent read model. `extra` overrides fields (a test's scripted state).
export function agentDetail(node, extra = {}) {
  const agent = node.agent
  return {
    agent_id: agent.agent_id, status: agent.status, task: `Synthetic instructions for ${node.title}.`, display_name: agent.display_name,
    display_color: agent.display_color, agent_type: agent.agent_type, model: agent.model, sandbox: 'workspace-write',
    provider_condition: agent.provider_condition, provider_history: [], managed: false, effort: agent.effort, session_config: null,
    latest_execution: null, resume: null, parent_id: null, child_ids: [], session_id: 'session-fixture',
    waiting_question: agent.asks_user ? QUESTION : null, waiting_context: node.assignment.current ? node.waiting_context : null,
    similar_decisions_enabled: false, always_decide_enabled: false, watcher_lost: null, task_ids: [node.task_id],
    allocation: null, allocation_task_id: node.task_id, provider_permission: null, auto_allocation: null,
    removed: agent.removed, removal: agent.removal, ...extra,
  }
}

// An account-scoped model catalog (GET /providers/{p}/models) listing the scenarios' models.
export function catalog(provider) {
  const names = { claude: ['Sonnet 4.6', 'Opus 4.8', 'Haiku 4.5'], codex: ['GPT-5.5'], gemini: ['auto'] }[provider] || []
  return { provider, entitlement_verified: true, refresh: { fresh: true, last_success_at: '2026-10-29T11:00:00Z' },
    models: names.map((name) => ({ model_id: name, display_name: name, selectable: true, status: 'available', status_label: 'Available',
      explanation: 'Listed for this account.', supported_efforts: provider === 'gemini' ? [] : ['low', 'medium', 'high'] })) }
}

// Read-only detail responses for the inspector, shaped like the backend's (synthetic content).
function detail(pathname, search, tasks, projects, agents) {
  const nodes = [...tasks.tasks, ...tasks.ancestors]
  const byId = Object.fromEntries(nodes.map((node) => [node.task_id, node]))
  let match = /^\/tasks\/([^/]+)$/.exec(pathname)
  if (match) {
    const node = byId[decodeURIComponent(match[1])]
    return node && { task_id: node.task_id, title: node.title, description: `Synthetic description for ${node.title}.`, status: node.status,
      parent_task_id: node.parent_task_id, project_id: node.project_id }
  }
  match = /^\/tasks\/([^/]+)\/assignments$/.exec(pathname)
  if (match) {
    const node = byId[decodeURIComponent(match[1])]
    if (!node) return null
    const list = []
    for (const agentId of [...node.assignment.previous_agent_ids].reverse()) {
      if (agentId === node.assignment.last_ended?.agent_id) continue
      list.push({ assignment_id: `old-${agentId}`, task_id: node.task_id, agent_id: agentId, assigned_at: '2026-01-01T00:00:00.000Z', ended_at: '2026-01-01T00:00:01.000Z', ended_reason: 'removed' })
    }
    if (node.assignment.last_ended) list.push({ ...node.assignment.last_ended, task_id: node.task_id })
    if (node.assignment.current) list.push({ ...node.assignment.current, task_id: node.task_id, ended_at: null, ended_reason: null })
    return { assignments: list }
  }
  match = /^\/agents\/([^/]+)\/activity$/.exec(pathname)
  if (match) {
    const agentId = decodeURIComponent(match[1])
    const node = nodes.find((item) => item.agent?.agent_id === agentId)
    if (!node) return null
    return { context: { agent_id: agentId, task_id: node.task_id, waiting_question: node.agent.asks_user ? QUESTION : null },
      events: [{ id: 'start:1', kind: 'start', label: 'Worker assigned', at: node.assignment.current?.assigned_at || node.created_at }],
      delegations: [], blockers: node.blockers, result: null, bounded: true }
  }
  match = /^\/agents\/([^/]+)\/output$/.exec(pathname)
  if (match) {
    const agentId = decodeURIComponent(match[1])
    return { agent_id: agentId, items: [{ seq: 1, text: 'Reading the test suite' }, { seq: 2, text: 'Two tests fail on quoted newlines' }], has_older: false, has_newer: false }
  }
  if (pathname === '/projects') return projects === null ? null : { projects }
  if (pathname === '/executions/unresolved') return { executions: [] }
  match = /^\/providers\/([a-z]+)\/models$/.exec(pathname)
  if (match) return catalog(match[1])
  match = /^\/agents\/([^/]+)$/.exec(pathname)
  if (match) {
    const agentId = decodeURIComponent(match[1])
    const node = nodes.find((item) => item.agent?.agent_id === agentId)
    return node ? agentDetail(node, agents[agentId] || {}) : null
  }
  return null
}

// `api` scripts any other backend answer: ({ method, path, search, body }) => { status, body } or
// undefined to fall through. Every backend request is recorded in `log.requests` with its body.
export async function openApp(page, { backend = 'online', path = '/', tasks = contract.all, taskPages = null, projects = PROJECTS,
  agents = {}, api = null } = {}) {
  const log = { backend: [], external: [], requests: [] }
  await page.addInitScript(fakeEventSource)
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.host === 'atlas.preview') {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '')
      try {
        const body = await readFile(dist + file)
        return route.fulfill({ status: 200, body, contentType: TYPES[extname(file)] || 'application/octet-stream' })
      } catch {
        return route.fulfill({ status: 404, body: 'not found' })
      }
    }
    if (url.host === '127.0.0.1:8000') {
      log.backend.push(`${request.method()} ${url.pathname}${url.search}`)
      let body = null
      try { body = request.postData() ? JSON.parse(request.postData()) : null } catch { body = request.postData() }
      const entry = { method: request.method(), path: url.pathname, search: url.search, body }
      log.requests.push(entry)
      if (backend === 'offline') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
      const scripted = api ? await api(entry) : undefined
      if (scripted) return route.fulfill({ status: scripted.status || 200, contentType: 'application/json', body: JSON.stringify(scripted.body ?? {}) })
      if (request.method() === 'GET' && url.pathname === '/workspace') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(workspace) })
      if (request.method() === 'GET' && url.pathname === '/workspace/tasks') {
        const body = taskPages ? await taskPages(url) : tasks
        if (body === null) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      }
      if (request.method() === 'GET' && url.pathname === '/projects' && projects === null) {
        return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
      }
      const found = request.method() === 'GET' ? detail(url.pathname, url.search, tasks, projects, agents) : null
      if (found) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(found) })
      if (request.method() === 'GET' && url.pathname === '/providers/notifications') return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      // The classic interface synchronises its notification preference on load (an existing behaviour).
      if (request.method() === 'GET' && url.pathname === '/security/csrf-token') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_token":"fixture"}' })
      if (request.method() === 'POST' && url.pathname.startsWith('/notifications/')) return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      // An action the test did not script succeeds plainly; tests that check refusals script them.
      if (request.method() !== 'GET' && /^\/(agents|tasks|agent-removals|executions|providers)\//.test(url.pathname)) {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, status: 'running' }) })
      }
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not in fixture"}' })
    }
    log.external.push(request.url())
    return route.abort()
  })
  await page.goto(path)
  return log
}

// Simulates the backend announcing a change on the single event connection.
export async function announceChange(page) {
  await page.evaluate(() => window.__sources.filter((source) => source.readyState !== 2).forEach((source) => source.dispatchEvent(new Event('agent-change'))))
}

export async function activeEventSources(page) {
  return page.evaluate(() => window.__events.created - window.__events.closed)
}
