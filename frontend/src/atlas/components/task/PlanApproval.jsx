import { useState } from 'react'
import { Button } from '../primitives'
import { agentActions, refusalText } from '../../api/agentClient.mjs'

// The plan phase of a task with helpers, and, while the plan waits for the person, Approve and Reject.
// Both are ordinary replies to the waiting lead; to change the plan, reply with what should change.
export default function PlanApproval({ phase, agentId }) {
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState('')
  if (!phase) return null
  async function answer(kind) {
    setBusy(kind)
    setError('')
    const result = await agentActions.reply(agentId, kind)
    setBusy(null)
    if (!result.ok) setError(refusalText(result, 'The answer could not be sent.'))
  }
  return <div className="a-plan-phase">
    <p className="a-fine">{phase.policyTitle} · {phase.text}{phase.retried ? ' The lead was asked once to correct its plan.' : ''}</p>
    {phase.awaiting && agentId && <>
      <div className="a-act-row">
        <Button size="sm" variant="primary" icon="check" disabled={Boolean(busy)} onClick={() => answer('approve')}>
          {busy === 'approve' ? 'Sending…' : 'Approve plan'}</Button>
        <Button size="sm" disabled={Boolean(busy)} onClick={() => answer('reject')}>{busy === 'reject' ? 'Sending…' : 'Reject: work alone'}</Button>
      </div>
      <p className="a-fine">To change the plan, reply to the lead with what you want changed; it revises the plan, then carries it out.</p>
    </>}
    {error && <p className="a-fine a-tone-alert" role="alert">{error}</p>}
  </div>
}
