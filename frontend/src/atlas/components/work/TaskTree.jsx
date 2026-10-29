import { useEffect, useRef } from 'react'
import Icon from '../Icon'
import { Avatar, StatusBead } from '../identity'
import { Button } from '../primitives'
import { PLACEMENT_NOTES, flatRows, nowView, timeView, treeKey, whoView } from '../../model/workList.mjs'

function Guides({ row }) {
  if (row.depth === 0) return null
  return <>
    {row.guides.map((continues, index) => <span key={index} className={`a-g${continues ? ' a-g--v' : ''}`} aria-hidden="true" />)}
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
  return <div className={`a-c-who${who.role === 'last' ? ' a-c-who--last' : ''}`}>
    <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={26} removed={who.removed} decorative />
    <span className="a-w">
      <b className="a-bidi" dir="auto"><span className="a-sr">{who.role === 'current' ? 'Agent: ' : 'Last agent: '}</span>{who.name}</b>
      <small>{who.detail}{who.replaced && <span className="a-replaced" title={`${who.previousCount} earlier agent${who.previousCount === 1 ? '' : 's'} worked on this task`}>
        {' · '}replaced</span>}</small>
    </span>
  </div>
}

function Row({ row, store, derived, projectName, now, selected, focused, onSelect, onToggle, onFocusRow }) {
  const task = store.tasks[row.id]
  const status = derived.status[row.id]
  const who = whoView(derived, row.id)
  const current = nowView(store, derived, row.id)
  const time = timeView(store, row.id, now)
  const family = row.depth === 0 ? derived.byTop[row.id] : null
  const childCount = task.childCount ?? (derived.forest.children[row.id] || []).length
  const hiddenAttention = row.hasChildren && !row.expanded && derived.attentionBelow.has(row.id)
  const placement = family && family.placement !== 'root' ? PLACEMENT_NOTES[family.placement] : null
  const title = task.title || 'Untitled task'
  const project = projectName(task.projectId)
  return <div className={`a-row${row.depth === 0 ? ' a-row--root' : ''}${selected ? ' is-selected' : ''}`}
    role="treeitem" aria-level={row.depth + 1} aria-setsize={row.setSize} aria-posinset={row.position}
    aria-expanded={row.hasChildren ? row.expanded : undefined} aria-selected={selected}
    tabIndex={focused ? 0 : -1} data-id={row.id}
    onClick={() => { onFocusRow(row.id); onSelect(row.id) }}>
    <div className="a-c-main">
      <Guides row={row} />
      {row.hasChildren
        ? <button type="button" className="a-fold" tabIndex={-1} aria-hidden="true"
          onClick={(event) => { event.stopPropagation(); onToggle(row.id) }}>
          <Icon name={row.expanded ? 'down' : 'right'} size={14} stroke={2.2} />
        </button>
        : <span className="a-fold-pad" />}
      <span className="a-row-bead" title={status.label}><StatusBead state={status.bead} decorative /></span>
      <span className="a-r-title">
        <span className="a-t a-bidi" dir="auto" title={title}>{title}</span>
        <span className="a-sr">{status.label}.</span>
        {row.depth === 0 && <span className="a-sub">
          <span className="a-bidi" dir="auto">{project}</span>
          {' · '}{childCount ? `${childCount} helper${childCount === 1 ? '' : 's'}` : 'no helpers'}
          {placement && <span className="a-placement"> · {placement}</span>}
        </span>}
      </span>
      {hiddenAttention && <span className="a-attn-dot" title="Something in this branch needs you" />}
    </div>
    <Who who={who} />
    <div className="a-c-now">
      <span className={`a-l a-tone-${current.tone}`}>{current.line}</span>
      {current.second && <span className="a-l2">{current.second}</span>}
    </div>
    <div className="a-c-time a-num" title={time.title}>{time.text}</div>
  </div>
}

// The hierarchical Work list: one card per task family, grouped by what needs doing. It is a
// single WAI-ARIA tree with one Tab stop; the arrow keys move, Right/Left open and close
// branches, Enter selects.
export default function TaskTree({ view, store, derived, projectName, now, selectedId, focusId, setFocusId, onSelect, onToggle,
  onLoadMore, loadingMore, empty }) {
  const tree = useRef(null)
  const keyboard = useRef(false)
  const rows = flatRows(view)
  const focusTarget = rows.some((row) => row.id === focusId) ? focusId : rows.some((row) => row.id === selectedId) ? selectedId : rows[0]?.id

  useEffect(() => {
    if (!keyboard.current) return
    keyboard.current = false
    tree.current?.querySelector(`[data-id="${CSS.escape(focusTarget || '')}"]`)?.focus()
  }, [focusTarget])

  function onKeyDown(event) {
    if (event.target.closest('button,a,input') && event.target.getAttribute('role') !== 'treeitem') return
    const action = treeKey(rows, focusTarget, event.key)
    if (!action) return
    event.preventDefault()
    keyboard.current = true
    if (action.focus) setFocusId(action.focus)
    if (action.expand) onToggle(action.expand, true)
    if (action.collapse) onToggle(action.collapse, false)
    if (action.select) onSelect(action.select)
  }

  return <div className="a-outline" ref={tree} role="tree" aria-label="Work hierarchy" aria-multiselectable="false" onKeyDown={onKeyDown}>
    <div className="a-ol-cols" aria-hidden="true"><span>Task</span><span>Agent</span><span>Now</span><span className="a-ol-time">Time</span></div>
    {view.sections.map((section) => <div key={section.key} role="none">
      <div className={`a-ol-sec${section.key === 'needs' ? ' a-ol-sec--needs' : ''}`} role="none">
        {section.key === 'needs' && <StatusBead state="ask" size={14} decorative />}
        <span>{section.label}</span><span className="a-ol-n a-num">{section.families.length}</span>
      </div>
      {section.families.map((family) => <div key={family.topId} className="a-tcard" role="none">
        {family.rows.map((row) => <Row key={row.id} row={row} store={store} derived={derived} projectName={projectName} now={now}
          selected={row.id === selectedId} focused={row.id === focusTarget} onSelect={onSelect} onToggle={onToggle} onFocusRow={setFocusId} />)}
        {family.summary.notLoaded > 0 && !store.complete && <div className="a-tcard-more" role="none">
          {family.summary.notLoaded} more helper{family.summary.notLoaded === 1 ? '' : 's'} on pages that aren’t loaded yet
        </div>}
      </div>)}
    </div>)}
    {!rows.length && empty}
    {!store.complete && <div className="a-ol-more" role="none">
      <span className="a-fine">Showing {Object.values(store.tasks).filter((task) => task.source === 'page').length} of {store.total ?? 'all'} tasks, newest first.</span>
      <Button size="sm" icon="down" onClick={onLoadMore} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Load older tasks'}</Button>
    </div>}
  </div>
}
