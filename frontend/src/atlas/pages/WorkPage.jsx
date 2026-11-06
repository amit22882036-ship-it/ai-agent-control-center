import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import { Segmented } from '../components/controls'
import { MenuButton } from '../components/overlays'
import Icon from '../components/Icon'
import WorkSheet from '../components/work/WorkSheet'
import NowStrip from '../components/work/NowStrip'
import TaskMap from '../components/work/TaskMap'
import TaskInspector from '../components/work/TaskInspector'
import SiteModel from '../components/work/SiteModel'
import ColorFilter from '../components/work/ColorFilter'
import Welcome from '../components/work/Welcome'
import { emptyStore, pathTo } from '../model/tasks.mjs'
import { reconcile } from '../model/selection.mjs'
import { COLOR_VALUES, FILTERS, familyCounts, flatRows, inBranch, markedPaths, taskCounts, visibleRows } from '../model/workList.mjs'
import { changedSince, rememberRecent, sheetRows } from '../model/workSheet.mjs'
import { mapLayout } from '../model/mapLayout.mjs'
import { siteLayout, stripLayout } from '../model/siteLayout.mjs'
import { readFinishedOpen, readInspectorWidth, readMapDirection, readRecent, readSeen, readSitePanel, saveFinishedOpen, saveInspectorWidth, saveMapDirection,
  saveRecent, saveSeen, saveSitePanel } from '../layout.mjs'
import { projectDirectory, projectHue, projectLabel, projectMenu } from '../model/projects.mjs'
import { captureAnchors, playLensTransition } from '../lensTransition'
import { rememberWork, requestReply } from '../workContext'
import { pageShortcut } from '../keys.mjs'
import { ATTENTION_KINDS, attentionInView, attentionKind, stepAttention } from '../model/attention.mjs'
import { StatusBead } from '../components/identity'

const EMPTY = emptyStore()
const FILTER_VALUES = new Set([...FILTERS.map((filter) => filter.value), 'changed'])
const LENSES = [{ value: 'list', label: 'List', icon: 'list', title: 'List · L' }, { value: 'site', label: 'Site', icon: 'site', title: 'Site · S' },
  { value: 'map', label: 'Map', icon: 'map', title: 'Map · M' }]
const clip = (text, limit) => (text && text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text)
// Up to this many loaded Tasks, every open family is shown expanded; beyond it, families are one
// line each and a family that needs the person shows only the path to what needs them.
const SMALL = 60

// A key press meant for the page: not in a field, a menu, a dialog or the details panel.
const pageKey = (event) => pageShortcut(event, { exclude: '.a-inspector' })

