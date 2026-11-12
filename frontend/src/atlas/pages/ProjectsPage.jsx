import { useCallback, useMemo, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import PageHeader from '../shell/PageHeader'
import { RegisterProjectDialog } from '../components/create/projects'
import ProjectPlates from '../components/projects/ProjectPlates'
import { DeleteDialog, RestoreDialog, TrashDialog } from '../components/projects/LifecycleDialogs'
import { API_BASE } from '../../apiClient.mjs'
import { agentActions, refusalText } from '../api/agentClient.mjs'
import { useLiveResource, useTaskFeed } from '../hooks'
import { projectDirectory } from '../model/projects.mjs'
import { lifecycleRefusal, trashSummary } from '../model/projectLifecycle.mjs'
import { deriveWorkspace, projectStats } from '../model/workList.mjs'
import { emptyStore } from '../model/tasks.mjs'
import MapPreview from '../components/work/MapPreview'
import { useProjectPreview } from '../components/work/useProjectPreview'

const EMPTY = emptyStore()

// A project's task hierarchy in miniature (the Work Map's own layout, loaded tasks only); it opens
// the Map. Families light up only for what is really happening: working, waiting for you, done.
function ProjectPicture({ derived, project, ready, complete, onMap }) {
  const preview = useProjectPreview(derived, project.id)
  if (!ready) return <span className="a-plate-pic a-pthumb a-plate-pic--empty" aria-hidden="true"><span className="a-pthumb-shimmer" /></span>
  if (!preview.families) return <span className="a-plate-pic a-pthumb a-plate-pic--empty"><span className="a-fine">{complete ? 'No tasks yet' : 'No tasks among the loaded ones'}</span></span>
  const scope = complete ? '' : ' loaded'
  const note = preview.truncated ? `${preview.shown} of ${preview.loaded}${scope} tasks shown` : null
  return <button type="button" className="a-plate-pic a-pthumb" onClick={() => onMap(project)}
    aria-label={`Open ${project.name} in the map. The picture shows ${preview.shown} of ${preview.loaded}${scope} tasks`
      + ` in ${preview.shownFamilies} of ${preview.families} task famil${preview.families === 1 ? 'y' : 'ies'}.`}>
    <MapPreview preview={preview} derived={derived} />{note && <span className="a-pthumb-note">{note}</span>}</button>
}

// Projects: the person's repositories, in their own order. Arrange them, rename them (the display
// name only), hide them from navigation, or move them to the Trash. The Trash moves the real
// folder; from there a project is restored or deleted permanently.
export default function ProjectsPage({ live, navigate }) {
  const [version, setVersion] = useState(0)
  const [adding, setAdding] = useState(false)
  const [message, setMessage] = useState(null)
  const [dialog, setDialog] = useState(null) // { kind: 'trash'|'restore'|'delete', project, refusal? }
  const [optimistic, setOptimistic] = useState(null) // the order sent, until the list confirms it
  const [showHidden, setShowHidden] = useState(false)
  const [query, setQuery] = useState('')
  const region = useRef(null)
  const refresh = useCallback(() => setVersion((value) => value + 1), [])
  const resource = useLiveResource(`${API_BASE}/projects`, `${live.version}-${version}`)
  const trashResource = useLiveResource(`${API_BASE}/projects/trash`, `${live.version}-${version}`)
  const directory = useMemo(() => projectDirectory(resource), [resource])
  const feed = useTaskFeed(live.version)
  const store = feed.store || EMPTY
  const derived = useMemo(() => deriveWorkspace(store), [store])
  const complete = Boolean(feed.store?.complete)
  const announce = useCallback((text) => { if (region.current) region.current.textContent = text }, [])
  const serverOrder = directory.projects.map((project) => project.id)
  const order = optimistic && optimistic.join() !== serverOrder.join() && optimistic.length === serverOrder.length
    && optimistic.every((id) => serverOrder.includes(id)) ? optimistic : serverOrder
  const byId = Object.fromEntries(directory.projects.map((project) => [project.id, project]))
  const visible = order.map((id) => byId[id]).filter((project) => project && !project.hidden)
  const q = query.trim().toLowerCase()
  const shown = q ? visible.filter((project) => project.name.toLowerCase().includes(q) || project.path.toLowerCase().includes(q)) : visible
  const hidden = directory.projects.filter((project) => project.hidden)
  const trashed = Array.isArray(trashResource.data?.projects) ? trashResource.data.projects : []

  const stats = (id) => projectStats(store, derived, id)
  const openMap = (project) => navigate('work', { project: project.id, lens: 'map' })
  const picture = (id) => <ProjectPicture derived={derived} project={byId[id]} ready={Boolean(feed.store)} complete={complete} onMap={openMap} />

  async function commitOrder(next, movedId) {
    setOptimistic(next)
    const result = await agentActions.reorderProjects(next)
    if (!result.ok) {
      setOptimistic(null)
      setMessage({ tone: 'error', title: 'The new order was not saved', text: refusalText(result, 'The order could not be saved.') })
      announce('The order was not saved.')
    } else {
      setMessage(null)
      if (movedId) announce(`Order saved.`)
    }
    refresh()
  }
  async function rename(project, name) {
    const result = await agentActions.renameProject(project.id, name)
    if (result.ok) { refresh(); announce(`Renamed to ${name}.`); return { ok: true } }
    return { ok: false, message: refusalText(result, 'The name could not be saved.') }
  }
  async function setHidden(project, value) {
    const result = value ? await agentActions.hideProject(project.id) : await agentActions.unhideProject(project.id)
    if (result.ok) {
      setMessage(value ? { tone: 'ok', title: `${project.name} is hidden`,
        text: 'It is no longer in navigation. Its tasks keep running, and anything that needs you still reaches you.',
        undo: () => setHidden(project, false) } : null)
      announce(value ? `${project.name} hidden.` : `${project.name} is shown again.`)
    } else setMessage({ tone: 'error', title: 'Nothing changed', text: refusalText(result, 'The project could not be changed.') })
    refresh()
  }
  async function restore(project) {
    const result = await agentActions.restoreProject(project.project_id, null)
    if (result.ok) {
      setMessage({ tone: 'ok', title: `${project.name} is back`, text: `Its folder is at ${result.data.root_path} again.` })
      announce(`${project.name} restored.`)
      refresh()
      return
    }
    setDialog({ kind: 'restore', project, refusal: lifecycleRefusal(result) || { message: refusalText(result, 'It could not be restored.'), reasons: [] } })
  }

  const ready = directory.state === 'ready' || directory.state === 'empty'
  return <div className="a-page a-projects">
    <PageHeader title="Projects" subtitle="Your repositories, in the order you work in them. Agents work in private copies; nothing here changes your files unless you move a project to the Trash."
      actions={<div className="a-act-row">
        <Button icon="refresh" onClick={refresh}>Refresh</Button>
        <Button variant="primary" icon="plus" onClick={() => setAdding(true)}>Add project</Button></div>} />
    <p id="a-plates-help" className="a-sr">Drag a project by its handle, or focus the handle, press Space and use the arrow keys.</p>
    {message && <Notice tone={message.tone} title={message.title} role="status"
      actions={message.undo ? <Button size="sm" icon="restore" onClick={message.undo}>Undo</Button> : null}>{message.text}</Notice>}
    {directory.stale && <Notice tone="warn" role="status" title="Showing the last list received">The backend isn’t answering right now.</Notice>}
    {!ready && directory.state === 'loading' && <Skeleton lines={4} label="Loading projects" />}
    {directory.state === 'unavailable' && <EmptyState icon="plug" title="Projects couldn’t be loaded"
      actions={<Button onClick={refresh}>Try again</Button>}>The Control Center backend isn’t answering.</EmptyState>}
    {ready && !visible.length && <EmptyState icon="folder" title={hidden.length ? 'Every project is hidden' : 'No projects yet'}
      actions={<Button variant="primary" icon="plus" onClick={() => setAdding(true)}>Add project</Button>}>
      {hidden.length ? 'Show a hidden project below, or add another.' : 'Add a local Git repository to start working in it.'}</EmptyState>}
    {ready && visible.length > 1 && <label className="a-find a-find--page a-projects-find"><Icon name="search" size={14} />
      <input type="search" name="project-find" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a project" aria-label="Find a project" dir="auto" /></label>}
    {ready && q && !shown.length && <p className="a-fine">No project matches.</p>}
    {ready && shown.length > 0 && <ProjectPlates projects={shown} reorderable={!q} order={order} stats={stats} renderPicture={picture}
      onCommit={commitOrder} onRename={rename} announce={announce}
      onOpen={(project) => navigate('work', { project: project.id })}
      onMap={openMap}
      onNewTask={(project) => navigate('new', { project: project.id })}
      onHide={(project) => setHidden(project, true)}
      onTrash={(project) => setDialog({ kind: 'trash', project })} />}
    {ready && !complete && visible.length > 0 && <p className="a-fine a-projects-note">Counts cover the loaded tasks.</p>}

    {hidden.length > 0 && <section className="a-shelf" aria-labelledby="a-shelf-hidden">
      <button type="button" className="a-shelf-head" id="a-shelf-hidden" aria-expanded={showHidden} onClick={() => setShowHidden((value) => !value)}>
        <Icon name="eyeoff" size={17} /><b>Hidden</b><span className="a-shelf-count a-num">{hidden.length}</span>
        <span className="a-shelf-note">Not in navigation. Their tasks keep running, and anything that needs you still reaches you.</span>
        <Icon name={showHidden ? 'down' : 'right'} size={15} />
      </button>
      {showHidden && <ul className="a-shelf-list">{hidden.map((project) => <li key={project.id}>
        <span className="a-shelf-name a-bidi" dir="auto">{project.name}</span><span className="a-mono a-fine a-shelf-path">{project.path}</span>
        <Button size="sm" icon="eye" onClick={() => setHidden(project, false)}>Show</Button>
      </li>)}</ul>}
    </section>}

    <section className="a-shelf a-shelf--trash" aria-labelledby="a-shelf-trash">
      <div className="a-shelf-head a-shelf-head--static" id="a-shelf-trash">
        <Icon name="trash" size={17} /><b>Trash</b><span className="a-shelf-count a-num">{trashed.length}</span>
        <span className="a-shelf-note">{trashed.length ? 'Restore a project with its folder and history, or delete it permanently.' : 'Projects you move to the Trash appear here. Their folders can be restored.'}</span>
      </div>
      {trashed.length > 0 && <ul className="a-shelf-list">{trashed.map((project) => <li key={project.project_id}
        className={project.trash?.state === 'deleting' ? 'is-incomplete' : undefined}>
        <span className="a-shelf-name a-bidi" dir="auto">{project.name}</span>
        <span className="a-fine a-shelf-path">{trashSummary(project)}</span>
        {project.trash?.state === 'trashed' && <Button size="sm" icon="restore" onClick={() => restore(project)}>Restore</Button>}
        <Button size="sm" variant="danger" icon="trash" onClick={() => setDialog({ kind: 'delete', project })}
          disabled={project.trash?.state === 'moving' || project.trash?.state === 'restoring'}>Delete permanently…</Button>
      </li>)}</ul>}
    </section>

    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={refresh}
      onRegistered={(project) => { setAdding(false); setMessage({ tone: 'ok', title: `${project?.name || 'The project'} was added`, text: 'It is ready for a new task.' }); refresh() }} />}
    {dialog?.kind === 'trash' && <TrashDialog project={dialog.project} onClose={() => setDialog(null)}
      onDone={(result) => { setDialog(null); setMessage({ tone: 'ok', title: `${dialog.project.name} is in the Trash`,
        text: result?.trash?.folder_missing ? 'Its folder had already been deleted, so nothing on disk changed. Its history is in the Trash below.'
          : `Its folder was moved to ${result?.trash?.path}. Restore it from the Trash below at any time.` }); announce(`${dialog.project.name} moved to the Trash.`); refresh() }} />}
    {dialog?.kind === 'restore' && <RestoreDialog project={dialog.project} initialRefusal={dialog.refusal} onClose={() => setDialog(null)}
      onDone={(result) => { setDialog(null); setMessage({ tone: 'ok', title: `${dialog.project.name} is back`, text: `Its folder is at ${result.root_path}.` }); refresh() }} />}
    {dialog?.kind === 'delete' && <DeleteDialog project={dialog.project} onClose={() => setDialog(null)}
      onDone={() => { setDialog(null); setMessage({ tone: 'ok', title: `${dialog.project.name} was deleted`,
        text: 'Its folder, its task worktrees and its records are gone from this computer. Remote repositories were not touched.' }); announce(`${dialog.project.name} deleted.`); refresh() }} />}
    <div ref={region} className="a-sr" role="status" aria-live="polite" />
  </div>
}
