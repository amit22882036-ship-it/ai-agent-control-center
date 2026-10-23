import { useEffect, useRef, useState } from 'react'
import { API_BASE, apiFetch } from './apiClient.mjs'
import { agentName } from './agentPresentation.mjs'
import Icon from './Icon'
import {
  MODE_LABELS, branchCounts, consequenceText, failureText, integrationGuidance, isRemoved, newRequestKey, previewSummary,
  readRemovalError, reasonText, removalRequest, removalState, responsibilitySummary, transferRequest,
} from './agentRemoval.mjs'

async function post(path, body) {
  const response = await apiFetch(`${API_BASE}${path}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}),
  })
  const data = await response.json().catch(() => ({}))
  return { response, data }
}

function useJson(path, version) {
  const [state, setState] = useState({ data: null, error: '' })
  useEffect(() => {
    if (!path) return undefined
    const controller = new AbortController()
    fetch(`${API_BASE}${path}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('unavailable')
        return response.json()
      })
      .then((data) => setState({ data, error: '' }))
      .catch(() => { if (!controller.signal.aborted) setState((current) => ({ ...current, error: 'Temporarily unavailable. Showing the last received state; it refreshes with the next update.' })) })
    return () => controller.abort()
  }, [path, version])
  return state
}

function nameOf(agents, id) {
  const known = agents.find((item) => item.agent_id === id)
  return known ? agentName(known) : 'Agent outside the loaded list'
}

function Counts({ operation }) {
  const counts = branchCounts(operation)
  return <dl className="removal-counts" aria-label="Branch removal progress">
    <div><dt>Affected</dt><dd>{counts.total}</dd></div>
    <div><dt>Removed</dt><dd>{counts.removed}</dd></div>
    <div><dt>Pending</dt><dd>{counts.pending}</dd></div>
    <div><dt>Blocked</dt><dd>{counts.blocked}</dd></div>
  </dl>
}

