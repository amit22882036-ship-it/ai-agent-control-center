import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import Icon from '../Icon'
import { Avatar, ProgressBar, StatusBead } from '../identity'
import { tagOf } from '../../vocabulary.mjs'
import { Button } from '../primitives'
import { PLACEMENT_NOTES, nowView, taskCategory, timeView, treeKey, whoView } from '../../model/workList.mjs'
import { ROW, TASK_KINDS, familyStrand, focusable, sheetOffsets, sheetWindow } from '../../model/workSheet.mjs'

const reducedMotion = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
const EASE = 'cubic-bezier(.2,.8,.2,1)'

// Indentation follows the hierarchy for a few levels, then stops growing (a deeper row says how
// deep it is), so a long delegation chain never pushes its titles out of view.
const DEEPEST = 7
function Guides({ row }) {
  if (row.depth === 0) return null
  const guides = row.guides.length > DEEPEST ? row.guides.slice(-DEEPEST) : row.guides
  return <>
    {row.guides.length > DEEPEST && <span className="a-g a-g--deep" aria-hidden="true" title={`Level ${row.depth + 1}`}>{row.depth + 1}</span>}
    {guides.map((continues, index) => <span key={index} className={`a-g${continues ? ' a-g--v' : ''}`} aria-hidden="true" />)}
    <span className={`a-g ${row.last ? 'a-g--el' : 'a-g--tee'}`} aria-hidden="true" />
  </>
}

function Who({ who }) {
  if (!who.agent) {
    return <div className="a-c-who a-c-who--none">
      <span className="a-avatar-empty" aria-hidden="true" />
      <span className="a-w"><b>No agent</b><small>Nobody is assigned</small></span>
    </div>
  }
  // The tool and model of an agent doing its job are in the tooltip and the details panel; the row
  // spends a second line only on exceptions (an ended, removed or replaced agent), so a long list
  // doesn't repeat "Codex · GPT-5.5" on every line.
  const notable = who.removed || who.replaced
  return <div className={`a-c-who${who.role === 'last' ? ' a-c-who--last' : ''}${notable ? '' : ' a-c-who--plain'}`} title={notable ? undefined : who.detail}>
    <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={26} removed={who.removed} decorative />
    <span className="a-w">
      <b className="a-bidi" dir="auto"><span className="a-sr">{who.role === 'current' ? 'Agent: ' : 'Last agent: '}</span>{who.name}</b>
      {notable ? <small>{who.detail}{who.replaced && <span className="a-replaced" title={`${who.previousCount} earlier agent${who.previousCount === 1 ? '' : 's'} worked on this task`}>
        {'\u00A0· '}replaced</span>}</small> : <span className="a-sr">. {who.detail}</span>}
    </span>
  </div>
}

const TEAM = [
  ['needs', 'ask', 'need you'], ['working', 'work', 'working'], ['waiting', 'helpers', 'waiting'], ['held', 'blocked', 'on hold'],
  ['pending', 'pending', 'not started'], ['done', 'done', 'finished'], ['canceled', 'stopped', 'canceled'],
]

// A family's progress at a glance: its bar and how many of its tasks are finished, never a percentage.
function Team({ derived, family }) {
  const strand = familyStrand(derived, family)
  const { counts } = strand
  const parts = TEAM.filter(([key]) => counts[key] > 0)
  const words = parts.map(([key, , label]) => `${counts[key]} ${label}`)
  return <div className="a-c-team" title={`${counts.size} task${counts.size === 1 ? '' : 's'}: ${words.join(' · ')}`}>
    {counts.size > 1 && <span className="a-sr">Team of {counts.size}: {words.join(', ')}.</span>}
    {counts.size > 1 && <ProgressBar counts={counts} />}
    {counts.size > 1 && <span className="a-strand-n a-num" aria-hidden="true">{strand.finished}/{counts.size}</span>}
  </div>
}

