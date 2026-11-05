// Test harness: serves the production build and a deterministic fixture backend through request
// routing, so no server process is started and no real provider can run.
import { readFile } from 'node:fs/promises'
import { extname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { DEFAULT_ONBOARDING } from './providers.mjs'

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

// A Task's branch (itself and everything below it), in the scenario's order.
function branchOf(nodes, rootId) {
  const ids = new Set([rootId])
  let grew = true
  while (grew) {
    grew = false
    for (const node of nodes) if (node.parent_task_id && ids.has(node.parent_task_id) && !ids.has(node.task_id)) { ids.add(node.task_id); grew = true }
  }
  return ids
}

// GET /workspace/tasks?root=<id>: the branch as one page, with the ancestors that place it.
export function rootPage(tasks, rootId) {
  const nodes = [...tasks.tasks, ...tasks.ancestors]
  const byId = Object.fromEntries(nodes.map((node) => [node.task_id, node]))
  if (!byId[rootId]) return { tasks: [], ancestors: [], ancestors_truncated: false, next_after: null, total: 0, root: rootId }
  const ids = branchOf(nodes, rootId)
  const ancestors = []
  for (let parent = byId[rootId].parent_task_id; parent && byId[parent] && ancestors.length < 64; parent = byId[parent].parent_task_id) ancestors.push(byId[parent])
  const page = tasks.tasks.filter((node) => ids.has(node.task_id))
  return { tasks: page, ancestors, ancestors_truncated: false, next_after: null, total: page.length, root: rootId }
}

const TERMINAL = new Set(['completed', 'canceled'])

// The read-only POST /tasks/{id}/control-impact, computed the backend's way over the scenario.
export function controlImpact(tasks, taskId, action) {
  const nodes = [...tasks.tasks, ...tasks.ancestors]
  const byId = Object.fromEntries(nodes.map((node) => [node.task_id, node]))
  const root = byId[taskId]
  if (!root) return { status: 404, body: { detail: 'Task not found' } }
  if (root.status === 'completed' || (action === 'pause' && (root.status === 'canceled' || root.control_intent === 'canceled'))) {
    return { status: 409, body: { detail: 'Terminal work cannot be controlled' } }
  }
  const subtree = [...branchOf(nodes, taskId)]
  const affected = subtree.filter((id) => !TERMINAL.has(byId[id].status) && byId[id].control_intent !== 'canceled').sort()
  const workers = affected.filter((id) => byId[id].assignment.current && byId[id].agent?.status === 'running')
    .map((id) => ({ task_id: id, agent_id: byId[id].assignment.current.agent_id }))
  return { status: 200, body: { target_task_id: taskId, action, affected_task_ids: affected, descendant_task_ids: subtree.filter((id) => id !== taskId).sort(),
    external_dependent_task_ids: [], workers_to_stop: workers, completed_descendants: subtree.filter((id) => id !== taskId && byId[id].status === 'completed').sort(),
    canceled_descendants: subtree.filter((id) => id !== taskId && byId[id].status === 'canceled').sort(), blocked_task_ids: [], replanning_task_ids: [] } }
}

// Read-only detail responses for the inspector, shaped like the backend's (synthetic content).
function detail(pathname, search, tasks, projects, agents, providers) {
  const nodes = [...tasks.tasks, ...tasks.ancestors]
  const byId = Object.fromEntries(nodes.map((node) => [node.task_id, node]))
  let match = /^\/tasks\/([^/]+)$/.exec(pathname)
  if (match) {
    const node = byId[decodeURIComponent(match[1])]
    // The fields of the real GET /tasks/{id} (see tests/fixtures/task-detail.contract.json).
    return node && { task_id: node.task_id, title: node.title, description: node.description ?? `Synthetic description for ${node.title}.`, status: node.status,
      created_at: node.created_at, updated_at: node.updated_at, parent_task_id: node.parent_task_id, project_id: node.project_id,
      control_intent: node.control_intent || 'active', resume_status: null, block_resume_status: null, stop_required: node.stop_required ? 1 : 0,
      legacy_pause: 0, stop_reason: null, orchestration_handoff: null, replan_transitions: [], promotions: [], promotion_decisions: [],
      active_blockers: (node.blockers || []).map((code, index) => ({ id: `b-${node.task_id}-${index}`, task_id: node.task_id, blocker_type: 'coordination',
        source_task_id: null, reason_code: code, active: 1, hard: 1 })),
      replanning_reasons: [], replanning_required: false, resource_claims_pending: false,
      current_assignment: node.assignment.current ? { assignment_id: node.assignment.current.assignment_id, agent_id: node.assignment.current.agent_id,
        agent_display_name: node.agent?.display_name || null, agent_display_color: node.agent?.display_color || null, agent_status: node.agent?.status || null } : null }
  }
  match = /^\/tasks\/([^/]+)\/orchestration$/.exec(pathname)
  if (match) {
    const node = byId[decodeURIComponent(match[1])]
    return node && { task_id: node.task_id, continuation: node.continuation ? { phase: node.continuation.phase, mode: node.continuation.mode || null, last_error: null, plan_consumed: 0 } : null,
      materializations: [], decision: { state: node.agent?.autonomous ? (node.plan_revision ? 'promoted' : 'not_recorded') : 'not_authorized', revision: node.plan_revision ?? null,
        decision_key: null, rationale: null, accepted_at: null, assignment_id: node.assignment.current?.assignment_id || null, superseded: null,
        promotion_rejected: false, source: null, last_rejection: null, promotions: [] }, wake: { state: 'idle' } }
  }
  match = /^\/tasks\/([^/]+)\/integrations$/.exec(pathname)
  if (match) {
    const node = byId[decodeURIComponent(match[1])]
    if (!node) return null
    const record = (id, kind, destination, status, appliedAt) => ({ integration_id: id, source_task_id: node.task_id, destination_kind: kind, destination_id: destination,
      status, changed_paths: ['src/exporter.py', 'tests/test_exporter.py'], changed_paths_remaining: 0, conflict_paths: status === 'conflict' ? ['src/exporter.py'] : [],
      conflict_paths_remaining: 0, validation_status: 'not_run', failure_reason: status === 'conflict' ? 'Source and destination have conflicting changes' : null,
      created_at: appliedAt || node.updated_at, updated_at: appliedAt || node.updated_at, applied_at: status === 'applied' ? appliedAt : null })
    const list = []
    const via = node.delegation?.result?.integration
    if (via) list.push(record(via.integration_id, 'task', node.parent_task_id, via.status, via.applied_at))
    if (node.integration) list.push(record(node.integration.integration_id, node.integration.destination_kind || 'project', node.project_id, node.integration.status, node.integration.applied_at))
    return { integrations: list }
  }
  match = /^\/tasks\/([^/]+)\/delegations$/.exec(pathname)
  if (match) {
    const id = decodeURIComponent(match[1])
    return { delegations: nodes.filter((node) => node.parent_task_id === id && node.delegation).map((node) => ({
      delegation_id: node.delegation.delegation_id, parent_task_id: id, child_task_id: node.task_id, status: node.delegation.status, instruction: `Synthetic instruction for ${node.title}.`,
      results: node.delegation.result ? [{ result_id: `res-${node.task_id}`, child_task_id: node.task_id, outcome: node.delegation.result.outcome, phase: node.delegation.result.phase,
        summary: node.delegation.result.summary ?? `Synthetic summary: ${node.title} is done.`, integration_id: node.delegation.result.integration?.integration_id || null }] : [] })) }
  }
  if (/^\/tasks\/[^/]+\/(dependencies|dependents|control-operations)$/.test(pathname)) {
    const kind = pathname.split('/').pop()
    return { [kind === 'control-operations' ? 'operations' : kind]: [] }
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
  if (pathname === '/providers/onboarding') return typeof providers === 'function' ? providers() : providers
  if (pathname === '/system/process-supervision') return { status: 'supervised' }
  if (pathname === '/provider-policy') return { allowed: ['claude', 'codex', 'gemini'], default_provider: null }
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
// `providers` is the GET /providers/onboarding answer (or a function returning it); null answers 503.
// `notices` is the GET /providers/notifications answer, the same way.
export async function openApp(page, { backend = 'online', path = '/?ui=classic', tasks = contract.all, taskPages = null, projects = PROJECTS,
  agents = {}, api = null, providers = DEFAULT_ONBOARDING, notices = [] } = {}) {
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
      if (scripted?.abort) return route.abort('connectionreset') // the request may or may not have reached the backend
      if (scripted) return route.fulfill({ status: scripted.status || 200, contentType: 'application/json', body: JSON.stringify(scripted.body ?? {}) })
      if (request.method() === 'GET' && url.pathname === '/workspace') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(workspace) })
      if (request.method() === 'GET' && url.pathname === '/workspace/tasks') {
        const root = url.searchParams.get('root')
        const body = taskPages ? await taskPages(url) : root ? rootPage(tasks, root) : tasks
        if (body === null) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
      }
      if (request.method() === 'GET' && url.pathname === '/projects' && projects === null) {
        return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
      }
      if (request.method() === 'GET' && url.pathname === '/providers/onboarding' && providers === null) {
        return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
      }
      const found = request.method() === 'GET' ? detail(url.pathname, url.search, tasks, projects, agents, providers) : null
      if (found) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(found) })
      // GET /providers/notifications: `notices` is the list (or a function returning it); null answers 503.
      if (request.method() === 'GET' && url.pathname === '/providers/notifications') {
        const list = typeof notices === 'function' ? notices() : notices
        return list === null ? route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
          : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(list) })
      }
      // The classic interface synchronises its notification preference on load (an existing behaviour).
      if (request.method() === 'GET' && url.pathname === '/security/csrf-token') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_token":"fixture"}' })
      if (request.method() === 'POST' && url.pathname.startsWith('/notifications/')) return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      if (request.method() === 'POST' && url.pathname === '/providers/check') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ providers: ['claude', 'codex', 'gemini']
          .map((provider) => ({ provider, state: { state: 'ready' } })) }) })
      }
      const impact = /^\/tasks\/([^/]+)\/control-impact$/.exec(url.pathname)
      if (request.method() === 'POST' && impact) {
        const answer = controlImpact(tasks, decodeURIComponent(impact[1]), body?.action)
        return route.fulfill({ status: answer.status, contentType: 'application/json', body: JSON.stringify(answer.body) })
      }
      // An action the test did not script succeeds plainly; tests that check refusals script them.
      if (request.method() !== 'GET' && /^\/(agents|tasks|agent-removals|executions|providers|provider-operations|provider-policy)(\/|$)/.test(url.pathname)) {
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
