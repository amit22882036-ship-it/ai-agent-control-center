import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import Icon from '../Icon'
import { Avatar, StatusBead } from '../identity'
import { Button, IconButton } from '../primitives'
import { MenuButton } from '../overlays'
import { boxOf, branchIds, mapKey } from '../../model/mapLayout.mjs'
import { ZOOM, ensureVisible, fitCamera, followAnchor, intersects, panBy, worldRect, zoomAt } from '../../model/mapCamera.mjs'
import { PLACEMENT_NOTES, descendantCount, timeView, whoView } from '../../model/workList.mjs'
import { familyStrand } from '../../model/workSheet.mjs'

// Above this many shown Tasks, only the ones near the view are drawn (the selected and the
// keyboard-focused Task always are). Smaller maps are drawn whole.
const CULL_AT = 300
const ENDED = new Set(['completed', 'integrated', 'canceled', 'stopped'])
const BADGES = new Set(['ask', 'paused', 'blocked', 'failed', 'done', 'review', 'stopped', 'removed'])
const RINGS = { work: 'work', ask: 'ask', helpers: 'idle' }
const reducedMotion = () => Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
const box = (node) => ({ minX: node.x, minY: node.y, maxX: node.x + node.w, maxY: node.y + node.h })

// The camera and the open area live outside React state so that effects, gestures and the wheel
// can move them freely; the component subscribes and draws the latest values.
function createCameraStore() {
  let state = { camera: null, animated: false, view: { l: 0, w: 1, h: 1 }, pane: { w: 1, h: 1 } }
  const listeners = new Set()
  const emit = () => { for (const listener of listeners) listener() }
  return {
    get: () => state,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener) },
    camera: () => state.camera,
    setCamera: (camera, animated = false) => { if (!camera) return; state = { ...state, camera, animated }; emit() },
    setView: (view, pane) => {
      const same = (a, b) => a.l === b.l && a.w === b.w && a.h === b.h
      if (same(state.view, view) && state.pane.w === pane.w && state.pane.h === pane.h) return
      state = { ...state, view, pane }
      emit()
    },
  }
}

// What one node says, from the shared model (the same words the List uses).
function nodeInfo({ store, derived, id, row, now, projectName, family }) {
  const task = store.tasks[id]
  const status = derived.status[id]
  const who = whoView(derived, id)
  const time = ENDED.has(status.key) ? '' : timeView(store, id, now).text
  const loadedKids = (derived.forest.children[id] || []).length
  const notLoaded = !store.complete && task.childCount !== null ? Math.max(0, task.childCount - loadedKids) : 0
  const hidden = row.hasChildren && !row.expanded ? descendantCount(derived.forest, id) : 0
  const placement = row.depth === 0 && family && family.placement !== 'root' ? PLACEMENT_NOTES[family.placement] : null
  const title = task.title || 'Untitled task'
  const whoText = !who.agent ? 'No agent' : `${who.role === 'current' ? 'Agent' : 'Last agent'}: ${who.name}${who.removed ? ', removed' : ''}`
  const project = row.depth === 0 ? projectName(task.projectId) : null
  // Read after the visible title and status (the node is named by its content).
  const extra = [whoText, project ? `Project: ${project}` : null, hidden ? `${hidden} task${hidden === 1 ? '' : 's'} hidden below` : null].filter(Boolean).join('. ')
  // A closed family shows its strand, so a team of sixteen never looks like a single task.
  const top = row.depth === 0 && hidden && derived.byTop[id] ? derived.byTop[id] : null
  const strand = top ? familyStrand(derived, top) : null
  return { title, status, who, time, hidden, notLoaded, planned: task.planned.unmaterialized, placement, extra, project, strand }
}

function Face({ who, size }) {
  if (!who.agent) return <span className="a-avatar-empty" style={{ width: size, height: size }} aria-hidden="true" />
  return <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={size} removed={who.removed} decorative />
}

