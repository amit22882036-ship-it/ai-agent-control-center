import { useEffect, useRef, useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { Dialog } from '../overlays'
import { Check } from '../agent/fields'
import { agentActions, OFFLINE_MESSAGE, refusalText } from '../../api/agentClient.mjs'
import { impactView, sameImpact } from '../../model/taskPage.mjs'

const COPY = {
  pause: {
    title: 'Pause this task',
    intro: 'Pausing stops the running agents of this task and the unfinished tasks below it. Nothing is lost: questions, sessions, workspaces and history are kept, and nothing runs again until you resume.',
    confirm: 'Pause', done: 'Paused. Nothing runs until it is resumed.', danger: false,
  },
  resume: {
    title: 'Resume this task',
    intro: 'Resuming releases the pause this task started. Each task returns to what it was doing before (waiting, blocked or not started). No agent is started by resuming.',
    confirm: 'Resume', done: 'Resumed. The tasks are back to where they were; start or reply to an agent to continue.', danger: false,
  },
  cancel: {
    title: 'Cancel this task',
    intro: 'Canceling ends this task and the unfinished tasks below it. Their output, workspaces, sessions and history are kept, but the work can’t be resumed.',
    confirm: 'Cancel task', done: 'Canceled. Its output, workspaces and history are kept.', danger: true,
  },
}

function Impact({ view }) {
  if (!view) return null
  const titled = (items) => items.map((item) => <li key={item.id} className="a-bidi" dir="auto">{item.title || 'A task that isn’t loaded here'}</li>)
  return <div className="a-impact">
    <ul className="a-impact-lines">{view.lines.map((line) => <li key={line}>{line}</li>)}</ul>
    {view.affected.length > 1 && <details className="a-disclosure"><summary><Icon name="right" size={13} stroke={2.2} /><span>Which tasks</span></summary>
      <div className="a-disclosure-body"><ul className="a-impact-list">{titled(view.affected)}</ul></div></details>}
    {view.workers.length > 0 && <p className="a-fine">Will stop: {view.workers.map((item) => item.name || 'an agent').join(', ')}.</p>}
    {view.external.length > 0 && <details className="a-disclosure"><summary><Icon name="right" size={13} stroke={2.2} /><span>Tasks elsewhere that will be blocked</span></summary>
      <div className="a-disclosure-body"><ul className="a-impact-list">{titled(view.external)}</ul></div></details>}
  </div>
}

// Pause, Resume and Cancel for one Task. Pause and Cancel show the backend's own preview of what
// they affect (POST /tasks/{id}/control-impact), read it again just before sending and ask again
// if it changed. One request at a time; a lost connection is reported as an unknown outcome and
// is never retried by itself; the backend's refusals are shown in its words.
// `blocked` (a reason) disables sending while the backend is unreachable or the task's state is stale.
export function TaskControlDialog({ action, taskId, store, held, blocked = null, onClose, onFinished }) {
  const copy = COPY[action]
  const [phase, setPhase] = useState(action === 'resume' ? 'review' : 'loading')
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [changed, setChanged] = useState(false)
  const [understood, setUnderstood] = useState(false)
  const sending = useRef(false)
  const storeRef = useRef(store)
  useEffect(() => { storeRef.current = store }, [store])

  useEffect(() => {
    if (action === 'resume') return undefined
    let live = true
    agentActions.controlImpact(taskId, action).then((result) => {
      if (!live) return
      if (result.offline) { setError({ tone: 'warn', text: `${OFFLINE_MESSAGE} What this would affect couldn’t be read, so nothing can be sent yet.` }); setPhase('blocked'); return }
      if (!result.ok) { setError({ tone: 'error', text: refusalText(result, 'The backend refused to preview this action.') }); setPhase('blocked'); return }
      setView(impactView(result.data, storeRef.current))
      setPhase('review')
    })
    return () => { live = false }
  }, [action, taskId])

  async function confirm() {
    if (sending.current || phase !== 'review' || blocked) return
    sending.current = true
    setPhase('sending')
    setError(null)
    try {
      if (action !== 'resume') {
        // The impact may have changed since it was shown: read it again and ask again if so.
        const fresh = await agentActions.controlImpact(taskId, action)
        if (fresh.offline) { setError({ tone: 'warn', text: `${OFFLINE_MESSAGE} Nothing was sent.` }); setPhase('review'); return }
        if (!fresh.ok) { setError({ tone: 'error', text: refusalText(fresh, 'The backend refused this action.') }); setPhase('blocked'); return }
        const next = impactView(fresh.data, storeRef.current)
        if (!sameImpact(view, next)) { setView(next); setChanged(true); setUnderstood(false); setPhase('review'); return }
      }
      const send = { pause: agentActions.pauseTask, resume: agentActions.resumeTask, cancel: agentActions.cancelTask }[action]
      const result = await send(taskId)
      if (result.offline) {
        onFinished({ tone: 'warn', text: `${OFFLINE_MESSAGE} The task may or may not have been ${action === 'cancel' ? 'canceled' : action === 'pause' ? 'paused' : 'resumed'}; its state is read again when the backend answers. Check it before trying again.` })
        return
      }
      if (result.status === 503) {
        // The backend may have recorded the intent and then failed to settle every agent.
        onFinished({ tone: 'warn', text: `The backend couldn’t finish this. The ${action === 'resume' ? 'resume' : action} may have been recorded while some agents weren’t confirmed stopped; its state is read again. Check the task (Advanced › control operations) before trying again.` })
        return
      }
      if (!result.ok) { setError({ tone: 'error', text: `Nothing was changed. ${refusalText(result, 'The backend refused this action.')}` }); setPhase('blocked'); return }
      onFinished({ tone: 'ok', text: copy.done })
    } finally {
      sending.current = false
      setPhase((current) => (current === 'sending' ? 'review' : current))
    }
  }

  const busy = phase === 'sending'
  const needsAck = action === 'cancel'
  const ready = phase === 'review' && (!needsAck || understood) && !blocked
  return <Dialog open onClose={() => { if (!busy) onClose() }} title={copy.title} description={copy.intro}
    actions={<>
      <Button onClick={onClose} disabled={busy}>Keep it as it is</Button>
      <Button variant={copy.danger ? 'danger' : 'primary'} onClick={confirm} disabled={!ready || busy} aria-busy={busy || undefined}>
        {busy ? 'Sending…' : changed ? `${copy.confirm} with this new impact` : copy.confirm}</Button>
    </>}>
    {phase === 'loading' && <p className="a-fine" role="status">Reading what this would affect…</p>}
    {view && <p className="a-impact-head">If you confirm:</p>}
    {blocked && <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} /><div>{blocked}</div></div>}
    {changed && <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} />
      <div>What this affects changed since it was shown. Review it again before confirming.</div></div>}
    {action === 'resume' ? <p className="a-fine">{held ? `This releases the pause for ${held.affected.length} task${held.affected.length === 1 ? '' : 's'}.` : ''}</p>
      : <Impact view={view} />}
    {view && view.unloaded > 0 && <p className="a-fine">{view.unloaded} of these tasks aren’t loaded on this page; the backend counts them all.</p>}
    {needsAck && phase !== 'loading' && phase !== 'blocked' && <Check checked={understood} disabled={busy} onChange={setUnderstood}>
      I understand that canceled work can’t be resumed.</Check>}
    {error && <div className={`a-result a-result--${error.tone === 'error' ? 'error' : 'warn'}`} role={error.tone === 'error' ? 'alert' : 'status'}>
      <Icon name={error.tone === 'error' ? 'warn' : 'info'} size={15} /><div>{error.text}</div></div>}
  </Dialog>
}
