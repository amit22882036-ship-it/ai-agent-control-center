// The Atlas inspector is docked on the right; its width is a per-browser convenience.
export const INSPECTOR = { min: 340, max: 600, default: 404 }
export const INSPECTOR_WIDTH_KEY = 'control-center.atlas.inspector-width'

export function clampInspector(value) {
  return Number.isFinite(value) ? Math.round(Math.max(INSPECTOR.min, Math.min(INSPECTOR.max, value))) : INSPECTOR.default
}

export function readInspectorWidth(storage) {
  let saved = null
  try { saved = Number((storage ?? globalThis.localStorage)?.getItem(INSPECTOR_WIDTH_KEY)) } catch { /* Optional. */ }
  return saved ? clampInspector(saved) : INSPECTOR.default
}

export function saveInspectorWidth(width, storage) {
  try { (storage ?? globalThis.localStorage)?.setItem(INSPECTOR_WIDTH_KEY, String(clampInspector(width))) } catch { /* Optional. */ }
}

// Dragging the left edge of a right-docked panel to the left makes it wider.
export function dragInspectorWidth(startWidth, startX, x) {
  return clampInspector(startWidth - (x - startX))
}

export function keyboardInspectorWidth(width, key) {
  if (key === 'Home') return INSPECTOR.default
  if (key === 'End') return INSPECTOR.max
  if (key === 'ArrowLeft') return clampInspector(width + 16)
  if (key === 'ArrowRight') return clampInspector(width - 16)
  return null
}

// The Work Map's direction (top to bottom or left to right) is also a per-browser convenience.
export const MAP_DIRECTION_KEY = 'control-center.atlas.map-direction'

export function readMapDirection(storage) {
  // Left to right unless the person chose top to bottom: a lead reads first, its helpers follow it.
  try { return (storage ?? globalThis.localStorage)?.getItem(MAP_DIRECTION_KEY) === 'down' ? 'down' : 'right' } catch { return 'right' }
}

export function saveMapDirection(direction, storage) {
  try { (storage ?? globalThis.localStorage)?.setItem(MAP_DIRECTION_KEY, direction === 'right' ? 'right' : 'down') } catch { /* Optional. */ }
}

// Whether the Work sheet lists finished families or only offers to ("open", "closed" or unset).
export const FINISHED_KEY = 'control-center.atlas.finished-open'
export function readFinishedOpen(storage) {
  try { const value = (storage ?? globalThis.localStorage)?.getItem(FINISHED_KEY); return value === 'open' ? true : value === 'closed' ? false : null } catch { return null }
}
export function saveFinishedOpen(open, storage) {
  try { (storage ?? globalThis.localStorage)?.setItem(FINISHED_KEY, open ? 'open' : 'closed') } catch { /* Optional. */ }
}

// What the person has looked at: when they last cleared "changed" marks, and when they last opened
// each Task (bounded, newest kept). Per browser; nothing is sent anywhere.
export const SEEN_KEY = 'control-center.atlas.seen'
const SEEN_LIMIT = 3000
export function readSeen(storage) {
  try {
    const value = JSON.parse((storage ?? globalThis.localStorage)?.getItem(SEEN_KEY) || 'null')
    if (value && typeof value.since === 'string') return { since: value.since, opened: value.opened && typeof value.opened === 'object' ? value.opened : {} }
  } catch { /* Start fresh. */ }
  return null
}
export function saveSeen(seen, storage) {
  try {
    const entries = Object.entries(seen.opened || {}).sort((a, b) => (a[1] < b[1] ? 1 : -1)).slice(0, SEEN_LIMIT)
    ;(storage ?? globalThis.localStorage)?.setItem(SEEN_KEY, JSON.stringify({ since: seen.since, opened: Object.fromEntries(entries) }))
  } catch { /* Optional. */ }
}

// Tasks opened recently, newest first, for "Go to".
export const RECENT_KEY = 'control-center.atlas.recent'
export function readRecent(storage) {
  try { const value = JSON.parse((storage ?? globalThis.localStorage)?.getItem(RECENT_KEY) || '[]'); return Array.isArray(value) ? value.filter((item) => typeof item === 'string').slice(0, 12) : [] } catch { return [] }
}
export function saveRecent(list, storage) {
  try { (storage ?? globalThis.localStorage)?.setItem(RECENT_KEY, JSON.stringify(list.slice(0, 12))) } catch { /* Optional. */ }
}
