import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
import './styles/shell.css'
import './styles/pages.css'
import './styles/work.css'
import './styles/agent.css'
import './styles/create.css'
import './styles/map.css'
import './styles/task.css'
import './styles/connections.css'
import './styles/notifications.css'
import './styles/lumen.css'
import './styles/sheet.css'
import './styles/signal.css'
import TopBar from './shell/TopBar'
import Rail from './shell/Rail'
import WorkPage from './pages/WorkPage'
import ConnectionsPage from './pages/ConnectionsPage'
import NewTaskPage from './pages/NewTaskPage'
import ProjectsPage from './pages/ProjectsPage'
import TaskPage from './pages/TaskPage'
import SettingsPage from './pages/SettingsPage'
import GalleryPage from './pages/GalleryPage'
import NotFoundPage from './pages/NotFoundPage'
import GoTo from './components/work/GoTo'
import { readRecent } from './layout.mjs'
import { useAppearance, useAtlasRoute, useConnections, useLiveResource, useLiveStatus, useNotificationPreference, useProviderNotices, useTaskFeed } from './hooks'
import { useBrowserNotifications } from './notifications'
import { emptyStore } from './model/tasks.mjs'
import { deriveWorkspace, familyCounts, projectStats, rowColor, taskCounts } from './model/workList.mjs'
import { projectDirectory } from './model/projects.mjs'
import { API_BASE } from '../apiClient.mjs'
import { attentionInView, attentionItems, attentionSummary, stepAttention } from './model/attention.mjs'
import { attentionOf, attentionText } from './model/connections.mjs'
import { lastWork, requestReply } from './workContext'
import { pageShortcut } from './keys.mjs'
import { documentTitle, routeKey } from './router.mjs'
import { agentActions } from './api/agentClient.mjs'

// The Lumen application shell (the code is still named Atlas). It is the default interface, and it
// owns the single event connection while it is on screen (the classic interface is unmounted).
const EMPTY = emptyStore()