// One Task row. It re-renders only when its own inputs change, so a live update elsewhere never
// redraws the whole sheet.
const TaskRow = memo(function TaskRow({ entry, top, height, store, derived, projectName, now, selected, focused, changed, onSelect, onToggle,
  onFocusRow, compact, onShowInMap, onOpenTask, onReply }) {
  const { row, family } = entry
  const task = store.tasks[row.id]
  const status = derived.status[row.id]
  const who = whoView(derived, row.id)
  const current = nowView(store, derived, row.id)
  const time = timeView(store, row.id, now)
  const top0 = row.depth === 0
  const marked = taskCategory(derived, row.id) === 'needs'
  const placement = top0 && family.placement !== 'root' ? PLACEMENT_NOTES[family.placement] : null
  const childCount = task.childCount ?? (derived.forest.children[row.id] || []).length
  const hiddenAttention = row.hasChildren && !row.expanded && derived.attentionBelow.has(row.id)
  const title = task.title || 'Untitled task'
  // The colour label the person gave this task's agent, as a bold tag at the row's edge so groups
  // of tasks stand out while scanning. Removed agents and the neutral label show none.
  const tag = tagOf(who.agent, who.removed)
  const finished = entry.section === 'done'
  const asking = entry.kind === 'ask'
  const past = entry.kind === 'past' || entry.kind === 'pastm'
  const classes = ['a-row', top0 ? 'a-row--root' : 'a-row--member', selected && 'is-selected', marked && 'is-marked', asking && 'is-ask', past && 'is-past',
    past && (status.key === 'completed' || status.key === 'canceled') && !current.second && 'is-plain',
    status.key === 'canceled' && 'is-canceled', finished && 'is-finished', entry.first && 'is-first', entry.last && 'is-last'].filter(Boolean).join(' ')
  return <div className={classes} role="treeitem" aria-level={row.depth + 1} aria-setsize={row.setSize} aria-posinset={row.position}
    aria-expanded={row.hasChildren ? row.expanded : undefined} aria-selected={selected} tabIndex={focused ? 0 : -1}
    data-id={row.id} data-key={entry.key} data-tag={tag || undefined} style={{ transform: `translateY(${top}px)`, height }}
    onFocus={(event) => { if (event.target === event.currentTarget) onFocusRow(row.id) }}
    onClick={() => { onFocusRow(row.id); onSelect(row.id) }}>
    {tag && <span className="a-tag" aria-hidden="true" />}
    <div className="a-c-main">
      <Guides row={row} />
      {row.hasChildren
        ? <button type="button" className="a-fold" tabIndex={-1} aria-hidden="true"
          onClick={(event) => { event.stopPropagation(); onToggle(row.id) }}>
          <Icon name={row.expanded ? 'down' : 'right'} size={14} stroke={2.2} />
        </button>
        : <span className="a-fold-pad" />}
      <span className="a-row-bead" title={status.label}>
        <StatusBead state={status.bead} decorative live={status.key === 'running' || status.key === 'reviewing'} />
      </span>
      <span className="a-r-title">
        <span className="a-t a-bidi" dir="auto" title={title}>{title}</span>
        <span className="a-sr">{status.label}.{tag ? ` Colour label: ${tag}.` : ''}</span>
        {asking && !compact && <span className="a-askline">
          {/* With the Reply button beside it, "waiting for your reply" goes without saying. */}
          <b>{current.line}</b>{current.second && !(onReply && status.key === 'needs_input') ? <span>{'\u00A0· '}{current.second}</span> : null}
          {onReply && status.key === 'needs_input' && <button type="button" className="a-askreply" tabIndex={-1} aria-hidden="true" title="Reply · R"
            onClick={(event) => { event.stopPropagation(); onReply(row.id) }}><Icon name="reply" size={13} />Reply</button>}
        </span>}
        {top0 && !compact && !past && <span className="a-sub">
          <span className="a-pdot" style={{ '--hue': entry.hue }} aria-hidden="true" />
          <span className="a-bidi" dir="auto">{projectName(task.projectId)}</span>
          {/* A flex line drops a leading space, so the separator starts with a no-break space. */}
          {childCount ? `\u00A0· ${childCount} helper${childCount === 1 ? '' : 's'}` : null}
          {placement && <span className="a-placement">{'\u00A0· '}{placement}</span>}
        </span>}
      </span>
      {hiddenAttention && <span className="a-attn-dot" title="Something in this branch needs you" />}
    </div>
    {!compact && <>
      {top0 ? <Team derived={derived} family={family} /> : <div className="a-c-team" aria-hidden="true" />}
      <Who who={who} />
      <div className="a-c-now">
        <span className={`a-l a-tone-${current.tone}`}>{current.line}</span>
        {current.second && !past && <span className="a-l2">{current.second}</span>}
      </div>
      <div className="a-c-time a-num" title={changed ? `${time.title}. Changed since you last looked.` : time.title}>
        {changed && <span className="a-changed" aria-hidden="true" />}{time.text}{changed && <span className="a-sr"> (changed since you last looked)</span>}
      </div>
      {(onShowInMap || onOpenTask) && <span className="a-row-quick">
        {/* A pointer shortcut; with the keyboard, select the row and press R (or use the details panel). */}
        {onReply && status.key === 'needs_input' && !asking && <button type="button" className="a-row-map a-row-reply" tabIndex={-1} aria-hidden="true" title="Reply · R"
          onClick={(event) => { event.stopPropagation(); onReply(row.id) }}><Icon name="reply" size={14} /><span>Reply</span></button>}
        {onOpenTask && <button type="button" className="a-row-map" tabIndex={-1} aria-hidden="true" title="Open task"
          onClick={(event) => { event.stopPropagation(); onOpenTask(row.id) }}><Icon name="open" size={14} /></button>}
        {onShowInMap && <button type="button" className="a-row-map" tabIndex={-1} aria-hidden="true" title="Show in map"
          onClick={(event) => { event.stopPropagation(); onShowInMap(row.id) }}><Icon name="map" size={14} /></button>}
      </span>}
    </>}
  </div>
})

