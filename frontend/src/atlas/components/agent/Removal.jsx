import { useRef, useState } from 'react'
import { Button, Notice } from '../primitives'
import { TextArea } from '../controls'
import { Dialog } from '../overlays'
import { Check } from './fields'
import { useLiveResource } from '../../hooks'
import { API_BASE } from '../../../apiClient.mjs'
import { agentActions, OFFLINE_MESSAGE } from '../../api/agentClient.mjs'
import { MODE_LABELS, branchCounts, consequenceText, failureText, newRequestKey, previewSummary, readRemovalError, reasonText,
  removalRequest } from '../../../agentRemoval.mjs'

const encode = encodeURIComponent

// Review the impact first, then confirm. The request is bound to the reviewed impact (plan token);
// if the impact changed, nothing runs and the new preview needs a fresh confirmation.
function RemovalDialog({ scope, open, onClose }) {
  const { detail, runner } = scope
  const children = detail.child_ids || []
  const [mode, setMode] = useState('single')
  const [preview, setPreview] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState(null)
  const requestKey = useRef(null)
  const busy = Boolean(runner.busy)
  const summary = previewSummary(preview)

  const review = (nextMode = mode) => runner.run('removal-preview', async () => {
    setError(null)
    setConfirmed(false)
    const result = await agentActions.removalPreview(scope.agentId, nextMode)
    if (!result.ok) {
      setError(result.offline ? { message: OFFLINE_MESSAGE } : readRemovalError(result.status, result.data))
      return { ok: false, message: 'The impact could not be reviewed.', changed: false }
    }
    setPreview(result.data)
    requestKey.current = newRequestKey()
    return { ok: true, message: 'Impact reviewed.', changed: false }
  })

  const remove = (event) => {
    event.preventDefault()
    const body = removalRequest(preview, { confirmed, requestKey: requestKey.current, reason })
    if (!body || busy) return
    runner.run('remove', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      const result = await agentActions.remove(scope.agentId, body)
      if (result.ok) {
        onClose()
        return { ok: true, message: result.data?.phase === 'removed' ? 'Removal confirmed by the backend.' : 'Removal admitted and in progress.' }
      }
      if (result.offline) { setError({ message: OFFLINE_MESSAGE }); return { ok: false, message: OFFLINE_MESSAGE } }
      const failure = readRemovalError(result.status, result.data)
      if (failure.code === 'stale_preview' && failure.preview) {
        setPreview(failure.preview)
        setConfirmed(false)
        requestKey.current = newRequestKey()
      }
      if (failure.code === 'removal_recovery_required') {
        onClose()
        return { ok: false, tone: 'warn', message: 'Removal admitted but blocked. Nothing was claimed as removed; see the details below.' }
      }
      setError(failure)
      return { ok: false, message: failure.message }
    })
  }

  const choose = (value) => { setMode(value); setPreview(null); setConfirmed(false) }
  return <Dialog open={open} onClose={() => { if (!busy) onClose() }} size="lg" title={`Remove ${scope.agentName}?`}
    description="Removing retires the agent identity permanently. It never deletes tasks, results, workspaces or history.">
    <fieldset className="a-removal-modes" disabled={busy}><legend>What to remove</legend>
      <label className="a-check"><input type="radio" name={`rm-${scope.agentId}`} checked={mode === 'single'} onChange={() => choose('single')} />
        <span><b>This agent only</b><small>{children.length ? 'Its child agents keep working on their own tasks.' : 'It has no child agents.'}</small></span></label>
      {children.length > 0 && <label className="a-check"><input type="radio" name={`rm-${scope.agentId}`} checked={mode === 'retire_branch'} onChange={() => choose('retire_branch')} />
        <span><b>Entire branch</b><small>This agent and every descendant, in one tracked operation.</small></span></label>}
    </fieldset>
    <div className="a-act-row">
      <Button disabled={busy} onClick={() => review()}>{runner.busy === 'removal-preview' ? 'Reviewing…' : preview ? 'Refresh impact' : 'Review impact'}</Button>
    </div>
    {summary && <form className="a-removal-preview" onSubmit={remove} aria-label="Removal impact">
      <p><b>{MODE_LABELS[summary.mode]}</b>: {summary.affected} {summary.affected === 1 ? 'agent' : 'agents'} affected
        {summary.running > 0 && `, ${summary.running} active`}{summary.unresolved > 0 && `, ${summary.unresolved} with an unconfirmed process end`}.</p>
      {summary.continuing > 0 && <p className="a-fine">{summary.continuing} child {summary.continuing === 1 ? 'agent continues' : 'agents continue'}
        {summary.resultsWait > 0 ? `; ${summary.resultsWait} will wait for a new owner to review their results` : ''}.</p>}
      <ul className="a-plain-list">{summary.consequences.map((code) => <li key={code}>{consequenceText(code)}</li>)}</ul>
      {!summary.available ? <Notice tone="warn" title="Removal is not available right now">
        {summary.reasons.map((code) => reasonText(code)).join(' ')}</Notice> : <>
        <TextArea label="Reason (optional)" rows={2} maxLength={2000} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} />
        <Check checked={confirmed} disabled={busy} required onChange={setConfirmed}>
          I understand {summary.affected === 1 ? 'this agent identity is' : 'these agent identities are'} retired permanently.</Check>
        <div className="a-act-row">
          <Button type="submit" variant="danger" disabled={busy || !confirmed}>
            {runner.busy === 'remove' ? 'Removing…' : summary.mode === 'retire_branch' ? `Remove branch (${summary.affected})` : 'Remove this agent'}</Button>
        </div>
      </>}
    </form>}
    {error && <div className="a-result a-result--error" role="alert"><div>{error.message}
      {error.code === 'stale_preview' && <p>The impact changed since you reviewed it. Review the updated preview above.</p>}
      {error.reasons?.length > 0 && <ul>{error.reasons.map((code) => <li key={code}>{reasonText(code)}</li>)}</ul>}</div></div>}
  </Dialog>
}