export default function AtlasApp() {
  const { route, navigate, markPush, navigationRef } = useAtlasRoute()
  const appearance = useAppearance()
  const live = useLiveStatus()
  // Read once for the whole shell: the Connections page shows it, the navigation marks attention.
  const connections = useConnections(live, route.name === 'connections')
  const notifications = useNotificationPreference(live.reachable, agentActions.notificationPreference)
  // The canonical workspace Task list, read once for the shell: Work shows it, and the attention
  // list, the "need you" button and browser notifications are derived from the same store.
  const feed = useTaskFeed(live.version)
  const store = feed.store || EMPTY
  const derived = useMemo(() => deriveWorkspace(store), [store])
  const attention = useMemo(() => attentionItems(store, derived), [store, derived])
  // `known`: the Task list has been read at least once; `stale`: what is shown is the last answer.
  const summary = { ...attentionSummary(attention, live.overview, feed.store ? store : null), known: Boolean(feed.readAt),
    stale: Boolean(feed.stale) || live.reachable === false }
  const notices = useProviderNotices(live, route.name === 'connections')
  // The registered projects, in the person's order, for the rail (re-read with every live update).
  const projectsResource = useLiveResource(`${API_BASE}/projects`, live.version)
  const directory = useMemo(() => projectDirectory(projectsResource), [projectsResource])
  const projectFacts = useCallback((id) => projectStats(store, derived, id), [store, derived])
  // The sidebar's smart lists and colour labels count loaded tasks, as the Work page does.
  const sideCounts = useMemo(() => ({ ...taskCounts(store, derived), doneFamilies: familyCounts(derived, null).done }), [store, derived])
  const sideTags = useMemo(() => {
    const out = {}
    for (const id of Object.keys(store.tasks)) { const color = rowColor(derived, id); out[color] = (out[color] || 0) + 1 }
    return out
  }, [store, derived])
  // Opening a Task from outside Work keeps the Work view the person had (project, filters, view).
  const showTask = useCallback((taskId, { reply = false } = {}) => {
    if (reply) requestReply(taskId)
    markPush()
    navigate('work', { ...(route.name === 'work' ? route.params : lastWork()), task: taskId })
  }, [navigate, markPush, route])
  const region = useRef(null)
  const announce = useCallback((text) => { if (region.current) region.current.textContent = text }, [])
  const delivery = useBrowserNotifications({ feed, live, preference: notifications, onOpen: showTask, announce })
  // "N need you": the next Task waiting for a reply, in the List's order, after the selected one.
  // A filter that would hide it is cleared; nothing else about the view changes.
  const nextQuestion = useCallback(() => {
    const params = route.name === 'work' ? route.params || {} : lastWork()
    const questions = attention.filter((entry) => entry.kind === 'reply').map((entry) => entry.taskId)
    if (!questions.length) { markPush(); navigate('work', { ...params, filter: 'needs', task: undefined }); return }
    const target = stepAttention(questions, params.task, 1)
    const visible = attentionInView(store, derived, { filter: params.filter, project: params.project, colors: params.colors ? new Set(params.colors.split(',')) : null,
      focus: params.focus && store.tasks[params.focus] ? params.focus : null })
    const keep = visible.includes(target) ? params : { lens: params.lens, side: params.side }
    requestReply(target)
    markPush()
    navigate('work', { ...keep, task: target })
  }, [attention, route, store, derived, navigate, markPush])
  const toolAttention = attentionOf(connections.data)
  const tools = useMemo(() => !toolAttention ? [] : [
    ...toolAttention.providers.map((item) => ({ provider: item.provider, label: item.label, text: `${item.label}: ${item.condition.label}${item.condition.detail ? `. ${item.condition.detail}` : ''}` })),
    ...(toolAttention.defaultProblem ? [{ provider: null, label: 'Connections', text: 'The default tool for new tasks can’t be used.' }] : []),
  ], [toolAttention])
  const main = useRef(null)
  const scrolls = useRef(new Map())
  const currentKey = useRef(null)
  const key = routeKey(route)

  useEffect(() => {
    const previous = document.title
    return () => { document.title = previous }
  }, [])

  // A new navigation starts at the top; Back/Forward restores where the page was. Focus moves to
  // the new page's heading, except on the first load. A change that stays on the same page (a
  // selected task, a filter) keeps focus and scroll where they are.
  useLayoutEffect(() => {
    const samePage = currentKey.current === key
    currentKey.current = key
    document.title = documentTitle(route)
    const element = main.current
    if (!element) return
    if (samePage) {
      navigationRef.current = 'traversal'
      return
    }
    const kind = navigationRef.current
    element.scrollTop = kind === 'traversal' ? (scrolls.current.get(key) || 0) : 0
    if (kind !== 'initial') element.querySelector('[data-page-heading]')?.focus({ preventScroll: true })
    navigationRef.current = 'traversal'
  }, [key, route, navigationRef])

  // Go to: Ctrl K (or Cmd K) from anywhere, or / when no field has the cursor.
  const [goTo, setGoTo] = useState(false)
  const openGoTo = useCallback(() => setGoTo(true), [])
  useEffect(() => {
    const onKey = (event) => {
      const combo = (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k'
      if (combo) { event.preventDefault(); setGoTo(true); return }
      if (event.key === '/' && pageShortcut(event)) { event.preventDefault(); setGoTo(true) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const goToTask = useCallback((taskId) => showTask(taskId), [showTask])
  const goToRoute = useCallback((name, params) => { markPush(); navigate(name, params) }, [markPush, navigate])

  // N opens New task from any page (not while typing, nor with a menu, dialog or panel open).
  useEffect(() => {
    const onKey = (event) => {
      if (event.key.toLowerCase() !== 'n' || event.shiftKey || route.name === 'new' || !pageShortcut(event)) return
      event.preventDefault()
      markPush()
      navigate('new')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [route.name, navigate, markPush])

  let page
  if (route.name === 'work') page = <WorkPage live={live} route={route} navigate={navigate} feed={feed} derived={derived} attention={summary}
    projectsResource={projectsResource} onGoTo={openGoTo} />
  else if (route.name === 'new') page = <NewTaskPage live={live} navigate={navigate} route={route} />
  else if (route.name === 'projects') page = <ProjectsPage live={live} navigate={navigate} />
  else if (route.name === 'task') page = <TaskPage key={route.params?.id || 'none'} live={live} navigate={navigate} route={route} />
  else if (route.name === 'connections') page = <ConnectionsPage key={key} live={live} connections={connections} route={route} navigate={navigate} />
  else if (route.name === 'settings') page = <SettingsPage appearance={appearance} live={live} navigate={navigate} route={route}
    notifications={notifications} connections={connections} delivery={delivery} />
  else if (route.name === 'gallery') page = <GalleryPage />
  else page = <NotFoundPage requested={route.requested} navigate={navigate} />

  const toolsText = tools.length ? 'A tool needs attention' : ''
  const toolsName = attentionText(toolAttention)
  return <div className="atlas">
    <div className="a-app a-app--lumen">
      <button type="button" className="a-skip" onClick={() => main.current?.focus()}>Skip to content</button>
      <div className="a-ground" aria-hidden="true" />
      <Rail route={route} markPush={markPush} directory={directory} projectFacts={projectFacts} needs={summary.reported || 0}
        toolsText={toolsText} toolsName={toolsName} currentProject={route.name === 'work' ? route.params?.project || null : null}
        counts={feed.readAt ? sideCounts : null} tags={feed.readAt ? sideTags : null} />
      <TopBar live={live} appearance={appearance} markPush={markPush} onGoTo={openGoTo}
        attention={{ summary, onNext: nextQuestion, panel: { attention, tools, notices, preference: notifications, delivery,
          onReply: (taskId) => showTask(taskId, { reply: true }), onShow: (taskId) => showTask(taskId),
          onShowAll: () => { markPush(); navigate('work', { ...(route.name === 'work' ? route.params : lastWork()), filter: 'needs' }) },
          onTool: (provider) => { markPush(); navigate('connections', provider ? { provider } : {}) },
          onReview: (provider) => { markPush(); navigate('connections', { provider, section: 'setup' }) } } }} />
      <main ref={main} className="a-main" tabIndex={-1}
        onScroll={() => { if (currentKey.current) scrolls.current.set(currentKey.current, main.current.scrollTop) }}>
        {page}
      </main>
      <GoTo open={goTo} onClose={() => setGoTo(false)} store={store} derived={derived} projects={directory.projects}
        recent={goTo ? readRecent() : []} onTask={goToTask} onRoute={goToRoute} />
      {/* New questions are announced politely; focus never moves. */}
      <div ref={region} className="a-sr" role="status" aria-live="polite" />
    </div>
  </div>
}
