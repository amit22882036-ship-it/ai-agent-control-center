import { useEffect, useId, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { Button } from '../components/primitives'
import { StatusBead } from '../components/identity'
import { ToolMark } from '../components/connections/ProviderParts'
import { useActionRunner } from '../hooks'
import { agentActions, refusalText } from '../api/agentClient.mjs'
import { noticeText } from '../../onboardingPresentation.mjs'
import { ATTENTION_KINDS, attentionLabel, coverageText } from '../model/attention.mjs'
import { humanTimes } from '../model/connections.mjs'
import { deliveryView } from '../model/notify.mjs'
import { formatHash } from '../router.mjs'

const SHOWN = 6
const BEAD = { reply: 'ask', owner: 'ask', recovery: 'blocked', integration: 'blocked' }

// One provider update notice, with Classic's three choices. Review opens that tool's setup in
// Connections; nothing is installed from here.
function UpdateNotice({ notice, runner, blocked, onReview }) {
  const choose = (mode) => runner.run(`${mode}:${notice.id}`, async () => {
    const result = await agentActions.dismissNotice(notice.provider, notice.available_version, mode)
    if (result.ok) return { ok: true, message: mode === 'dismiss' ? `${notice.label} ${notice.available_version} dismissed.` : `${notice.label}: reminding you again tomorrow.` }
    if (result.offline || result.status >= 500) return { ok: null, tone: 'warn', message: 'It may or may not have been saved. The notices are read again.' }
    return { ok: false, message: `Nothing was changed. ${refusalText(result, 'The notice could not be updated.')}` }
  })
  const checked = humanTimes(notice.checked_at)
  return <li className="a-ntf-item">
    <ToolMark provider={notice.provider} size="sm" />
    <div className="a-ntf-main">
      <p className="a-ntf-line">{noticeText(notice)}</p>
      <small className={notice.stale ? 'a-tone-ask-text' : undefined}>{checked ? `Checked ${checked}` : 'Check time not recorded'}
        {notice.stale ? ' · may be out of date' : ''} · updating is optional and reviewed first</small>
      <div className="a-ntf-acts">
        <Button size="sm" onClick={() => onReview(notice.provider)}>Review update</Button>
        <Button size="sm" variant="ghost" disabled={Boolean(runner.busy) || blocked} onClick={() => choose('remind_later')}>Remind me later</Button>
        <Button size="sm" variant="ghost" disabled={Boolean(runner.busy) || blocked} onClick={() => choose('dismiss')}>Dismiss</Button>
      </div>
    </div>
  </li>
}

// The notifications panel: what needs the person now (derived from the live Task list, never a
// stored history), tools that need attention, and provider updates. It is a small non-modal panel:
// Escape or a click outside closes it and returns focus to the bell.
export default function NotificationsPanel({ attention, summary, tools, notices, preference, delivery, live, onClose, onReply, onShow, onShowAll,
  onTool, onReview, triggerRef }) {
  const panel = useRef(null)
  const headingId = useId()
  const [, setTick] = useState(0)
  const runner = useActionRunner(notices.reload)
  // Opening the panel reads the update notices afresh.
  const reloadNotices = notices.reload
  useEffect(() => { reloadNotices() }, [reloadNotices])
  useEffect(() => {
    panel.current?.querySelector('[data-panel-heading]')?.focus()
    const outside = (event) => {
      if (panel.current?.contains(event.target) || triggerRef.current?.contains(event.target)) return
      onClose(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [onClose, triggerRef])
  // Relative times ("5 minutes ago") stay right while it is open.
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => value + 1), 30000)
    return () => clearInterval(timer)
  }, [])
  // Moving somewhere closes the panel first; focus then goes where the person went.
  const go = (action) => (...args) => { onClose(false); action(...args) }
  const offline = live.reachable === false
  const coverage = coverageText(summary)
  const shown = attention.slice(0, SHOWN)
  const more = attention.length - shown.length
  const unreadNotices = notices.error && !notices.data
  const status = deliveryView(preference, delivery).short

  return <div ref={panel} className="a-ntf" role="dialog" aria-modal="false" aria-labelledby={headingId}
    onKeyDown={(event) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onClose(true) } }}>
    <div className="a-ntf-head">
      <h2 id={headingId} tabIndex={-1} data-panel-heading>Notifications</h2>
      <a className="a-link a-ntf-settings" href={formatHash('settings', { section: 'notifications' })} onClick={() => onClose(false)}>Settings</a>
    </div>
    {offline && <p className="a-ntf-offline" role="status"><Icon name="plug" size={14} />The backend isn’t answering. What you see is from the last answer; actions wait until it returns.</p>}

    <section className="a-ntf-sec" aria-labelledby={`${headingId}-needs`}>
      <h3 id={`${headingId}-needs`}>Needs you</h3>
      {summary.known === false ? <p className="a-ntf-empty">Not known yet: the task list hasn’t been read{offline ? ' because the backend isn’t answering' : ''}.</p>
        : !attention.length && <p className="a-ntf-empty">{summary.reported > 0 ? 'Nothing in the loaded tasks.' : 'Nothing needs you right now.'}</p>}
      {summary.known !== false && summary.stale && <p className="a-fine a-ntf-note">From the last task list received.</p>}
      {shown.length > 0 && <ul className="a-ntf-list">
        {shown.map((entry) => <li key={entry.taskId} className="a-ntf-item">
          <StatusBead state={BEAD[entry.kind]} size={14} decorative />
          <div className="a-ntf-main">
            <p className="a-ntf-line"><span className="a-bidi" dir="auto">{entry.title}</span></p>
            <small>{attentionLabel(entry)}{entry.agentName ? <> · <span className="a-bidi" dir="auto">{entry.agentName}</span></> : null}</small>
            <div className="a-ntf-acts">
              {entry.kind === 'reply' && entry.agentId && <Button size="sm" variant="ask" icon="reply" onClick={() => go(onReply)(entry.taskId)}>Reply</Button>}
              <Button size="sm" variant={entry.kind === 'reply' ? 'ghost' : 'secondary'} onClick={() => go(onShow)(entry.taskId)}>
                {entry.kind === 'reply' ? 'Show' : ATTENTION_KINDS[entry.kind].action}</Button>
            </div>
          </div>
        </li>)}
      </ul>}
      {summary.known !== false && (more > 0 || coverage) && <p className="a-fine a-ntf-note">
        {more > 0 && <>{more} more in the loaded tasks. </>}{coverage}{' '}
        {(more > 0 || summary.unlisted > 0 || !summary.complete) && <button type="button" className="a-link" onClick={go(onShowAll)}>Show in Work</button>}</p>}
    </section>

    {tools.length > 0 && <section className="a-ntf-sec" aria-labelledby={`${headingId}-tools`}>
      <h3 id={`${headingId}-tools`}>Tools</h3>
      <ul className="a-ntf-list">{tools.map((tool) => <li key={tool.provider || 'default'} className="a-ntf-item">
        {tool.provider ? <ToolMark provider={tool.provider} size="sm" /> : <Icon name="warn" size={16} />}
        <div className="a-ntf-main">
          <p className="a-ntf-line">{tool.text}</p>
          <div className="a-ntf-acts"><Button size="sm" onClick={() => go(onTool)(tool.provider)}>{tool.provider ? `Open ${tool.label}` : 'Open Connections'}</Button></div>
        </div>
      </li>)}</ul>
    </section>}

    <section className="a-ntf-sec" aria-labelledby={`${headingId}-updates`}>
      <h3 id={`${headingId}-updates`}>Tool updates</h3>
      {unreadNotices ? <p className="a-ntf-empty">Update notices couldn’t be read{notices.error === 'unreachable' ? ': the backend isn’t answering' : ''}.</p>
        : notices.loading && !notices.data ? <p className="a-ntf-empty">Reading…</p>
          : notices.notices.length === 0 ? <p className="a-ntf-empty">No updates to review.</p>
            : <ul className="a-ntf-list">{notices.notices.map((notice) => <UpdateNotice key={notice.id} notice={notice} runner={runner}
              blocked={offline} onReview={go(onReview)} />)}</ul>}
      {notices.error && notices.data && <p className="a-fine">These are from the last answer; the latest read failed.</p>}
      {runner.result && <p className={`a-fine a-ntf-result${runner.result.ok === false ? ' a-tone-alert-text' : ''}`} role="status">{runner.result.message}</p>}
    </section>

    <div className="a-ntf-foot"><Icon name="bell" size={14} /><span>Notifications: {status}</span></div>
  </div>
}