function Composer({ agent, agentId, onChanged, announce }) {
  const children = agent.child_ids || []
  const [mode, setMode] = useState('single')
  const [preview, setPreview] = useState(null)
  const [confirmed, setConfirmed] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const requestKey = useRef(null)
  const summary = previewSummary(preview)

  async function review(nextMode = mode) {
    setBusy(true)
    setError(null)
    setConfirmed(false)
    try {
      const { response, data } = await post(`/agents/${encodeURIComponent(agentId)}/removal-preview`, { mode: nextMode })
      if (!response.ok) throw readRemovalError(response.status, data)
      setPreview(data)
      requestKey.current = newRequestKey()  // one request per reviewed impact
    } catch (err) {
      setError(err.message ? err : { message: 'The preview could not be loaded.' })
    } finally {
      setBusy(false)
    }
  }

  async function remove(event) {
    event.preventDefault()
    const body = removalRequest(preview, { confirmed, requestKey: requestKey.current, reason })
    if (!body || busy) return
    setBusy(true)
    setError(null)
    try {
      const { response, data } = await post(`/agents/${encodeURIComponent(agentId)}/remove`, body)
      if (response.ok) {
        announce(data.phase === 'removed' ? 'Removal confirmed by the backend.' : 'Removal admitted and in progress.')
        onChanged()
        return
      }
      const failure = readRemovalError(response.status, data)
      if (failure.code === 'stale_preview' && failure.preview) {
        // Never executed: the impact changed, so the new preview needs a fresh confirmation.
        setPreview(failure.preview)
        setConfirmed(false)
        requestKey.current = newRequestKey()
      }
      if (failure.code === 'removal_recovery_required') {
        announce('Removal admitted but blocked. Nothing was claimed as removed; see the details below.')
        onChanged()
        return
      }
      setError(failure)
    } catch {
      setError({ message: 'The backend could not be reached. Nothing is assumed; retry when it is available.' })
    } finally {
      setBusy(false)
    }
  }

  return <details className="detail-section removal-section"><summary>Remove agent</summary>
    <fieldset className="removal-modes" disabled={busy}><legend>What to remove</legend>
      <label className="removal-mode"><input type="radio" name={`removal-mode-${agentId}`} checked={mode === 'single'} onChange={() => { setMode('single'); setPreview(null) }} />
        <span><strong>This agent only</strong><small>{children.length ? 'Its child agents keep working on their own tasks.' : 'It has no child agents.'}</small></span></label>
      {children.length > 0 && <label className="removal-mode"><input type="radio" name={`removal-mode-${agentId}`} checked={mode === 'retire_branch'} onChange={() => { setMode('retire_branch'); setPreview(null) }} />
        <span><strong>Entire branch</strong><small>This agent and every descendant, in one tracked operation.</small></span></label>}
    </fieldset>
    <p className="agent-type-note">Removing never deletes tasks, results, workspaces or history.</p>
    <button className="close-button" type="button" onClick={() => review()} disabled={busy} aria-busy={busy && !preview}>{preview ? 'Refresh impact' : 'Review impact'}</button>
    {summary && <form className="removal-preview" onSubmit={remove} aria-label="Removal impact">
      <p><strong>{MODE_LABELS[summary.mode]}</strong>: {summary.affected} {summary.affected === 1 ? 'agent' : 'agents'} affected
        {summary.running > 0 && `, ${summary.running} active`}{summary.unresolved > 0 && `, ${summary.unresolved} with an unconfirmed process end`}.</p>
      {summary.continuing > 0 && <p className="agent-type-note">{summary.continuing} child {summary.continuing === 1 ? 'agent continues' : 'agents continue'}{summary.resultsWait > 0 ? `; ${summary.resultsWait} will wait for a new owner to review their results` : ''}.</p>}
      <ul className="removal-consequences">{summary.consequences.map((code) => <li key={code}>{consequenceText(code)}</li>)}</ul>
      {!summary.available ? <div className="message" role="status"><p>Removal is not available right now.</p>
        <ul className="removal-reasons">{summary.reasons.map((code) => <li key={code}>{reasonText(code)}</li>)}</ul></div> : <>
        <label htmlFor={`removal-reason-${agentId}`}>Reason (optional)</label>
        <textarea id={`removal-reason-${agentId}`} rows={2} maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} disabled={busy} />
        <label className="attestation-check"><input type="checkbox" checked={confirmed} required disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />
          <span>I understand {summary.affected === 1 ? 'this agent identity is' : 'these agent identities are'} retired permanently.</span></label>
        <button className="stop-button" type="submit" disabled={busy || !confirmed} aria-busy={busy}>
          {busy ? 'Removing…' : summary.mode === 'retire_branch' ? `Remove branch (${summary.affected})` : 'Remove this agent'}</button>
      </>}
    </form>}
    {error && <div className="message error" role="alert"><p>{error.message}</p>
      {error.code === 'stale_preview' && <p>The impact changed since you reviewed it. Review the updated preview above.</p>}
      {error.reasons?.length > 0 && <ul className="removal-reasons">{error.reasons.map((code) => <li key={code}>{reasonText(code)}</li>)}</ul>}</div>}
  </details>
}

function Progress({ agent, agents, refreshVersion, onChanged, announce }) {
  const removalId = agent.removal.removal_id
  const [version, setVersion] = useState(0)
  const { data: operation, error } = useJson(`/agent-removals/${encodeURIComponent(removalId)}`, `${refreshVersion}-${version}`)
  const [busy, setBusy] = useState(false)
  const [retryError, setRetryError] = useState('')
  const blocked = agent.removal.state === 'blocked'

  async function retry() {
    if (busy) return
    setBusy(true)
    setRetryError('')
    try {
      const { response, data } = await post(`/agent-removals/${encodeURIComponent(removalId)}/retry`, {})
      if (response.ok) announce(data.phase === 'removed' ? 'Removal confirmed by the backend.' : 'Removal retried.')
      else setRetryError(readRemovalError(response.status, data).message)
    } catch {
      setRetryError('The backend could not be reached. Nothing changed.')
    } finally {
      setBusy(false)
      setVersion((value) => value + 1)
      onChanged()
    }
  }

  const pending = (operation?.members || []).filter((m) => m.phase !== 'removed')
  return <details className="detail-section removal-section" open><summary>{blocked ? 'Removal blocked' : 'Removal in progress'}</summary>
    <p>{blocked ? 'The removal could not finish safely. Nothing it covers is claimed as removed, and their tasks and resources stay held.'
      : 'This agent is being removed. It cannot start new work, create children or continue orchestration.'}</p>
    {operation && operation.mode === 'retire_branch' && <Counts operation={operation} />}
    {pending.length > 0 && <ul className="removal-members">{pending.map((member) => <li key={member.agent_id}>
      <strong>{nameOf(agents, member.agent_id)}</strong>
      <span className={`removal-phase removal-phase-${member.phase}`}>{member.phase === 'recovery_required' ? 'Blocked' : 'Pending'}</span>
      {member.failure && <span className="agent-type-note">{failureText(member.failure)}</span>}
    </li>)}</ul>}
    {pending.some((m) => (m.failure || '').startsWith('execution_unresolved')) && <p className="agent-type-note">
      Open <a href="#process-ownership">Process ownership</a> to review the evidence. Retry after it is settled.</p>}
    {error && <p className="agent-type-note" role="status">{error}</p>}
    <div className="details-actions">
      <button className="close-button" type="button" onClick={retry} disabled={busy} aria-busy={busy}>{busy ? 'Retrying…' : 'Retry safe removal steps'}</button>
      <button className="close-button" type="button" onClick={() => setVersion((value) => value + 1)} disabled={busy}>Refresh</button>
    </div>
    {retryError && <p className="message error" role="alert">{retryError}</p>}
  </details>
}