function Plain({ entry, top, height, onToggleFinished, onShowAll, compact }) {
  const style = { transform: `translateY(${top}px)`, height }
  if (entry.kind === 'section') {
    return <div className={`a-sheet-sec a-sheet-sec--${entry.section}`} role="none" data-key={entry.key} style={style}>
      {entry.section === 'needs' && <StatusBead state="ask" size={14} decorative />}
      <span className="a-sheet-sec-label">{entry.label}</span><span className="a-ol-n a-num">{entry.count}</span>
      {/* Folded, the history row below offers it; opened, the heading folds it away again. */}
      {entry.collapsible && entry.open && <button type="button" className="a-sheet-sec-toggle" tabIndex={-1} aria-hidden="true" onClick={() => onToggleFinished(false)}>
        Fold away</button>}
    </div>
  }
  if (entry.kind === 'day') return <div className={`a-sheet-day${entry.first ? ' is-first' : ''}${entry.last ? ' is-last' : ''}`} role="none" data-key={entry.key} style={style}><span>{entry.label}</span></div>
  if (entry.kind === 'reveal') {
    return <div className="a-sheet-reveal" role="none" data-key={entry.key} style={style}>
      <button type="button" tabIndex={-1} aria-hidden="true" onClick={() => onToggleFinished(true)}>
        <Icon name="down" size={14} />{entry.count} {entry.more ? 'more ' : ''}task {entry.count === 1 ? 'family' : 'families'} in history{compact ? '' : ' · show them by day'}</button>
    </div>
  }
  if (entry.kind === 'more') {
    const indent = { '--depth': entry.depth }
    if (entry.notLoaded) {
      return <div className={`a-sheet-more${entry.last ? ' is-last' : ''}`} role="none" data-key={entry.key} style={{ ...style, ...indent }}>
        <span className="a-fine">{entry.notLoaded} more helper{entry.notLoaded === 1 ? '' : 's'} on pages that aren’t loaded yet</span></div>
    }
    return <div className={`a-sheet-more${entry.last ? ' is-last' : ''}`} role="none" data-key={entry.key} style={{ ...style, ...indent }}>
      <button type="button" tabIndex={-1} aria-hidden="true" onClick={() => onShowAll(entry.id)}>
        <Icon name="plus" size={12} />{entry.hidden} more {entry.hidden === 1 ? 'task' : 'tasks'} in this branch</button></div>
  }
  return null
}

