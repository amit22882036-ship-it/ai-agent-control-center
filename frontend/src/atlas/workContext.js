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
