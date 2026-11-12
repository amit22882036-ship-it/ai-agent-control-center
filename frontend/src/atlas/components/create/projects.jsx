import { useEffect, useId, useRef, useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { TextField } from '../controls'
import { Dialog } from '../overlays'
import { agentActions, refusalText, send } from '../../api/agentClient.mjs'
import { folderRefusal, folderRemedy, setupWarnings, setupWrites } from '../../model/projectFolder.mjs'

// Registering a project by its full path (POST /projects). The backend validates the folder: an
// existing Git repository with a commit, not inside another project. Atlas does not browse folders
// and does not read dropped folders; the native folder picker is a separate, later milestone.
//
// When the request fails in transit the backend may or may not have registered the folder. The
// typed values are kept, nothing is sent again by itself, and the person can check the projects
// list (a fresh GET /projects) before deciding to add it again.
//
// When the folder isn't usable yet, the backend says why (folderRefusal). For a folder without Git
// or without commits, the person can review exactly what would be written (a read-only folder
// check) and confirm; only then is Git set up, and the project is added again. Nothing is written
// to the folder without that confirmation, and Git settings are never changed here.
const samePath = (a, b) => {
  const norm = (value) => String(value || '').trim().replace(/\//g, '\\').replace(/\\+$/, '').toLowerCase()
  return norm(a) !== '' && norm(a) === norm(b)
}

function CopyCommand({ command }) {
  const [copied, setCopied] = useState(false)
  const canCopy = typeof navigator !== 'undefined' && Boolean(navigator.clipboard)
  const copy = async () => {
    try { await navigator.clipboard.writeText(command); setCopied(true) } catch { setCopied(false) }
  }
  return <div className="a-folder-cmd">
    <code className="a-mono a-cx-code">{command}</code>
    {canCopy && <Button size="sm" icon={copied ? 'check' : undefined} onClick={copy}>{copied ? 'Copied' : 'Copy'}</Button>}
  </div>
}

// The confirmation step: what Git setup will write into the folder, from the backend's own check.
function FolderSetup({ check, busy, error, writeGitignore, onWriteGitignore, onConfirm, onBack }) {
  const plan = check.setup
  const writes = setupWrites(plan, writeGitignore)
  const warnings = setupWarnings(plan, writeGitignore)
  const suggestion = plan.suggested_gitignore
  const isCommit = plan.action === 'commit'
  return <div className="a-folder-setup" role="group" aria-labelledby="folder-setup-title">
    <h3 id="folder-setup-title">{isCommit ? 'Create the first commit' : 'Initialize Git and create the first commit'}</h3>
    <p className="a-fine">This writes into <b className="a-mono a-folder-path">{check.root_path}</b>. Nothing is uploaded or pushed, your Git settings aren’t changed, and nothing else is added.</p>
    <ul className="a-folder-writes">{writes.map((line) => <li key={line}>{line}</li>)}</ul>
    {warnings.map((text) => <div key={text} className="a-result a-result--warn" role="status"><Icon name="warn" size={15} /><div>{text}</div></div>)}
    {suggestion && <label className="a-check">
      <input type="checkbox" checked={writeGitignore} disabled={busy} onChange={(event) => onWriteGitignore(event.target.checked)} />
      <span>Also add a .gitignore that leaves out {plan.bulky_directories.map((name) => `${name}/`).join(', ')}
        <small>The first commit then has {suggestion.file_count} {suggestion.file_count === 1 ? 'file' : 'files'} instead of {plan.file_count}. You can edit the file later.</small></span>
    </label>}
    {plan.blocked && <div className="a-result a-result--error" role="alert"><Icon name="warn" size={15} /><div>{plan.blocked.message}</div></div>}
    {error && <div className="a-result a-result--error" role="alert"><Icon name="warn" size={15} /><div>{error}</div></div>}
    <div className="a-act-row">
      <Button variant="primary" disabled={busy || Boolean(plan.blocked)} aria-busy={busy || undefined} onClick={onConfirm}>
        {busy ? 'Setting up…' : isCommit ? 'Create the first commit' : 'Initialize Git and commit'}</Button>
      <Button disabled={busy} onClick={onBack}>Back</Button>
    </div>
  </div>
}

export function RegisterProjectDialog({ open, onClose, onRegistered, onRefresh = () => {} }) {
  const [name, setName] = useState('')
  const [path, setPath] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // Why the folder can't be added yet (folderRefusal), and the setup step:
  // null | { phase: 'checking' | 'review' | 'sending', check, writeGitignore, error }
  const [refusal, setRefusal] = useState(null)
  const [setup, setSetup] = useState(null)
  // null, or what is known after a request whose outcome is unknown:
  // 'unknown' | 'checking' | 'absent' | 'check-failed' | { found: project }
  const [unknown, setUnknown] = useState(null)
  const blocked = unknown === 'unknown' || unknown === 'checking' || unknown === 'check-failed' || Boolean(unknown?.found)
  const working = busy || setup?.phase === 'checking' || setup?.phase === 'sending'

  async function register(event, anyway = false, rootPath = path) {
    event?.preventDefault()
    if (busy || !name.trim() || !rootPath.trim() || (blocked && !anyway)) return
    setBusy(true)
    setError('')
    setRefusal(null)
    setSetup(null)
    setUnknown(null)
    const result = await agentActions.registerProject(name.trim(), rootPath.trim())
    setBusy(false)
    if (result.offline) { setUnknown('unknown'); return }
    if (!result.ok) {
      const reason = folderRefusal(result)
      if (reason) { setRefusal(reason); return }
      setError(refusalText(result, 'Unable to register Project. Check the repository path and retry.'))
      return
    }
    setName('')
    setPath('')
    onRegistered(result.data)
  }

  // Read-only: what is wrong with the folder and what setup would write into it.
  async function reviewSetup() {
    setSetup({ phase: 'checking', check: null, writeGitignore: false, error: '' })
    const result = await agentActions.checkProjectFolder(path.trim())
    if (result.ok && result.data?.state === 'ready') { setSetup(null); register(null); return }
    if (result.ok && result.data?.setup) { setSetup({ phase: 'review', check: result.data, writeGitignore: false, error: '' }); return }
    setSetup(null)
    if (result.ok) { setRefusal(folderRefusal({ data: { detail: result.data } })); return }
    setRefusal(null)
    setError(refusalText(result, 'The folder couldn’t be checked. Try again.'))
  }

  async function confirmSetup() {
    if (!setup?.check || setup.phase !== 'review') return
    const current = setup
    setSetup({ ...current, phase: 'sending', error: '' })
    const result = await agentActions.setupProjectFolder(current.check.root_path, current.check.state, current.writeGitignore)
    if (result.offline) {
      setSetup({ ...current, phase: 'review', error: 'The backend could not be reached, so it is unknown whether Git was set up. Close this and check the folder before trying again.' })
      return
    }
    if (!result.ok) {
      if (result.data?.detail?.code === 'state_changed') { reviewSetup(); return }
      setSetup({ ...current, phase: 'review', error: refusalText(result, 'Git setup didn’t finish. Check the folder with “git status” in a terminal.') })
      return
    }
    setSetup(null)
    setRefusal(null)
    register(null)
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

  const edit = (setter) => (event) => {
    setter(event.target.value)
    setRefusal(null)
    setSetup(null)
    if (unknown && unknown !== 'checking') setUnknown(null)
  }
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

  const remedy = folderRemedy(refusal)
  const useTop = () => { setPath(refusal.topLevel); register(null, false, refusal.topLevel) }
  const reason = refusal && !setup?.check && <div className="a-result a-result--warn" role="alert"><Icon name="info" size={15} />
    <div className="a-folder-fix">
      <div>{refusal.message}</div>
      {remedy?.kind === 'command' && <CopyCommand command={refusal.command} />}
      {remedy?.kind === 'setup' && <div className="a-act-row">
        <Button size="sm" icon="folder" disabled={working} onClick={reviewSetup}>{setup?.phase === 'checking' ? 'Checking the folder…' : remedy.label}</Button></div>}
      {remedy?.kind === 'use-top' && <div className="a-act-row">
        <Button size="sm" icon="folder" disabled={working} onClick={useTop}>{remedy.label}</Button>
        <span className="a-fine a-mono a-folder-path">{refusal.topLevel}</span></div>}
    </div></div>

  // In the desktop app the window offers the system's folder picker; in a browser, the path is typed.
  const picker = typeof window !== 'undefined' ? window.controlCenterDesktop?.chooseFolder : undefined
  const browse = async () => {
    const chosen = await picker().catch(() => null)
    if (!chosen) return
    setPath(chosen)
    setRefusal(null)
    setSetup(null)
    if (!name.trim()) setName(chosen.split(/[\\/]/).filter(Boolean).pop() || '')
  }

  return <Dialog open={open} onClose={() => { if (!working) onClose() }} title="Add a project"
    description="A project is the top folder of a Git repository with at least one commit. Agents work in private copies of it; nothing here writes their changes into your folder.">
    <form className="a-register" onSubmit={register}>
      <TextField label="Project name" value={name} maxLength={100} disabled={working} onChange={edit(setName)} />
      <TextField label="Folder path" mono value={path} disabled={working} placeholder="C:\path\to\repository"
        hint={picker ? 'Choose the folder, or type or paste its full path.' : 'Type or paste the full path.'} onChange={edit(setPath)} />
      {picker && <div className="a-act-row"><Button size="sm" icon="folder" disabled={working} onClick={browse}>Choose folder…</Button></div>}
      {error && <div className="a-result a-result--error" role="alert"><Icon name="warn" size={15} /><div>{error}</div></div>}
      {reason}
      {setup?.check && <FolderSetup check={setup.check} busy={setup.phase === 'sending'} error={setup.error}
        writeGitignore={setup.writeGitignore} onWriteGitignore={(value) => setSetup((current) => ({ ...current, writeGitignore: value }))}
        onConfirm={confirmSetup} onBack={() => setSetup(null)} />}
      {note}
      {!setup?.check && <div className="a-act-row">
        <Button type="submit" variant="primary" disabled={working || blocked || !name.trim() || !path.trim()}>{busy ? 'Checking and adding…' : 'Add project'}</Button>
        <Button disabled={working} onClick={onClose}>Cancel</Button>
      </div>}
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
  const trigger = useRef(null)
  // Closing with Escape or by choosing returns focus to the field's button.
  const close = () => { setOpen(false); setQuery(''); trigger.current?.focus() }
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
  return <div className="a-pjfield" ref={root} onKeyDown={(event) => { if (event.key === 'Escape' && open) { event.stopPropagation(); close() } }}>
    <span className="a-field-label" id={`${id}-label`}>Project</span>
    <button ref={trigger} type="button" id={`${id}-button`} className="a-pj" aria-haspopup="dialog" aria-expanded={open} aria-labelledby={`${id}-label ${id}-button`}
      disabled={disabled} onClick={() => setOpen((current) => !current)}>
      <span className="a-pji a-pji--lg"><Icon name="folder" size={18} /></span>
      {chosen ? <span className="a-pjt"><b className="a-bidi" dir="auto">{chosen.name}</b><small className="a-mono">{chosen.path}</small></span>
        : <span className="a-pjt"><b>Choose a project</b><small>Required</small></span>}
      <span className="a-pjchg">Change<Icon name="down" size={13} stroke={2.2} /></span>
    </button>
    {open && <div className="a-pjpop" role="dialog" aria-label="Choose a project">
      <label className="a-pjsearch"><Icon name="search" size={15} /><input name="project-pick-find" value={query} onChange={(event) => setQuery(event.target.value)}
        placeholder="Find a project" aria-label="Find a project" dir="auto" autoFocus /></label>
      <div className="a-pjh">Your projects</div>
      {directory.state === 'loading' && <p className="a-fine a-pjnote">Loading projects…</p>}
      {directory.state === 'unavailable' && <p className="a-fine a-pjnote">Projects couldn’t be loaded. <button type="button" className="a-link" onClick={onRefresh}>Try again</button></p>}
      {directory.state === 'empty' && <p className="a-fine a-pjnote">No projects registered yet.</p>}
      {shown.map((project) => <button key={project.id} type="button" className="a-pjo" aria-pressed={project.id === value}
        onClick={() => { onChange(project.id); close() }}>
        <span className="a-pji"><Icon name="folder" size={15} /></span>
        <span className="a-pjt"><b className="a-bidi" dir="auto">{project.name}</b><small className="a-mono">{project.path}</small></span>
        {project.id === value && <Icon name="check" size={15} stroke={2.4} />}
      </button>)}
      {q && !shown.length && directory.state === 'ready' && <p className="a-fine a-pjnote">No project matches.</p>}
      <div className="a-pjsep" />
      <button type="button" className="a-pjo" onClick={() => { setOpen(false); setAdding(true) }}>
        <span className="a-pji"><Icon name="plus" size={15} stroke={2.2} /></span>
        <span className="a-pjt"><b>Add a project…</b><small>{window.controlCenterDesktop ? "Choose a Git repository folder" : "Type the full path of a Git repository"}</small></span>
      </button>
    </div>}
    <p className="a-fine a-pjhint">Agents work in a private copy; nothing here writes their changes into your folder.</p>
    {adding && <RegisterProjectDialog open onClose={() => setAdding(false)} onRefresh={onRefresh}
      onRegistered={(project) => { setAdding(false); onRegistered(project) }} />}
  </div>
}
