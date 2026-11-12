import { useEffect, useState } from 'react'
import Icon from '../Icon'
import { Button, Notice, Skeleton } from '../primitives'
import { Dialog } from '../overlays'
import { agentActions, projectLifecycle, refusalText } from '../../api/agentClient.mjs'
import { activityLines, deletionScope, lifecycleRefusal, localOnlyWarnings, trashPlace } from '../../model/projectLifecycle.mjs'

// The facts a decision needs, read once when the dialog opens (local Git reads, no network).
function usePreview(projectId, nonce) {
  const [state, setState] = useState({ key: null, data: null, error: null })
  const key = `${projectId}:${nonce}`
  useEffect(() => {
    let alive = true
    projectLifecycle(projectId).then((result) => {
      if (!alive) return
      setState(result.ok ? { key, data: result.data, error: null } : { key, data: null, error: refusalText(result, 'The project could not be read.') })
    })
    return () => { alive = false }
  }, [projectId, key])
  return state.key === key ? state : { key, data: null, error: null, loading: true }
}

function Remotes({ git }) {
  if (!git?.available) return <p className="a-fine">Git details could not be read.</p>
  return <div className="a-life-remotes">
    {git.remotes.length ? git.remotes.map((remote) => <span key={remote.name} className="a-life-remote">
      <Icon name="globe" size={14} /><b>{remote.name}</b><span className="a-mono">{remote.url}</span></span>)
      : <span className="a-fine">No remote repository.</span>}
  </div>
}

// Move to Trash: what moves where, what stops it, and a way to stop the work first.
export function TrashDialog({ project, onClose, onDone }) {
  const [nonce, setNonce] = useState(0)
  const preview = usePreview(project.id, nonce)
  const [busy, setBusy] = useState(null)
  const [refusal, setRefusal] = useState(null)
  const data = preview.data
  const blocked = Boolean(data?.activity?.busy)
  // The folder was deleted outside the app: nothing to move, so the project itself is removed (to the Trash).
  const missing = data?.folder?.exists === false || (refusal?.code === 'folder_missing' && refusal?.canRemove)
  const protectedFolder = !missing && (data?.protections || []).length > 0

  async function stopAll() {
    setBusy('stop')
    const result = await agentActions.stopProjectWork(project.id)
    setBusy(null)
    if (!result.ok) setRefusal(lifecycleRefusal(result) || { message: refusalText(result, 'The work could not be stopped.'), reasons: [] })
    else if (result.data?.failures?.length) {
      setRefusal({ message: 'Some agents could not be stopped.', reasons: result.data.failures.map((item) => `${item.name || item.agent_id}: ${item.error}`) })
    } else setRefusal(null)
    setNonce((value) => value + 1)
  }
  async function trash() {
    setBusy('trash')
    const result = await agentActions.trashProject(project.id, undefined, missing)
    setBusy(null)
    if (result.ok) { onDone(result.data); return }
    const next = lifecycleRefusal(result) || { message: refusalText(result, 'The project could not be moved to the Trash.'), reasons: [] }
    setRefusal({ ...next, canRemove: Boolean(result.data?.detail?.can_remove) })
    setNonce((value) => value + 1)
  }
  return <Dialog open onClose={onClose} size="lg"
    title={missing ? `Remove “${project.name}” from the Control Center?` : `Move “${project.name}” to the Trash?`}
    description={missing
      ? 'Its folder no longer exists, so nothing on disk is moved or deleted. The project and its history go to the Trash: restore it once the folder is back, or delete it permanently.'
      : 'The project folder moves out of its location into the Trash. You can restore it with everything in it, or delete it permanently later.'}
    actions={<>
      <Button onClick={onClose}>Keep project</Button>
      {blocked && <Button icon="stop" onClick={stopAll} disabled={Boolean(busy)}>{busy === 'stop' ? 'Stopping…' : 'Stop all work in this project'}</Button>}
      <Button variant="danger" icon="trash" onClick={trash} disabled={!data || blocked || protectedFolder || Boolean(busy)}>
        {busy === 'trash' ? (missing ? 'Removing…' : 'Moving…') : (missing ? 'Remove from Control Center' : 'Move to Trash')}</Button>
    </>}>
    {!data && !preview.error && <Skeleton lines={4} label="Reading the project" />}
    {preview.error && <Notice tone="error" title="The project could not be read">{preview.error}</Notice>}
    {data && <div className="a-life">
      <div className="a-life-move">
        <span className="a-life-folder"><Icon name="folder" size={18} /><span className="a-mono">{data.folder.path}</span>
          {missing && <span className="a-fine"> · not found</span>}</span>
        <Icon name="right" size={16} />
        <span className="a-life-folder a-life-folder--trash"><Icon name="trash" size={18} />Trash</span>
      </div>
      <p className="a-fine">{missing ? `Nothing was found at ${data.folder.path}. Only the project’s records go to the Trash; no folder is created, moved or deleted.` : trashPlace(data)}</p>
      <ul className="a-life-facts">
        <li><b>{data.activity.tasks}</b> tasks and <b>{data.agents}</b> agents keep their history.</li>
        <li><b>{data.worktrees}</b> private task worktrees stay where they are.</li>
        <li>Nothing runs in the project while it is in the Trash.</li>
      </ul>
      {blocked && <Notice tone="warn" title="Work is still running in this project">{activityLines(data).join(' ')}</Notice>}
      {missing && <p className="a-fine">If you moved the folder yourself rather than deleting it, move it back to {data.folder.path} first to keep it with the project.</p>}
      {protectedFolder && <Notice tone="error" title="This folder cannot be moved">{data.protections.join(' ')}</Notice>}
    </div>}
    {refusal && !(missing && refusal.code === 'folder_missing') && <Notice tone="error" title={refusal.message} role="alert">{refusal.reasons.join(' ')}</Notice>}
  </Dialog>
}