// The Work sheet: a single WAI-ARIA tree with one Tab stop, drawn as a window over fixed-height
// rows so a thousand families cost what a screenful does. Arrow keys move, Right and Left open and
// close branches, Enter selects. Live changes keep the row the person is looking at where it is on
// screen, and rows that move glide to their new place instead of jumping.
export default function WorkSheet({ rows, viewKey, store, derived, projectName, hueOf, now, selectedId, focusId, setFocusId, onSelect, onToggle,
  onShowAll, onToggleFinished, onLoadMore, loadingMore, empty, compact = false, onShowInMap, onOpenTask, onReply, changed, label }) {
  const scroller = useRef(null)
  const keyboard = useRef(false)
  const lastScroll = useRef(0)
  const previous = useRef(null)
  const pendingFlip = useRef(null)
  // A row the keyboard moved to, focused as soon as it is drawn (a long jump draws it a frame later).
  const pendingFocus = useRef(null)
  // The row the person just opened or closed (a branch, its hidden tasks, History) stays under the
  // pointer when the rows change because of it.
  const acted = useRef(null)
  const toggleRow = useCallback((id, open) => { acted.current = { key: `task:${id}`, at: performance.now() }; onToggle(id, open) }, [onToggle])
  const showAllRow = useCallback((id) => { acted.current = { key: `task:${id}`, at: performance.now() }; onShowAll(id) }, [onShowAll])
  const toggleHistory = useCallback((open) => { acted.current = { key: 'section:done', at: performance.now() }; onToggleFinished(open) }, [onToggleFinished])
  const [port, setPort] = useState({ top: 0, height: 900 })
  const [scale, setScale] = useState(1)

  const { tops, total } = useMemo(() => sheetOffsets(rows, scale), [rows, scale])
  const keyIndex = useMemo(() => new Map(rows.map((row, index) => [row.key, index])), [rows])
  const items = useMemo(() => rows.filter(focusable).map((entry) => entry.row), [rows])
  const height = (index) => Math.round(ROW[rows[index].kind] * scale)
  const focusTarget = items.some((row) => row.id === focusId) ? focusId : items.some((row) => row.id === selectedId) ? selectedId : items[0]?.id

  useLayoutEffect(() => {
    const element = scroller.current
    if (!element) return undefined
    const measure = () => {
      setScale((parseFloat(getComputedStyle(document.documentElement).fontSize) || 16) / 16)
      setPort((current) => (current.height === element.clientHeight && current.top === element.scrollTop ? current
        : { top: element.scrollTop, height: element.clientHeight }))
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(element)
    return () => observer?.disconnect()
  }, [])

  // Keep what the person is looking at in place when the rows change underneath them.
  useLayoutEffect(() => {
    const element = scroller.current
    const before = previous.current
    previous.current = { rows, tops, keyIndex, viewKey, scrollTop: element ? element.scrollTop : 0 }
    if (!element || !before || before.rows === rows) return
    if (before.viewKey !== viewKey) {
      // A different view (scope, project, search, focus) starts at its top, or at the selection.
      const index = selectedId ? keyIndex.get(`task:${selectedId}`) : undefined
      element.scrollTop = index === undefined ? 0 : Math.max(0, tops[index] - element.clientHeight / 3)
      lastScroll.current = element.scrollTop
      setPort({ top: element.scrollTop, height: element.clientHeight })
      return
    }
    const scrollBefore = lastScroll.current
    const viewport = element.clientHeight
    const onScreen = (index) => index !== undefined && before.tops[index] >= scrollBefore - 1 && before.tops[index] < scrollBefore + viewport
    // The anchor: the row the person just acted on; else the selected row when it was on screen;
    // else the first row fully on screen. The first two hold at any scroll position. The last one
    // holds once the person has scrolled: at the very top, new work arriving above is shown.
    let anchor = null
    let held = false
    const action = acted.current
    acted.current = null
    const selectedBefore = selectedId ? before.keyIndex.get(`task:${selectedId}`) : undefined
    if (action && performance.now() - action.at < 1500 && onScreen(before.keyIndex.get(action.key))) {
      anchor = action.key
      held = true
    } else if (onScreen(selectedBefore)) {
      anchor = `task:${selectedId}`
      held = true
    } else {
      for (let i = 0; i < before.rows.length; i += 1) {
        if (before.tops[i] >= scrollBefore) { anchor = before.rows[i].key; break }
      }
    }
    let scrollAfter = element.scrollTop
    if (anchor && keyIndex.has(anchor) && (held || scrollBefore > 0)) {
      const offset = before.tops[before.keyIndex.get(anchor)] - scrollBefore
      const desired = Math.max(0, tops[keyIndex.get(anchor)] - offset)
      if (Math.abs(element.scrollTop - desired) >= 1) element.scrollTop = desired
      scrollAfter = element.scrollTop
    }
    lastScroll.current = scrollAfter
    if (scrollAfter !== port.top) setPort({ top: scrollAfter, height: viewport })
    // Rows that moved or appeared on screen are animated once the window covers them.
    if (!reducedMotion()) {
      const moves = new Map()
      for (let i = 0; i < rows.length; i += 1) {
        const y = tops[i] - scrollAfter
        if (y < -120 || y > viewport + 120) continue
        const was = before.keyIndex.get(rows[i].key)
        if (was === undefined) moves.set(rows[i].key, null)
        else {
          const delta = (before.tops[was] - scrollBefore) - y
          if (Math.abs(delta) >= 2 && Math.abs(delta) < viewport) moves.set(rows[i].key, delta)
        }
      }
      pendingFlip.current = moves.size && moves.size <= 80 ? moves : null
    }
  }, [rows, tops, keyIndex, viewKey, selectedId, port.top])

  useLayoutEffect(() => {
    const moves = pendingFlip.current
    const element = scroller.current
    if (!moves || !element) return
    pendingFlip.current = null
    for (const [key, delta] of moves) {
      const node = element.querySelector(`[data-key="${CSS.escape(key)}"]`)
      if (!node?.animate) continue
      if (delta === null) node.animate([{ opacity: 0, translate: '0 -6px' }, { opacity: 1, translate: '0 0' }], { duration: 260, easing: EASE })
      else node.animate([{ translate: `0 ${delta}px` }, { translate: '0 0' }], { duration: 320, easing: EASE })
    }
  })

  useLayoutEffect(() => {
    const id = pendingFocus.current
    if (!id) return
    const target = scroller.current?.querySelector(`[data-key="${CSS.escape(`task:${id}`)}"]`)
    if (!target) return
    pendingFocus.current = null
    target.focus({ preventScroll: true })
  })

  // A selection made elsewhere (the Map, Go to, the attention keys) is brought into view.
  useEffect(() => {
    const element = scroller.current
    const index = selectedId ? keyIndex.get(`task:${selectedId}`) : undefined
    if (!element || index === undefined) return
    const top = tops[index]
    const bottom = top + height(index)
    if (top < element.scrollTop || bottom > element.scrollTop + element.clientHeight) {
      element.scrollTop = Math.max(0, top - element.clientHeight / 3)
      lastScroll.current = element.scrollTop
      setPort({ top: element.scrollTop, height: element.clientHeight })
    }
  // Only when the selection itself changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId])

  // The keyboard moves focus to rows that may not be drawn yet: scroll them in, then focus.
  useEffect(() => {
    if (!keyboard.current) return
    const element = scroller.current
    const index = focusTarget ? keyIndex.get(`task:${focusTarget}`) : undefined
    if (!element || index === undefined) return
    const top = tops[index]
    const bottom = top + height(index)
    if (top < element.scrollTop + 8) element.scrollTop = Math.max(0, top - 8)
    else if (bottom > element.scrollTop + element.clientHeight - 8) element.scrollTop = bottom - element.clientHeight + 8
    lastScroll.current = element.scrollTop
    setPort({ top: element.scrollTop, height: element.clientHeight })
    keyboard.current = false
    pendingFocus.current = focusTarget
  // Runs when the keyboard moved the focus target.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusTarget])

  const frame = useRef(0)
  function onScroll() {
    const element = scroller.current
    lastScroll.current = element.scrollTop
    if (frame.current) return
    frame.current = requestAnimationFrame(() => {
      frame.current = 0
      setPort({ top: element.scrollTop, height: element.clientHeight })
    })
  }

  function onKeyDown(event) {
    if (event.target.closest('button,a,input') && event.target.getAttribute('role') !== 'treeitem') return
    let action
    if (event.key === 'PageDown' || event.key === 'PageUp') {
      const step = Math.max(1, Math.floor(port.height / (ROW.member * scale)) - 1)
      const index = Math.max(0, items.findIndex((row) => row.id === focusTarget))
      const next = items[Math.min(items.length - 1, Math.max(0, index + (event.key === 'PageDown' ? step : -step)))]
      action = next ? { focus: next.id } : null
    } else {
      action = treeKey(items, focusTarget, event.key)
    }
    if (!action) return
    event.preventDefault()
    keyboard.current = true
    if (action.focus) {
      setFocusId(action.focus)
      if (action.focus === focusTarget) {
        // Same target (for example after a collapse): focus it again in place.
        scroller.current?.querySelector(`[data-key="${CSS.escape(`task:${action.focus}`)}"]`)?.focus({ preventScroll: true })
        keyboard.current = false
      }
    }
    if (action.expand) toggleRow(action.expand, true)
    if (action.collapse) toggleRow(action.collapse, false)
    if (action.select) onSelect(action.select)
  }

  const { start, end } = sheetWindow(tops, total, port.top, port.height)
  // The section being read stays named at the top of the sheet once its own heading scrolls away.
  let pinned = null
  for (let i = Math.min(rows.length - 1, start + Math.max(0, end - start - 1)); i >= 0; i -= 1) {
    if (rows[i].kind === 'section' && tops[i] <= port.top) { pinned = rows[i]; break }
  }
  if (pinned && tops[keyIndex.get(pinned.key)] + height(keyIndex.get(pinned.key)) > port.top + 4) pinned = null
  const shown = []
  for (let i = start; i < end; i += 1) {
    const entry = rows[i]
    const h = height(i)
    if (TASK_KINDS.has(entry.kind)) {
      shown.push(<TaskRow key={entry.key} entry={{ ...entry, hue: hueOf?.(store.tasks[entry.id]?.projectId) }} top={tops[i]} height={h} store={store} derived={derived}
        projectName={projectName} now={now} selected={entry.id === selectedId} focused={entry.id === focusTarget}
        changed={Boolean(changed?.(entry.id))} onSelect={onSelect} onToggle={toggleRow} onFocusRow={setFocusId} compact={compact}
        onShowInMap={onShowInMap} onOpenTask={onOpenTask} onReply={onReply} />)
    } else {
      shown.push(<Plain key={entry.key} entry={entry} top={tops[i]} height={h} onToggleFinished={toggleHistory} onShowAll={showAllRow} compact={compact} />)
    }
  }
  const loaded = Object.values(store.tasks).filter((task) => task.source === 'page').length
  return <div className={`a-outline a-sheet${compact ? ' a-outline--compact' : ''}`} ref={scroller} role="tree"
    aria-label={label || (compact ? 'Work hierarchy beside the map' : 'Work hierarchy')} aria-multiselectable="false"
    onKeyDown={onKeyDown} onScroll={onScroll}>
    {pinned && !compact && <div className="a-sheet-pin" role="none" aria-hidden="true">
      <span className={`a-sheet-pin-label a-sheet-pin--${pinned.section}`}>{pinned.label}<span className="a-num">{pinned.count}</span></span>
    </div>}
    <div className="a-sheet-space" role="none" style={{ height: rows.length ? total : 0 }}>{shown}</div>
    {!items.length && empty}
    {!store.complete && <div className="a-ol-more" role="none">
      <span className="a-fine">{compact ? `${loaded} of ${store.total ?? 'all'} loaded` : `Showing ${loaded} of ${store.total ?? 'all'} tasks, newest first.`}</span>
      <Button size="sm" icon="down" onClick={onLoadMore} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Load older tasks'}</Button>
    </div>}
  </div>
}
