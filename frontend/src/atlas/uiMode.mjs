// Which interface renders. Atlas is an explicit opt-in preview: it renders only while the URL
// carries an Atlas route (a hash that starts with "#/"). Every other URL shows the classic
// interface, which stays the default. Leaving Atlas removes the hash, so Back returns to Atlas.
export const UI_EVENT = 'control-center:ui-change'
export const LAST_ROUTE_KEY = 'control-center.atlas.last-route'
export const DEFAULT_ATLAS_HASH = '#/work'

export function resolveUi(location) {
  return /^#\//.test(location?.hash || '') ? 'atlas' : 'classic'
}

// The Atlas route to open from the classic interface: the one last used in this tab, if valid.
export function atlasEntryHash(storage) {
  let saved = null
  try { saved = (storage ?? globalThis.sessionStorage)?.getItem(LAST_ROUTE_KEY) } catch { /* Storage is optional. */ }
  return typeof saved === 'string' && /^#\/[a-z-]+(\?[^#\s]*)?$/.test(saved) ? saved : DEFAULT_ATLAS_HASH
}

export function rememberAtlasHash(hash, storage) {
  if (resolveUi({ hash }) !== 'atlas') return
  try { (storage ?? globalThis.sessionStorage)?.setItem(LAST_ROUTE_KEY, hash) } catch { /* Storage is optional. */ }
}

// The classic URL keeps the path and query string and drops only the Atlas route.
export function classicUrl(location) {
  return `${location.pathname || '/'}${location.search || ''}`
}

export function enterAtlas(win = globalThis) {
  win.location.hash = atlasEntryHash(win.sessionStorage)
}

// Leaving Atlas from a selected task can carry its current agent to Classic, which then opens with
// that agent selected. The record lives in this tab's session storage for a few seconds only and
// is cleared once Classic has read it; a missing, expired or malformed record selects nothing.
export const CLASSIC_HANDOFF_KEY = 'control-center.atlas.classic-agent'
export const CLASSIC_HANDOFF_MS = 30000

export function writeClassicHandoff(agentId, storage, now = Date.now()) {
  try {
    if (typeof agentId === 'string' && agentId.length > 0 && agentId.length <= 200) {
      (storage ?? globalThis.sessionStorage)?.setItem(CLASSIC_HANDOFF_KEY, JSON.stringify({ agentId, at: now }))
    } else (storage ?? globalThis.sessionStorage)?.removeItem(CLASSIC_HANDOFF_KEY)
  } catch { /* Classic then simply opens without a selection. */ }
}

export function peekClassicHandoff(storage, now = Date.now()) {
  let record = null
  try { record = JSON.parse((storage ?? globalThis.sessionStorage)?.getItem(CLASSIC_HANDOFF_KEY) || 'null') } catch { return null }
  const fresh = record && typeof record.at === 'number' && now - record.at >= 0 && now - record.at <= CLASSIC_HANDOFF_MS
  return fresh && typeof record.agentId === 'string' && record.agentId.length > 0 && record.agentId.length <= 200 ? record.agentId : null
}

export function clearClassicHandoff(storage) {
  try { (storage ?? globalThis.sessionStorage)?.removeItem(CLASSIC_HANDOFF_KEY) } catch { /* Nothing to clear. */ }
}

export function exitAtlas(win = globalThis, { agentId = null } = {}) {
  rememberAtlasHash(win.location.hash, win.sessionStorage)
  writeClassicHandoff(agentId, win.sessionStorage)
  win.history.pushState(null, '', classicUrl(win.location))
  win.dispatchEvent(new win.Event(UI_EVENT))
}
