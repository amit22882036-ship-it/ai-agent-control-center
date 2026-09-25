export const panelLimits = {
  left: { min: 112, max: 240, default: 144 },
  right: { min: 320, max: 600, default: 380 },
}
export const layoutKey = 'control-center.panel-layout'
export function clampWidth(side, value) {
  const limits = panelLimits[side]
  return Number.isFinite(value) ? Math.round(Math.max(limits.min, Math.min(limits.max, value))) : limits.default
}
export function readLayout(storage) {
  let saved
  try { saved = JSON.parse((storage ?? globalThis.localStorage).getItem(layoutKey)) } catch { /* Optional persistence. */ }
  // Ignore obsolete sidebar width/collapse; the next save removes those fields.
  return { right: clampWidth('right', saved?.right) }
}
export function dragWidth(side, initialWidth, initialX, x) {
  return clampWidth(side, initialWidth + (side === 'left' ? 1 : -1) * (x - initialX))
}
export function keyboardWidth(side, width, key) {
  if (key === 'Home') return panelLimits[side].default
  if (key === 'End') return panelLimits[side].max
  if (key !== 'ArrowLeft' && key !== 'ArrowRight') return null
  return dragWidth(side, width, 0, key === 'ArrowRight' ? 16 : -16)
}
