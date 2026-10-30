import { useCallback, useEffect, useLayoutEffect, useMemo, useRef } from 'react'
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
import TopBar from './shell/TopBar'
import WorkPage from './pages/WorkPage'
import ConnectionsPage from './pages/ConnectionsPage'
import NewTaskPage from './pages/NewTaskPage'
import ProjectsPage from './pages/ProjectsPage'
import TaskPage from './pages/TaskPage'
import SettingsPage from './pages/SettingsPage'
import GalleryPage from './pages/GalleryPage'
import NotFoundPage from './pages/NotFoundPage'
import { useAppearance, useAtlasRoute, useConnections, useLiveStatus, useNotificationPreference, useProviderNotices, useTaskFeed } from './hooks'
import { useBrowserNotifications } from './notifications'
import { emptyStore } from './model/tasks.mjs'
import { deriveWorkspace } from './model/workList.mjs'
import { attentionInView, attentionItems, attentionSummary, stepAttention } from './model/attention.mjs'
import { attentionOf } from './model/connections.mjs'
import { lastWork, requestReply } from './workContext'
import { pageShortcut } from './keys.mjs'
import { documentTitle, routeKey } from './router.mjs'
import { agentActions } from './api/agentClient.mjs'
import { exitAtlas } from './uiMode.mjs'

// The Atlas preview shell (milestone M1). It is loaded only after the person opts in, and it
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
  if (route.name === 'work') page = <WorkPage live={live} route={route} navigate={navigate} feed={feed} derived={derived} attention={summary} />
  else if (route.name === 'new') page = <NewTaskPage live={live} navigate={navigate} route={route} />
  else if (route.name === 'projects') page = <ProjectsPage live={live} navigate={navigate} />
  else if (route.name === 'task') page = <TaskPage key={route.params?.id || 'none'} live={live} navigate={navigate} route={route} />
  else if (route.name === 'connections') page = <ConnectionsPage key={key} live={live} connections={connections} route={route} navigate={navigate} />
  else if (route.name === 'settings') page = <SettingsPage appearance={appearance} live={live} navigate={navigate} route={route}
    notifications={notifications} connections={connections} delivery={delivery} />
  else if (route.name === 'gallery') page = <GalleryPage />
  else page = <NotFoundPage requested={route.requested} navigate={navigate} />

  return <div className="atlas">
    <div className="a-app">
      <button type="button" className="a-skip" onClick={() => main.current?.focus()}>Skip to content</button>
      <TopBar route={route} live={live} appearance={appearance} markPush={markPush} onClassic={() => exitAtlas()} connections={connections}
        attention={{ summary, onNext: nextQuestion, panel: { attention, tools, notices, preference: notifications, delivery,
          onReply: (taskId) => showTask(taskId, { reply: true }), onShow: (taskId) => showTask(taskId),
          onShowAll: () => { markPush(); navigate('work', { ...(route.name === 'work' ? route.params : lastWork()), filter: 'needs' }) },
          onTool: (provider) => { markPush(); navigate('connections', provider ? { provider } : {}) },
          onReview: (provider) => { markPush(); navigate('connections', { provider, section: 'setup' }) } } }} />
      <main ref={main} className="a-main" tabIndex={-1}
        onScroll={() => { if (currentKey.current) scrolls.current.set(currentKey.current, main.current.scrollTop) }}>
        {page}
      </main>
      {/* New questions are announced politely; focus never moves. */}
      <div ref={region} className="a-sr" role="status" aria-live="polite" />
    </div>
  </div>
}
