// Which interface renders. The Lumen interface (the code is still named Atlas) is the default;
// the classic interface stays one step away as a fallback and renders only while the address
// carries `ui=classic` in its query string. Leaving Classic removes that marker again.
export const UI_EVENT = 'control-center:ui-change'
export const LAST_ROUTE_KEY = 'control-center.atlas.last-route'
export const DEFAULT_ATLAS_HASH = '#/work'
const CLASSIC = /(^|[?&])ui=classic(?=&|$)/

export function resolveUi(location) {
  return CLASSIC.test(location?.search || '') ? 'classic' : 'atlas'
}

// The route to open when leaving Classic: the one last used in this tab, if valid.
export function atlasEntryHash(storage) {
  let saved = null
  try { saved = (storage ?? globalThis.sessionStorage)?.getItem(LAST_ROUTE_KEY) } catch { /* Storage is optional. */ }
  return typeof saved === 'string' && /^#\/[a-z-]+(\?[^#\s]*)?$/.test(saved) ? saved : DEFAULT_ATLAS_HASH
}

export function rememberAtlasHash(hash, storage) {
  if (!/^#\//.test(hash || '')) return
  try { (storage ?? globalThis.sessionStorage)?.setItem(LAST_ROUTE_KEY, hash) } catch { /* Storage is optional. */ }
}

function withoutClassic(search) {
  const kept = String(search || '').replace(/^\?/, '').split('&').filter((part) => part && part !== 'ui=classic')
  return kept.length ? `?${kept.join('&')}` : ''
}

// The classic URL keeps the path and the other query parameters, adds the Classic marker and drops
// only the Lumen route.
export function classicUrl(location) {
  const search = withoutClassic(location.search)
  return `${location.pathname || '/'}${search ? `${search}&` : '?'}ui=classic`
}

export function enterAtlas(win = globalThis) {
  const { pathname, search } = win.location
  win.history.pushState(null, '', `${pathname || '/'}${withoutClassic(search)}${atlasEntryHash(win.sessionStorage)}`)
  win.dispatchEvent(new win.Event(UI_EVENT))
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