// "Needs you" navigation: where the selected Task is among the Tasks that need the person in this
// view, Previous and Next (Shift A, A), and Reply (R) when the selected Task asks a question. Counts
// are of loaded Tasks; questions the backend counts that may be on unloaded pages are said to be.
function AttentionBar({ ids, selectedId, derived, summary, filtered, complete, onStep, onReply, onLoadMore, loadingMore }) {
  const unlisted = summary?.unlisted || 0
  const index = ids.indexOf(selectedId)
  // The Now strip already counts what needs you; the bar appears while you step through those tasks,
  // or when the backend counts questions the loaded pages don't show.
  if ((!ids.length || index < 0) && !unlisted) return null
  const kind = index >= 0 ? attentionKind(derived, selectedId) : null
  const n = ids.length
  const where = filtered ? ' in this view' : complete ? '' : ' among loaded tasks'
  const text = !n ? `Nothing loaded${filtered ? ' in this view' : ''} needs you`
    : index >= 0 ? `${index + 1} of ${n} that need you${where}` : `${n} ${n === 1 ? 'task needs' : 'tasks need'} you${where}`
  const alone = n < 2 && index >= 0
  return <div className="a-attnbar" role="navigation" aria-label="Tasks that need you">
    <StatusBead state="ask" size={14} decorative />
    <span className="a-attnbar-text">{text}{kind ? <span className="a-attnbar-kind"> · {ATTENTION_KINDS[kind].label}</span> : null}</span>
    {n > 0 && <span className="a-attnbar-steps">
      <Button size="sm" variant="ghost" icon="left" onClick={() => onStep(-1)} disabled={alone}
        aria-label="Previous task that needs you" title="Previous · Shift A" aria-keyshortcuts="Shift+A">Previous</Button>
      <Button size="sm" variant="ghost" onClick={() => onStep(1)} disabled={alone}
        aria-label={index >= 0 ? 'Next task that needs you' : 'Go to the first task that needs you'} title="Next · A" aria-keyshortcuts="A">{index >= 0 ? 'Next' : 'Go'}<Icon name="right" size={14} /></Button>
    </span>}
    {kind === 'reply' && <Button size="sm" variant="ask" icon="reply" onClick={() => onReply(selectedId)} title="Reply · R" aria-keyshortcuts="R">Reply</Button>}
    {unlisted > 0 && <span className="a-attnbar-more">
      <span className="a-fine">{complete ? `The backend counts ${summary.reported} waiting for a reply` : `${unlisted} more waiting for a reply may be on older pages`}</span>
      {!complete && <Button size="sm" icon="down" onClick={onLoadMore} disabled={loadingMore}>{loadingMore ? 'Loading…' : 'Look on older pages'}</Button>}
    </span>}
  </div>
}

