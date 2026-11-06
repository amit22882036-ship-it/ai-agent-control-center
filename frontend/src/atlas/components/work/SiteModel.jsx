import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { buildingSummary } from '../../model/siteLayout.mjs'

// The Site: the workspace drawn as an architectural massing model on a canvas, in an axonometric
// view the person can turn, tilt and zoom. Each family is a building whose floors are its tasks in
// their construction materials (see model/siteLayout.mjs). Everything drawn comes from recorded
// task states; motion only ever follows the person's hand or a recorded change.
//
// Interaction follows the platform's fluid-interface rules: the model tracks the pointer 1:1 while
// dragged, a flick keeps its velocity and comes to rest where its momentum projects, every camera
// change is a critically damped spring that can be grabbed again mid-flight, zoom rubber-bands at
// its limits, and reduced motion makes every change instant.
const DEG = Math.PI / 180
const reducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

// Critically damped spring, implicit Euler: stable for any frame time, never overshoots.
function spring(x, v, target, dt, response) {
  const w = (2 * Math.PI) / response
  const f = 1 + 2 * dt * w
  const hoo = dt * w * w
  const hhoo = dt * hoo
  const inv = 1 / (f + hhoo)
  return [(f * x + dt * v + hhoo * target) * inv, (v + hoo * (target - x)) * inv]
}
// Where a flick comes to rest (exponential deceleration, as scrolling does).
const projectMomentum = (velocity, rate = 0.994) => ((velocity / 1000) * rate) / (1 - rate)
const rubber = (over, dimension, k = 0.55) => (over * dimension * k) / (dimension + k * Math.abs(over))

function readPalette(element) {
  const css = getComputedStyle(element)
  const v = (name) => css.getPropertyValue(name).trim()
  const mat = (name) => ({ top: v(`--a-mat-${name}-top`), left: v(`--a-mat-${name}-l`), right: v(`--a-mat-${name}-r`) })
  return {
    board: v('--a-mat-board'), boardSide: v('--a-mat-board-side'), edge: v('--a-mat-edge'), shadow: v('--a-mat-shadow'),
    built: mat('built'), lit: mat('lit'), glass: mat('glass'), held: mat('held'), needs: mat('needs'), archive: mat('archive'),
    wire: v('--a-mat-wire'), glassEdge: v('--a-mat-glass-edge'), hatch: v('--a-mat-hatch'), litGlow: v('--a-mat-lit-glow'),
    needsGlow: v('--a-mat-needs-glow'), select: v('--a-mat-select'), hover: v('--a-mat-hover'), night: v('--a-mat-night') === '1',
    ink: v('--a-text-2'), inkSoft: v('--a-text-3'), font: v('--a-font'), labelBack: v('--a-mat-label'),
  }
}

function hatchPattern(ctx, color, dpr) {
  const size = Math.round(6 * dpr)
  const tile = document.createElement('canvas')
  tile.width = size
  tile.height = size
  const g = tile.getContext('2d')
  g.strokeStyle = color
  g.lineWidth = Math.max(1, 0.9 * dpr)
  g.beginPath()
  g.moveTo(0, size)
  g.lineTo(size, 0)
  g.moveTo(-size / 2, size / 2)
  g.lineTo(size / 2, -size / 2)
  g.moveTo(size / 2, size * 1.5)
  g.lineTo(size * 1.5, size / 2)
  g.stroke()
  return ctx.createPattern(tile, 'repeat')
}

function inside(poly, x, y) {
  let hit = false
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i]
    const [xj, yj] = poly[j]
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit
  }
  return hit
}

function hull(points) {
  const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1])
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])
  const lower = []
  for (const q of p) { while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop(); lower.push(q) }
  const upper = []
  for (let i = p.length - 1; i >= 0; i -= 1) { const q = p[i]; while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop(); upper.push(q) }
  return lower.slice(0, -1).concat(upper.slice(0, -1))
}

// Footprint corners in world coordinates, in the order whose outward edge normal is (dy, -dx).
const footprint = (b) => [[b.x, b.y], [b.x + b.w, b.y], [b.x + b.w, b.y + b.d], [b.x, b.y + b.d]]

