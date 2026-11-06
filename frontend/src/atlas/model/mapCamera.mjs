// Camera arithmetic for the Work Map. A camera is { k, x, y }: screen = world * k + (x, y), where
// screen coordinates are relative to the map pane. `view` is the part of the pane that is not
// covered by the side list or a floating panel: { l, w, h } (left offset, width, height).
// Pure functions; the Map component applies the result.

export const ZOOM = { min: 0.25, max: 1.6, legible: 0.85, step: 1.2 }
const PAD = { x: 36, top: 64, bottom: 28 }

export const clampZoom = (k) => Math.max(ZOOM.min, Math.min(ZOOM.max, k))

// Fits a box. When it would only fit at an unreadable size, keeps text legible and centres on
// `anchor` (a box inside it, usually the selected Task) instead.
export function fitCamera(box, view, { maxK = 1, anchor = null, allowSmall = false } = {}) {
  if (!box || view.w <= 0 || view.h <= 0) return null
  const w = Math.max(1, box.maxX - box.minX)
  const h = Math.max(1, box.maxY - box.minY)
  const fit = Math.min(maxK, (view.w - 2 * PAD.x) / w, (view.h - PAD.top - PAD.bottom) / h)
  if (fit >= ZOOM.legible || (allowSmall && !anchor)) {
    const k = clampZoom(Math.min(fit, maxK))
    return { k, x: view.l + (view.w - w * k) / 2 - box.minX * k, y: Math.max(PAD.top, (view.h - h * k) / 2) - box.minY * k }
  }
  const k = clampZoom(Math.max(ZOOM.legible, Math.min(maxK, fit)))
  const a = anchor || box
  const ax = (a.minX + a.maxX) / 2
  let x
  if (w * k <= view.w - 2 * PAD.x) {
    // Only too tall: centre it across.
    x = view.l + (view.w - w * k) / 2 - box.minX * k
  } else {
    // Too wide: centre the anchor, but never leave empty space beside the box's edges.
    x = view.l + view.w / 2 - ax * k
    x = Math.min(x, view.l + PAD.x - box.minX * k)
    x = Math.max(x, view.l + view.w - PAD.x - box.maxX * k)
  }
  const tall = h * k > view.h - PAD.top - PAD.bottom
  const y = tall ? Math.min(view.h / 2 - ((a.minY + a.maxY) / 2) * k, PAD.top - box.minY * k) : Math.max(PAD.top, (view.h - h * k) / 2) - box.minY * k
  return { k, x, y }
}

// Zooms by `factor` keeping the point (px, py) of the pane where it is.
export function zoomAt(camera, factor, px, py) {
  const k = clampZoom(camera.k * factor)
  return { k, x: px - (px - camera.x) * (k / camera.k), y: py - (py - camera.y) * (k / camera.k) }
}

// Centres a box in the view at the current zoom.
export function centreOn(camera, box, view) {
  const k = camera?.k ?? 0.9
  return { k, x: view.l + view.w / 2 - ((box.minX + box.maxX) / 2) * k, y: view.h / 2 - ((box.minY + box.maxY) / 2) * k }
}

// Moves only when the box is (partly) outside the view; otherwise returns the same camera. It
// moves only along the axis that needs it, centring the box there, so a box hidden by the side
// list does not also jump vertically. With `bounds` (the whole graph), the move never opens empty
// space before the graph's top or left edge, nor after its bottom or right edge when the graph is
// larger than the view.
export function ensureVisible(camera, box, view, margin = 24, bounds = null) {
  if (!camera) return null
  const { k } = camera
  const left = camera.x + box.minX * k
  const right = camera.x + box.maxX * k
  const top = camera.y + box.minY * k
  const bottom = camera.y + box.maxY * k
  const outX = left < view.l + margin || right > view.l + view.w - margin
  const outY = top < margin || bottom > view.h - margin
  if (!outX && !outY) return camera
  const centred = centreOn(camera, box, view)
  let x = outX ? centred.x : camera.x
  let y = outY ? centred.y : camera.y
  if (bounds && outX) {
    if ((bounds.maxX - bounds.minX) * k > view.w - 2 * PAD.x) x = Math.max(x, view.l + view.w - PAD.x - bounds.maxX * k)
    x = Math.min(x, view.l + PAD.x - bounds.minX * k)
  }
  if (bounds && outY) {
    if ((bounds.maxY - bounds.minY) * k > view.h - PAD.top - PAD.bottom) y = Math.max(y, view.h - PAD.bottom - bounds.maxY * k)
    y = Math.min(y, PAD.top - bounds.minY * k)
  }
  return { k, x, y }
}

// The world rectangle the view shows, grown by `margin` screen pixels (for drawing only what is near).
export function worldRect(camera, view, margin = 0) {
  return {
    minX: (view.l - margin - camera.x) / camera.k, maxX: (view.l + view.w + margin - camera.x) / camera.k,
    minY: (-margin - camera.y) / camera.k, maxY: (view.h + margin - camera.y) / camera.k,
  }
}

export const intersects = (a, b) => a.minX <= b.maxX && a.maxX >= b.minX && a.minY <= b.maxY && a.maxY >= b.minY

// Keeps the same Task at the same place on screen after the layout moved it.
export function followAnchor(camera, before, after) {
  if (!camera || !before || !after) return camera
  return { ...camera, x: camera.x - (after.x - before.x) * camera.k, y: camera.y - (after.y - before.y) * camera.k }
}

// Pans by a screen offset.
export const panBy = (camera, dx, dy) => ({ ...camera, x: camera.x + dx, y: camera.y + dy })
