import { useEffect, useRef, useState } from 'react'
import { API_BASE, apiFetch } from './apiClient.mjs'
import {
  CANDIDATE_LABELS, attestationGate, attestationRequest, describeSources, diagnosticSummary, explanations,
  newRequestKey, outcomeMessage, readError, refusalHint, settlement,
} from './executionRecovery.mjs'

const CLOCK_MS = 30000

function useClock() {
  const [now, setNow] = useState(null)
  useEffect(() => {
    const tick = () => setNow(Date.now())
    const first = setTimeout(tick, 0)
    const timer = setInterval(tick, CLOCK_MS)
    return () => { clearTimeout(first); clearInterval(timer) }
  }, [])
  return now
}

function Reasons({ reasons, id }) {
  if (!reasons?.length) return null
  return (
    <ul className="recovery-reasons" id={id}>
      {reasons.map((reason) => (
        <li key={reason.code}>
          <span>{reason.message}</span>
          <span className="agent-type-note">{refusalHint(reason.code)}</span>
        </li>
      ))}
    </ul>
  )
}

function ExecutionCard({ execution, now, onChanged, announce }) {
  const id = execution.execution_id
  const state = settlement(execution.state)
  const summary = diagnosticSummary(execution.diagnostic, now)
  const gate = attestationGate(execution, now)
  const [refreshing, setRefreshing] = useState(false)
  const [refreshError, setRefreshError] = useState('')
  const [acknowledged, setAcknowledged] = useState(false)
  const [note, setNote] = useState('')
  const [attesting, setAttesting] = useState(false)
  const [refusal, setRefusal] = useState(null)
  const requestKey = useRef(null)
  const busy = refreshing || attesting

  async function refresh() {
    if (busy) return
    setRefreshing(true)
    setRefreshError('')
    try {
      const response = await apiFetch(`${API_BASE}/executions/${encodeURIComponent(id)}/diagnostics`, { method: 'POST' })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(readError(response.status, data).message)
      requestKey.current = null  // a new review is a new request
      setRefusal(null)
      announce('Diagnostics refreshed.')
      onChanged()
    } catch (err) {
      setRefreshError(`${err.message} Nothing was changed.`)
    } finally {
      setRefreshing(false)
    }
  }

  async function attest(event) {
    event.preventDefault()
    if (busy) return
    requestKey.current = requestKey.current || newRequestKey()  // reused if this exact request is retried
    const body = attestationRequest(gate, { acknowledged, note, requestKey: requestKey.current })
    if (!body) return
    setAttesting(true)
    setRefusal(null)
    try {
      const response = await apiFetch(`${API_BASE}/executions/${encodeURIComponent(id)}/attestation`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        if (response.status !== 503) requestKey.current = null  // decided: a new attempt is a new request
        setRefusal(readError(response.status, data))
        setAcknowledged(false)
        onChanged()
        return
      }
      requestKey.current = null
      announce(outcomeMessage(data))
      onChanged()
    } catch {
      setRefusal({ message: 'The backend could not be reached. Nothing is assumed; retry when it is available.', reasons: [] })
    } finally {
      setAttesting(false)
    }
  }

  const reasonsId = `attestation-reasons-${id}`
  return (
    <article className="recovery-card" aria-labelledby={`execution-${id}`}>
      <header className="recovery-card-header">
        <h4 id={`execution-${id}`}>{({ codex: 'Codex run', claude: 'Claude Code run', gemini: 'Gemini CLI run' })[execution.provider] || 'Run'} started {execution.created_at?.slice(0, 16).replace('T', ' ')} UTC</h4>
        <span className={`recovery-badge recovery-${state.basis}`}>{state.label}</span>
      </header>
      <p>{state.detail}</p>
      {explanations(execution).map((item) => (
        <p key={item.code} className="recovery-explanation"><strong>{item.title}.</strong> {item.text}</p>
      ))}
      <div className="recovery-diagnostic" aria-live="polite">
        <p><span className={`recovery-dot recovery-${summary.tone}`} aria-hidden="true" />
          {summary.label}{summary.age && <span className="agent-type-note"> · checked {summary.age}</span>}</p>
        {execution.diagnostic && <p className="agent-type-note">{summary.status}</p>}
        <button className="close-button" type="button" onClick={refresh} disabled={busy} aria-busy={refreshing}>
          {refreshing ? 'Checking…' : 'Refresh diagnostics'}
        </button>
        <p className="agent-type-note">Refreshing looks again and re-runs automatic recovery. It never ends a process.</p>
        {refreshError && <p className="message error" role="alert">{refreshError}</p>}
      </div>
      {execution.diagnostic && (
        <details className="recovery-observations"><summary>What was observed</summary>
          <dl>
            {describeSources(execution.diagnostic.sources).map((source) => (
              <div key={source.key}><dt>{source.label}</dt><dd>{source.text}</dd></div>
            ))}
          </dl>
          {execution.diagnostic.candidates.length > 0 ? (
            <ul className="recovery-candidates">
              {execution.diagnostic.candidates.map((candidate) => (
                <li key={`${candidate.source}-${candidate.pid}`}>
                  <span><strong>{candidate.name || 'Process'}</strong> · PID {candidate.pid}</span>
                  <span className={`recovery-candidate recovery-candidate-${candidate.classification}`}>{CANDIDATE_LABELS[candidate.classification]}</span>
                  <span className="agent-type-note">{candidate.basis.join('; ')}</span>
                </li>
              ))}
            </ul>
          ) : <p className="agent-type-note">No related process was seen. A clean list is not proof that none exists.</p>}
          <dl className="recovery-ids">
            <div><dt>Execution</dt><dd>{id}</dd></div>
            {execution.job_name && <div><dt>Job Object</dt><dd>{execution.job_name}</dd></div>}
            {execution.root_pid && <div><dt>Main process</dt><dd>PID {execution.root_pid}</dd></div>}
          </dl>
        </details>
      )}
      {gate.open ? (
        <details className="attestation-disclosure"><summary>I verified it myself</summary>
          <form className="attestation-form" onSubmit={attest} aria-describedby={`attestation-warning-${id}`}>
            <p id={`attestation-warning-${id}`} className="attestation-warning">
              The system could not prove that this run ended. Only continue if you have checked yourself, for
              example in Task Manager, that none of its processes is still running. Your decision is recorded
              as a human attestation and is never shown as a technical verification.
            </p>
            <blockquote className="attestation-statement">{gate.statement}</blockquote>
            <label className="attestation-check">
              <input type="checkbox" checked={acknowledged} required disabled={busy}
                onChange={(event) => setAcknowledged(event.target.checked)} />
              <span>I have read this statement and accept it.</span>
            </label>
            <label htmlFor={`attestation-note-${id}`}>What did you check? (optional)</label>
            <textarea id={`attestation-note-${id}`} value={note} maxLength={2000} rows={2} disabled={busy}
              onChange={(event) => setNote(event.target.value)} />
            <button className="stop-button" type="submit" disabled={busy || !acknowledged} aria-busy={attesting}>
              {attesting ? 'Re-checking…' : 'Release on my responsibility'}
            </button>
            <p className="agent-type-note">The backend checks everything again first and refuses if anything changed.</p>
          </form>
        </details>
      ) : (
        <div className="recovery-ineligible">
          <p>Attestation is not available{execution.diagnostic ? '' : ' until diagnostics have run'}.</p>
          <Reasons reasons={gate.reasons} id={reasonsId} />
        </div>
      )}
      {refusal && (
        <div className="message error" role="alert">
          <p>{refusal.message} Nothing was released.</p>
          <Reasons reasons={refusal.reasons} id={`${reasonsId}-refusal`} />
        </div>
      )}
    </article>
  )
}

