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

export function exitAtlas(win = globalThis) {
  rememberAtlasHash(win.location.hash, win.sessionStorage)
  win.history.pushState(null, '', classicUrl(win.location))
  win.dispatchEvent(new win.Event(UI_EVENT))
}