const MapNode = memo(function MapNode({ node, row, info, selected, tabbable, dim, historic, away, onPick, onOpen, onFold }) {
  const bead = info.status.bead
  const ring = selected ? 'sel' : RINGS[bead]
  const root = node.depth === 0
  const common = {
    role: 'treeitem', 'data-id': node.id, tabIndex: tabbable ? 0 : -1, 'aria-selected': selected,
    'aria-level': row.depth + 1, 'aria-setsize': row.setSize, 'aria-posinset': row.position,
    'aria-expanded': row.hasChildren ? row.expanded : undefined,
    onClick: () => onPick(node.id), onDoubleClick: () => onOpen(node.id),
  }
  const fold = row.hasChildren && <button type="button" className="a-gfold" tabIndex={-1} aria-hidden="true"
    title={row.expanded ? 'Hide the helpers below' : 'Show the helpers below'}
    onClick={(event) => { event.stopPropagation(); onFold(node.id, !row.expanded) }}>
    {row.expanded ? <Icon name="down" size={12} stroke={2.4} /> : <>+{info.hidden}</>}
  </button>
  if (node.kind === 'row') {
    return <div {...common} className={`a-gr${selected ? ' is-selected' : ''}${dim ? ' is-dim' : ''}${historic ? ' is-historic' : ''}${away ? ' is-away' : ''}${info.status.tone === 'ask' ? ' a-gr--ask' : ''}${info.status.key === 'canceled' ? ' is-canceled' : ''}`}
      style={{ left: node.x, top: node.y, width: node.w, height: node.h }}>
      <span className="a-gav">{ring && <span className={`a-ring a-ring--${ring}`} />}<Face who={info.who} size={30} />
        {BADGES.has(bead) && <span className="a-gbadge a-gbadge--sm"><StatusBead state={bead} size={13} decorative /></span>}</span>
      <span className="a-grt"><span className="a-t a-bidi" dir="auto">{info.title}</span>
        <span className={`a-s a-tone-${info.status.tone}`}><span className="a-sr">. </span>{info.status.label}{info.time ? ` · ${info.time}` : ''}</span></span>
      <span className="a-sr">. {info.extra}.</span>
      {fold}
    </div>
  }
  return <div {...common} className={`a-gn${root ? ' a-gn--root' : ''}${selected ? ' is-selected' : ''}${dim ? ' is-dim' : ''}${historic ? ' is-historic' : ''}${away ? ' is-away' : ''}${info.status.key === 'canceled' ? ' is-canceled' : ''}`}
    style={{ left: node.x, top: node.y, width: node.w }}>
    <span className="a-gav">{ring && <span className={`a-ring a-ring--${ring}`} />}<Face who={info.who} size={root ? 54 : 44} />
      {BADGES.has(bead) && <span className="a-gbadge"><StatusBead state={bead} size={16} decorative /></span>}</span>
    <span className="a-gt a-bidi" dir="auto">{info.title}</span>
    <span className={`a-gs a-tone-${info.status.tone}`}><span className="a-sr">. </span>{info.status.label}{info.time ? ` · ${info.time}` : ''}</span>
    <span className="a-sr">. {info.extra}.</span>
    {info.project && <span className="a-gproj a-bidi" dir="auto" aria-hidden="true">{info.project}</span>}
    {info.strand && <span className="a-gstrand" aria-hidden="true" title={`${info.strand.size} tasks, ${info.strand.finished} finished`}>
      <span className="a-strand">{info.strand.marks.map((mark, index) => <i key={index} className={`a-sm a-sm--${mark.category}${mark.lead ? ' is-lead' : ''}`} />)}</span>
      <span className="a-strand-n a-num">{info.strand.finished}/{info.strand.size}</span></span>}
    {(info.notLoaded > 0 || info.planned > 0 || row.hasChildren || info.placement) && <span className="a-gchips">
      {info.placement && <span className="a-gchip a-gchip--warn" title={info.placement}><Icon name="info" size={11} />{info.placement}</span>}
      {fold}
      {info.notLoaded > 0 && <span className="a-gchip" title="On pages that aren’t loaded yet">{info.notLoaded} not loaded</span>}
      {info.planned > 0 && <span className="a-gchip a-gchip--plan" title="Planned by the lead; not a task yet">{info.planned} planned<span className="a-sr">, not started</span></span>}
    </span>}
  </div>
})

