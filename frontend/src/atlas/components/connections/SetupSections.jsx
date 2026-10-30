import { useState } from 'react'
import { Button, Card } from '../primitives'
import { Check, PlainSelect, ActionResult } from '../agent/fields'
import { agentActions } from '../../api/agentClient.mjs'
import { ACTION_LABELS, CHANNELS, approvalRequest, planCommand, planRequest, planSource } from '../../../onboardingPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { accountNotice } from '../../../providerGuidance.mjs'
import { setupView } from '../../model/connections.mjs'
import { outcome } from './outcome.mjs'

const SETUP_KEYS = ['plan', 'approve', 'cancel', 'resolve', 'check']

// The exact operation an approval would run, as the backend planned it. Approving names that plan
// (its identity and digest) and is used once; a refusal needs a fresh review.
function OperationReview({ plan, busy, blocked, onApprove, onDismiss }) {
  const [reviewed, setReviewed] = useState(false)
  const command = planCommand(plan)
  const blockers = plan.blockers || []
  const runnable = Boolean(plan.plan_id) && blockers.length === 0
  return <div className="a-cx-review" role="group" aria-labelledby="cx-review-title">
    <h3 id="cx-review-title">{ACTION_LABELS[plan.action]} {providerLabel(plan.provider)}: review</h3>
    <dl className="a-kv">
      {command && <><dt>Runs</dt><dd><code className="a-mono a-cx-code">{command}</code></dd></>}
      {planSource(plan) && <><dt>Installs</dt><dd>{planSource(plan)}</dd></>}
      {plan.method && <><dt>Method</dt><dd>{plan.method.replaceAll('_', ' ')}</dd></>}
      {plan.current_version && <><dt>Installed</dt><dd>{plan.current_version}</dd></>}
      {plan.target && <><dt>Target</dt><dd>{plan.target}{plan.target_version ? ` (resolved to ${plan.target_version} for this approval)` : ''}</dd></>}
      {plan.source && <><dt>Source</dt><dd><a href={plan.source} target="_blank" rel="noreferrer noopener" className="a-cx-ext">{plan.source}</a></dd></>}
      {plan.console && <><dt>Window</dt><dd>Opens the tool’s own sign-in window; you sign in there and in your browser.</dd></>}
    </dl>
    {plan.changes?.length > 0 && <ul className="a-cx-list">{plan.changes.map((line) => <li key={line}>{line}</li>)}</ul>}
    {plan.dependencies?.length > 0 && <p className="a-fine">Needs: {plan.dependencies.join(', ')}.</p>}
    {plan.risks?.length > 0 && <ul className="a-cx-list a-cx-list--risk">{plan.risks.map((line) => <li key={line}>{line}</li>)}</ul>}
    {plan.manual?.length > 0 && <div className="a-cx-manual"><b>Manual steps</b>{plan.manual.map((line) => <p key={line}>{line}</p>)}</div>}
    {plan.action === 'login' && accountNotice(plan.provider) && <p className="a-fine a-tone-ask-text">{accountNotice(plan.provider).title}: {accountNotice(plan.provider).lines[1]}</p>}
    {blockers.length > 0 && <ul className="a-cx-list a-cx-list--block" role="alert">{blockers.map((line) => <li key={line}>{line}</li>)}</ul>}
    {runnable && <Check checked={reviewed} disabled={busy} onChange={setReviewed}>
      I reviewed this exact operation. It runs once, without administrator rights, in its own supervised process tree, and can be canceled.</Check>}
    <div className="a-act-row">
      {runnable && <Button variant="primary" disabled={!reviewed || busy || Boolean(blocked)} aria-busy={busy || undefined} onClick={onApprove}>
        {busy ? 'Starting…' : `Run ${ACTION_LABELS[plan.action].toLowerCase()}`}</Button>}
      <Button onClick={onDismiss} disabled={busy}>{runnable ? 'Cancel' : 'Close'}</Button>
    </div>
  </div>
}

