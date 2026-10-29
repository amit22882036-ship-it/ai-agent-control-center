import { useRef, useState } from 'react'
import { Button } from '../primitives'
import { TextArea } from '../controls'
import { ActionResult, Check, Disclosure } from './fields'
import { useLiveResource } from '../../hooks'
import { API_BASE } from '../../../apiClient.mjs'
import { agentActions, OFFLINE_MESSAGE } from '../../api/agentClient.mjs'
import { CANDIDATE_LABELS, attestationGate, attestationRequest, describeSources, diagnosticSummary, explanations, newRequestKey,
  outcomeMessage, readError, refusalHint, settlement } from '../../../executionRecovery.mjs'

const PROVIDER_RUN = { codex: 'Codex run', claude: 'Claude Code run', gemini: 'Gemini CLI run' }

function ExecutionCard({ execution, now, runner }) {
  const id = execution.execution_id
  const state = settlement(execution.state)
  const summary = diagnosticSummary(execution.diagnostic, now)
  const gate = attestationGate(execution, now)
  const [acknowledged, setAcknowledged] = useState(false)
  const [note, setNote] = useState('')
  const requestKey = useRef(null)
  const busy = Boolean(runner.busy)
  const refresh = () => runner.run(`diag-${id}`, async () => {
    const result = await agentActions.refreshDiagnostics(id)
    if (!result.ok) return { ok: false, message: `${result.offline ? OFFLINE_MESSAGE : readError(result.status, result.data).message} Nothing was changed.` }
    requestKey.current = null
    return { ok: true, message: 'Diagnostics refreshed.' }
  })
  const attest = (event) => {
    event.preventDefault()
    requestKey.current = requestKey.current || newRequestKey()
    const body = attestationRequest(gate, { acknowledged, note, requestKey: requestKey.current })
    if (!body || busy) return
    runner.run(`attest-${id}`, async () => {
      const result = await agentActions.attest(id, body)
      if (result.ok) { requestKey.current = null; return { ok: true, message: outcomeMessage(result.data) } }
      if (result.offline) return { ok: false, message: OFFLINE_MESSAGE }
      if (result.status !== 503) requestKey.current = null
      setAcknowledged(false)
      const refusal = readError(result.status, result.data)
      return { ok: false, message: `${refusal.message} Nothing was released.`,
        details: (refusal.reasons || []).map((reason) => `${reason.message} ${refusalHint(reason.code)}`.trim()) }
    })
  }
  return <article className="a-run-card" aria-labelledby={`run-${id}`}>
    <header><h4 id={`run-${id}`}>{PROVIDER_RUN[execution.provider] || 'Run'} started {execution.created_at?.slice(0, 16).replace('T', ' ')} UTC</h4>
      <span className={`a-chip a-chip--${state.basis === 'technical' ? 'work' : 'ask'}`}>{state.label}</span></header>
    <p>{state.detail}</p>
    {explanations(execution).map((item) => <p key={item.code} className="a-fine"><b>{item.title}.</b> {item.text}</p>)}
    <p className="a-fine" aria-live="polite">{summary.label}{summary.age ? ` · checked ${summary.age}` : ''}{execution.diagnostic ? ` · ${summary.status}` : ''}</p>
    <div className="a-act-row">
      <Button size="sm" icon="refresh" disabled={busy} onClick={refresh}>{runner.busy === `diag-${id}` ? 'Checking…' : 'Refresh diagnostics'}</Button>
    </div>
    <p className="a-fine">Refreshing looks again and re-runs automatic recovery. It never ends a process.</p>
    {execution.diagnostic && <Disclosure summary="What was observed">
      <dl className="a-kv">{describeSources(execution.diagnostic.sources).map((source) => <div key={source.key} className="a-kv-row"><dt>{source.label}</dt><dd>{source.text}</dd></div>)}</dl>
      {execution.diagnostic.candidates.length > 0 ? <ul className="a-plain-list">{execution.diagnostic.candidates.map((candidate) =>
        <li key={`${candidate.source}-${candidate.pid}`}><b>{candidate.name || 'Process'}</b> · PID {candidate.pid} · {CANDIDATE_LABELS[candidate.classification]}
          <span className="a-fine"> · {candidate.basis.join('; ')}</span></li>)}</ul>
        : <p className="a-fine">No related process was seen. A clean list is not proof that none exists.</p>}
      <dl className="a-kv"><dt>Execution</dt><dd className="a-mono">{id}</dd>
        {execution.job_name && <><dt>Job Object</dt><dd className="a-mono">{execution.job_name}</dd></>}
        {execution.root_pid && <><dt>Main process</dt><dd>PID {execution.root_pid}</dd></>}</dl>
    </Disclosure>}
    {gate.open ? <Disclosure summary="I verified it myself">
      <form onSubmit={attest}>
        <p className="a-attest-warning">The system could not prove that this run ended. Only continue if you have checked yourself, for example in Task
          Manager, that none of its processes is still running. Your decision is recorded as a human attestation and is never shown as a technical verification.</p>
        <blockquote className="a-attest-statement">{gate.statement}</blockquote>
        <Check checked={acknowledged} required disabled={busy} onChange={setAcknowledged}>I have read this statement and accept it.</Check>
        <TextArea label="What did you check? (optional)" rows={2} maxLength={2000} value={note} disabled={busy} onChange={(event) => setNote(event.target.value)} />
        <div className="a-act-row"><Button type="submit" variant="danger" size="sm" disabled={busy || !acknowledged}>
          {runner.busy === `attest-${id}` ? 'Re-checking…' : 'Release on my responsibility'}</Button></div>
        <p className="a-fine">The backend checks everything again first and refuses if anything changed.</p>
      </form>
    </Disclosure> : <div className="a-fine"><p>Attestation is not available{execution.diagnostic ? '' : ' until diagnostics have run'}.</p>
      {gate.reasons?.length > 0 && <ul className="a-plain-list">{gate.reasons.map((reason) => <li key={reason.code}>{reason.message} {refusalHint(reason.code)}</li>)}</ul>}</div>}
    <ActionResult result={runner.result} keys={[`diag-${id}`, `attest-${id}`]} />
  </article>
}

// Runs of an agent whose end the system could not prove. Shown only when there are any; the task,
// assignment and resources stay held until each is settled.
export function ProcessOwnership({ agentId, runner, version, now, historical }) {
  const data = useLiveResource(agentId ? `${API_BASE}/executions/unresolved?agent_id=${encodeURIComponent(agentId)}` : null, version)
  const executions = data.data?.executions || []
  if (!executions.length && !data.error) return null
  return <section className="a-attention a-attention--alert" aria-labelledby={`own-${agentId}`}>
    <h3 id={`own-${agentId}`}>Process ownership{executions.length > 0 ? ` · ${executions.length} unresolved` : ''}</h3>
    {data.error && <p className="a-fine">Process ownership details are temporarily unavailable. Ownership stays held; retrying with the next update.</p>}
    {executions.length > 0 && <p className="a-fine">{historical ? 'Runs of this task’s last agent. ' : ''}This agent’s task, assignment and resources stay held until
      every run below is settled. Settlement comes from Windows evidence; an attestation is your own decision and is recorded as such.</p>}
    {executions.map((execution) => <ExecutionCard key={execution.execution_id} execution={execution} now={now} runner={runner} />)}
  </section>
}