export default function SiteModel({ mode = 'site', layout, selectedTop = null, titleOf, projectNameOf, onSelect, onOpen, onArchive, label,
  className = '', onReady }) {
  const box = useRef(null)
  const canvas = useRef(null)
  const tip = useRef(null)
  const strip = mode === 'strip'
  const [active, setActive] = useState(null)
  const state = useRef(null)
  if (!state.current) {
    state.current = {
      cam: { az: strip ? (layout.azimuth ?? 32) : 45, el: strip ? 30 : 38, zoom: 24, tx: 0, ty: 0 },
      goal: null, vel: { az: 0, el: 0, zoom: 0, tx: 0, ty: 0 }, size: { w: 0, h: 0, dpr: 1 }, palette: null, hatch: null,
      hits: [], hover: null, drag: null, raf: 0, last: 0, fitted: false, appear: new Map(), seen: null, lift: 0, liftVel: 0, liftId: null,
    }
  }
  const items = useMemo(() => [...layout.buildings, ...(layout.archives || []).map((archive) => ({ ...archive, archive: true }))], [layout])
  const order = useMemo(() => (strip ? [...layout.buildings].sort((a, b) => a.u - b.u) : layout.buildings), [layout, strip])

  // Remember when each building first appeared after the first frame, so new work rises from the board.
  useEffect(() => {
    const s = state.current
    const now = performance.now()
    if (!s.seen) { s.seen = new Set(items.map((item) => item.id)); return }
    for (const item of items) if (!s.seen.has(item.id)) { s.seen.add(item.id); if (!reducedMotion()) s.appear.set(item.id, now) }
  }, [items])

  // --------------------------------------------------------------- camera
  const fit = useCallback((animate) => {
    const s = state.current
    const { w, h } = s.size
    if (!w || !h) return
    const cam = { ...s.cam }
    const c = Math.cos(cam.az * DEG); const sn = Math.sin(cam.az * DEG); const se = Math.sin(cam.el * DEG); const ce = Math.cos(cam.el * DEG)
    const corners = []
    if (strip) {
      for (const plot of layout.plots) for (const [u, v] of [[plot.u0, plot.v0], [plot.u1, plot.v0], [plot.u1, plot.v1], [plot.u0, plot.v1]]) corners.push(plot.world(u, v))
    } else {
      for (const plot of layout.plots) corners.push({ x: plot.x, y: plot.y }, { x: plot.x + plot.w, y: plot.y }, { x: plot.x, y: plot.y + plot.d }, { x: plot.x + plot.w, y: plot.y + plot.d })
    }
    if (!corners.length) return
    const xs = corners.map((p) => p.x * c - p.y * sn)
    const ys = corners.map((p) => (p.x * sn + p.y * c) * se)
    const top = 4.4 * ce
    const spanX = Math.max(...xs) - Math.min(...xs)
    const spanY = Math.max(...ys) - Math.min(...ys) + top
    const pad = strip ? 26 : 40
    let zoom = Math.min((w - pad * 2) / Math.max(spanX, 1), (h - pad - (strip ? 34 : 64)) / Math.max(spanY, 1))
    zoom = Math.max(strip ? 9 : 4, Math.min(strip ? 34 : 50, zoom))
    // Centre: middle of the projected extents (the strip starts at its left edge when it is longer than the view).
    let xr = (Math.max(...xs) + Math.min(...xs)) / 2
    if (strip && spanX * zoom > w - pad * 2) xr = Math.min(...xs) + (w / 2 - pad) / zoom
    const yr = (Math.max(...ys) + Math.min(...ys)) / 2 / se - (strip ? 0.9 : 1.4) * ce / se
    const target = { ...cam, zoom, tx: xr * c + yr * sn, ty: -xr * sn + yr * c }
    s.fitZoom = zoom
    if (animate && !reducedMotion()) s.goal = target
    else { s.cam = target; s.goal = null }
  }, [layout, strip])

  // --------------------------------------------------------------- drawing
  const draw = useCallback(() => {
    const s = state.current
    const el = canvas.current
    if (!el) return
    const { w, h, dpr } = s.size
    const ctx = el.getContext('2d')
    if (!s.palette) s.palette = readPalette(box.current)
    const pal = s.palette
    if (!s.hatch) s.hatch = hatchPattern(ctx, pal.hatch, dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)
    const { az, el: elev, zoom, tx, ty } = s.cam
    const c = Math.cos(az * DEG); const sn = Math.sin(az * DEG); const se = Math.sin(elev * DEG); const ce = Math.cos(elev * DEG)
    const txr = tx * c - ty * sn
    const tyr = tx * sn + ty * c
    const P = (x, y, z) => [w / 2 + (x * c - y * sn - txr) * zoom, h / 2 + ((x * sn + y * c - tyr) * se - z * ce) * zoom]
    const depth = (x, y) => x * sn + y * c
    const detail = zoom > 13
    const fine = zoom > 26
    const now = performance.now()
    const hits = []

    const poly = (points, fill, stroke, width = 1) => {
      ctx.beginPath()
      ctx.moveTo(points[0][0], points[0][1])
      for (let i = 1; i < points.length; i += 1) ctx.lineTo(points[i][0], points[i][1])
      ctx.closePath()
      if (fill) { ctx.fillStyle = fill; ctx.fill() }
      if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = width; ctx.stroke() }
    }
    // A prism over a convex footprint: the side faces that face the viewer, then the top.
    const prism = (corners, z0, z1, fills, { stroke = null, pattern = false, dashed = false, glow = null, keepTop = true } = {}) => {
      const faces = []
      for (let i = 0; i < corners.length; i += 1) {
        const a = corners[i]
        const b = corners[(i + 1) % corners.length]
        const nx = b[1] - a[1]
        const ny = -(b[0] - a[0])
        if (nx * sn + ny * c <= 1e-6) continue
        const shade = nx * c - ny * sn < 0 ? fills.left : fills.right
        faces.push({ points: [P(a[0], a[1], z0), P(b[0], b[1], z0), P(b[0], b[1], z1), P(a[0], a[1], z1)], fill: shade })
      }
      if (keepTop) faces.push({ points: corners.map(([x, y]) => P(x, y, z1)), fill: fills.top })
      if (glow) { ctx.shadowColor = glow; ctx.shadowBlur = 14 }
      if (dashed) ctx.setLineDash([3, 2.5])
      for (const face of faces) {
        poly(face.points, face.fill || null, stroke, stroke ? (dashed ? 1.1 : 0.7) : 1)
        if (pattern && s.hatch && face.fill) { ctx.save(); ctx.globalAlpha = 0.55; ctx.fillStyle = s.hatch; ctx.fill(); ctx.restore() }
      }
      if (dashed) ctx.setLineDash([])
      if (glow) { ctx.shadowBlur = 0; ctx.shadowColor = 'transparent' }
      return faces.map((face) => face.points)
    }

    // An agent working lights its floor: warm window bands across each face the viewer sees, one per
    // task; where floors are too thin to tell apart, the face glows as one band.
    const windows = (corners, z0, z1, count) => {
      const rows = Math.max(1, count)
      const floorPx = ((z1 - z0) / rows) * ce * zoom
      if (pal.night) { ctx.shadowColor = pal.litGlow; ctx.shadowBlur = 9 }
      for (let i = 0; i < corners.length; i += 1) {
        const a = corners[i]
        const b = corners[(i + 1) % corners.length]
        const nx = b[1] - a[1]
        const ny = -(b[0] - a[0])
        if (nx * sn + ny * c <= 1e-6) continue
        const fill = nx * c - ny * sn < 0 ? pal.lit.left : pal.lit.right
        const at = (t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        const p0 = at(0.14)
        const p1 = at(0.86)
        const bands = floorPx < 3.2 ? [[z0 + (z1 - z0) * 0.1, z1 - (z1 - z0) * 0.1]]
          : Array.from({ length: rows }, (_, k) => [z0 + ((k + 0.26) / rows) * (z1 - z0), z0 + ((k + 0.74) / rows) * (z1 - z0)])
        for (const [za, zb] of bands) poly([P(p0[0], p0[1], za), P(p1[0], p1[1], za), P(p1[0], p1[1], zb), P(p0[0], p0[1], zb)], fill)
      }
      ctx.shadowBlur = 0
      ctx.shadowColor = 'transparent'
    }

    // The base board and its districts.
    ctx.font = `600 ${Math.max(10, Math.min(13, zoom * 0.46))}px ${pal.font}`
    const plotCorners = (plot) => (strip
      ? [[plot.u0, plot.v0], [plot.u1, plot.v0], [plot.u1, plot.v1], [plot.u0, plot.v1]].map(([u, v]) => { const p = plot.world(u, v); return [p.x, p.y] })
      : [[plot.x, plot.y], [plot.x + plot.w, plot.y], [plot.x + plot.w, plot.y + plot.d], [plot.x, plot.y + plot.d]])
    const plots = [...layout.plots].sort((a, b) => {
      const ca = plotCorners(a)
      const cb = plotCorners(b)
      return depth(...ca[2]) - depth(...cb[2])
    })
    for (const plot of plots) {
      const corners = plotCorners(plot)
      prism(corners, -0.14, 0, { top: pal.board, left: pal.boardSide, right: pal.boardSide }, { stroke: pal.edge })
    }

    // Soft contact shadows, cast away from a light at the front left.
    if (detail) {
      ctx.fillStyle = pal.shadow
      for (const item of items) {
        const height = (item.archive ? item.height : item.height) || 0
        const base = item.archive ? archiveCorners(item) : footprint(item)
        const off = base.map(([x, y]) => [x + height * 0.42, y - height * 0.28])
        const shape = hull([...base, ...off].map(([x, y]) => P(x, y, 0)))
        poly(shape, pal.shadow)
      }
    }

    // Buildings, far to near.
    const lift = s.lift
    const sorted = [...items].sort((a, b) => depth(a.x + (a.w || 1) / 2, a.y + (a.d || 1) / 2) - depth(b.x + (b.w || 1) / 2, b.y + (b.d || 1) / 2))
    for (const item of sorted) {
      const born = s.appear.get(item.id)
      let rise = 1
      if (born) {
        const t = Math.min(1, (now - born) / 650)
        rise = 1 - (1 - t) ** 3
        if (t >= 1) s.appear.delete(item.id)
      }
      const selected = !item.archive && item.id === selectedTop
      const raised = selected ? lift : 0
      const hovered = s.hover === item.id
      if (item.archive) {
        const corners = archiveCorners(item)
        const polys = prism(corners, raised, raised + item.height * rise, pal.archive, { stroke: pal.edge })
        hits.push({ id: item.id, item, polys })
        if (detail) {
          const mid = P((corners[2][0] + corners[3][0]) / 2, (corners[2][1] + corners[3][1]) / 2, item.height * rise * 0.45)
          ctx.fillStyle = pal.inkSoft
          ctx.textAlign = 'center'
          ctx.textBaseline = 'middle'
          ctx.font = `500 ${Math.max(9.5, Math.min(12, zoom * 0.42))}px ${pal.font}`
          ctx.fillText(`${item.count} finished`, mid[0], mid[1])
        }
        continue
      }
      const corners = footprint(item)
      const polys = []
      if (item.canceled) {
        ctx.setLineDash([3, 3])
        polys.push(...prism(corners, 0, 0.04, { top: 'transparent', left: 'transparent', right: 'transparent' }, { stroke: pal.wire }))
        ctx.setLineDash([])
      }
      for (const band of item.bands) {
        const z0 = raised + band.z0 * rise
        const z1 = raised + band.z1 * rise
        const top = band === item.bands[item.bands.length - 1]
        const m = band.material
        if (m === 'wire') polys.push(...prism(corners, z0, z1, { top: null, left: null, right: null }, { stroke: pal.wire, dashed: true, keepTop: top }))
        else if (m === 'glass') polys.push(...prism(corners, z0, z1, pal.glass, { stroke: pal.glassEdge, keepTop: top }))
        else if (m === 'held') polys.push(...prism(corners, z0, z1, pal.held, { stroke: pal.edge, pattern: true, keepTop: top }))
        else if (m === 'lit') {
          polys.push(...prism(corners, z0, z1, pal.built, { stroke: pal.edge, keepTop: top }))
          windows(corners, z0, z1, band.count)
        }
        else if (m === 'needs') polys.push(...prism(corners, z0, z1, pal.needs, { stroke: pal.edge, glow: pal.night ? pal.needsGlow : null, keepTop: top }))
        else polys.push(...prism(corners, z0, z1, pal.built, { stroke: pal.edge, keepTop: top }))
        // Floor lines: one per task, where there is room to see them.
        if (fine && band.count > 1 && m !== 'wire') {
          ctx.strokeStyle = pal.edge
          ctx.lineWidth = 0.6
          ctx.beginPath()
          for (let k = 1; k < band.count; k += 1) {
            const z = z0 + ((z1 - z0) * k) / band.count
            for (let i = 0; i < 4; i += 1) {
              const a = corners[i]
              const b = corners[(i + 1) % 4]
              const nx = b[1] - a[1]
              const ny = -(b[0] - a[0])
              if (nx * sn + ny * c <= 1e-6) continue
              const p = P(a[0], a[1], z)
              const q = P(b[0], b[1], z)
              ctx.moveTo(p[0], p[1])
              ctx.lineTo(q[0], q[1])
            }
          }
          ctx.stroke()
        }
      }
      if (selected || hovered) {
        const outline = hull(polys.flat())
        poly(outline, null, selected ? pal.select : pal.hover, selected ? 1.6 : 1.1)
      }
      hits.push({ id: item.id, item, polys })
    }
    s.hits = hits

    // District names, last so nothing covers them: on a solid pill at the board's nearest corner
    // (the strip sets them along the front edge, where there is always room).
    ctx.font = `600 ${strip ? 11.5 : 12}px ${pal.font}`
    ctx.textBaseline = 'middle'
    ctx.textAlign = 'left'
    const labels = layout.plots.map((plot) => {
      const corners = plotCorners(plot)
      const front = corners.map(([x, y]) => ({ p: P(x, y, -0.14), d: depth(x, y) })).sort((a, b) => b.d - a.d)
      const [a, b] = [front[0].p, front[1].p].sort((m, n) => m[0] - n[0])
      return { plot, front, a, b }
    }).sort((m, n) => m.a[0] - n.a[0])
    const ellipsize = (text, max) => {
      if (ctx.measureText(text).width <= max) return text
      let cut = text
      while (cut.length > 1 && ctx.measureText(`${cut}…`).width > max) cut = cut.slice(0, -1)
      return `${cut.trimEnd()}…`
    }
    labels.forEach(({ plot, front, a, b }, index) => {
      const full = strip ? `${plot.name}  ·  ${plot.count} open${plot.finished ? `  ·  ${plot.finished} finished` : ''}` : `${plot.name}  ·  ${plot.count}`
      const room = strip && labels[index + 1] ? labels[index + 1].a[0] - a[0] - 26 : Infinity
      const text = ellipsize(full, Math.max(30, room))
      const width = ctx.measureText(text).width
      let x
      let y
      if (strip) {
        x = a[0] + 4
        y = Math.max(a[1], b[1]) + 13
      } else {
        x = front[0].p[0] - width / 2
        y = front[0].p[1] + 16
      }
      ctx.fillStyle = pal.labelBack
      ctx.beginPath()
      ctx.roundRect(x - 8, y - 10, width + 16, 20, 10)
      ctx.fill()
      ctx.fillStyle = pal.ink
      ctx.fillText(text, x, y + 0.5)
    })

    // The label of the building under the pointer or the selected one.
    const shown = s.hover ? items.find((item) => item.id === s.hover) : (selectedTop ? items.find((item) => item.id === selectedTop) : null)
    const tipEl = tip.current
    if (tipEl) {
      if (shown) {
        const top = shown.archive ? shown.height : shown.height + (shown.id === selectedTop ? lift : 0)
        const cx = shown.archive ? archiveCorners(shown).reduce((sum, p) => sum + p[0], 0) / 4 : shown.x + shown.w / 2
        const cy = shown.archive ? archiveCorners(shown).reduce((sum, p) => sum + p[1], 0) / 4 : shown.y + shown.d / 2
        const [px, py] = P(cx, cy, top)
        const title = shown.archive ? `${shown.count} finished task ${shown.count === 1 ? 'family' : 'families'}` : titleOf?.(shown.id) || 'Untitled task'
        const sub = shown.archive ? `${projectNameOf?.(shown.district) || ''} · show them` : `${projectNameOf?.(shown.projectId) || ''} · ${buildingSummary(shown)}`
        if (tipEl.dataset.id !== shown.id) {
          tipEl.dataset.id = shown.id
          tipEl.querySelector('b').textContent = title
          tipEl.querySelector('small').textContent = sub
        }
        tipEl.style.transform = `translate(${Math.round(Math.min(w - 170, Math.max(170, px)))}px, ${Math.round(Math.max(52, py - 10))}px) translate(-50%, -100%)`
        tipEl.hidden = false
      } else tipEl.hidden = true
    }
    return s.appear.size > 0
  }, [items, layout, selectedTop, strip, titleOf, projectNameOf])

  // --------------------------------------------------------------- frame loop
  const frame = useCallback((time) => {
    const s = state.current
    s.raf = 0
    const dt = Math.min(0.05, s.last ? (time - s.last) / 1000 : 1 / 60)
    s.last = time
    let moving = false
    if (s.goal && !s.drag) {
      for (const key of ['az', 'el', 'zoom', 'tx', 'ty']) {
        const [x, v] = spring(s.cam[key], s.vel[key], s.goal[key], dt, key === 'zoom' ? 0.42 : 0.5)
        s.cam[key] = x
        s.vel[key] = v
        if (Math.abs(x - s.goal[key]) > (key === 'zoom' ? 0.01 : 0.002) || Math.abs(v) > 0.01) moving = true
      }
      if (!moving) { Object.assign(s.cam, s.goal); s.goal = null; s.vel = { az: 0, el: 0, zoom: 0, tx: 0, ty: 0 } }
    }
    const liftTarget = selectedTop ? 0.42 : 0
    if (s.liftId !== selectedTop) { s.liftId = selectedTop; s.lift = 0; s.liftVel = 0 }
    if (reducedMotion()) s.lift = liftTarget
    else {
      const [x, v] = spring(s.lift, s.liftVel, liftTarget, dt, 0.36)
      s.lift = x
      s.liftVel = v
      if (Math.abs(x - liftTarget) > 0.002) moving = true
    }
    const rising = draw()
    if (moving || rising) s.raf = requestAnimationFrame(frame)
    else s.last = 0
  }, [draw, selectedTop])
  const kick = useCallback(() => {
    const s = state.current
    if (!s.raf) s.raf = requestAnimationFrame(frame)
  }, [frame])

  // --------------------------------------------------------------- size and theme
  useLayoutEffect(() => {
    const host = box.current
    const el = canvas.current
    if (!host || !el) return undefined
    const s = state.current
    const measure = () => {
      const rect = host.getBoundingClientRect()
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      if (rect.width === s.size.w && rect.height === s.size.h && dpr === s.size.dpr) return
      s.size = { w: rect.width, h: rect.height, dpr }
      el.width = Math.max(1, Math.round(rect.width * dpr))
      el.height = Math.max(1, Math.round(rect.height * dpr))
      el.style.width = `${rect.width}px`
      el.style.height = `${rect.height}px`
      s.hatch = null
      if (!s.fitted && rect.width > 0) { fit(false); s.fitted = true; onReady?.() }
      kick()
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(host)
    const repaint = () => { s.palette = null; s.hatch = null; kick() }
    const themes = new MutationObserver(repaint)
    themes.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    const scheme = window.matchMedia?.('(prefers-color-scheme: dark)')
    scheme?.addEventListener?.('change', repaint)
    return () => { observer.disconnect(); themes.disconnect(); scheme?.removeEventListener?.('change', repaint) }
  }, [fit, kick, onReady])

  // A different layout (new work, another scope) keeps the camera unless nothing was fitted yet.
  useEffect(() => {
    const s = state.current
    if (!s.fitted && s.size.w) { fit(false); s.fitted = true }
    kick()
  }, [layout, fit, kick])
  useEffect(() => { kick() }, [selectedTop, kick])
  useEffect(() => () => { const s = state.current; if (s.raf) cancelAnimationFrame(s.raf) }, [])

  // The selected building is brought into view when it was chosen elsewhere (the List, Go to).
  useEffect(() => {
    const s = state.current
    if (!selectedTop || !s.size.w) return
    const item = items.find((entry) => entry.id === selectedTop)
    if (!item || item.archive) return
    const { az, el: elev, zoom } = s.cam
    const c = Math.cos(az * DEG); const sn = Math.sin(az * DEG); const se = Math.sin(elev * DEG)
    const xr = (item.x + item.w / 2) * c - (item.y + item.d / 2) * sn
    const yr = (item.x + item.w / 2) * sn + (item.y + item.d / 2) * c
    const txr = s.cam.tx * c - s.cam.ty * sn
    const tyr = s.cam.tx * sn + s.cam.ty * c
    const sx = (xr - txr) * zoom
    const sy = (yr - tyr) * se * zoom
    if (Math.abs(sx) < s.size.w / 2 - 60 && Math.abs(sy) < s.size.h / 2 - 40) return
    const nxr = strip ? xr : xr
    const nyr = strip ? tyr / 1 : yr
    const goal = { ...s.cam, tx: nxr * c + nyr * sn, ty: -nxr * sn + nyr * c }
    if (reducedMotion()) { s.cam = goal; s.goal = null } else s.goal = goal
    kick()
  }, [selectedTop, items, strip, kick])

  // --------------------------------------------------------------- pointer
  const hitAt = (x, y) => {
    const hits = state.current.hits
    for (let i = hits.length - 1; i >= 0; i -= 1) if (hits[i].polys.some((polygon) => inside(polygon, x, y))) return hits[i]
    return null
  }
  const local = (event) => {
    const rect = box.current.getBoundingClientRect()
    return [event.clientX - rect.left, event.clientY - rect.top]
  }
  function onPointerDown(event) {
    if (event.button > 2) return
    const s = state.current
    const [x, y] = local(event)
    s.drag = { x, y, x0: x, y0: y, t: performance.now(), moved: false, pan: strip || event.shiftKey || event.button === 1 || event.button === 2,
      history: [{ x, y, t: performance.now() }], start: { ...s.cam } }
    s.goal = null
    s.vel = { az: 0, el: 0, zoom: 0, tx: 0, ty: 0 }
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }
  function onPointerMove(event) {
    const s = state.current
    const [x, y] = local(event)
    const drag = s.drag
    if (!drag) {
      const hit = hitAt(x, y)
      const id = hit ? hit.id : null
      if (id !== s.hover) { s.hover = id; box.current.style.cursor = id ? 'pointer' : strip ? 'grab' : 'grab'; kick() }
      return
    }
    const dx = x - drag.x0
    const dy = y - drag.y0
    if (!drag.moved && Math.hypot(dx, dy) < 4) return
    drag.moved = true
    s.hover = null
    const now = performance.now()
    drag.history.push({ x, y, t: now })
    if (drag.history.length > 6) drag.history.shift()
    const { az } = drag.start
    const c = Math.cos(az * DEG); const sn = Math.sin(az * DEG)
    if (drag.pan) {
      // The board follows the hand, from where it was grabbed.
      const du = -dx / s.cam.zoom
      const dv = strip ? 0 : -dy / (s.cam.zoom * Math.sin(s.cam.el * DEG))
      s.cam.tx = drag.start.tx + du * c + dv * sn
      s.cam.ty = drag.start.ty - du * sn + dv * c
      if (strip) clampStrip(s)
    } else {
      s.cam.az = drag.start.az + dx * 0.35
      const el = drag.start.el - dy * 0.25
      s.cam.el = el < 16 ? 16 - rubber(16 - el, 40) : el > 88 ? 88 + rubber(el - 88, 40) : el
    }
    box.current.style.cursor = 'grabbing'
    kick()
  }
  function onPointerUp(event) {
    const s = state.current
    const drag = s.drag
    s.drag = null
    if (!drag) return
    const [x, y] = local(event)
    if (!drag.moved) {
      const hit = hitAt(x, y)
      if (hit?.item.archive) onArchive?.(hit.item.district)
      else if (hit) { onSelect?.(hit.id); setActive(hit.id) }
      return
    }
    // Hand the release velocity to the camera and let it come to rest where the flick projects.
    const first = drag.history[0]
    const lastPoint = drag.history[drag.history.length - 1]
    const span = Math.max(1, lastPoint.t - first.t)
    const vx = ((lastPoint.x - first.x) / span) * 1000
    const vy = ((lastPoint.y - first.y) / span) * 1000
    const goal = { ...s.cam }
    if (drag.pan) {
      const c = Math.cos(s.cam.az * DEG); const sn = Math.sin(s.cam.az * DEG)
      const du = -projectMomentum(vx) / s.cam.zoom
      const dv = strip ? 0 : -projectMomentum(vy) / (s.cam.zoom * Math.sin(s.cam.el * DEG))
      goal.tx += du * c + dv * sn
      goal.ty += -du * sn + dv * c
    } else {
      goal.az = s.cam.az + projectMomentum(vx) * 0.35
      goal.el = Math.max(16, Math.min(88, s.cam.el - projectMomentum(vy) * 0.25))
    }
    if (strip) { const clamped = { ...goal }; clampStrip({ cam: clamped, layout, size: s.size }); Object.assign(goal, clamped) }
    if (reducedMotion()) { s.cam = goal; s.goal = null } else { s.goal = goal; s.vel = { az: 0, el: 0, zoom: 0, tx: 0, ty: 0 } }
    box.current.style.cursor = 'grab'
    kick()
  }
  function clampStrip(target) {
    const s = target.cam ? target : state.current
    const cam = s.cam
    const size = s.size || state.current.size
    const a = (layout.azimuth ?? 32) * DEG
    const u = cam.tx * Math.cos(a) - cam.ty * Math.sin(a)
    const v = cam.tx * Math.sin(a) + cam.ty * Math.cos(a)
    const half = size.w / 2 / cam.zoom
    const lo = Math.min(half - 1, (layout.length || 0) / 2)
    const hi = Math.max((layout.length || 0) - half + 1, (layout.length || 0) / 2)
    const nu = Math.max(lo, Math.min(hi, u))
    cam.tx = nu * Math.cos(a) + v * Math.sin(a)
    cam.ty = -nu * Math.sin(a) + v * Math.cos(a)
  }
  function onWheel(event) {
    const s = state.current
    if (strip && !event.ctrlKey) {
      // A strip scrolls sideways; the wheel's vertical travel scrolls it too.
      const delta = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY
      if (!delta) return
      event.preventDefault()
      const a = (layout.azimuth ?? 32) * DEG
      const goal = { ...(s.goal || s.cam) }
      goal.tx += (delta / goal.zoom) * Math.cos(a)
      goal.ty -= (delta / goal.zoom) * Math.sin(a)
      clampStrip({ cam: goal, size: s.size })
      if (reducedMotion()) { s.cam = goal; s.goal = null } else s.goal = goal
      kick()
      return
    }
    event.preventDefault()
    const [x, y] = local(event)
    const goal = { ...(s.goal || s.cam) }
    const min = (s.fitZoom || 20) * 0.45
    const max = (s.fitZoom || 20) * 7
    let zoom = goal.zoom * Math.exp(-event.deltaY * 0.0016)
    zoom = zoom < min ? min - rubber(min - zoom, min) : zoom > max ? max + rubber(zoom - max, max) : zoom
    // Keep the point under the pointer where it is.
    const c = Math.cos(goal.az * DEG); const sn = Math.sin(goal.az * DEG); const se = Math.sin(goal.el * DEG)
    const dxr = (x - s.size.w / 2) * (1 / goal.zoom - 1 / zoom)
    const dyr = ((y - s.size.h / 2) * (1 / goal.zoom - 1 / zoom)) / se
    goal.tx += dxr * c + dyr * sn
    goal.ty += -dxr * sn + dyr * c
    goal.zoom = Math.max(min, Math.min(max, zoom))
    if (reducedMotion()) { s.cam = goal; s.goal = null } else s.goal = goal
    kick()
  }
  function onLeave() {
    const s = state.current
    if (s.hover) { s.hover = null; kick() }
  }
  function onDoubleClick(event) {
    const [x, y] = local(event)
    const hit = hitAt(x, y)
    if (hit && !hit.item.archive) onOpen?.(hit.id)
    else if (!hit && !strip) { fit(true); kick() }
  }

  // --------------------------------------------------------------- keyboard
  const activeId = active && order.some((item) => item.id === active) ? active : (selectedTop && order.some((item) => item.id === selectedTop) ? selectedTop : order[0]?.id)
  function onKeyDown(event) {
    const s = state.current
    const index = order.findIndex((item) => item.id === activeId)
    const move = (next) => {
      const item = order[Math.max(0, Math.min(order.length - 1, next))]
      if (!item) return
      setActive(item.id)
      s.hover = item.id
      kick()
    }
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') { event.preventDefault(); move(index + 1) }
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') { event.preventDefault(); move(index - 1) }
    else if (event.key === 'Home') { event.preventDefault(); move(0) }
    else if (event.key === 'End') { event.preventDefault(); move(order.length - 1) }
    else if (event.key === 'Enter' || event.key === ' ') { if (activeId) { event.preventDefault(); onSelect?.(activeId) } }
    else if (!strip && (event.key === '[' || event.key === ']')) {
      event.preventDefault()
      const goal = { ...(s.goal || s.cam) }
      goal.az = Math.round((goal.az + (event.key === ']' ? 45 : -45)) / 45) * 45
      if (reducedMotion()) { s.cam = goal; s.goal = null } else s.goal = goal
      kick()
    } else if (!strip && (event.key === '+' || event.key === '=' || event.key === '-')) {
      event.preventDefault()
      const goal = { ...(s.goal || s.cam) }
      goal.zoom = Math.max((s.fitZoom || 20) * 0.45, Math.min((s.fitZoom || 20) * 7, goal.zoom * (event.key === '-' ? 0.8 : 1.25)))
      if (reducedMotion()) { s.cam = goal; s.goal = null } else s.goal = goal
      kick()
    } else if (!strip && event.key === '0') { event.preventDefault(); fit(true); kick() }
  }

  const describe = (item) => `${titleOf?.(item.id) || 'Untitled task'}. ${projectNameOf?.(item.projectId) || ''}. ${buildingSummary(item)}`
  return <div ref={box} className={`a-site a-site--${mode}${className ? ` ${className}` : ''}`} role="listbox" tabIndex={0} data-site=""
    aria-label={label || (strip ? 'Site: every open task family as a building' : 'Site model of all work')}
    aria-activedescendant={activeId ? `site-${mode}-${activeId}` : undefined}
    onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
    onPointerLeave={onLeave} onWheel={onWheel} onDoubleClick={onDoubleClick} onKeyDown={onKeyDown} onContextMenu={(event) => event.preventDefault()}
    onFocus={() => { const s = state.current; if (activeId && !s.drag) { s.hover = activeId; kick() } }}
    onBlur={() => { const s = state.current; s.hover = null; kick() }}>
    <canvas ref={canvas} className="a-site-canvas" aria-hidden="true" />
    <div ref={tip} className="a-site-tip" hidden aria-hidden="true"><b /><small /></div>
    <ul className="a-sr" role="none">
      {order.map((item) => <li key={item.id} id={`site-${mode}-${item.id}`} role="option" aria-selected={item.id === selectedTop}>{describe(item)}</li>)}
    </ul>
  </div>
}

// An archive block's footprint (the strip's low block of a district's finished families).
function archiveCorners(item) {
  const a = 32 * DEG
  const w = (u, v) => [item.x + u * Math.cos(a) + v * Math.sin(a), item.y - u * Math.sin(a) + v * Math.cos(a)]
  return [w(0, -item.vDepth), w(item.uLength, -item.vDepth), w(item.uLength, 0), w(0, 0)]
}
