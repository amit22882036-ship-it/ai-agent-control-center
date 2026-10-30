import { useEffect, useId, useRef, useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { TextField } from '../controls'
import { Dialog } from '../overlays'
import { agentActions, refusalText, send } from '../../api/agentClient.mjs'

// Registering a project by its full path (POST /projects). The backend validates the folder: an
// existing Git repository with a commit, not inside another project. Atlas does not browse folders
// and does not read dropped folders; the native folder picker is a separate, later milestone.
//
// When the request fails in transit the backend may or may not have registered the folder. The
// typed values are kept, nothing is sent again by itself, and the person can check the projects
// list (a fresh GET /projects) before deciding to add it again.
const samePath = (a, b) => {
  const norm = (value) => String(value || '').trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
  return norm(a) !== '' && norm(a) === norm(b)
}

export function RegisterProjectDialog({ open, onClose, onRegistered, onRefresh = () => {} }) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // null, or what is known after a request whose outcome is unknown:
  // 'unknown' | 'checking' | 'absent' | 'check-failed' | { found: project }
  const [unknown, setUnknown] = useState(null)
  const blocked = unknown === 'unknown' || unknown === 'checking' || unknown === 'check-failed' || Boolean(unknown?.found)

  async function register(event, anyway = false) {
    event?.preventDefault()
    if (busy || !name.trim() || !path.trim() || (blocked && !anyway)) return
    setBusy(true)
    setError('')
    setUnknown(null)
    const result = await agentActions.registerProject(name.trim(), path.trim())
    setBusy(false)
    if (result.offline) { setUnknown('unknown'); return }
    if (!result.ok) {
      setError(refusalText(result, 'Unable to register Project. Check the repository path and retry.'))
      return
    }
    setName('')
    setPath('')
    onRegistered(result.data)
  }

  async function check() {
    setUnknown('checking')
    onRefresh() // the page's own list is read again too
    const result = await send('GET', '/projects')
    const list = result.ok && Array.isArray(result.data?.projects) ? result.data.projects : null
    if (!list) { setUnknown('check-failed'); return }
    const match = list.find((project) => samePath(project.root_path, path))
    setUnknown(match ? { found: match } : 'absent')
  }

  const edit = (setter) => (event) => { setter(event.target.value); if (unknown && unknown !== 'checking') setUnknown(null) }
  let note = null
  if (unknown === 'unknown' || unknown === 'check-failed' || unknown === 'checking') {
    note = <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} /><div>
      {unknown === 'check-failed' ? 'The projects list couldn’t be read either, so it is still unknown whether this folder was added.'
        : 'The backend could not be reached, so it is unknown whether this folder was added. Check the projects list before adding it again.'}
      <div className="a-act-row">
        <Button size="sm" icon="refresh" disabled={unknown === 'checking'} onClick={check}>{unknown === 'checking' ? 'Checking…' : 'Check the projects list'}</Button>
        <Button size="sm" variant="ghost" disabled={unknown === 'checking'} onClick={() => register(null, true)}>Add it again anyway</Button>
      </div></div></div>
  } else if (unknown === 'absent') {
    note = <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} /><div>
      No registered project has this path, so it was not added. You can add it now.</div></div>
  } else if (unknown?.found) {
    note = <div className="a-result a-result--ok" role="status"><Icon name="check" size={15} /><div>
      This folder is registered as <b className="a-bidi" dir="auto">{unknown.found.name || 'a project'}</b>.{' '}
      <button type="button" className="a-link" onClick={() => { setName(''); setPath(''); onRegistered(unknown.found) }}>Use this project</button></div></div>
  }

  return <Dialog open={open} onClose={() => { if (!busy) onClose() }} title="Add a project"
    description="A project is the top folder of a Git repository with at least one commit. Agents work in private copies; your folder changes only when you apply a result.">
    <form className="a-register" onSubmit={register}>
      <TextField label="Project name" value={name} maxLength={100} disabled={busy} onChange={edit(setName)} />
      <TextField label="Folder path" mono value={path} disabled={busy} placeholder="C:\path\to\repository"
        hint="Type or paste the full path. Browsing for a folder is not available yet." onChange={edit(setPath)} />
      {error && <div className="a-result a-result--error" role="alert"><Icon name="warn" size={15} /><div>{error}</div></div>}
      {note}
      <div className="a-act-row">
        <Button type="submit" variant="primary" disabled={busy || blocked || !name.trim() || !path.trim()}>{busy ? 'Checking and adding…' : 'Add project'}</Button>
        <Button disabled={busy} onClick={onClose}>Cancel</Button>
      </div>
    </form>
  </Dialog>
}

