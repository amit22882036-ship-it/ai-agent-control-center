import './projectSelection.css'
import { useEffect, useId, useRef, useState } from 'react'
import { loadProjects, projectRequest, selectedProject } from './projectCreation.mjs'

export default function ProjectSelection({ disabled, onChange }) {
  const id = useId(), registration = useRef(null)
  const [projects, setProjects] = useState([])
  const [selected, setSelected] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [revision, setRevision] = useState(0)
  const [adding, setAdding] = useState(false)
  const [saving, setSaving] = useState(false)
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    loadProjects(controller.signal).then(items => {
      if (!controller.signal.aborted) { setProjects(items); setLoading(false) }
    }).catch(err => {
      if (!controller.signal.aborted) { setError(err.message || 'Unable to load Projects. Check the backend and retry.'); setLoading(false) }
    })
    return () => controller.abort()
  }, [revision])
  useEffect(() => () => registration.current?.abort(), [])
  const project = selectedProject(projects, selected)
  function choose(value) { setSelected(value); onChange(selectedProject(projects, value)) }
  function reload() { setLoading(true); setError(''); setSelected(''); onChange(null); setRevision(value => value + 1) }
  async function register() {
    if (!name.trim() || !path.trim() || saving || disabled) return
    const controller = new AbortController()
    registration.current = controller
    setSaving(true); setError(''); choose('')
    try {
      const created = await projectRequest('/projects', { signal: controller.signal, body: { name: name.trim(), root_path: path.trim() } })
      if (controller.signal.aborted) return
      const items = await loadProjects(controller.signal)
      if (controller.signal.aborted) return
      const registered = selectedProject(items, created.project_id)
      if (!registered) throw new Error('Project registered but unavailable in the list. Refresh Projects before starting.')
      setProjects(items); setSelected(registered.project_id); onChange(registered)
      setAdding(false); setName(''); setPath('')
    } catch (err) {
      if (!controller.signal.aborted) setError(err.message || 'Unable to register Project. Check the repository path and retry.')
    } finally { if (!controller.signal.aborted) setSaving(false) }
  }
  return <div className="project-selection">
    <label htmlFor={`${id}-project`}>Project</label>
    <div className="project-selection-row">
      <select id={`${id}-project`} value={selected} onChange={event => choose(event.target.value)} disabled={disabled || loading || saving} required aria-describedby={`${id}-path`}>
        <option value="">{loading ? 'Loading Projects…' : 'Choose a Project'}</option>
        {projects.map(item => <option key={item.project_id} value={item.project_id}>{item.name}</option>)}
      </select>
      <button type="button" disabled={disabled || saving || loading} onClick={reload}>Refresh</button>
      <button type="button" aria-expanded={adding} disabled={disabled || saving || loading} onClick={() => setAdding(value => !value)}>Add existing project</button>
    </div>
    <p id={`${id}-path`} className="agent-type-note project-path">{project ? `Repository: ${project.root_path}` : loading ? 'Loading registered repositories.' : projects.length ? 'Select the repository for this agent. Work runs in an isolated Task workspace.' : 'No Projects available. Add an existing Git repository to start.'}</p>
    {adding && <fieldset className="project-registration" disabled={disabled || saving}>
      <legend>Add existing project</legend>
      <label htmlFor={`${id}-name`}>Project name</label><input id={`${id}-name`} value={name} maxLength={100} onChange={event => setName(event.target.value)} />
      <label htmlFor={`${id}-root`}>Repository path</label><input id={`${id}-root`} value={path} onChange={event => setPath(event.target.value)} placeholder="Absolute path to an existing Git repository" />
      <p className="agent-type-note">Use a Git repository with an initial commit. Already registered? Refresh and select it above.</p>
      <button type="button" disabled={!name.trim() || !path.trim() || saving} onClick={register}>{saving ? 'Adding…' : 'Register project'}</button>
    </fieldset>}
    {error && <p role="alert" className="message error">{error}</p>}
  </div>
}