// Danger zone entry point: removal is never a plain delete button.
export function RemovalEntry({ scope }) {
  const [open, setOpen] = useState(false)
  return <section className="a-blk a-danger-zone" aria-labelledby={`remove-${scope.agentId}`}>
    <h3 id={`remove-${scope.agentId}`}>Remove agent</h3>
    <div className="a-setting-row">
      <div><small>Removing shows what would stop and who takes over its responsibility before anything happens.</small></div>
      <Button size="sm" variant="danger" disabled={Boolean(scope.runner.busy)} onClick={() => setOpen(true)}>Review removal…</Button>
    </div>
    {open && <RemovalDialog scope={scope} open={open} onClose={() => setOpen(false)} />}
  </section>
}

// A removal that is in progress or blocked: what is pending, why, and a safe retry.
export function RemovalProgress({ scope, nameOf, version }) {
  const { detail, runner } = scope
  const removalId = detail.removal?.removal_id
  const operation = useLiveResource(removalId ? `${API_BASE}/agent-removals/${encode(removalId)}` : null, version)
  const blocked = detail.removal?.state === 'blocked'
  const pending = (operation.data?.members || []).filter((member) => member.phase !== 'removed')
  const counts = branchCounts(operation.data)
  const retry = () => runner.run('removal-retry', async () => {
    const result = await agentActions.retryRemoval(removalId)
    if (result.ok) return { ok: true, message: result.data?.phase === 'removed' ? 'Removal confirmed by the backend.' : 'Removal retried.' }
    return { ok: false, message: result.offline ? 'The backend could not be reached. Nothing changed.' : readRemovalError(result.status, result.data).message }
  })
  return <section className={`a-attention${blocked ? ' a-attention--alert' : ''}`} aria-labelledby={`rmprog-${scope.agentId}`}>
    <h3 id={`rmprog-${scope.agentId}`}>{blocked ? 'Removal blocked' : 'Removal in progress'}</h3>
    <p>{blocked ? 'The removal could not finish safely. Nothing it covers is claimed as removed, and their tasks and resources stay held.'
      : 'This agent is being removed. It cannot start new work, create children or continue orchestration.'}</p>
    {operation.data?.mode === 'retire_branch' && <p className="a-fine">Branch: {counts.total} affected · {counts.removed} removed · {counts.pending} pending · {counts.blocked} blocked</p>}
    {pending.length > 0 && <ul className="a-plain-list">{pending.map((member) => <li key={member.agent_id}>
      <b>{nameOf(member.agent_id)}</b> · {member.phase === 'recovery_required' ? 'Blocked' : 'Pending'}
      {member.failure && <span className="a-fine"> · {failureText(member.failure)}</span>}</li>)}</ul>}
    {pending.some((member) => (member.failure || '').startsWith('execution_unresolved')) && <p className="a-fine">Review Process ownership below, then retry after it is settled.</p>}
    {operation.error && <p className="a-fine">Temporarily unavailable. Showing the last received state; it refreshes with the next update.</p>}
    <div className="a-act-row">
      <Button size="sm" disabled={Boolean(runner.busy)} onClick={retry}>{runner.busy === 'removal-retry' ? 'Retrying…' : 'Retry safe removal steps'}</Button>
    </div>
  </section>
}