// The New Task project field: the chosen project, and a searchable list of registered projects.
export function ProjectPicker({ directory, value, onChange, onRegistered, disabled, onRefresh }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [adding, setAdding] = useState(false)
  const root = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const outside = (event) => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  const projects = directory.projects
  const chosen = projects.find((project) => project.id === value) || null
  const q = query.trim().toLowerCase()
  const shown = projects.filter((project) => !q || project.name.toLowerCase().includes(q) || (project.path || '').toLowerCase().includes(q))
  return <div className="a-pjfield" ref={root} onKeyDown={(event) => { if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false) } }}>
    <span className="a-field-label" id={`${id}-label`}>Project</span>
    <button type="button" id={`${id}-button`} className="a-pj" aria-haspopup="dialog" aria-expanded={open} aria-labelledby={`${id}-label ${id}-button`}
      disabled={disabled} onClick={() => setOpen((current) => !current)}>
      <span className="a-pji a-pji--lg"><Icon name="folder" size={18} /></span>
      {chosen ? <span className="a-pjt"><b className="a-bidi" dir="auto">{chosen.name}</b><small className="a-mono">{chosen.path}</small></span>
        : <span className="a-pjt"><b>Choose a project</b><small>Required</small></span>}
      <span className="a-pjchg">Change<Icon name="down" size={13} stroke={2.2} /></span>
    </button>
    {open && <div className="a-pjpop" role="dialog" aria-label="Choose a project">
      <label className="a-pjsearch"><Icon name="search" size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder="Find a project" aria-label="Find a project" dir="auto" autoFocus /></label>
      <div className="a-pjh">Your projects</div>
      {directory.state === 'loading' && <p className="a-fine a-pjnote">Loading projects…</p>}
      {directory.state === 'unavailable' && <p className="a-fine a-pjnote">Projects couldn’t be loaded. <button type="button" className="a-link" onClick={onRefresh}>Try again</button></p>}
      {directory.state === 'empty' && <p className="a-fine a-pjnote">No projects registered yet.</p>}
      {shown.map((project) => <button key={project.id} type="button" className="a-pjo" aria-pressed={project.id === value}
        onClick={() => { onChange(project.id); setOpen(false); setQuery('') }}>
        <span className="a-pji"><Icon name="folder" size={15} /></span>
        <span className="a-pjt"><b className="a-bidi" dir="auto">{project.name}</b><small className="a-mono">{project.path}</small></span>
        {project.id === value && <Icon name="check" size={15} stroke={2.4} />}
      </button>)}
      {q && !shown.length && directory.state === 'ready' && <p className="a-fine a-pjnote">No project matches.</p>}
      <div className="a-pjsep" />
      <button type="button" className="a-pjo" onClick={() => { setOpen(false); setAdding(true) }}>
        <span className="a-pji"><Icon name="plus" size={15} stroke={2.2} /></span>
        <span className="a-pjt"><b>Add a project…</b><small>Type the full path of a Git repository</small></span>
      </button>
    </div>}
    <p className="a-fine a-pjhint">Agents work in a private copy; your folder changes only when you apply a result.</p>
    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={onRefresh}
      onRegistered={(project) => { setAdding(false); onRegistered(project) }} />}
  </div>
}
