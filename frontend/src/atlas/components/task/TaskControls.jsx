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
    intro: 'Pausing keeps this task and the unfinished tasks below it from being started, and stops any of their agents that are running. Nothing is lost: questions, sessions, workspaces and history are kept, and nothing runs again until you resume.',
    confirm: 'Pause', done: 'Paused. Nothing runs until it is resumed.', danger: false,
  },
  resume: {
    title: 'Resume this task',
    intro: 'Resuming releases the pause this task started. Each task returns to what it was doing before (waiting, blocked or not started). No agent is started by resuming.',
    confirm: 'Resume', done: 'Resumed. The tasks are back to where they were; start or reply to an agent to continue.', danger: false,
  },
  cancel: {
    title: 'Cancel this task',
    intro: 'Canceling ends this task and the unfinished tasks below it, and stops any of their agents that are running. Their output, workspaces, sessions and history are kept, but the work can’t be resumed.',
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

const OUTCOMES = {
  applied: { tone: 'ok', text: 'Applied. The task’s changes are now in the project folder. Nothing was committed: review and commit them with your usual Git tools.' },
  noop: { tone: 'ok', text: 'Nothing to apply: the task made no changes to its copy.' },
}

function PathList({ paths, remaining, label }) {
  if (!paths?.length) return null
  return <details className="a-disclosure" open={paths.length <= 12}><summary><Icon name="right" size={13} stroke={2.2} />
    <span>{label} ({paths.length + (remaining || 0)})</span></summary>
    <div className="a-disclosure-body"><ul className="a-impact-list a-pathlist">{paths.map((path) => <li key={path} className="a-mono" dir="ltr">{path}</li>)}</ul>
      {remaining > 0 && <p className="a-fine">and {remaining} more</p>}</div></details>
}

// Apply a root task's work to its project. The backend's read-only preview lists the files the task
// changed and the files the project changed meanwhile; it is read again just before applying and
// the person is asked again if it differs. The backend merges in one step, keeps the person's own
// edits, staged files and branch, commits nothing, and refuses (in its own words) whenever a live
// agent, an unknown process, an in-progress Git operation or a conflict makes it unsafe.
export function ApplyDialog({ taskId, blocked = null, onClose, onFinished }) {
  const [phase, setPhase] = useState('loading')
  const [view, setView] = useState(null)
  const [error, setError] = useState(null)
  const [changed, setChanged] = useState(false)
  const sending = useRef(false)
  const same = (a, b) => JSON.stringify([a?.changed_paths, a?.destination_changed_paths]) === JSON.stringify([b?.changed_paths, b?.destination_changed_paths])

  useEffect(() => {
    let live = true
    agentActions.integrationPreview(taskId).then((result) => {
      if (!live) return
      if (result.offline) { setError({ tone: 'warn', text: `${OFFLINE_MESSAGE} The changes couldn’t be read, so nothing can be applied yet.` }); setPhase('blocked'); return }
      if (!result.ok) { setError({ tone: 'error', text: refusalText(result, 'The backend can’t prepare this task’s changes.') }); setPhase('blocked'); return }
      setView(result.data)
      setPhase('review')
    })
    return () => { live = false }
  }, [taskId])

  async function confirm() {
    if (sending.current || phase !== 'review' || blocked) return
    sending.current = true
    setPhase('sending')
    setError(null)
    try {
      const fresh = await agentActions.integrationPreview(taskId)
      if (fresh.offline) { setError({ tone: 'warn', text: `${OFFLINE_MESSAGE} Nothing was applied.` }); setPhase('review'); return }
      if (!fresh.ok) { setError({ tone: 'error', text: refusalText(fresh, 'The backend can’t prepare this task’s changes.') }); setPhase('blocked'); return }
      if (!same(view, fresh.data)) { setView(fresh.data); setChanged(true); setPhase('review'); return }
      const result = await agentActions.applyToProject(taskId)
      if (result.offline) {
        onFinished({ tone: 'warn', text: `${OFFLINE_MESSAGE} The changes may or may not have been applied; the task’s Result is read again when the backend answers. Check it before trying again.` })
        return
      }
      const record = result.ok ? result.data : result.data?.detail
      const status = record?.status
      if (OUTCOMES[status]) { onFinished(OUTCOMES[status]); return }
      if (status === 'conflict') {
        setError({ tone: 'error', text: `Nothing was applied: the task and the project changed the same lines in ${(record.conflict_paths || []).length + (record.conflict_paths_remaining || 0)} file(s). Resolve them in the task’s copy or the project, then try again.` })
        setView((current) => ({ ...current, conflicts: record.conflict_paths, conflictsRemaining: record.conflict_paths_remaining }))
        setPhase('blocked')
        return
      }
      setError({ tone: 'error', text: record?.failure_reason ? `${record.failure_reason}. Its history is on this page under Result.` : refusalText(result, 'The backend refused to apply this task.') })
      setPhase('blocked')
    } finally {
      sending.current = false
      setPhase((current) => (current === 'sending' ? 'review' : current))
    }
  }

  const busy = phase === 'sending'
  const nothing = view && !view.changed_paths?.length
  return <Dialog open onClose={() => { if (!busy) onClose() }} title="Apply this task to its project"
    description="The task worked in its own copy of the project. Applying merges what it changed into your project folder. Your own edits, staged files and branch are kept, and nothing is committed."
    actions={<>
      <Button onClick={onClose} disabled={busy}>Not now</Button>
      <Button variant="primary" onClick={confirm} disabled={phase !== 'review' || busy || Boolean(blocked) || nothing} aria-busy={busy || undefined}>
        {busy ? 'Applying…' : changed ? 'Apply these changes' : 'Apply'}</Button>
    </>}>
    {phase === 'loading' && <p className="a-fine" role="status">Reading what the task changed…</p>}
    {blocked && <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} /><div>{blocked}</div></div>}
    {changed && <div className="a-result a-result--warn" role="status"><Icon name="info" size={15} />
      <div>The changes are different from when they were shown. Review them again before applying.</div></div>}
    {view && <>
      {nothing ? <p className="a-fine">The task didn’t change any files, so there is nothing to apply.</p>
        : <PathList paths={view.changed_paths} remaining={view.changed_paths_remaining} label="Files the task changed" />}
      {view.destination_changed_paths?.length > 0 && <>
        <p className="a-fine">Your project also changed since the task started. Those changes are kept; where both touched the same lines, nothing is applied and the conflict is listed.</p>
        <PathList paths={view.destination_changed_paths} remaining={view.destination_changed_paths_remaining} label="Files changed in the project meanwhile" />
      </>}
      {view.conflicts?.length > 0 && <PathList paths={view.conflicts} remaining={view.conflictsRemaining} label="Files in conflict" />}
    </>}
    {error && <div className={`a-result a-result--${error.tone === 'error' ? 'error' : 'warn'}`} role={error.tone === 'error' ? 'alert' : 'status'}>
      <Icon name={error.tone === 'error' ? 'warn' : 'info'} size={15} /><div>{error.text}</div></div>}
  </Dialog>
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