// Work: every task family as a List and as a Map, two views of the same Tasks. Selection, scope,
// project, colours, focus, the view and the side list live in the address, so Back/Forward and
// reloads restore them; both views share the open branches and the details panel.
export default function WorkPage({ live, route, navigate, feed, derived, attention, projectsResource, onGoTo }) {
  // The shell owns the canonical feed and its derived workspace (the attention list uses them too).
  const store = feed.store || EMPTY
  // Actions check the latest store when they run, not the one drawn when a form opened.
  const storeRef = useRef(store)
  useEffect(() => { storeRef.current = store }, [store])
  const getStore = useCallback(() => storeRef.current, [])
  // The registered projects (GET /projects) are the authoritative list; the shell reads them once
  // per change for the whole app.
  const directory = useMemo(() => projectDirectory(projectsResource), [projectsResource])
  const projectName = useCallback((id) => projectLabel(directory, id), [directory])
  const params = useMemo(() => route.params || {}, [route.params])
  const selectedId = typeof params.task === 'string' && params.task ? params.task : null
  const filter = FILTER_VALUES.has(params.filter) ? params.filter : 'all'
  const project = params.project && params.project.length <= 200 ? params.project : null
  const colorParam = typeof params.colors === 'string' ? params.colors : ''
  const colors = useMemo(() => new Set(colorParam.split(',').filter((value) => COLOR_VALUES.includes(value))), [colorParam])
  const lens = params.lens === 'map' ? 'map' : params.lens === 'site' ? 'site' : 'list'
  const side = lens === 'map' && params.side === '1'
  const focusParam = typeof params.focus === 'string' && params.focus && params.focus.length <= 200 ? params.focus : null
  const focus = focusParam && store.tasks[focusParam] ? focusParam : null
  const [query, setQuery] = useState('')
  const [overrides, setOverrides] = useState(() => new Map())
  const [focusId, setFocusId] = useState(null)
  const [width, setWidth] = useState(() => readInspectorWidth())
  const [direction, setDirection] = useState(() => readMapDirection())
  const turnMap = useCallback((next) => { setDirection(next); saveMapDirection(next) }, [])
  const [finishedChoice, setFinishedChoice] = useState(() => readFinishedOpen())
  // What the person has looked at, for "changed since you looked". The first visit starts clean.
  const [seen, setSeen] = useState(() => readSeen() || { since: new Date(Date.now()).toISOString(), opened: {} })
  useEffect(() => { saveSeen(seen) }, [seen])
  const [tick, setTick] = useState(() => Date.now())
  const [revealedFor, setRevealedFor] = useState(null)
  const [mapRequest, setMapRequest] = useState(null)
  const cameraRef = useRef(null)
  const body = useRef(null)
  const workRef = useRef(null)
  const pendingLens = useRef(null)
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 30000)
    return () => clearInterval(timer)
  }, [])
  // Row times count from the latest of the clock tick and the last successful read.
  const now = Math.max(tick, feed.readAt || 0)

  const loadedCount = Object.keys(store.tasks).length
  const large = loadedCount > SMALL
  const marked = useMemo(() => markedPaths(store, derived), [store, derived])
  // The view the person chose: scope, project, search, colours and focus.
  const viewKey = `${filter}|${project}|${query}|${colorParam}|${focus}`
  // The selected Task is held in the view it was listed in: a live change (it finishes, its question
  // is answered, it is no longer "changed") never takes its row away, folds its branch shut or moves
  // its family to another section while the person looks at it. Choosing another view or another
  // Task lets go, and the family then moves to where it belongs.
  const [held, setHeld] = useState({ id: null, key: '', section: null })
  const holdId = selectedId && held.id === selectedId && held.key === viewKey && store.tasks[selectedId] ? selectedId : null
  const holdPath = useMemo(() => (holdId ? new Set(pathTo(derived.forest, holdId)) : null), [holdId, derived])
  // Branches: in a small workspace every open family starts expanded; in a large one families are
  // one line each, and a family that needs the person opens only along the path to what needs them.
  // Finished families start closed. The person's own choices always win.
  const { expanded, prune } = useMemo(() => {
    const open = new Set()
    const pruned = new Set()
    for (const family of derived.families) {
      for (const id of family.members) {
        const choice = overrides.has(id) ? overrides.get(id) : undefined
        let fallback = family.section !== 'done' && !large
        if (large && family.section === 'needs') fallback = marked.has(id)
        if (holdPath?.has(id)) fallback = true
        if (choice ?? fallback) open.add(id)
        if (large && family.section === 'needs' && choice !== true) pruned.add(id)
      }
    }
    return { expanded: open, prune: pruned }
  }, [derived, overrides, large, marked, holdPath])

  // Opening a task (from the address, Back/Forward or a link) reveals the loaded path to it once;
  // collapsing a branch afterwards is the person's choice.
  if (selectedId && store.tasks[selectedId] && revealedFor !== selectedId) {
    setRevealedFor(selectedId)
    const path = pathTo(derived.forest, selectedId)
    if (path.some((id) => !expanded.has(id) || (prune.has(id) && !marked.has(selectedId)))) {
      setOverrides((current) => { const next = new Map(current); for (const id of path) next.set(id, true); return next })
    }
    setFocusId(selectedId)
  }

  // The Task on screen in the details panel is being looked at, so it is never marked as changed.
  const changed = useMemo(() => {
    const base = changedSince(store, seen)
    return (id) => id !== selectedId && base(id)
  }, [store, seen, selectedId])
  const holdSection = holdId ? held.section : null
  const view = useMemo(() => visibleRows(store, derived, { expanded, filter, project, query, colors, focus, prune, marked, changed, hold: holdId, holdSection }),
    [store, derived, expanded, filter, project, query, colors, focus, prune, marked, changed, holdId, holdSection])
  const familyTotals = familyCounts(derived, project)
  const finishedOpen = finishedChoice ?? (familyTotals.done + familyTotals.canceled <= 6 || filter === 'done' || filter === 'canceled')
  const rows = useMemo(() => (lens === 'list' || side ? sheetRows(view, store, derived, { now, finishedOpen, complete: store.complete, keep: selectedId }) : []),
    [lens, side, view, store, derived, now, finishedOpen, selectedId])
  const layout = useMemo(() => (lens === 'map' ? mapLayout(view, derived, { chips: (id, row) => {
    const task = store.tasks[id]
    const loaded = (derived.forest.children[id] || []).length
    return row.hasChildren || task.planned.unmaterialized > 0 || (!store.complete && (task.childCount ?? 0) > loaded)
      || (row.depth === 0 && row.parentId === null && derived.byTop[id] && derived.byTop[id].placement !== 'root')
  }, direction }) : null), [lens, view, derived, store, direction])
  // The Site: the same families as a massing model. The full Site lays every district out; the
  // panorama above the List keeps open work standing and gathers each project's history into one block.
  const siteProjects = useMemo(() => directory.projects.filter((item) => !item.hidden || item.id === project).map((item) => ({ id: item.id, name: item.name })),
    [directory, project])
  const [panel, setPanel] = useState(() => readSitePanel())
  const togglePanel = useCallback(() => setPanel((open) => { saveSitePanel(!open); return !open }), [])
  const site = useMemo(() => (lens === 'site' ? siteLayout(derived, view, siteProjects) : null), [lens, derived, view, siteProjects])
  const strip = useMemo(() => (lens === 'list' && panel ? stripLayout(derived, view, siteProjects) : null), [lens, panel, derived, view, siteProjects])
  const selectedTop = selectedId && store.tasks[selectedId] ? derived.forest.rootOf[selectedId] ?? selectedId : null
  const titleOf = useCallback((id) => store.tasks[id]?.title || 'Untitled task', [store])
  const counts = useMemo(() => taskCounts(store, derived, project), [store, derived, project])
  const changedCount = useMemo(() => {
    let n = 0
    for (const id of Object.keys(store.tasks)) if ((!project || store.tasks[id].projectId === project) && changed(id)) n += 1
    return n
  }, [store, project, changed])
  const selection = reconcile({ taskId: selectedId, focusId: null, expanded: [] }, store).status
  const shownIds = new Set(flatRows(view).map((row) => row.id))
  const selectedHidden = selection === 'present' && !shownIds.has(selectedId)
  // Hold the selection from the first render that lists it in this view (see `held` above), and let
  // go of a hold that no longer applies, so returning to an earlier view doesn't bring it back.
  if (selectedId && holdId !== selectedId && store.tasks[selectedId] && shownIds.has(selectedId)) {
    const top = derived.forest.rootOf[selectedId]
    setHeld({ id: selectedId, key: viewKey, section: view.sections.find((section) => section.families.some((family) => family.topId === top))?.key ?? null })
  } else if (held.id && !holdId && !(selectedId && shownIds.has(selectedId))) setHeld({ id: null, key: '', section: null })

  const go = useCallback((patch) => navigate('work', { ...params, ...patch }), [navigate, params])
  // Selecting a Task outside a focused branch leaves the focus, so the selection is never hidden.
  const select = useCallback((id) => {
    setSeen((current) => ({ ...current, opened: { ...current.opened, [id]: new Date(Date.now()).toISOString() } }))
    go({ task: id, ...(focus && !inBranch(derived.forest, focus, id) ? { focus: undefined } : {}) })
  }, [go, focus, derived])
  // Opening a Task counts as looking at it, and it joins the recently opened list for Go to.
  const selectedLoaded = Boolean(selectedId && store.tasks[selectedId])
  useEffect(() => { if (selectedLoaded) saveRecent(rememberRecent(readRecent(), selectedId)) }, [selectedId, selectedLoaded])
  // The Tasks that need the person in this view, in the List's order; A and Shift A move between
  // them, and moving opens the path to the Task in both views.
  const attentionIds = useMemo(() => attentionInView(store, derived, { filter, project, query, colors, focus }),
    [store, derived, filter, project, query, colors, focus])
  const goAttention = useCallback((direction) => {
    const id = stepAttention(attentionIds, selectedId, direction)
    if (id) select(id)
  }, [attentionIds, selectedId, select])
  // Reply: select the Task and put the cursor in its answer (the details panel's own form).
  const replyTo = useCallback((id) => { requestReply(id); select(id) }, [select])
  const toggle = useCallback((id, open) => setOverrides((current) => {
    const next = new Map(current)
    next.set(id, open ?? !expanded.has(id))
    return next
  }), [expanded])
  const showAll = useCallback((id) => setOverrides((current) => new Map(current).set(id, true)), [])
  const setAll = (open) => setOverrides(() => new Map(Object.keys(store.tasks).map((id) => [id, open])))
  const toggleFinished = useCallback((open) => { setFinishedChoice(open); saveFinishedOpen(open) }, [])
  const clearChanged = useCallback(() => {
    setSeen({ since: new Date(Date.now()).toISOString(), opened: {} })
    if (filter === 'changed') go({ filter: undefined })
  }, [filter, go])
  const reveal = () => {
    setQuery('')
    const path = pathTo(derived.forest, selectedId)
    setOverrides((current) => { const next = new Map(current); for (const id of path) next.set(id, true); return next })
    go({ filter: undefined, project: undefined, colors: undefined, focus: undefined })
  }
  // Switching views keeps everything else; the agents' avatars travel between the two.
  const switchLens = useCallback((next, patch = {}) => {
    if (next !== lens) pendingLens.current = { to: next, anchors: captureAnchors(body.current) }
    go({ lens: next === 'list' ? undefined : next, ...patch })
  }, [go, lens])
  const showInMap = useCallback((id) => {
    setMapRequest((current) => ({ seq: (current?.seq || 0) + 1, id }))
    switchLens('map', { task: id, ...(focus && !inBranch(derived.forest, focus, id) ? { focus: undefined } : {}) })
  }, [switchLens, focus, derived])
  const focusBranch = useCallback((id) => go({ focus: id, task: id }), [go])
  const openTask = useCallback((id) => navigate('task', { id }), [navigate])
  // The Work view is remembered so a Task page's "Work" link returns to it.
  useEffect(() => { rememberWork(params) }, [params])
  const leaveFocus = useCallback(() => go({ focus: undefined }), [go])
  const scope = useCallback((value) => go({ filter: value === 'all' ? undefined : value }), [go])

  useLayoutEffect(() => {
    const pending = pendingLens.current
    if (!pending || pending.to !== lens) return
    pendingLens.current = null
    playLensTransition(body.current, pending.anchors)
  }, [lens])

  // L and M switch views, F focuses the selected Task's branch, Escape leaves the focus.
  useEffect(() => {
    const onKey = (event) => {
      if (!pageKey(event)) return
      const key = event.key.toLowerCase()
      if (key === 'l' && lens !== 'list') { event.preventDefault(); switchLens('list') }
      else if (key === 'm' && lens !== 'map') { event.preventDefault(); switchLens('map') }
      else if (key === 's' && lens !== 'site') { event.preventDefault(); switchLens('site') }
      else if (key === 'f' && selectedId && store.tasks[selectedId]) { event.preventDefault(); focusBranch(selectedId) }
      else if (key === 'o' && selectedId && store.tasks[selectedId]) { event.preventDefault(); openTask(selectedId) }
      else if (key === 'a' && attentionIds.length) { event.preventDefault(); goAttention(event.shiftKey ? -1 : 1) }
      else if (key === 'r' && selectedId && store.tasks[selectedId] && attentionKind(derived, selectedId) === 'reply') { event.preventDefault(); replyTo(selectedId) }
      else if (event.key === 'Escape' && focusParam) { event.preventDefault(); leaveFocus() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lens, switchLens, selectedId, store, focusBranch, focusParam, leaveFocus, openTask, attentionIds, goAttention, replyTo, derived])

  // Pointing at a Task in one view highlights it in the other (the side list beside the Map). The
  // highlight is a class set directly, re-applied after each render so a refresh never drops it.
  const hovered = useRef(null)
  const highlight = useCallback(() => {
    const root = body.current
    if (!root) return
    const id = hovered.current
    for (const element of root.querySelectorAll('.is-hl')) if (element.dataset.id !== id) element.classList.remove('is-hl')
    if (id) for (const element of root.querySelectorAll(`[data-id="${CSS.escape(id)}"]`)) element.classList.add('is-hl')
  }, [])
  useLayoutEffect(() => { highlight() })
  const hover = (event) => {
    hovered.current = event.type === 'mouseleave' || !(event.target instanceof Element) ? null : event.target.closest('[data-id]')?.dataset.id || null
    highlight()
  }

  // In narrow windows the details panel floats over the list; it starts below the header and any
  // notices (which wrap onto more lines as the window narrows), so it never covers their controls.
  useLayoutEffect(() => {
    const work = workRef.current
    const wbody = body.current
    if (!work || !wbody) return undefined
    const place = () => {
      const top = Math.max(0, Math.round(wbody.getBoundingClientRect().top - work.getBoundingClientRect().top))
      if (work.style.getPropertyValue('--a-work-head') !== `${top}px`) work.style.setProperty('--a-work-head', `${top}px`)
    }
    place()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(place)
    observer?.observe(wbody.parentElement)
    observer?.observe(wbody)
    return () => observer?.disconnect()
  })

  const ready = feed.phase === 'ready' || (feed.store && feed.readAt)
  // A new installation (no tasks at all) is welcomed instead of shown an empty toolbar.
  const welcoming = ready && store.complete && !Object.keys(store.tasks).length
  // While older pages remain, counts describe the loaded tasks only.
  const partial = ready && !store.complete
  const loadedTasks = Object.values(store.tasks).filter((task) => task.source === 'page').length
  const countNote = partial ? `Counts cover the ${loadedTasks} loaded tasks${store.total !== null ? ` of ${store.total}` : ''}` : null
  const stale = feed.stale || live.reachable === false
  const crumbs = focus ? pathTo(derived.forest, focus).reverse() : []
  const hueOf = useCallback((id) => (id ? projectHue(id) : 250), [])

  const empty = <p className="a-ol-empty">{project && !derived.families.some((family) => family.projectId === project)
    ? `No loaded tasks in ${projectName(project)}.${store.complete ? '' : ' Older tasks may belong to it.'}`
    : filter === 'changed' ? 'Nothing changed since you last looked.' : 'Nothing matches.'}{' '}
    <Button size="sm" onClick={() => { setQuery(''); go({ filter: undefined, project: undefined, colors: undefined, focus: undefined }) }}>Clear filters</Button></p>
  const sheet = (compact) => <WorkSheet rows={rows} viewKey={viewKey} store={store} derived={derived} projectName={projectName} hueOf={hueOf} now={now}
    selectedId={selectedId} focusId={focusId} setFocusId={setFocusId} onSelect={select} onToggle={toggle} onShowAll={showAll}
    onToggleFinished={toggleFinished} onLoadMore={feed.loadMore} loadingMore={feed.loadingMore} empty={empty} compact={compact}
    onShowInMap={compact ? undefined : showInMap} onOpenTask={compact ? undefined : openTask} onReply={compact ? undefined : replyTo}
    changed={changed} />

  let content
  if (!ready && feed.phase === 'error') {
    content = <div className="a-work-state"><EmptyState icon="plug" title="The task list couldn’t be loaded">
      The Control Center backend isn’t answering. This page retries by itself.</EmptyState></div>
  } else if (!ready) {
    content = <div className="a-work-state"><Skeleton lines={6} label="Loading your tasks" /></div>
  } else if (!Object.keys(store.tasks).length) {
    content = <div className="a-work-state a-work-state--welcome"><Welcome directory={directory} navigate={navigate} /></div>
  } else if (lens === 'site') {
    content = <>
      <SiteModel mode="site" layout={site} selectedTop={selectedTop} titleOf={titleOf} projectNameOf={projectName}
        onSelect={select} onOpen={openTask} onArchive={(id) => go({ project: id === '__none' ? undefined : id, filter: 'done' })} />
      <p className="a-site-hint" aria-hidden="true">Drag to turn · Shift-drag to move · Scroll to zoom · Double-click a building to open it · [ ] turn · 0 fit</p>
    </>
  } else if (lens === 'map') {
    content = <>
      <TaskMap layout={layout} view={view} store={store} derived={derived} projectName={projectName} now={now} selectedId={selectedId}
        onSelect={select} onToggle={toggle} onFocusBranch={focusBranch} focusBranch={focus} side={side}
        onToggleSide={() => go({ side: side ? undefined : '1' })} onDirection={turnMap} cameraRef={cameraRef} request={mapRequest}
        onLoadMore={feed.loadMore} loadingMore={feed.loadingMore} empty={empty} />
      {side && <div className="a-side" data-side-list>{sheet(true)}</div>}
    </>
  } else {
    content = sheet(false)
  }

  return <div className={`a-work${side ? ' has-side' : ''}`} ref={workRef}>
    <div className="a-wmain">
      <h1 className="a-sr" tabIndex={-1} data-page-heading>Work</h1>
      {!welcoming && <div className="a-whead">
        <MenuButton label="Choose a project" text={project ? projectName(project) : 'All projects'} icon="folder" align="start"
          items={projectMenu(directory, project).map((item) => item.separator ? item : { label: item.label, disabled: item.disabled,
            icon: item.checked ? 'check' : undefined, onSelect: item.value === undefined ? undefined : () => go({ project: item.value || undefined }) })} />
        {countNote && <span id="work-count-note" className="a-count-note" title={`${countNote}. Load older tasks to include them.`}>
          <Icon name="info" size={13} />{countNote}</span>}
        <span className="a-sp" />
        <ColorFilter selected={colors} onChange={(next) => go({ colors: [...next].join(',') || undefined })} />
        <label className="a-find">
          <Icon name="search" size={14} />
          <input type="search" name="work-find" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find in loaded tasks"
            aria-label="Find in loaded tasks" dir="auto" onKeyDown={(event) => { if (event.key === 'Escape' && query) { event.stopPropagation(); setQuery('') } }} />
        </label>
        <Segmented label="View as" size="sm" className="a-lens" value={lens} onChange={(value) => switchLens(value)} options={LENSES} />
        <MenuButton label="View options" icon="sliders" items={[
          ...(onGoTo ? [{ label: 'Go to a task…', icon: 'search', hint: 'Ctrl K', onSelect: onGoTo }, { separator: true }] : []),
          { label: 'Expand everything', icon: 'down', onSelect: () => setAll(true) },
          { label: 'Collapse to task families', icon: 'right', onSelect: () => setAll(false) },
          { label: finishedOpen ? 'Fold history away' : 'List history by day', icon: finishedOpen ? 'right' : 'down', onSelect: () => toggleFinished(!finishedOpen) },
          ...(changedCount ? [{ label: 'Mark everything as seen', icon: 'check', onSelect: clearChanged }] : []),
          ...(selectedId && store.tasks[selectedId] ? [{ separator: true }, { label: 'Open the selected task', icon: 'open', hint: 'O', onSelect: () => openTask(selectedId) },
            { label: 'Focus the selected branch', icon: 'focus', hint: 'F', onSelect: () => focusBranch(selectedId) }] : []),
        ]} />
      </div>}
      {ready && !welcoming && lens === 'list' && <div className={`a-panorama${panel ? '' : ' is-closed'}`}>
        {panel && strip && <SiteModel mode="strip" layout={strip} selectedTop={selectedTop} titleOf={titleOf} projectNameOf={projectName}
          onSelect={select} onOpen={openTask} onArchive={(id) => go({ project: id === '__none' ? undefined : id, filter: 'done' })}
          label="Site: open work as buildings, each project's history as one block" />}
        <button type="button" className="a-panorama-toggle" onClick={togglePanel} aria-expanded={panel}
          title={panel ? 'Fold the site away' : 'Show the site above the list'}><Icon name="site" size={14} />{panel ? 'Fold the site' : 'Site'}</button>
      </div>}
      {ready && !welcoming && <NowStrip counts={counts} families={familyTotals} filter={filter} onScope={scope} partial={partial}
        changedCount={changedCount} onChanged={scope} onClearChanged={clearChanged} onFirstNeed={attentionIds.length ? () => goAttention(1) : null}
        stepping={attentionIds.includes(selectedId)} describedBy={countNote ? 'work-count-note' : undefined} />}
      {ready && !welcoming && <AttentionBar ids={attentionIds} selectedId={selectedId} derived={derived} summary={attention}
        filtered={Boolean(project || focus || query || colors.size || filter !== 'all')} complete={store.complete} onStep={goAttention} onReply={replyTo}
        onLoadMore={feed.loadMore} loadingMore={feed.loadingMore} />}
      {(stale || selectedHidden || (query && !store.complete) || focus || (focusParam && ready && !focus)) && <div className="a-work-notices">
        {stale && <Notice tone="warn" role="status" title="Showing the last list received">
          {feed.readAt ? `Received at ${new Date(feed.readAt).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}. ` : ''}
          The backend isn’t answering right now; the list updates by itself when it returns.</Notice>}
        {focus && <div className="a-focusbar" role="navigation" aria-label="Focused branch">
          <span className="a-focuspill"><Icon name="focus" size={13} />Focused</span>
          <button type="button" className="a-link" onClick={leaveFocus}>All work</button>
          {crumbs.map((id) => <span key={id} className="a-crumb"><span aria-hidden="true">›</span>
            <button type="button" className="a-link a-bidi" dir="auto" title="Focus this branch instead" onClick={() => focusBranch(id)}>{clip(store.tasks[id].title || 'Untitled task', 32)}</button></span>)}
          <span className="a-crumb"><span aria-hidden="true">›</span><b className="a-bidi" dir="auto" aria-current="location">{clip(store.tasks[focus].title || 'Untitled task', 48)}</b></span>
          <span className="a-sp" />
          <Button size="sm" variant="ghost" icon="close" onClick={leaveFocus} title="Leave focus · Esc">Leave focus</Button>
        </div>}
        {focusParam && ready && !focus && <Notice tone="info" title="The focused branch isn’t loaded"
          actions={<>{!store.complete && <Button size="sm" icon="down" onClick={feed.loadMore}>Load older tasks</Button>}
            <Button size="sm" onClick={leaveFocus}>Leave focus</Button></>}>
          {store.complete ? 'That task no longer exists, so all work is shown.' : 'It may be on a page that isn’t loaded yet; all loaded work is shown meanwhile.'}</Notice>}
        {selectedHidden && <Notice tone="info" title="The selected task is hidden by the current view"
          actions={<Button size="sm" onClick={reveal}>Show it</Button>}>Its details stay open on the right.</Notice>}
        {query && !store.complete && <p className="a-fine">Searching the loaded tasks only. Load older tasks to search them too.</p>}
      </div>}
      <div className={`a-wbody a-wbody--${lens}`} ref={body} onMouseOver={hover} onMouseLeave={hover}>{content}</div>
    </div>
    {selectedId && ready && <TaskInspector key={selectedId} store={store} derived={derived} taskId={selectedId} selection={selection}
      projectName={projectName} version={live.version} now={now} width={width}
      onResize={(value) => { setWidth(value); saveInspectorWidth(value) }}
      onClose={() => { setFocusId(selectedId); go({ task: undefined }) }} onSelect={select} onLoadMore={feed.loadMore}
      getStore={getStore} onRefresh={feed.refresh} lens={lens} onShowInMap={showInMap} onFocusBranch={focusBranch} onOpenTask={openTask} />}
  </div>
}
