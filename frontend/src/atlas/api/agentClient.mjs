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
  // Applying a root task's work to its project: a read-only preview of the files first, then the
  // backend's single integrate step (which re-checks every safety condition itself).
  integrationPreview: (taskId, options) => send('GET', `${task(taskId)}/integration-preview`, undefined, options),
  applyToProject: (taskId, options) => send('POST', `${task(taskId)}/integrate`, undefined, options),
  // Connections (M8): the same endpoints and payloads as Classic's AI Providers view. Setup runs
  // only from a reviewed plan (plan_id + digest, used once); checks and catalog refreshes are
  // non-billable local reads; settings payloads come from model/connections.mjs (billingView).
  checkProvider: (provider, options) => send('POST', `/providers/${encode(provider)}/check`, undefined, options),
  checkUpdates: (options) => send('POST', '/providers/updates/check', undefined, options),
  planOperation: (provider, request, options) => send('POST', `/providers/${encode(provider)}/operations/plan`, request, options),
  startOperation: (provider, approval, options) => send('POST', `/providers/${encode(provider)}/operations`, approval, options),
  cancelOperation: (operationId, options) => send('POST', `/provider-operations/${encode(operationId)}/cancel`, undefined, options),
  resolveOperation: (operationId, options) => send('POST', `/provider-operations/${encode(operationId)}/resolve`, { confirm_ended: true }, options),
  providerSettings: (provider, payload, options) => send('PUT', `/providers/${encode(provider)}/settings`, payload, options),
  clearUsageLimit: (provider, options) => send('POST', `/providers/${encode(provider)}/usage/allow`, undefined, options),
  refreshUsage: (provider, options) => send('POST', `/providers/${encode(provider)}/usage/refresh`, undefined, options),
  savePolicy: (payload, options) => send('PUT', '/provider-policy', payload, options),
  // Settings (M8): the notification preference Classic also sends (it is shared with Classic).
  notificationPreference: (enabled, options) => send('POST', '/notifications/preferences', { enabled }, options),
  // Notifications (M9). The heartbeat is Classic's: it tells the backend that this page is showing
  // browser notifications, so the backend holds back its Windows notification for the same event.
  notificationHeartbeat: (options) => send('POST', '/notifications/heartbeat', undefined, options),
  // Provider update notices, per version, exactly as Classic's bell: 'dismiss' or 'remind_later'.
  dismissNotice: (provider, version, mode, options) => send('POST', `/providers/${encode(provider)}/notifications/dismiss`, { version, mode }, options),
  // Process ownership.
  refreshDiagnostics: (executionId, options) => send('POST', `/executions/${encode(executionId)}/diagnostics`, undefined, options),
  attest: (executionId, body, options) => send('POST', `/executions/${encode(executionId)}/attestation`, body, options),
  // Project organization and lifecycle (schema v39). Names and order are presentation only; the
  // Trash moves the real folder, and a permanent deletion removes it after a typed confirmation.
  renameProject: (projectId, name, options) => send('PATCH', `/projects/${encode(projectId)}`, { name }, options),
  reorderProjects: (projectIds, options) => send('PUT', '/projects/order', { project_ids: projectIds }, options),
  hideProject: (projectId, options) => send('POST', `/projects/${encode(projectId)}/hide`, undefined, options),
  unhideProject: (projectId, options) => send('POST', `/projects/${encode(projectId)}/unhide`, undefined, options),
  stopProjectWork: (projectId, options) => send('POST', `/projects/${encode(projectId)}/stop-work`, undefined, options),
  trashProject: (projectId, options) => send('POST', `/projects/${encode(projectId)}/trash`, undefined, options),
  restoreProject: (projectId, targetPath, options) => send('POST', `/projects/${encode(projectId)}/restore`,
    targetPath ? { target_path: targetPath } : {}, options),
  deleteProject: (projectId, confirmName, options) => send('DELETE', `/projects/${encode(projectId)}`, { confirm_name: confirmName }, options),
  // A Task's display title only; its instructions and history are unchanged.
  renameTask: (taskId, title, options) => send('PATCH', `${task(taskId)}`, { title }, options),
}

// The lifecycle preview is a read (GET), but it is asked for at a moment the person chose, not kept
// live: it runs local Git reads, so it is fetched once per dialog.
export const projectLifecycle = (projectId, options) => send('GET', `/projects/${encode(projectId)}/lifecycle`, undefined, options)

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
