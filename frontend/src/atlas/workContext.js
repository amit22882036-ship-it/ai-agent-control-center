// The Work view a person last had (project, filters, colours, view, side list, focus), so leaving
// a Task page for Work returns to it. Kept in memory and in this tab's sessionStorage only.
const KEY = 'control-center.atlas-work'
const KEPT = ['project', 'filter', 'colors', 'lens', 'side', 'focus']
let last = null

export function rememberWork(params) {
  last = Object.fromEntries(KEPT.filter((key) => params?.[key]).map((key) => [key, params[key]]))
  try { sessionStorage.setItem(KEY, JSON.stringify(last)) } catch { /* Memory alone is enough for this tab. */ }
}

export function lastWork() {
  if (last) return last
  try {
    const saved = JSON.parse(sessionStorage.getItem(KEY) || 'null')
    return saved && typeof saved === 'object' ? Object.fromEntries(KEPT.filter((key) => typeof saved[key] === 'string').map((key) => [key, saved[key]])) : {}
  } catch {
    return {}
  }
}

// A reply the person asked for (a row's Reply, the R key, the notifications panel or the "need you"
// button): the Task's answer field takes the cursor once, then the request is forgotten. It is
// only ever acted on by an explicit request, never by a refresh. In memory only.
let pendingReply = null
const listeners = new Set()
const FRESH_MS = 30000
export function requestReply(taskId) {
  pendingReply = taskId ? { taskId, at: Date.now() } : null
  for (const listener of listeners) listener(taskId)
}
export function replyPending(taskId) {
  return Boolean(pendingReply && pendingReply.taskId === taskId && Date.now() - pendingReply.at <= FRESH_MS)
}
export function takeReply(taskId) {
  if (!replyPending(taskId)) return false
  pendingReply = null
  return true
}
export function onReplyRequest(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
