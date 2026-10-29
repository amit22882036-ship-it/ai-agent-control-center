import { useRef, useState } from 'react'
import { Button } from '../primitives'
import { TextArea } from '../controls'
import { ActionResult, Check, Disclosure, PlainSelect } from './fields'
import { useLiveResource } from '../../hooks'
import { API_BASE } from '../../../apiClient.mjs'
import { agentActions, OFFLINE_MESSAGE } from '../../api/agentClient.mjs'
import { integrationGuidance, newRequestKey, readRemovalError, responsibilitySummary, transferRequest } from '../../../agentRemoval.mjs'
import { TOOLS } from '../../vocabulary.mjs'

const encode = encodeURIComponent
const TRANSFER_TYPES = [
  { value: 'mock', note: 'Local test agent' }, { value: 'codex', note: 'Uses your Codex account' },
  { value: 'claude', note: 'Uses your Claude subscription when enabled' }, { value: 'gemini', note: 'Uses your Google sign-in when enabled' },
]

// Who is responsible for a task whose agent was removed, and choosing a new owner. A task-level
// action: it starts a new agent for this task; no child is promoted and nothing moves automatically.
export function ResponsibilityPanel({ taskId, runner, version, nameOf }) {
  const state = useLiveResource(`${API_BASE}/tasks/${encode(taskId)}/responsibility`, version)
  const data = state.data
  const summary = responsibilitySummary(data)
  const guidance = integrationGuidance(data)
  return <section className="a-attention" aria-labelledby={`resp-owner-${taskId}`}>
    <h3 id={`resp-owner-${taskId}`}>Task responsibility</h3>
    <p>{summary.text}</p>
    {data?.owner && <p className="a-fine">Owner: {nameOf(data.owner.agent_id)}</p>}
    {data?.awaiting_transfer && !data.transferable && <ul className="a-plain-list">{(data.reasons || []).map((item) => <li key={item.code}>{item.message}</li>)}</ul>}
    {(guidance.available.length > 0 || guidance.unavailable.length > 0) && <Disclosure summary="Completed child work">
      <p className="a-fine">{guidance.note}</p>
      {guidance.available.length > 0 && <ul className="a-plain-list">{guidance.available.map((c) => <li key={c.child_task_id}>{c.title}{c.previously_integrated ? ' (integrated before)' : ''}</li>)}</ul>}
      {guidance.unavailable.length > 0 && <ul className="a-plain-list">{guidance.unavailable.map((c) => <li key={c.child_task_id}>{c.title}: {c.integration_blockers.map((b) => b.message).join(' ')}</li>)}</ul>}
    </Disclosure>}
    {data?.transferable && <TransferForm taskId={taskId} state={data} runner={runner} />}
    {data?.transfers?.length > 0 && <p className="a-fine">{data.transfers.length} recorded owner {data.transfers.length === 1 ? 'change' : 'changes'}; earlier ownership is kept in the assignment history.</p>}
    {state.error && <p className="a-fine">Temporarily unavailable. Showing the last received state; it refreshes with the next update.</p>}
  </section>
}

function TransferForm({ taskId, state, runner }) {
  const [agentType, setAgentType] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const requestKey = useRef(null)
  const busy = Boolean(runner.busy)
  function submit(event) {
    event.preventDefault()
    requestKey.current = requestKey.current || newRequestKey()
    const body = transferRequest(state, { confirmed, requestKey: requestKey.current, agentType, sandbox, reason })
    if (!body || busy) return
    runner.run('transfer', async () => {
      const result = await agentActions.transfer(taskId, body)
      if (result.ok) { requestKey.current = null; return { ok: true, message: 'A new owner took over this task.' } }
      if (result.offline) return { ok: false, message: OFFLINE_MESSAGE }
      // The same request may be retried only when the backend could not decide (503).
      if (result.status !== 503) requestKey.current = null
      setConfirmed(false)
      const failure = readRemovalError(result.status, result.data)
      return { ok: false, message: failure.message, details: (failure.reasons || []).map((item) => item.message || String(item)) }
    })
  }
  return <Disclosure summary="Choose a new owner" className="a-disclosure--card">
    <form onSubmit={submit}>
      <p className="a-fine">A new agent takes over this task with its workspace, results and history. No child is promoted and nothing is reassigned automatically.</p>
      <PlainSelect label="New agent" value={agentType} disabled={busy} onChange={setAgentType}
        options={TRANSFER_TYPES.map((item) => ({ value: item.value, label: `${TOOLS[item.value]?.label || item.value} · ${item.note}` }))} />
      {agentType !== 'mock' && <PlainSelect label="Access" value={sandbox} disabled={busy} onChange={setSandbox}
        options={[{ value: 'read-only', label: 'Read only' }, { value: 'workspace-write', label: 'Workspace write' }]} />}
      <TextArea label="Note (optional)" rows={2} maxLength={2000} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} />
      <Check checked={confirmed} disabled={busy} required onChange={setConfirmed}>Start a new agent that takes responsibility for this task.</Check>
      <div className="a-act-row"><Button type="submit" variant="primary" disabled={busy || !confirmed}>{runner.busy === 'transfer' ? 'Checking and starting…' : 'Assign new owner'}</Button></div>
    </form>
    <ActionResult result={runner.result} keys={['transfer']} />
  </Disclosure>
}