// Installation, updates, sign-in and sign-out: reviewed plans only, one at a time, with any
// running operation, one whose end could not be verified, and the latest result.
export function SetupCard({ entry, update, runner, blocked }) {
  const provider = entry.provider
  const view = setupView(entry, update)
  const [plan, setPlan] = useState(null)
  const [channel, setChannel] = useState('')
  const [ended, setEnded] = useState(false)
  const busy = Boolean(runner.busy)
  const off = busy || Boolean(blocked)
  const channels = CHANNELS[provider] || []
  const review = (action) => runner.run('plan', async () => {
    const result = await agentActions.planOperation(provider, planRequest(action, ['install', 'update'].includes(action) ? channel || null : null))
    if (result.ok) { setPlan(result.data); return { ok: true, message: 'Prepared. Nothing runs until you approve this exact operation.', changed: false } }
    return { ...outcome(result, { refused: 'The operation could not be prepared.', unknown: 'Nothing runs without an approval; prepare it again when the backend answers.', prefix: '' }), changed: false }
  })
  const approve = () => runner.run('approve', async () => {
    let approval
    try { approval = approvalRequest(plan) } catch (error) { return { ok: false, message: error.message, changed: false } }
    setPlan(null)  // An approval is used once: whatever happens next, a new run needs a fresh review.
    const result = await agentActions.startOperation(provider, approval)
    return outcome(result, { done: `${ACTION_LABELS[plan.action]} started. Its progress shows here.${plan.console ? ' Finish signing in in the window that opened.' : ''}`,
      refused: 'The operation was refused.', prefix: 'Nothing ran. ',
      unknown: 'It may or may not have started. Its state is read again; check it here before reviewing it again.' })
  })
  const cancel = () => runner.run('cancel', async () => outcome(await agentActions.cancelOperation(view.running.operation_id), {
    done: 'Cancel requested. The operation and everything it started are being stopped.', refused: 'It could not be canceled.',
    unknown: 'The cancel may or may not have reached the backend. Its state is read again.' }))
  const resolve = () => runner.run('resolve', async () => {
    const result = await agentActions.resolveOperation(view.uncertain.operation_id)
    if (result.ok) setEnded(false)
    return outcome(result, { done: 'Recorded that the operation ended.', refused: 'It could not be confirmed.',
      unknown: 'The confirmation may or may not have been recorded. Its state is read again.' })
  })
  return <Card className="a-cx-sec" id="cx-sec-setup" titleId="cx-sec-setup-h" title="Installation and sign-in">
    <dl className="a-kv">
      <dt>Installation</dt><dd>{view.install?.value || 'Not known'}</dd>
      <dt>Updates</dt><dd>{view.update?.text || 'Not checked yet'}</dd>
    </dl>
    {view.elsewhere.length > 0 && <p className="a-fine">Also found (reported only, never run by the Control Center): {view.elsewhere.join(', ')}</p>}
    {view.activeWork.length > 0 && <p className="a-fine">Running now: {view.activeWork.map((work, index) => <span key={work.execution_id || index}>{index > 0 ? ', ' : ''}
      <span className="a-bidi a-cx-agent" dir="auto">{work.name}</span></span>)}. Updates, sign-in and sign-out wait until they finish; nothing is stopped for you.</p>}
    {view.running && <div className="a-result a-result--warn" role="status"><div>
      <b>{ACTION_LABELS[view.running.action]} is running.</b>{view.running.action === 'login' ? ' Finish signing in in the window that opened.' : ''}
      <div className="a-act-row"><Button size="sm" disabled={off} onClick={cancel}>{runner.busy === 'cancel' ? 'Canceling…' : 'Cancel it'}</Button></div>
    </div></div>}
    {view.uncertain && <div className="a-cx-review" role="group" aria-labelledby="cx-uncertain-title">
      <h3 id="cx-uncertain-title">Confirm the last {ACTION_LABELS[view.uncertain.action]?.toLowerCase()} ended</h3>
      <p className="a-fine">The Control Center couldn’t verify that it and everything it started have ended. Close its window if one is open and wait for any
        installer to finish, then confirm. Until then this tool starts no new work or setup. It is refused while its processes still run.</p>
      <Check checked={ended} disabled={off} onChange={setEnded}>The setup window and installer have ended.</Check>
      <div className="a-act-row"><Button disabled={!ended || off} onClick={resolve}>{runner.busy === 'resolve' ? 'Confirming…' : 'Confirm it ended'}</Button></div>
    </div>}
    {view.latest && <p className={`a-fine a-cx-op a-cx-op--${view.latest.tone}`}>Last setup operation: {view.latest.text}{view.latest.at ? ` · ${view.latest.at}` : ''}</p>}
    {!plan && view.actions.length > 0 && <div className="a-cx-actions">
      {view.actions.map((action) => <Button key={action} variant={view.step?.kind === action ? 'primary' : 'secondary'} disabled={off}
        onClick={() => review(action)}>{runner.busy === 'plan' ? 'Preparing…' : `${ACTION_LABELS[action]}…`}</Button>)}
      {channels.length > 1 && view.actions.some((action) => action === 'install' || action === 'update') && <div className="a-cx-channel">
        <PlainSelect label="Channel" value={channel} disabled={off} onChange={setChannel}
          options={[{ value: '', label: entry.discovery?.state === 'installed' ? 'Current' : 'latest' }, ...channels.map((value) => ({ value, label: value }))]} />
      </div>}
    </div>}
    {!plan && view.actions.length > 0 && <p className="a-fine">Each one is prepared first and shown to you in full; nothing runs until you approve that exact operation.</p>}
    {plan && <OperationReview plan={plan} busy={runner.busy === 'approve'} blocked={blocked} onApprove={approve} onDismiss={() => setPlan(null)} />}
    <ActionResult result={runner.result} keys={SETUP_KEYS} />
  </Card>
}
