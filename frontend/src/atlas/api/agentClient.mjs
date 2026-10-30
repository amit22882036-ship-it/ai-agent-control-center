// The only Atlas module that sends state-changing requests. Every call goes through the shared
// `apiFetch` client (CSRF header, retry only for a token rejected before execution) and uses the
// same endpoints and payloads as the classic inspector, so the backend's eligibility checks,
// refusals and messages are unchanged. Nothing here decides whether an action is allowed.
import { API_BASE, apiFetch } from '../../apiClient.mjs'

const encode = encodeURIComponent

// `{ ok, status, data, offline }`; a network failure never throws to the caller.
export async function send(method, path, body, { fetcher = apiFetch, base = API_BASE } = {}) {
  try {
    const response = await fetcher(`${base}${path}`, {
      method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }) })
    const data = await response.json().catch(() => ({}))
    return { ok: response.ok, status: response.status, data, offline: false }
  } catch {
    return { ok: false, status: 0, data: null, offline: true }
  }
}

const agent = (id) => `/agents/${encode(id)}`
const task = (id) => `/tasks/${encode(id)}`

export const agentActions = {
  // Instructions. `fields` come from modelPresentation.instructionPlan (one-time override, explicit
  // attempt, new-session confirmation); an explicit defaults change is sent first by the caller.
  reply: (id, answer, fields, options) => send('POST', `${agent(id)}/reply`, { answer, ...fields }, options),
  redirect: (id, instruction, fields, options) => send('POST', `${agent(id)}/redirect`, { instruction, ...fields }, options),
  decide: (id, options) => send('POST', `${agent(id)}/decide`, undefined, options),
  similar: (id, enable, options) => send('POST', `${agent(id)}/decide-similar${enable ? '' : '/disable'}`, undefined, options),
  always: (id, enable, options) => send('POST', `${agent(id)}/decide-always${enable ? '' : '/disable'}`, undefined, options),
  stop: (id, options) => send('POST', `${agent(id)}/stop`, undefined, options),
  stopBranch: (id, options) => send('POST', `${agent(id)}/stop-branch`, undefined, options),
  // Provider, model and defaults.
  resume: (id, body, options) => send('POST', `${agent(id)}/resume`, body, options),
  changeProvider: (id, payload, options) => send('POST', `${agent(id)}/provider`, payload, options),
  saveDefaults: (id, payload, options) => send('POST', `${agent(id)}/defaults`, payload, options),
  refreshClaudeUsage: (options) => send('POST', '/providers/claude/usage/refresh', undefined, options),
  // Identity.
  rename: (id, displayName, options) => send('POST', `${agent(id)}/rename`, { display_name: displayName }, options),
  color: (id, displayColor, options) => send('POST', `${agent(id)}/color`, { display_color: displayColor }, options),
  // Removal and responsibility.
  removalPreview: (id, mode, options) => send('POST', `${agent(id)}/removal-preview`, { mode }, options),
  remove: (id, body, options) => send('POST', `${agent(id)}/remove`, body, options),
  retryRemoval: (removalId, options) => send('POST', `/agent-removals/${encode(removalId)}/retry`, {}, options),
  transfer: (taskId, body, options) => send('POST', `${task(taskId)}/responsibility/transfer`, body, options),
  // Task policies (the agent's allocation task).
  saveAutoPolicy: (taskId, payload, options) => send('PUT', `${task(taskId)}/auto-allocation`, payload, options),
  removeAutoPolicy: (taskId, revision, options) => send('DELETE', `${task(taskId)}/auto-allocation?expected_revision=${encode(revision)}`, undefined, options),
  saveRestriction: (taskId, payload, options) => send('PUT', `${task(taskId)}/provider-restrictions`, payload, options),
  removeRestriction: (taskId, revision, options) => send('DELETE', `${task(taskId)}/provider-restrictions?expected_revision=${encode(revision)}`, undefined, options),
  // Creation (M5). Payloads come from autonomousCreation.creationPayload, as in the classic form.
  checkProviders: (options) => send('POST', '/providers/check', undefined, options),
  startRoot: (payload, options) => send('POST', '/agents/start', payload, options),
  startChild: (parentId, payload, options) => send('POST', `${agent(parentId)}/children/start`, payload, options),
  startTaskAgent: (taskId, payload, options) => send('POST', `${task(taskId)}/start-agent`, payload, options),
  registerProject: (name, rootPath, options) => send('POST', '/projects', { name, root_path: rootPath }, options),
  // Task lifecycle (M7). Pause and Cancel are previewed first with the read-only control-impact.
  controlImpact: (taskId, action, options) => send('POST', `${task(taskId)}/control-impact`, { action }, options),
  pauseTask: (taskId, options) => send('POST', `${task(taskId)}/pause`, undefined, options),
  resumeTask: (taskId, options) => send('POST', `${task(taskId)}/resume`, undefined, options),
  cancelTask: (taskId, options) => send('POST', `${task(taskId)}/cancel`, undefined, options),
  // Process ownership.
  refreshDiagnostics: (executionId, options) => send('POST', `/executions/${encode(executionId)}/diagnostics`, undefined, options),
  attest: (executionId, body, options) => send('POST', `/executions/${encode(executionId)}/attestation`, body, options),
}

export const OFFLINE_MESSAGE = 'The backend could not be reached. Nothing is assumed; retry when it is available.'

// The backend's own refusal text when it gave one (string detail, structured message or a
// validation list), otherwise the classic fallback for that action.
export function refusalText(result, fallback) {
  if (result?.offline) return OFFLINE_MESSAGE
  const detail = result?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  if (detail && typeof detail.message === 'string' && detail.message) return detail.message
  if (Array.isArray(detail)) {
    const text = detail.map((item) => item?.msg).filter(Boolean).join(' ')
    if (text) return text
  }
  return fallback
}