// Restore: back where it was, or somewhere else if that place is taken.
export function RestoreDialog({ project, onClose, onDone, initialRefusal }) {
  const [refusal, setRefusal] = useState(initialRefusal)
  const [target, setTarget] = useState(initialRefusal?.suggestion || '')
  const [busy, setBusy] = useState(false)
  async function restore() {
    setBusy(true)
    const result = await agentActions.restoreProject(project.project_id, target.trim() || null)
    setBusy(false)
    if (result.ok) { onDone(result.data); return }
    const next = lifecycleRefusal(result) || { message: refusalText(result, 'The project could not be restored.'), reasons: [] }
    setRefusal(next)
    if (next.suggestion) setTarget(next.suggestion)
  }
  return <Dialog open onClose={onClose} title={`Restore “${project.name}”`}
    description={`It was at ${project.trash?.original_root_path}. Nothing that is there now will be changed.`}
    actions={<><Button onClick={onClose}>Cancel</Button>
      <Button variant="primary" icon="restore" onClick={restore} disabled={busy || !target.trim()}>{busy ? 'Restoring…' : 'Restore here'}</Button></>}>
    {refusal && <Notice tone="warn" title={refusal.message}>{refusal.reasons.join(' ')}</Notice>}
    <label className="a-field">
      <span className="a-field-label">Restore to</span>
      <input className="a-input a-mono" name="restore-to" value={target} onChange={(event) => setTarget(event.target.value)} spellCheck={false}
        aria-label="Folder to restore the project to" />
      <span className="a-fine">A folder that does not exist yet, on the same drive.</span>
    </label>
  </Dialog>
}

// Delete permanently: exactly what goes, what does not, and a typed confirmation.
export function DeleteDialog({ project, onClose, onDone }) {
  const preview = usePreview(project.project_id, 0)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [refusal, setRefusal] = useState(null)
  const data = preview.data
  const warnings = localOnlyWarnings(data?.git)
  const matches = typed.trim() === project.name
  async function remove() {
    setBusy(true)
    const result = await agentActions.deleteProject(project.project_id, typed.trim())
    setBusy(false)
    if (result.ok) { onDone(result.data); return }
    setRefusal(lifecycleRefusal(result) || { message: refusalText(result, 'The project could not be deleted.'), reasons: [] })
  }
  return <Dialog open onClose={onClose} size="lg" title={`Delete “${project.name}” permanently?`}
    description="This cannot be undone. It deletes files from this computer."
    actions={<><Button onClick={onClose}>Cancel</Button>
      <Button variant="danger" icon="trash" onClick={remove} disabled={!data || !matches || busy}>{busy ? 'Deleting…' : 'Delete permanently'}</Button></>}>
    {!data && !preview.error && <Skeleton lines={4} label="Reading the project" />}
    {preview.error && <Notice tone="error" title="The project could not be read">{preview.error}</Notice>}
    {data && <div className="a-life">
      <h3 className="a-life-head">What will be deleted</h3>
      <ul className="a-life-facts a-life-facts--danger">{deletionScope(data).map((line) => <li key={line}>{line}</li>)}</ul>
      <h3 className="a-life-head">What will not be affected</h3>
      <ul className="a-life-facts">
        <li>Remote repositories, such as GitHub. Nothing is sent to them, and they are not changed.</li>
        <li>Other projects and their folders.</li>
      </ul>
      <Remotes git={data.git} />
      {warnings.length > 0 && <Notice tone="warn" title="Some of this exists only on this computer">{warnings.join(' ')}</Notice>}
      <label className="a-field">
        <span className="a-field-label">Type <b className="a-bidi" dir="auto">{project.name}</b> to confirm</span>
        <input className="a-input" name="confirm-name" value={typed} onChange={(event) => setTyped(event.target.value)} dir="auto" autoComplete="off"
          aria-label={`Type ${project.name} to confirm the permanent deletion`} />
      </label>
    </div>}
    {refusal && <Notice tone="error" title={refusal.message} role="alert">{refusal.reasons.join(' ')}</Notice>}
  </Dialog>
}
