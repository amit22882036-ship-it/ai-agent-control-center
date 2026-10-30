import { useMemo, useState } from 'react'
import Icon from '../components/Icon'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import PageHeader from '../shell/PageHeader'
import { RegisterProjectDialog } from '../components/create/projects'
import { API_BASE } from '../../apiClient.mjs'
import { useLiveResource, useTaskFeed } from '../hooks'
import { projectDirectory } from '../model/projects.mjs'
import { deriveWorkspace, projectStats } from '../model/workList.mjs'
import { emptyStore } from '../model/tasks.mjs'
import MapPreview from '../components/work/MapPreview'
import { useProjectPreview } from '../components/work/useProjectPreview'

const EMPTY = emptyStore()

// Projects: every registered project (GET /projects). Counts come from the loaded tasks only and say so.
export default function ProjectsPage({ live, navigate }) {
  const [version, setVersion] = useState(0)
  const [query, setQuery] = useState('')
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(null)
  const resource = useLiveResource(`${API_BASE}/projects`, `${live.version}-${version}`)
  const directory = useMemo(() => projectDirectory(resource), [resource])
  const feed = useTaskFeed(live.version)
  const derived = useMemo(() => deriveWorkspace(feed.store || EMPTY), [feed.store])
  const complete = Boolean(feed.store?.complete)
  const store = feed.store || EMPTY
  const q = query.trim().toLowerCase()
  const shown = directory.projects.filter((project) => !q || project.name.toLowerCase().includes(q) || project.path.toLowerCase().includes(q))
  return <div className="a-page">
    <PageHeader title="Projects" subtitle="Repositories your agents work in. Choosing one narrows Work to it."
      actions={<div className="a-act-row">
        <Button icon="refresh" onClick={() => setVersion((value) => value + 1)}>Refresh</Button>
        <Button variant="primary" icon="plus" onClick={() => setAdding(true)}>Add project</Button></div>} />
    {added && <Notice tone="ok" role="status" title={`${added} was added`}>It is ready to choose for a new task.</Notice>}
    {directory.stale && <Notice tone="warn" role="status" title="Showing the last list received">The backend isn’t answering right now.</Notice>}
    <label className="a-find a-find--page"><Icon name="search" size={14} />
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find a project" aria-label="Find a project" dir="auto" /></label>
    {directory.state === 'loading' ? <Skeleton lines={3} label="Loading projects" />
      : directory.state === 'unavailable' ? <EmptyState icon="plug" title="Projects couldn’t be loaded"
        actions={<Button onClick={() => setVersion((value) => value + 1)}>Try again</Button>}>The Control Center backend isn’t answering.</EmptyState>
        : <div className="a-pgrid">
          {shown.map((project) => {
            return <ProjectCard key={project.id} project={project} stats={projectStats(store, derived, project.id)} feed={feed} derived={derived}
              complete={complete} navigate={navigate} />
          })}
          {q && !shown.length && <p className="a-fine">No project matches.</p>}
          <article className="a-card a-pcard a-pcard--add">
            <h2><Icon name="plus" size={18} />Add a project</h2>
            <span className="a-fine">Any local Git repository with at least one commit. Agents never change it directly; they work in private copies.</span>
            <Button size="sm" onClick={() => setAdding(true)}>Add project</Button>
          </article>
        </div>}
    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={() => setVersion((value) => value + 1)}
      onRegistered={(project) => { setAdding(false); setAdded(project?.name || 'The project'); setVersion((value) => value + 1) }} />}
  </div>
}

// One project: its hierarchy in miniature (from the loaded tasks, bounded), counts of its Tasks,
// and ways into its work.
function ProjectCard({ project, stats, feed, derived, complete, navigate }) {
  const preview = useProjectPreview(derived, project.id)
  const openMap = () => navigate('work', { project: project.id, lens: 'map' })
  let picture
  if (!feed.store && feed.phase !== 'error') picture = <div className="a-pthumb a-pthumb--empty" aria-hidden="true"><span className="a-pthumb-shimmer" /></div>
  else if (!feed.store) picture = <div className="a-pthumb a-pthumb--empty"><span className="a-fine">Task picture unavailable</span></div>
  else if (!preview.families) picture = <div className="a-pthumb a-pthumb--empty"><span className="a-fine">{complete ? 'No tasks yet' : 'No tasks among the loaded ones'}</span></div>
  else {
    const scope = complete ? '' : ' loaded'
    const note = preview.truncated ? `${preview.shown} of ${preview.loaded}${scope} tasks shown` : complete ? null : 'Loaded tasks only'
    picture = <button type="button" className="a-pthumb" onClick={openMap}
      aria-label={`Open ${project.name} in the map. The picture shows ${preview.shown} of ${preview.loaded}${scope} tasks`
        + ` in ${preview.shownFamilies} of ${preview.families} task famil${preview.families === 1 ? 'y' : 'ies'}.`}>
      <MapPreview preview={preview} derived={derived} />
      {note && <span className="a-pthumb-note">{note}</span>}
    </button>
  }
  const quiet = [stats.waiting ? `${stats.waiting} waiting` : null, stats.blocked ? `${stats.blocked} blocked` : null,
    `${stats.tasks} task${stats.tasks === 1 ? '' : 's'} in ${stats.families} famil${stats.families === 1 ? 'y' : 'ies'}`].filter(Boolean).join(' · ')
  return <article className="a-card a-pcard">
    {picture}
    <h2><Icon name="folder" size={18} /><span className="a-bidi" dir="auto">{project.name}</span></h2>
    <span className="a-mono a-fine a-pcard-path">{project.path}</span>
    <div className="a-pstats" title={complete ? undefined : 'Counted in the loaded tasks; older tasks are not counted'}>
      <span title="Tasks that are not finished"><b className="a-num">{stats.open}</b>open tasks</span>
      <span title="Agents running right now on a task that is working"><b className="a-num">{stats.working}</b>agents working</span>
      <span title="Tasks waiting for you"><b className={`a-num${stats.needsYou ? ' a-tone-ask' : ''}`}>{stats.needsYou}</b>need you</span>
    </div>
    <p className="a-fine">{quiet}{complete ? '' : ', in the loaded tasks'}.</p>
    <div className="a-act-row">
      <Button size="sm" icon="list" onClick={() => navigate('work', { project: project.id })}>Show its work</Button>
      <Button size="sm" icon="map" onClick={openMap}>Open map</Button>
      <Button size="sm" variant="ghost" icon="plus" onClick={() => navigate('new', { project: project.id })}>New task here</Button>
    </div>
  </article>
}