function TransferForm({ taskId, state, onChanged, announce }) {
  const [agentType, setAgentType] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const requestKey = useRef(null)

  async function submit(event) {
    event.preventDefault()
    requestKey.current = requestKey.current || newRequestKey()
    const body = transferRequest(state, { confirmed, requestKey: requestKey.current, agentType, sandbox, reason })
    if (!body || busy) return
    setBusy(true)
    setError(null)
    try {
      const { response, data } = await post(`/tasks/${encodeURIComponent(taskId)}/responsibility/transfer`, body)
      if (response.ok) {
        requestKey.current = null
        announce('A new owner took over this task.')
        onChanged()
        return
      }
      if (response.status !== 503) requestKey.current = null
      setError(readRemovalError(response.status, data))
      setConfirmed(false)
      onChanged()
    } catch {
      setError({ message: 'The backend could not be reached. Nothing is assumed; retry when it is available.' })
    } finally {
      setBusy(false)
    }
  }

  return <details className="transfer-disclosure"><summary>Choose a new owner</summary>
    <form className="transfer-form" onSubmit={submit}>
      <p className="agent-type-note">A new agent takes over this task with its workspace, results and history. No child is promoted and nothing is reassigned automatically.</p>
      <fieldset className="removal-modes" disabled={busy}><legend>New agent</legend>
        <label className="removal-mode"><input type="radio" name={`transfer-type-${taskId}`} checked={agentType === 'mock'} onChange={() => setAgentType('mock')} /><span><strong>Mock</strong><small>Local test agent</small></span></label>
        <label className="removal-mode"><input type="radio" name={`transfer-type-${taskId}`} checked={agentType === 'codex'} onChange={() => setAgentType('codex')} /><span><strong>Codex</strong><small>Uses your Codex account</small></span></label>
        <label className="removal-mode"><input type="radio" name={`transfer-type-${taskId}`} checked={agentType === 'claude'} onChange={() => setAgentType('claude')} /><span><strong>Claude Code</strong><small>Uses your Claude subscription when enabled</small></span></label>
        <label className="removal-mode"><input type="radio" name={`transfer-type-${taskId}`} checked={agentType === 'gemini'} onChange={() => setAgentType('gemini')} /><span><strong>Gemini CLI</strong><small>Uses your Google sign-in when enabled</small></span></label>
      </fieldset>
      {agentType !== 'mock' && <><label htmlFor={`transfer-sandbox-${taskId}`}>Sandbox</label>
        <select id={`transfer-sandbox-${taskId}`} value={sandbox} onChange={(event) => setSandbox(event.target.value)} disabled={busy}>
          <option value="read-only">Read only</option><option value="workspace-write">Workspace write</option></select></>}
      <label htmlFor={`transfer-reason-${taskId}`}>Note (optional)</label>
      <textarea id={`transfer-reason-${taskId}`} rows={2} maxLength={2000} value={reason} onChange={(event) => setReason(event.target.value)} disabled={busy} />
      <label className="attestation-check"><input type="checkbox" checked={confirmed} required disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />
        <span>Start a new agent that takes responsibility for this task.</span></label>
      <button className="start-button" type="submit" disabled={busy || !confirmed} aria-busy={busy}>{busy ? 'Checking and starting…' : 'Assign new owner'}</button>
    </form>
    {error && <div className="message error" role="alert"><p>{error.message}</p>
      {error.reasons?.length > 0 && <ul className="removal-reasons">{error.reasons.map((item) => <li key={item.code}>{item.message}</li>)}</ul>}</div>}
  </details>
}

