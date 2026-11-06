import { useCallback, useEffect, useMemo, useState } from 'react'
import Icon from '../components/Icon'
import { Avatar, StatusBead } from '../components/identity'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import { ApplyDialog, TaskControlDialog } from '../components/task/TaskControls'
import { AdvancedCard, AgentCard, PlaceCard, ProgressCard, RequestCard, ResultCard, TeamCard } from '../components/task/TaskSections'
import { API_BASE } from '../../apiClient.mjs'
import { useLiveResource, useTaskFeed } from '../hooks'
import { emptyStore, pathTo } from '../model/tasks.mjs'
import { deriveWorkspace, inBranch, nowView, whoView } from '../model/workList.mjs'
import { projectDirectory, projectLabel } from '../model/projects.mjs'
import { assignmentsView, controlState, pauseHeld, progressView, resultView, when } from '../model/taskPage.mjs'
import { lastWork } from '../workContext'
import TaskTitle from '../components/task/TaskTitle'
import { CoordinationCard } from '../components/task/Coordination'

const EMPTY = emptyStore()
const encode = encodeURIComponent
const clip = (text, limit) => (text && text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text)

// A Task as a complete, durable record: what was asked, who is responsible and who was before,
// how the work is going, its team of helper tasks (List or Map), its result in separate
// statements, and the lifecycle controls the backend supports. The page reads only this Task's
// branch (and the ancestors that place it) plus its own detail endpoints, all re-read when the
// shared event connection reports a change.
export default function TaskPage({ live, route, navigate }) {
  const taskId = typeof route.params?.id === 'string' && route.params.id.length <= 200 ? route.params.id : null
  const [local, setLocal] = useState(0)
  const [dialog, setDialog] = useState(null)
  const [outcome, setOutcome] = useState(null)
  const [tick, setTick] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setTick(Date.now()), 30000)
    return () => clearInterval(timer)
  }, [])
  const version = `${live.version}-${local}`
  const feed = useTaskFeed(live.version, taskId)
  const store = feed.store || EMPTY
  const derived = useMemo(() => deriveWorkspace(store), [store])
  const projectsResource = useLiveResource(`${API_BASE}/projects`, live.version)
  const directory = useMemo(() => projectDirectory(projectsResource), [projectsResource])
  const projectName = useCallback((id) => projectLabel(directory, id), [directory])
  const base = taskId ? `${API_BASE}/tasks/${encode(taskId)}` : null
  const detail = useLiveResource(base, version)
  const assignments = useLiveResource(base && `${base}/assignments`, version)
  const orchestration = useLiveResource(base && `${base}/orchestration`, version)
  const integrations = useLiveResource(base && `${base}/integrations`, version)
  const operations = useLiveResource(base && `${base}/control-operations`, version)
  const dependencies = useLiveResource(base && `${base}/dependencies`, version)
  const dependents = useLiveResource(base && `${base}/dependents`, version)
  const claims = useLiveResource(base && `${base}/resource-claims`, version)
  const intents = useLiveResource(base && `${base}/work-intents`, version)
  const overlaps = useLiveResource(base && `${base}/work-overlaps`, version)
  const deadlocks = useLiveResource(base && `${base}/deadlocks`, version)
  const task = taskId ? store.tasks[taskId] : null
  const parentId = task?.parentId ?? detail.data?.parent_task_id ?? null
  const parentDelegations = useLiveResource(parentId ? `${API_BASE}/tasks/${encode(parentId)}/delegations` : null, version)
  const who = task ? whoView(derived, taskId) : null
  const agentId = who?.agent && !who.agent.unknown ? who.agent.id : null
  const activity = useLiveResource(agentId ? `${API_BASE}/agents/${encode(agentId)}/activity` : null, version)

  const refresh = () => { setLocal((value) => value + 1); feed.refresh() }
  const openTask = useCallback((id) => navigate('task', { id }), [navigate])
  // Back to Work keeps the Work view the person had, with this task selected (a focus that does
  // not contain it is left, so the selection is never hidden).
  const toWork = useCallback((id = taskId, extra = {}) => {
    const context = lastWork()
    const keepFocus = context.focus && store.tasks[context.focus] && inBranch(derived.forest, context.focus, id)
    navigate('work', { ...context, ...(keepFocus ? {} : { focus: undefined }), task: id, ...extra })
  }, [navigate, taskId, store, derived])

  const ready = feed.phase === 'ready' || (feed.store && feed.readAt)
  const notFound = detail.error === 404 || (ready && store.complete && !task && !detail.data && !detail.loading)
  if (!taskId) {
    return <div className="a-page"><EmptyState icon="info" title="No task was chosen" actions={<Button onClick={() => navigate('work')}>Open Work</Button>}>
      Open a task from Work.</EmptyState></div>
  }
  if (notFound) {
    return <div className="a-page"><h1 className="a-title" tabIndex={-1} data-page-heading>Task not found</h1>
      <EmptyState icon="info" title="This task no longer exists" actions={<Button onClick={() => navigate('work')}>Open Work</Button>}>
        It may have been removed from the Control Center. Nothing else was opened in its place.</EmptyState></div>
  }
  if (!task || !ready) {
    return <div className="a-page"><h1 className="a-title" tabIndex={-1} data-page-heading>{detail.data?.title || 'Loading the task…'}</h1>
      {feed.phase === 'error' && !feed.store ? <EmptyState icon="plug" title="The task couldn’t be loaded">The Control Center backend isn’t answering. This page retries by itself.</EmptyState>
        : <Skeleton lines={6} label="Loading the task" />}</div>
  }

  const status = derived.status[taskId]
  const now = Math.max(tick, feed.readAt || 0)
  const ancestors = pathTo(derived.forest, taskId).reverse()
  const progress = progressView({ task, status, detail: detail.data, orchestration: orchestration.data })
  const result = resultView({ task, detail: detail.data, integrations: integrations.data?.integrations || [], parentDelegations: parentDelegations.data?.delegations || [], taskId })
  const history = assignmentsView(assignments.data?.assignments || [], store)
  const controls = controlState({ task, detail: detail.data, operations: operations.error ? null : operations.data ? operations.data.operations : null })
  const held = pauseHeld(operations.data?.operations)
  const line = nowView(store, derived, taskId)
  const stale = feed.stale || live.reachable === false || detail.error === 'unreachable'
  // Read-only information may be shown from the last answer; changing anything needs a backend
  // that answers now and a fresh read of the task and its control operations.
  const unavailable = live.reachable === false ? 'The backend isn’t reachable, so nothing can be changed right now. These actions return when it answers.'
    : detail.error || operations.error || feed.stale ? 'The task’s latest state couldn’t be read, so these actions wait until it can.' : null
  const action = (key, label, icon, variant) => {
    const state = controls[key]
    if (!state.available) return null
    return <Button key={key} size="sm" variant={variant} icon={icon} disabled={Boolean(unavailable)} title={unavailable || undefined}
      onClick={() => { setOutcome(null); setDialog(key) }}>{state.retry ? 'Finish canceling…' : label}</Button>
  }
  const actions = [action('pause', 'Pause…', 'pause'), action('resume', 'Resume…', 'play'), action('cancel', 'Cancel task…', 'stop', 'danger')].filter(Boolean)
  const reasons = [...new Set(['pause', 'resume', 'cancel'].filter((key) => !controls[key].available).map((key) => controls[key].reason).filter(Boolean))]

  return <div className="a-page a-page--wide a-taskpage">
    <nav className="a-crumbs a-tcrumbs" aria-label="Where this task sits">
      <button type="button" className="a-link" onClick={() => toWork()}><Icon name="left" size={13} stroke={2.4} />Work</button>
      {ancestors.map((id) => <span key={id} className="a-crumb"><span aria-hidden="true">›</span>
        <button type="button" className="a-link a-bidi" dir="auto" onClick={() => openTask(id)}>{clip(store.tasks[id].title || 'Untitled task', 34)}</button></span>)}
      {task.parentId && !store.tasks[task.parentId] && <span className="a-crumb"><span aria-hidden="true">›</span><span className="a-fine">lead not loaded</span></span>}
      <span className="a-crumb"><span aria-hidden="true">›</span><span>{task.parentId ? 'Helper task' : 'Task'}</span></span>
    </nav>
    <header className="a-thead">
      <div className="a-thead-main">
        <TaskTitle taskId={taskId} title={task.title} as="h1" className="a-title" headingProps={{ tabIndex: -1, 'data-page-heading': true }}
          onRenamed={refresh} canceled={status?.key === 'canceled'} />
        <div className="a-tmeta">
          <span><Icon name="folder" size={14} /><span className="a-bidi" dir="auto">{projectName(task.projectId)}</span></span>
          <span>{task.parentId ? 'Helper task' : 'Top-level task'}</span>
          {task.createdAt && <span>Created {when(task.createdAt)}</span>}
          <span className="a-tmeta-who">{who.agent ? <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={20} removed={who.removed} decorative />
            : <span className="a-avatar-empty" style={{ width: 20, height: 20 }} aria-hidden="true" />}
          <span className="a-bidi" dir="auto">{who.agent ? `${who.role === 'current' ? '' : 'Last agent: '}${who.name}` : 'No agent'}</span></span>
        </div>
      </div>
      <div className="a-thead-acts">
        <Button size="sm" icon="map" onClick={() => toWork(taskId, { lens: 'map' })}>Show in map</Button>
        {actions}
      </div>
    </header>
    <div className={`a-tbanner a-tone-${status.tone}`} role="status">
      <StatusBead state={status.bead} decorative />
      <span><b>{line.line}</b>{line.second ? ` · ${line.second}` : ''}</span>
      {status.attention && <span className="a-sp" />}
      {status.attention && <Button size="sm" variant="primary" onClick={() => toWork()}>Open in Work</Button>}
    </div>
    {reasons.length > 0 && actions.length === 0 && <p className="a-fine">No lifecycle actions: {reasons[0]}</p>}
    {unavailable && actions.length > 0 && <p className="a-fine a-tone-alert" role="status">{unavailable}</p>}
    {outcome && <div className={`a-result a-result--${outcome.tone === 'ok' ? 'ok' : outcome.tone === 'warn' ? 'warn' : 'error'}`} role="status">
      <Icon name={outcome.tone === 'ok' ? 'check' : 'info'} size={15} /><div>{outcome.text}</div></div>}
    {stale && <Notice tone="warn" role="status" title="Showing the last information received">The backend isn’t answering right now; this page updates by itself when it returns.</Notice>}
    {who.agent && who.role === 'current' && history.length > 1 && <p className="a-fine">{history.length - 1} earlier agent{history.length === 2 ? '' : 's'} worked on this task; it is the same task.</p>}
    {/* Quick ways to the parts that matter most, whatever the length of the request. */}
    <nav className="a-tjump" aria-label="On this page">
      {[['t-request', 'Request'], ['t-progress', 'How it’s going'], ['t-result', 'Result'], ['t-agent', 'Responsible agent'], ['t-team', 'Who’s on it']].map(([id, label]) =>
        <button key={id} type="button" className="a-link" onClick={() => {
          const heading = document.getElementById(id)
          heading?.closest('section')?.scrollIntoView({ block: 'start', behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
          if (heading) { heading.tabIndex = -1; heading.focus({ preventScroll: true }) }
        }}>{label}</button>)}
    </nav>
    <div className="a-tgrid">
      <div className="a-tmain">
        <RequestCard task={task} detail={detail} />
        <ProgressCard progress={progress} activity={activity} taskId={taskId} store={store} onOpenTask={openTask} />
        <ResultCard result={result} onApply={task && !task.parentId ? () => setDialog('apply') : undefined} />
      </div>
      <div className="a-tside">
        <AgentCard derived={derived} taskId={taskId} history={history} assignments={assignments} onOpenWork={() => toWork()} />
        <PlaceCard store={store} taskId={taskId} projectName={projectName} dependencies={dependencies} dependents={dependents} onOpenTask={openTask} />
        <CoordinationCard taskId={taskId} claims={claims} intents={intents} overlaps={overlaps} deadlocks={deadlocks} onChanged={refresh} />
        <AdvancedCard taskId={taskId} detail={detail} orchestration={orchestration} operations={operations} task={task} />
      </div>
    </div>
    <TeamCard store={store} derived={derived} taskId={taskId} projectName={projectName} now={now} complete={store.complete}
      onOpenTask={openTask} onShowInWork={(id) => toWork(id)} onLoadMore={feed.loadMore} loadingMore={feed.loadingMore} />
    {dialog === 'apply' && <ApplyDialog taskId={taskId} blocked={unavailable} onClose={() => setDialog(null)}
      onFinished={(result) => { setDialog(null); setOutcome(result); refresh() }} />}
    {dialog && dialog !== 'apply' && <TaskControlDialog action={dialog} taskId={taskId} store={store} held={held} blocked={unavailable} onClose={() => setDialog(null)}
      onFinished={(result) => { setDialog(null); setOutcome(result); refresh() }} />}
  </div>
}