// A small overview of the whole map, with the visible area; clicking moves there.
const Minimap = memo(function Minimap({ layout, derived, camera, view, onJump }) {
  const W = 184
  const H = 112
  const s = Math.min((W - 12) / Math.max(1, layout.width), (H - 12) / Math.max(1, layout.height))
  const ox = (W - layout.width * s) / 2
  const oy = (H - layout.height * s) / 2
  const dots = useMemo(() => layout.order.map((id) => {
    const node = layout.nodes[id]
    const tone = derived.status[id]?.tone || 'neutral'
    return node.kind === 'row'
      ? <rect key={id} className={`a-mini-dot a-mini-dot--${tone}`} x={ox + node.x * s} y={oy + (node.y + node.h / 2) * s - 1.5} width={Math.max(4, node.w * s * 0.55)} height={3} rx={1.5} />
      : <circle key={id} className={`a-mini-dot a-mini-dot--${tone}`} cx={ox + (node.x + node.w / 2) * s} cy={oy + (node.y + 28) * s} r={node.depth === 0 ? 3.4 : 2.3} />
  }), [layout, derived, ox, oy, s])
  const r = worldRect(camera, view)
  return <div className="a-mini" aria-hidden="true" title="Overview · click to move there"
    onClick={(event) => {
      const rect = event.currentTarget.getBoundingClientRect()
      onJump((event.clientX - rect.left - ox) / s, (event.clientY - rect.top - oy) / s)
    }}>
    <svg width={W} height={H}>{dots}
      <rect className="a-mini-view" x={ox + r.minX * s} y={oy + r.minY * s} width={(r.maxX - r.minX) * s} height={(r.maxY - r.minY) * s} rx={4} />
    </svg>
  </div>
})