function IntegrationGuidance({ state }) {
  const guidance = integrationGuidance(state)
  if (!guidance.available.length && !guidance.unavailable.length) return null
  return <details className="transfer-disclosure integration-guidance"><summary>Completed child work</summary>
    <p className="agent-type-note">{guidance.note}</p>
    {guidance.available.length > 0 && <><p>May be integrated with the existing task integration (<code>POST /tasks/&#123;child task&#125;/integrate</code>), which re-checks every condition:</p>
      <ul className="removal-reasons">{guidance.available.map((c) => <li key={c.child_task_id}>{c.title}{c.previously_integrated ? ' (integrated before)' : ''}</li>)}</ul></>}
    {guidance.unavailable.length > 0 && <><p>Not integrable now:</p>
      <ul className="removal-reasons">{guidance.unavailable.map((c) => <li key={c.child_task_id}>{c.title}: {c.integration_blockers.map((b) => b.message).join(' ')}</li>)}</ul></>}
  </details>
}

function Removed({ agent, agentId, agents, refreshVersion, onChanged, onSelect, announce }) {
  const taskId = agent.task_ids?.[0] || null
  const [version, setVersion] = useState(0)
  const responsibility = useJson(taskId ? `/tasks/${encodeURIComponent(taskId)}/responsibility` : null, `${refreshVersion}-${version}`)
  const operation = useJson(agent.removal?.removal_id ? `/agent-removals/${encodeURIComponent(agent.removal.removal_id)}` : null, refreshVersion)
  const state = responsibility.data
  const summary = responsibilitySummary(state)
  const children = agents.filter((item) => item.parent_id === agentId)
  const continuing = children.filter((item) => !isRemoved(item))
  const changed = () => { setVersion((value) => value + 1); onChanged() }
  return <details className="detail-section removal-section" open><summary>Removed</summary>
    <p className="removal-banner"><Icon name="removed" size={16} className="removal-mark" />
      <span>Removed{agent.removal?.removed_at ? ` ${new Date(agent.removal.removed_at).toLocaleString()}` : ''}
        {agent.removal?.mode === 'retire_branch' ? ' with its branch' : ''}. Its identity is retired; its history, output, task and workspace are kept.</span></p>
    {operation.data?.mode === 'retire_branch' && operation.data.target_agent_id === agentId && <Counts operation={operation.data} />}
    {children.length > 0 && <div className="removal-children"><h4>Child agents</h4>
      <p className="agent-type-note">{continuing.length ? `${continuing.length} continue their own work. Results that need this agent wait for a new owner of its task.` : 'All of its child agents are removed too.'}</p>
      <ul>{children.slice(0, 6).map((item) => <li key={item.agent_id}><button type="button" onClick={() => onSelect(item.agent_id)}>
        {isRemoved(item) && <Icon name="removed" size={13} className="removal-mark" />}<span className={isRemoved(item) ? 'agent-name removed-name' : 'agent-name'}>{agentName(item)}</span></button></li>)}</ul>
      {children.length > 6 && <p className="agent-type-note">+{children.length - 6} more</p>}</div>}
    {taskId && <div className="responsibility" aria-live="polite"><h4>Task responsibility</h4>
      <p><span className={`recovery-dot recovery-${summary.tone === 'clear' ? 'clear' : 'blocking'}`} aria-hidden="true" /> {summary.text}</p>
      {state?.owner && <p>Owner: <button type="button" onClick={() => onSelect(state.owner.agent_id)}>{nameOf(agents, state.owner.agent_id)}</button></p>}
      {state?.awaiting_transfer && !state.transferable && <ul className="removal-reasons">{state.reasons.map((item) => <li key={item.code}>{item.message}</li>)}</ul>}
      <IntegrationGuidance state={state} />
      {state?.transferable && <TransferForm taskId={taskId} state={state} onChanged={changed} announce={announce} />}
      {state?.transfers?.length > 0 && <p className="agent-type-note">{state.transfers.length} recorded owner {state.transfers.length === 1 ? 'change' : 'changes'}; earlier ownership is kept in the assignment history.</p>}
      {responsibility.error && <p className="agent-type-note" role="status">{responsibility.error}</p>}
    </div>}
  </details>
}

export default function RemovalSection(props) {
  const [announcement, setAnnouncement] = useState('')
  const state = removalState(props.agent)
  return <>
    <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
    {announcement && <p className="message removal-outcome">{announcement}</p>}
    {state === 'removed' ? <Removed {...props} announce={setAnnouncement} />
      : state ? <Progress {...props} announce={setAnnouncement} />
        : <Composer {...props} announce={setAnnouncement} />}
  </>
}
