import { useEffect, useMemo, useState } from 'react'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import { Segmented } from '../components/controls'
import { MenuButton } from '../components/overlays'
import Icon from '../components/Icon'
import TaskTree from '../components/work/TaskTree'
import TaskInspector from '../components/work/TaskInspector'
import { API_BASE } from '../../apiClient.mjs'
import { useLiveResource, useTaskFeed } from '../hooks'
import { emptyStore, pathTo } from '../model/tasks.mjs'
import { reconcile } from '../model/selection.mjs'
import { FILTERS, deriveWorkspace, familyCounts, flatRows, visibleRows } from '../model/workList.mjs'
import { readInspectorWidth, saveInspectorWidth } from '../layout.mjs'
import { projectDirectory, projectLabel, projectMenu } from '../model/projects.mjs'
import { exitAtlas } from '../uiMode.mjs'

const FILTER_VALUES = new Set(FILTERS.map((filter) => filter.value))

// Work: every task family as a hierarchical list, with a read-only inspector for the selected task.
// Selection, filter and project live in the address, so Back/Forward and reloads restore them.
export default function WorkPage({ live, route, navigate }) {
  const feed = useTaskFeed(live.version)
  const store = feed.store || emptyStore()
  const derived = useMemo(() => deriveWorkspace(store), [store])
  // The registered projects (GET /projects) are the authoritative list; they are re-read with the
  // shared connection's updates, so a project registered meanwhile appears without a reload.
  const projectsResource = useLiveResource(`${API_BASE}/projects`, live.version)
  const directory = useMemo(() => projectDirectory(projectsResource), [projectsResource])
  const projectName = (id) => projectLabel(directory, id)
  const params = route.params || {}
  const selectedId = typeof params.task === 'string' && params.task ? params.task : null
  const filter = FILTER_VALUES.has(params.filter) ? params.filter : 'all'
  const project = params.project && params.project.length <= 200 ? params.project : null
  const [query, setQuery] = useState('')
  const [overrides, setOverrides] = useState(() => new Map())
  const [focusId, setFocusId] = useState(null)
  const [width, setWidth] = useState(() => readInspectorWidth())
  const [tick, setTick] = useState(() => Date.now())
  const [revealedFor, setRevealedFor] = useState(null)
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 30000)
    return () => clearInterval(timer)
  }, [])
  // Row times count from the latest of the clock tick and the last successful read.
  const now = Math.max(tick, feed.readAt || 0)

  // Branches start open, except finished families; the person's own choices win.
  const expanded = useMemo(() => {
    const open = new Set()
    for (const family of derived.families) {
      for (const id of family.members) {
        const fallback = family.section !== 'done'
        if (overrides.has(id) ? overrides.get(id) : fallback) open.add(id)
      }
    }
    return open
  }, [derived, overrides])

  // Opening a task (from the address, Back/Forward or a link) reveals the loaded path to it once;
  // collapsing a branch afterwards is the person's choice.
  if (selectedId && store.tasks[selectedId] && revealedFor !== selectedId) {
    setRevealedFor(selectedId)
    const path = pathTo(derived.forest, selectedId)
    if (path.some((id) => !expanded.has(id))) {
      setOverrides((current) => { const next = new Map(current); for (const id of path) next.set(id, true); return next })
    }
    setFocusId(selectedId)
  }

  const view = useMemo(() => visibleRows(store, derived, { expanded, filter, project, query }), [store, derived, expanded, filter, project, query])
  const counts = familyCounts(derived, project)
  const selection = reconcile({ taskId: selectedId, focusId: null, expanded: [] }, store).status
  const shownIds = new Set(flatRows(view).map((row) => row.id))
  const selectedHidden = selection === 'present' && !shownIds.has(selectedId)

  const go = (patch) => navigate('work', { ...params, ...patch })
  const select = (id) => go({ task: id })
  const toggle = (id, open) => setOverrides((current) => {
    const next = new Map(current)
    next.set(id, open ?? !expanded.has(id))
    return next
  })
  const reveal = () => {
    setQuery('')
    const path = pathTo(derived.forest, selectedId)
    setOverrides((current) => { const next = new Map(current); for (const id of path) next.set(id, true); return next })
    go({ filter: undefined, project: undefined })
  }

  const ready = feed.phase === 'ready' || (feed.store && feed.readAt)
  // While older pages remain, the section counts describe the loaded tasks only.
  const partial = ready && !store.complete
  const loadedTasks = Object.values(store.tasks).filter((task) => task.source === 'page').length
  const countNote = partial ? `Counts cover the ${loadedTasks} loaded tasks${store.total !== null ? ` of ${store.total}` : ''}` : null
  const stale = feed.stale || live.reachable === false

  let body
  if (!ready && feed.phase === 'error') {
    body = <div className="a-work-state"><EmptyState icon="plug" title="The task list couldn’t be loaded">
      The Control Center backend isn’t answering. This page retries by itself.</EmptyState></div>
  } else if (!ready) {
    body = <div className="a-work-state"><Skeleton lines={6} label="Loading your tasks" /></div>
  } else if (!Object.keys(store.tasks).length) {
    body = <div className="a-work-state"><EmptyState icon="layers" title="No tasks yet"
      actions={<Button variant="primary" icon="swap" onClick={() => exitAtlas()}>Start a task in Classic view</Button>}>
      Tasks you start appear here with their agents and helpers. Starting a task from Atlas comes in a later milestone.</EmptyState></div>
  } else {
    body = <TaskTree view={view} store={store} derived={derived} projectName={projectName} now={now} selectedId={selectedId}
      focusId={focusId} setFocusId={setFocusId} onSelect={select} onToggle={toggle} onLoadMore={feed.loadMore} loadingMore={feed.loadingMore}
      empty={<p className="a-ol-empty">{project && !derived.families.some((family) => family.projectId === project)
        ? `No loaded tasks in ${projectName(project)}.${store.complete ? '' : ' Older tasks may belong to it.'}` : 'Nothing matches.'}{' '}
        <Button size="sm" onClick={() => { setQuery(''); go({ filter: undefined, project: undefined }) }}>Clear filters</Button></p>} />
  }

  return <div className="a-work">
    <div className="a-wmain">
      <h1 className="a-sr" tabIndex={-1} data-page-heading>Work</h1>
      <div className="a-whead">
        <MenuButton label="Choose a project" text={project ? projectName(project) : 'All projects'} icon="folder" align="start"
          items={projectMenu(directory, project).map((item) => item.separator ? item : { label: item.label, disabled: item.disabled,
            icon: item.checked ? 'check' : undefined, onSelect: item.value === undefined ? undefined : () => go({ project: item.value || undefined }) })} />
        <Segmented label="Show" size="sm" value={filter} onChange={(value) => go({ filter: value === 'all' ? undefined : value })}
          describedBy={countNote ? 'work-count-note' : undefined}
          options={FILTERS.map((option) => ({ ...option, count: ready ? counts[option.value] : undefined,
            title: ready ? `${counts[option.value]} task ${counts[option.value] === 1 ? 'family' : 'families'}${partial ? ' among the loaded tasks; older tasks are not counted' : ''}` : undefined }))} />
        {countNote && <span id="work-count-note" className="a-count-note" title={`${countNote}. Load older tasks to include them.`}>
          <Icon name="info" size={13} />{countNote}</span>}
        <span className="a-sp" />
        <label className="a-find">
          <Icon name="search" size={14} />
          <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find in loaded tasks"
            aria-label="Find in loaded tasks" dir="auto" onKeyDown={(event) => { if (event.key === 'Escape' && query) { event.stopPropagation(); setQuery('') } }} />
        </label>
      </div>
      {(stale || selectedHidden || (query && !store.complete)) && <div className="a-work-notices">
        {stale && <Notice tone="warn" role="status" title="Showing the last list received">
          {feed.readAt ? `Received at ${new Date(feed.readAt).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}. ` : ''}
          The backend isn’t answering right now; the list updates by itself when it returns.</Notice>}
        {selectedHidden && <Notice tone="info" title="The selected task is hidden by the current view"
          actions={<Button size="sm" onClick={reveal}>Show it</Button>}>Its details stay open on the right.</Notice>}
        {query && !store.complete && <p className="a-fine">Searching the loaded tasks only. Load older tasks to search them too.</p>}
      </div>}
      <div className="a-wbody">{body}</div>
    </div>
    {selectedId && ready && <TaskInspector key={selectedId} store={store} derived={derived} taskId={selectedId} selection={selection}
      projectName={projectName} version={live.version} now={now} width={width}
      onResize={(value) => { setWidth(value); saveInspectorWidth(value) }}
      onClose={() => { setFocusId(selectedId); go({ task: undefined }) }} onSelect={select} onLoadMore={feed.loadMore} />}
  </div>
}
