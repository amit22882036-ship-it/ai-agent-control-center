import { useMemo, useState } from 'react'
import Icon from '../components/Icon'
import { Button, EmptyState, Notice, Skeleton } from '../components/primitives'
import PageHeader from '../shell/PageHeader'
import { RegisterProjectDialog } from '../components/create/projects'
import { API_BASE } from '../../apiClient.mjs'
import { useLiveResource, useTaskFeed } from '../hooks'
import { projectDirectory } from '../model/projects.mjs'
import { deriveWorkspace } from '../model/workList.mjs'
import { emptyStore } from '../model/tasks.mjs'

// Projects: every registered project (GET /projects). Counts come from the loaded tasks only and say so.
export default function ProjectsPage({ live, navigate }) {
  const [version, setVersion] = useState(0)
  const [query, setQuery] = useState('')
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(null)
  const resource = useLiveResource(`${API_BASE}/projects`, `${live.version}-${version}`)
  const directory = useMemo(() => projectDirectory(resource), [resource])
  const feed = useTaskFeed(live.version)
  const derived = useMemo(() => deriveWorkspace(feed.store || emptyStore()), [feed.store])
  const complete = Boolean(feed.store?.complete)
  const stats = (id) => {
    const families = derived.families.filter((family) => family.projectId === id)
    return { active: families.filter((f) => f.section !== 'done').length, needs: families.filter((f) => f.section === 'needs').length }
  }
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
            const s = stats(project.id)
            return <article key={project.id} className="a-card a-pcard">
              <h3><Icon name="folder" size={18} /><span className="a-bidi" dir="auto">{project.name}</span></h3>
              <span className="a-mono a-fine a-pcard-path">{project.path}</span>
              <div className="a-pstats" title={complete ? undefined : 'Counted in the loaded tasks; older tasks are not counted'}>
                <span><b className="a-num">{s.active}</b>active tasks</span><span><b className={`a-num${s.needs ? ' a-tone-ask' : ''}`}>{s.needs}</b>need you</span>
              </div>
              {!complete && <p className="a-fine">In the loaded tasks.</p>}
              <div className="a-act-row">
                <Button size="sm" onClick={() => navigate('work', { project: project.id })}>Show its work</Button>
                <Button size="sm" variant="ghost" icon="plus" onClick={() => navigate('new', { project: project.id })}>New task here</Button>
              </div>
            </article>
          })}
          {q && !shown.length && <p className="a-fine">No project matches.</p>}
          <article className="a-card a-pcard a-pcard--add">
            <h3><Icon name="plus" size={18} />Add a project</h3>
            <span className="a-fine">Any local Git repository with at least one commit. Agents never change it directly; they work in private copies.</span>
            <Button size="sm" onClick={() => setAdding(true)}>Add project</Button>
          </article>
        </div>}
    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={() => setVersion((value) => value + 1)}
      onRegistered={(project) => { setAdding(false); setAdded(project?.name || 'The project'); setVersion((value) => value + 1) }} />}
  </div>
}
