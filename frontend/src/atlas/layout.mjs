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
  try { return (storage ?? globalThis.localStorage)?.getItem(MAP_DIRECTION_KEY) === 'right' ? 'right' : 'down' } catch { return 'down' }
}

export function saveMapDirection(direction, storage) {
  try { (storage ?? globalThis.localStorage)?.setItem(MAP_DIRECTION_KEY, direction === 'right' ? 'right' : 'down') } catch { /* Optional. */ }
}
