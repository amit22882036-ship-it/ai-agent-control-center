import { useState } from 'react'
import Icon from '../Icon'
import { Button, Skeleton } from '../primitives'
import { agentActions, OFFLINE_MESSAGE, refusalText } from '../../api/agentClient.mjs'
import { when } from '../../model/taskPage.mjs'
import { Section } from './TaskSections'

// What a task holds so that agents don't step on each other: resource claims (a file, a port, a
// database, held shared or exclusive) and work intents ("I own the login flow"), plus the overlaps
// and deadlocks the backend detected. Agents create these; the person can see them and release one
// that is stuck. Releasing never stops an agent; the backend re-evaluates who may proceed.
const CLAIM_MODE = { advisory: 'advisory', shared: 'shared', exclusive: 'exclusive' }
const CLAIM_STATUS = { active: 'held', waiting: 'waiting for another task', suspended: 'suspended', released: 'released' }
const INTENT_MODE = { advisory: 'advisory', single_owner: 'single owner' }
const OVERLAP = { blocking: 'another task owns this too; one waits', advisory: 'another task works on this too', delegated: 'delegated between them' }

function Release({ label, onRelease }) {
  const [phase, setPhase] = useState('idle')
  const [error, setError] = useState(null)
  if (phase === 'confirm') {
    return <span className="a-coord-confirm">
      <span className="a-fine">Release it? The agent isn’t stopped; other tasks may proceed.</span>
      <Button size="sm" variant="danger" onClick={async () => {
        setPhase('sending')
        const result = await onRelease()
        if (result.offline) { setError(`${OFFLINE_MESSAGE} It may or may not have been released.`); setPhase('idle'); return }
        if (!result.ok) { setError(refusalText(result, 'The backend refused to release it.')); setPhase('idle'); return }
        setPhase('done')
      }}>Release</Button>
      <Button size="sm" variant="ghost" onClick={() => setPhase('idle')}>Keep</Button>
    </span>
  }
  return <>
    {phase === 'done' ? <span className="a-fine">Released</span>
      : <Button size="sm" variant="ghost" disabled={phase === 'sending'} aria-label={`Release ${label}`} onClick={() => setPhase('confirm')}>Release</Button>}
    {error && <span className="a-fine a-coord-error" role="alert">{error}</span>}
  </>
}

export function CoordinationCard({ taskId, claims, intents, overlaps, deadlocks, onChanged }) {
  const loading = [claims, intents].some((item) => item.loading && !item.data)
  const held = (claims.data?.claims || []).filter((claim) => claim.status !== 'released')
  const owned = (intents.data?.intents || []).filter((intent) => intent.status !== 'released')
  const overlap = (overlaps.data?.overlaps || []).filter((item) => item.other_task_id && item.other_task_id !== taskId)
  const locks = (deadlocks.data?.deadlocks || []).filter((item) => item.status === 'open')
  const failed = claims.error || intents.error
  const release = (call) => async () => { const result = await call(); if (result.ok) onChanged(); return result }
  return <Section id="t-coord" title="Coordination">
    {loading ? <Skeleton lines={2} label="Loading coordination" />
      : failed ? <p className="a-fine">What this task holds is unavailable right now.</p>
        : !held.length && !owned.length && !overlap.length && !locks.length
          ? <p className="a-fine">This task holds no resources or work areas, and nothing overlaps with it.</p>
          : <>
            {locks.length > 0 && <div className="a-result a-result--error" role="status"><Icon name="warn" size={15} />
              <div>{locks.length === 1 ? 'A deadlock involves this task' : `${locks.length} deadlocks involve this task`}: tasks wait for resources each other holds.
                Releasing one of the claims below lets one of them go on.</div></div>}
            {held.length > 0 && <><h3 className="a-tsub">Resources it holds</h3>
              <ul className="a-coord">{held.map((claim) => <li key={claim.claim_id}>
                <div><b className="a-mono" dir="ltr">{claim.resource_key}</b>
                  <small>{claim.resource_type} · {CLAIM_MODE[claim.mode] || claim.mode} · {claim.scope === 'global' ? 'on this computer' : 'in this project'} · {CLAIM_STATUS[claim.status] || claim.status}
                    {claim.acquired_at ? ` since ${when(claim.acquired_at)}` : ''}</small></div>
                <Release label={claim.resource_key} onRelease={release(() => agentActions.releaseClaim(taskId, claim.claim_id))} />
              </li>)}</ul></>}
            {owned.length > 0 && <><h3 className="a-tsub">Work it has said it owns</h3>
              <ul className="a-coord">{owned.map((intent) => <li key={intent.intent_id}>
                <div><b className="a-bidi" dir="auto">{intent.key}</b>
                  <small>{intent.namespace} · {INTENT_MODE[intent.mode] || intent.mode}{intent.status === 'suspended' ? ' · suspended' : ''}
                    {intent.holds_authority ? ' · holds the decision' : ''}</small></div>
                <Release label={intent.key} onRelease={release(() => agentActions.releaseIntent(taskId, intent.intent_id))} />
              </li>)}</ul></>}
            {overlap.length > 0 && <><h3 className="a-tsub">Overlaps with other tasks</h3>
              <ul className="a-coord">{overlap.map((item, index) => <li key={`${item.intent_id}-${item.other_intent_id || index}`}>
                <div><b className="a-bidi" dir="auto">{item.normalized_key}</b><small>{item.namespace} · {OVERLAP[item.classification] || 'another task works on this too'}
                  {item.blocked_task_id === taskId ? ' · this task waits for it' : item.blocked_task_id ? ' · the other task waits for this one' : ''}</small></div>
              </li>)}</ul></>}
          </>}
  </Section>
}