// The Work Map: the same Tasks as the List (same rows, selection, filters, search, project, open
// branches and focus), drawn as a hierarchy. A tree for assistive technology, with arrow keys that
// follow what is drawn; pan, zoom and fit with the pointer, the keyboard or the controls.
export default function TaskMap({ layout, view, store, derived, projectName, now, selectedId, onSelect, onToggle, onFocusBranch,
  focusBranch, side, onToggleSide, onDirection, cameraRef, request, onLoadMore, loadingMore, empty }) {
  const pane = useRef(null)
  const [store2] = useState(createCameraStore)
  const { camera, animated, view: area, pane: paneSize } = useSyncExternalStore(store2.subscribe, store2.get)
  const [focusId, setFocusId] = useState(selectedId)
  const keyboard = useRef(false)
  const descend = useRef(null)
  const drag = useRef(null)
  const dragged = useRef(false)

  const rows = useMemo(() => {
    const map = new Map()
    for (const section of view.sections) for (const family of section.families) for (const row of family.rows) map.set(row.id, { row, family })
    return map
  }, [view])
  const infos = useMemo(() => {
    const out = {}
    for (const [id, { row, family }] of rows) out[id] = nodeInfo({ store, derived, id, row, now, projectName, family })
    return out
  }, [rows, store, derived, now, projectName])

  const setCamera = useCallback((next, { animated: smooth = false } = {}) => {
    if (!next) return
    cameraRef.current = next
    store2.setCamera(next, smooth && !reducedMotion())
  }, [cameraRef, store2])

  // The part of the pane not covered by the side list or a floating details panel.
  const viewRect = useCallback(() => {
    const element = pane.current
    if (!element) return { l: 0, w: 1000, h: 700 }
    const rect = element.getBoundingClientRect()
    const list = element.parentElement?.querySelector('[data-side-list]')
    const left = list ? Math.max(0, list.getBoundingClientRect().right - rect.left) : 0
    const inspector = element.closest('.a-work')?.querySelector(':scope > .a-inspector')
    const right = inspector && getComputedStyle(inspector).position === 'absolute' ? Math.max(0, rect.right - inspector.getBoundingClientRect().left) : 0
    return { l: left, w: Math.max(1, rect.width - left - right), h: rect.height }
  }, [])

  const fitIds = useCallback((ids, { anchor = null, animated = true, allowSmall = false } = {}) => {
    const target = boxOf(layout, ids)
    if (!target) return
    const anchorBox = anchor && layout.nodes[anchor] ? box(layout.nodes[anchor]) : null
    setCamera(fitCamera(target, viewRect(), { anchor: anchorBox, allowSmall }), { animated })
  }, [layout, setCamera, viewRect])

  const defaultCamera = useCallback((animated = false) => {
    if (focusBranch && layout.nodes[focusBranch]) return fitIds(branchIds(layout, focusBranch), { anchor: selectedId, animated })
    if (selectedId && layout.nodes[selectedId]) return fitIds(branchIds(layout, layout.nodes[selectedId].topId), { anchor: selectedId, animated })
    if (layout.tops.length) fitIds(null, { anchor: layout.tops[0], animated })
  }, [fitIds, focusBranch, layout, selectedId])

  // First view: the remembered camera when the selection is still in it, otherwise a fit.
  useLayoutEffect(() => {
    const saved = cameraRef.current
    const selected = selectedId && layout.nodes[selectedId]
    if (saved && (!selected || ensureVisible(saved, box(selected), viewRect(), 24) === saved)) setCamera(saved)
    else defaultCamera(false)
    // Mount only: later changes are handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // A refresh or an opened branch moves things: keep the selected (or focused) Task where it was.
  const previous = useRef(layout)
  useLayoutEffect(() => {
    const before = previous.current
    previous.current = layout
    if (before === layout || !store2.camera()) return
    const anchor = [selectedId, focusId].find((id) => id && before.nodes[id] && layout.nodes[id])
    if (anchor) { setCamera(followAnchor(store2.camera(), before.nodes[anchor], layout.nodes[anchor])); return }
    const all = boxOf(layout)
    if (all && !intersects(worldRect(store2.camera(), viewRect()), all)) defaultCamera(false)
  }, [layout, selectedId, focusId, setCamera, viewRect, defaultCamera, store2])

  // Entering or leaving a focused branch frames it.
  const lastFocus = useRef(focusBranch)
  useEffect(() => {
    if (lastFocus.current === focusBranch) return
    lastFocus.current = focusBranch
    if (store2.camera()) defaultCamera(true)
  }, [focusBranch, defaultCamera, store2])

  // A new selection (from the List, the details panel or the address) is brought into view.
  const lastSelected = useRef(selectedId)
  useEffect(() => {
    if (lastSelected.current === selectedId) return
    lastSelected.current = selectedId
    if (!selectedId || !layout.nodes[selectedId] || !store2.camera()) return
    const next = ensureVisible(store2.camera(), box(layout.nodes[selectedId]), viewRect(), 36, boxOf(layout))
    if (next !== store2.camera()) setCamera(next, { animated: true })
  }, [selectedId, layout, setCamera, viewRect, store2])

  // "Show in map" and similar explicit requests frame the Task's branch.
  const lastRequest = useRef(request?.seq)
  useEffect(() => {
    if (!request || request.seq === lastRequest.current) return
    lastRequest.current = request.seq
    if (layout.nodes[request.id]) fitIds(branchIds(layout, layout.nodes[request.id].topId), { anchor: request.id })
  }, [request, layout, fitIds])

  // Showing or hiding the side list changes the open area: keep the selection in it.
  useEffect(() => {
    if (!store2.camera() || !selectedId || !layout.nodes[selectedId]) return
    const next = ensureVisible(store2.camera(), box(layout.nodes[selectedId]), viewRect(), 36, boxOf(layout))
    if (next !== store2.camera()) setCamera(next, { animated: true })
    // Only when the side list opens or closes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [side])

  // The open area changes with the window, the side list and a floating details panel.
  useEffect(() => {
    const element = pane.current
    if (!element) return undefined
    const measure = () => store2.setView(viewRect(), { w: element.clientWidth, h: element.clientHeight })
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(element)
    const list = element.parentElement?.querySelector('[data-side-list]')
    if (list) observer.observe(list)
    return () => observer.disconnect()
  }, [store2, viewRect, side, selectedId])

  // Wheel and pinch zoom around the pointer (the listener must be able to prevent page scrolling).
  // The pane never scrolls (the camera moves instead): undo any scroll a browser or a focus change makes.
  useEffect(() => {
    const element = pane.current
    if (!element) return undefined
    const reset = (event) => { event.currentTarget.scrollTop = 0; event.currentTarget.scrollLeft = 0 }
    const tree = element.querySelector('.a-mtree')
    element.addEventListener('scroll', reset)
    tree?.addEventListener('scroll', reset)
    return () => { element.removeEventListener('scroll', reset); tree?.removeEventListener('scroll', reset) }
  }, [])

  useEffect(() => {
    const element = pane.current
    if (!element) return undefined
    const onWheel = (event) => {
      if (!store2.camera() || event.target.closest('.a-mini, .a-mtool, .a-mpartial')) return
      event.preventDefault()
      const rect = element.getBoundingClientRect()
      const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0016))
      setCamera(zoomAt(store2.camera(), factor, event.clientX - rect.left, event.clientY - rect.top))
    }
    element.addEventListener('wheel', onWheel, { passive: false })
    return () => element.removeEventListener('wheel', onWheel)
  }, [setCamera, store2])

  const focusTarget = layout.nodes[focusId] ? focusId : layout.nodes[selectedId] ? selectedId : layout.tops[0]

  // Keyboard focus follows the arrow keys; the Task is brought into view first.
  useEffect(() => {
    if (!keyboard.current || !focusTarget) return
    keyboard.current = false
    pane.current?.querySelector(`[data-id="${CSS.escape(focusTarget)}"]`)?.focus({ preventScroll: true })
  }, [focusTarget, camera])

  // After ArrowDown opened a branch, move to its first helper once it is drawn.
  useEffect(() => {
    const id = descend.current
    if (!id || !layout.children[id]?.length) return
    descend.current = null
    keyboard.current = true
    setFocusId(layout.children[id][0])
  }, [layout])

  const moveTo = useCallback((id) => {
    keyboard.current = true
    setFocusId(id)
    if (store2.camera() && layout.nodes[id]) {
      const next = ensureVisible(store2.camera(), box(layout.nodes[id]), viewRect(), 36, boxOf(layout))
      if (next !== store2.camera()) setCamera(next, { animated: true })
    }
  }, [layout, setCamera, viewRect, store2])

  const zoomBy = (factor) => {
    if (!store2.camera()) return
    const open = viewRect()
    setCamera(zoomAt(store2.camera(), factor, open.l + open.w / 2, open.h / 2), { animated: true })
  }

  function onKeyDown(event) {
    if (event.target.closest('.a-mtool, .a-mctl, .a-mpartial') || event.altKey || event.ctrlKey || event.metaKey) return
    if (event.shiftKey && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
      event.preventDefault()
      const step = 90
      const d = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] }[event.key]
      if (store2.camera()) setCamera(panBy(store2.camera(), d[0], d[1]))
      return
    }
    if (event.key === '+' || event.key === '=') { event.preventDefault(); zoomBy(ZOOM.step); return }
    if (event.key === '-' || event.key === '_') { event.preventDefault(); zoomBy(1 / ZOOM.step); return }
    if (event.target.getAttribute('role') !== 'treeitem') return
    const action = mapKey(layout, focusTarget, event.key, { collapsed: (id) => Boolean(rows.get(id)?.row.hasChildren && !rows.get(id)?.row.expanded) })
    if (!action) return
    event.preventDefault()
    if (action.focus) moveTo(action.focus)
    if (action.expand) { onToggle(action.expand, true); if (action.descend) descend.current = action.expand }
    if (action.collapse) onToggle(action.collapse, false)
    if (action.select) onSelect(action.select)
  }

  // Pointer: drag anywhere to pan (a drag never counts as a click); a click selects.
  function onPointerDown(event) {
    if (event.button !== 0 || !store2.camera() || event.target.closest('.a-mtool, .a-mctl, .a-mini, .a-mpartial')) return
    drag.current = { x: event.clientX, y: event.clientY, camera: store2.camera(), id: event.pointerId, moved: false }
    dragged.current = false
  }
  function onPointerMove(event) {
    const state = drag.current
    if (!state || state.id !== event.pointerId) return
    const dx = event.clientX - state.x
    const dy = event.clientY - state.y
    if (!state.moved && Math.abs(dx) + Math.abs(dy) < 5) return
    if (!state.moved) {
      state.moved = true
      pane.current?.setPointerCapture?.(event.pointerId)
      pane.current?.classList.add('is-dragging')
    }
    setCamera(panBy(state.camera, dx, dy))
  }
  function onPointerUp(event) {
    const state = drag.current
    if (!state || state.id !== event.pointerId) return
    drag.current = null
    if (state.moved) {
      dragged.current = true
      pane.current?.classList.remove('is-dragging')
      if (pane.current?.hasPointerCapture?.(event.pointerId)) pane.current.releasePointerCapture(event.pointerId)
    }
  }

  const pick = useCallback((id) => { setFocusId(id); onSelect(id) }, [onSelect])
  const open = useCallback((id) => onFocusBranch(id), [onFocusBranch])
  const fold = useCallback((id, openIt) => onToggle(id, openIt), [onToggle])

  const whole = { l: 0, w: paneSize.w, h: paneSize.h }
  const near = camera && layout.order.length > CULL_AT ? worldRect(camera, whole, 480) : null
  const shown = near ? layout.order.filter((id) => id === selectedId || id === focusTarget || intersects(near, box(layout.nodes[id]))) : layout.order
  const far = camera && camera.k < 0.62
  const empty2 = !layout.order.length
  const anchorTop = selectedId && layout.nodes[selectedId] ? layout.nodes[selectedId].topId : null
  const showMini = camera && !empty2 && (layout.width * camera.k > area.w - 40 || layout.height * camera.k > area.h - 40)
  const loaded = Object.values(store.tasks).filter((task) => task.source === 'page').length
  const jumps = layout.tops.slice(0, 40).map((id) => ({ label: infos[id]?.title || id, icon: id === anchorTop ? 'check' : undefined,
    onSelect: () => { onSelect(id); fitIds(branchIds(layout, id), { anchor: id }) } }))
  if (layout.tops.length > 40) jumps.push({ label: `and ${layout.tops.length - 40} more task families`, disabled: true })

  return <div className={`a-mappane${far ? ' is-far' : ''}`} ref={pane} onKeyDown={onKeyDown}
    onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={onPointerUp}
    onClickCapture={(event) => { if (dragged.current) { event.stopPropagation(); event.preventDefault(); dragged.current = false } }}>
    <div className="a-mtree" role="tree" aria-label="Work map" aria-describedby="map-keys">
      <div className={`a-minner${animated ? ' is-animated' : ''}${anchorTop ? ' has-anchor' : ''}`}
        style={camera ? { width: layout.width, height: layout.height, transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.k})` } : { visibility: 'hidden' }}>
        {/* Finished families (the "Completed and history" band) are drawn quieter, never hidden. */}
        {layout.zones.map((zone) => <div key={zone.topId} className={`a-mzone${anchorTop === zone.topId ? ' is-current' : ''}${derived.byTop[zone.topId]?.section === 'done' ? ' is-historic' : ''}`} aria-hidden="true"
          style={{ left: zone.x, top: zone.y, width: zone.w, height: zone.h }} />)}
        {!focusBranch && layout.bands.map((band) => <div key={band.key} className={`a-mband${band.key === 'needs' ? ' a-mband--needs' : ''}`} aria-hidden="true"
          style={{ left: band.x, top: band.y }}>{band.label}<span className="a-num">{band.count}</span></div>)}
        <svg className="a-edges" width={layout.width} height={layout.height} aria-hidden="true" focusable="false">
          {Object.entries(layout.edges).map(([tone, d]) => d && <path key={tone} className={`a-e a-e--${tone}`} d={d} />)}
        </svg>
        {shown.map((id) => <MapNode key={id} node={layout.nodes[id]} row={rows.get(id).row} info={infos[id]} selected={id === selectedId}
          tabbable={id === focusTarget} dim={view.searching && !rows.get(id).row.match} historic={rows.get(id).family?.section === 'done'}
          away={Boolean(anchorTop) && layout.nodes[id].topId !== anchorTop}
          onPick={pick} onOpen={open} onFold={fold} />)}
      </div>
    </div>
    <p id="map-keys" className="a-sr">{layout.direction === 'right' ? 'Arrow keys move between tasks: left to the lead, right to the first helper, up and down between tasks at the same level.' : 'Arrow keys move between tasks: up to the lead, down to the first helper, left and right between tasks at the same level.'}
      Enter opens a task’s details, Space shows or hides its helpers. Plus and minus zoom; Shift with an arrow key moves the map.</p>
    {empty2 && <div className="a-map-empty">{empty}</div>}
    <div className="a-mtool">
      {onToggleSide && <button type="button" className="a-mbtn" aria-pressed={side} onClick={onToggleSide} title={side ? 'Hide the list beside the map' : 'Show the list beside the map'}>
        <Icon name="sidebar" size={16} />{side ? 'Hide list' : 'Show list'}</button>}
      {!focusBranch && layout.tops.length > 1 && <MenuButton label="Jump to a task family" text="Jump to" icon="map" align="end" items={jumps} />}
    </div>
    {!store.complete && <div className="a-mpartial" role="status">
      <span>Showing {loaded} of {store.total ?? 'all'} tasks, newest first.</span>
      <Button size="sm" icon="down" onClick={onLoadMore} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Load older tasks'}</Button>
    </div>}
    {!empty2 && <div className="a-mctl" role="group" aria-label="Map view">
      <IconButton icon="zout" size="sm" label="Zoom out" onClick={() => zoomBy(1 / ZOOM.step)} disabled={!camera || camera.k <= ZOOM.min + 0.001} />
      <span className="a-pct a-num" aria-hidden="true">{camera ? Math.round(camera.k * 100) : 100}%</span>
      <IconButton icon="zin" size="sm" label="Zoom in" onClick={() => zoomBy(ZOOM.step)} disabled={!camera || camera.k >= ZOOM.max - 0.001} />
      <span className="a-mctl-sep" aria-hidden="true" />
      <IconButton icon="fit" size="sm" label={focusBranch ? 'Fit the focused branch' : 'Fit the selected task’s family'}
        onClick={() => defaultCamera(true)} />
      <IconButton icon="grid" size="sm" label="Show everything" onClick={() => fitIds(null, { allowSmall: true })} />
      {onDirection && <>
        <span className="a-mctl-sep" aria-hidden="true" />
        <IconButton icon={layout.direction === 'right' ? 'treedown' : 'treeright'} size="sm"
          label={layout.direction === 'right' ? 'Draw the map top to bottom' : 'Draw the map left to right'}
          onClick={() => onDirection(layout.direction === 'right' ? 'down' : 'right')} />
      </>}
    </div>}
    {showMini && <Minimap layout={layout} derived={derived} camera={camera} view={area}
      onJump={(x, y) => {
        const open = viewRect()
        const current = store2.camera()
        setCamera({ ...current, x: open.l + open.w / 2 - x * current.k, y: open.h / 2 - y * current.k }, { animated: true })
      }} />}
  </div>
}