function ProcessOwnership({ agentId, refreshVersion, onChanged }) {
  const [data, setData] = useState(null)
  const [loadError, setLoadError] = useState('')
  const [version, setVersion] = useState(0)
  const [announcement, setAnnouncement] = useState('')
  const clock = useClock()

  useEffect(() => {
    const controller = new AbortController()
    fetch(`${API_BASE}/executions/unresolved?agent_id=${encodeURIComponent(agentId)}`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error('unavailable')
        return response.json()
      })
      .then((result) => {
        setData({ executions: result.executions, fetchedAt: Date.now() })
        setLoadError('')
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadError('Process ownership details are temporarily unavailable. Ownership stays held; retrying with the next update.')
      })
    return () => controller.abort()
  }, [agentId, refreshVersion, version])

  const executions = data?.executions || []
  if (!executions.length && !loadError && !announcement) return null
  const now = Math.max(clock || 0, data?.fetchedAt || 0)
  const changed = () => { setVersion((value) => value + 1); onChanged() }
  return (
    <details id="process-ownership" className="detail-section recovery-section" open>
      <summary>Process ownership{executions.length > 0 && <span className="recovery-count"> · {executions.length} unresolved</span>}</summary>
      <p className="sr-only" role="status" aria-live="polite">{announcement}</p>
      {announcement && <p className="message recovery-outcome">{announcement}</p>}
      {loadError && <p className="agent-type-note" role="status">{loadError}</p>}
      {executions.length > 0 && (
        <p className="agent-type-note">
          This Agent&apos;s Task, assignment and resources stay held until every run below is settled. Settlement
          comes from Windows evidence; an attestation is your own decision and is recorded as such.
        </p>
      )}
      {executions.map((execution) => (
        <ExecutionCard key={execution.execution_id} execution={execution} now={now} onChanged={changed}
          announce={setAnnouncement} />
      ))}
    </details>
  )
}

export default ProcessOwnership
