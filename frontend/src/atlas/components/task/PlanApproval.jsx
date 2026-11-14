import { useState } from 'react'
import { Button } from '../primitives'
import { TextArea } from '../controls'
import { agentActions, refusalText } from '../../api/agentClient.mjs'

// The plan phase of a task with helpers, and, while the plan waits for the person: the plan itself (one row
// per part) with Approve, Request changes and Work alone. Each is an ordinary reply to the waiting lead
// ('approve', the requested changes, 'reject'), so typing a reply in the answer box keeps working too.
export default function PlanApproval({ phase, agentId }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')
  const [changing, setChanging] = useState(false)
  const [changes, setChanges] = useState('')
  const [sent, setSent] = useState('')
  if (!phase) return null
  async function answer(kind, text) {
    setBusy(kind)
    setError('')
    const result = await agentActions.reply(agentId, text)
    setBusy(null)
    if (!result.ok) { setError(refusalText(result, 'The answer could not be sent.')); return }
    setChanging(false)
    setSent(kind === 'approve' ? 'Approved. The lead starts the plan.' : kind === 'reject'
      ? 'Sent. The lead does the work itself, without helpers.' : 'Sent. The lead revises the plan, then carries it out.')
  }
  const proposal = phase.proposal
  return <div className="a-plan-phase">
    <p className="a-fine">{phase.policyTitle} · {phase.text}{phase.retried ? ' The lead was asked once to correct its plan.' : ''}</p>
    {phase.awaiting && proposal && <>
      {proposal.alone ? <p>The lead proposes to do the work itself, without helpers.</p>
        : <ol className="a-plan-parts">{proposal.items.map((item) => <li key={item.name}>
          <b className="a-bidi" dir="auto">{item.name}</b>
          <span className="a-bidi" dir="auto"> · {item.deliverable}</span>
          {item.dependsOn.length > 0 && <small className="a-fine"> · after {item.dependsOn.join(', ')}</small>}
        </li>)}</ol>}
      {proposal.rationale && <p className="a-fine a-bidi" dir="auto">Why: {proposal.rationale}</p>}
    </>}
    {phase.awaiting && agentId && !sent && <>
      <div className="a-act-row">
        <Button size="sm" variant="primary" icon="check" disabled={Boolean(busy)} onClick={() => answer('approve', 'approve')}>
          {busy === 'approve' ? 'Sending…' : 'Approve'}</Button>
        <Button size="sm" disabled={Boolean(busy)} onClick={() => setChanging((open) => !open)} aria-expanded={changing}>Request changes</Button>
        <Button size="sm" variant="ghost" disabled={Boolean(busy)} onClick={() => answer('reject', 'reject')}>
          {busy === 'reject' ? 'Sending…' : 'Work alone'}</Button>
      </div>
      {changing && <form className="a-plan-changes" onSubmit={(event) => { event.preventDefault(); if (changes.trim()) answer('changes', changes.trim()) }}>
        <TextArea label="What should change in the plan?" value={changes} rows={3} maxLength={4000} disabled={Boolean(busy)}
          onChange={(event) => setChanges(event.target.value)} />
        <Button size="sm" type="submit" disabled={Boolean(busy) || !changes.trim()}>{busy === 'changes' ? 'Sending…' : 'Send changes'}</Button>
      </form>}
    </>}
    {sent && <p className="a-fine" role="status">{sent}</p>}
    {error && <p className="a-fine a-tone-alert" role="alert">{error}</p>}
  </div>
}
