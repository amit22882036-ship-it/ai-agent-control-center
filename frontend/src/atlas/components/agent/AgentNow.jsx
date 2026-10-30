import { useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { TextArea } from '../controls'
import { ActionResult, Check, Disclosure, PlainSelect } from './fields'
import { InstructionOptions, ModelChoice } from './models'
import useModelCatalog from '../../../useModelCatalog'
import { agentActions, refusalText } from '../../api/agentClient.mjs'
import { formatHash } from '../../router.mjs'
import { chosenConfig, defaultNeedsAttempt, emptyOverride, initialChoice, instructionPlan, instructionReady,
  selectionReady } from '../../../modelPresentation.mjs'
import { PROVIDER_IDS, accessOptions, blockingText, changePayload, conditionText, preservedText, providerLabel,
  resumeText } from '../../../providerPresentation.mjs'

// A draft remembers the agent it was written for. If the task's agent changes before it is sent,
// the first Send is refused with an explanation and the next one addresses the new agent.
function useDraft(scope, kind) {
  const draft = scope.drafts.get(kind)
  return [draft, (text, agentId) => scope.drafts.set(kind, { text, for: text ? draft.for || agentId : null }),
    () => scope.drafts.set(kind, { text: '', for: null }), (agentId) => scope.drafts.set(kind, { ...draft, for: agentId })]
}

function changedAgentMessage(scope) {
  return `This task’s agent changed while you were writing. Nothing was sent. Review your text; sending again addresses ${scope.agentName}.`
}

// Sends a reply or a redirect exactly as the classic inspector does: an explicit defaults change
// first (only when asked), then the instruction with its one-time fields.
async function sendInstruction(scope, kind, text, override) {
  const plan = instructionPlan(scope.detail, scope.models.catalog, override)
  if (plan.defaults) {
    const saved = await agentActions.saveDefaults(scope.agentId, plan.defaults)
    if (!saved.ok) return { ok: false, message: refusalText(saved, 'The agent defaults could not be changed.') }
  }
  const result = kind === 'reply' ? await agentActions.reply(scope.agentId, text, plan.fields) : await agentActions.redirect(scope.agentId, text, plan.fields)
  if (!result.ok) {
    return { ok: false, message: refusalText(result, kind === 'reply' ? 'Unable to send reply. Please try again.' : 'Unable to redirect agent. Please try again.') }
  }
  return { ok: true, message: kind === 'reply' ? 'Reply sent. The agent continues in the same session.' : 'Redirect sent. The agent continues with your correction.' }
}

export function QuestionCard({ scope }) {
  const { caps, detail, runner } = scope
  const [draft, setText, clearDraft, retarget] = useDraft(scope, 'reply')
  const [override, setOverride] = useState(emptyOverride)
  const busy = Boolean(runner.busy)
  const ready = draft.text.trim() && instructionReady(detail, scope.models.catalog, override)

  async function reply(event) {
    event.preventDefault()
    if (busy || !ready) return
    if (draft.for && draft.for !== scope.agentId) {
      retarget(scope.agentId)
      runner.run('reply', async () => ({ ok: false, message: changedAgentMessage(scope), changed: false }))
      return
    }
    const outcome = await runner.run('reply', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      return sendInstruction(scope, 'reply', draft.text, override)
    })
    if (outcome?.ok) { clearDraft(); setOverride(emptyOverride()) }
  }
  const simple = (key, call, success, fallback) => runner.run(key, async () => {
    const stale = scope.guard()
    if (stale) return { ok: false, message: stale, changed: false }
    const result = await call()
    return result.ok ? { ok: true, message: success } : { ok: false, message: refusalText(result, fallback) }
  })

  return <section className="a-qcard a-qcard--form" aria-labelledby={`q-${scope.agentId}`}>
    <div className="a-qh" id={`q-${scope.agentId}`}><Icon name="hand" size={15} stroke={2} />{scope.agentName} asked you</div>
    <p className="a-qt a-bidi" dir="auto">{detail.waiting_question || 'The question text was not recorded.'}</p>
    <form onSubmit={reply}>
      <TextArea label="Your answer" rows={3} value={draft.text} disabled={busy} required
        onChange={(event) => setText(event.target.value, scope.agentId)} />
      {caps.instructionOptions && <InstructionOptions agent={detail} models={scope.models} value={override} onChange={setOverride} disabled={busy} />}
      <div className="a-act-row">
        <Button type="submit" variant="ask" icon="reply" disabled={busy || !ready} aria-busy={runner.busy === 'reply' || undefined}>
          {runner.busy === 'reply' ? 'Sending…' : 'Send reply'}</Button>
        <Button disabled={busy} aria-describedby={`decide-help-${scope.agentId}`}
          onClick={() => simple('decide', () => agentActions.decide(scope.agentId), 'The agent decides this one itself and continues.',
            'Unable to delegate this decision. Please try again.')}>
          {runner.busy === 'decide' ? 'Deciding…' : 'Decide for me'}</Button>
      </div>
      <p className="a-fine" id={`decide-help-${scope.agentId}`}>Decide for me lets the agent make this decision itself and continue. It applies only this time.</p>
      <Disclosure summary="Delegate future decisions">
        <div className="a-policy-choice">
          <Button size="sm" disabled={busy} onClick={() => simple('similar-on', () => agentActions.similar(scope.agentId, true),
            'Similar decisions are now made automatically for this agent.', 'Unable to change similar decisions. Please try again.')}>
            {runner.busy === 'similar-on' ? 'Enabling…' : 'Decide similar automatically'}</Button>
          <p className="a-fine">Let the agent handle this decision and materially similar decisions for this agent automatically.</p>
        </div>
        {caps.enableAlways && <div className="a-policy-choice">
          <Button size="sm" disabled={busy} onClick={() => simple('always-on', () => agentActions.always(scope.agentId, true),
            'Always decide is on for this agent.', 'Unable to change Always Decide. Please try again.')}>
            {runner.busy === 'always-on' ? 'Enabling…' : 'Always decide for this agent'}</Button>
          <p className="a-fine">Let this agent make future decisions itself when possible. It can still ask when genuinely missing information.</p>
        </div>}
      </Disclosure>
    </form>
    <ActionResult result={runner.result} keys={['reply', 'decide', 'similar-on', 'always-on']} />
  </section>
}

export function RedirectCard({ scope }) {
  const { caps, detail, runner } = scope
  const [draft, setText, clearDraft, retarget] = useDraft(scope, 'redirect')
  const [override, setOverride] = useState(emptyOverride)
  const busy = Boolean(runner.busy)
  if (!caps.redirect) return null
  if (!caps.redirectReady) return <p className="a-fine a-blk">Redirect will be available once the provider session starts.</p>
  const ready = draft.text.trim() && instructionReady(detail, scope.models.catalog, override)
  async function redirect(event) {
    event.preventDefault()
    if (busy || !ready) return
    if (draft.for && draft.for !== scope.agentId) {
      retarget(scope.agentId)
      runner.run('redirect', async () => ({ ok: false, message: changedAgentMessage(scope), changed: false }))
      return
    }
    const outcome = await runner.run('redirect', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      return sendInstruction(scope, 'redirect', draft.text, override)
    })
    if (outcome?.ok) { clearDraft(); setOverride(emptyOverride()) }
  }
  return <section className="a-blk" aria-labelledby={`redirect-${scope.agentId}`}>
    <h3 id={`redirect-${scope.agentId}`}>Redirect</h3>
    <Disclosure summary="Send a correction to this agent" className="a-disclosure--card">
      <form onSubmit={redirect}>
        <TextArea label="Correction" rows={3} value={draft.text} disabled={busy} required placeholder="Describe the correction for this agent"
          onChange={(event) => setText(event.target.value, scope.agentId)} />
        {caps.instructionOptions && <InstructionOptions agent={detail} models={scope.models} value={override} onChange={setOverride} disabled={busy} />}
        <div className="a-act-row">
          <Button type="submit" variant="primary" icon="redirect" disabled={busy || !ready} aria-busy={runner.busy === 'redirect' || undefined}>
            {runner.busy === 'redirect' ? 'Sending…' : 'Send correction'}</Button>
        </div>
        <p className="a-fine">The agent continues in the same session with your correction.</p>
      </form>
    </Disclosure>
    <ActionResult result={runner.result} keys={['redirect']} />
  </section>
}

// A provider limit, sign-in or availability wait, and the explicit Resume (classic words and rules).
export function ProviderWaitCard({ scope }) {
  const { detail, runner } = scope
  const [explicitRetry, setExplicitRetry] = useState(false)
  const [defaultAttempt, setDefaultAttempt] = useState(false)
  const busy = Boolean(runner.busy)
  const condition = detail.provider_condition
  const resume = detail.resume
  const defaultUnverified = defaultNeedsAttempt(detail, scope.models.catalog, Boolean(scope.models.error))
  const blocked = !resume?.available || (resume.mode === 'explicit_retry' && !explicitRetry) || (defaultUnverified && !defaultAttempt)
  const resets = (condition?.blocking || []).filter((item) => item.reset_at)
  const call = (key, request, success) => runner.run(key, async () => {
    const stale = key === 'refresh-usage' ? null : scope.guard()
    if (stale) return { ok: false, message: stale, changed: false }
    const result = await request()
    return result.ok ? { ok: true, message: success } : { ok: false, message: refusalText(result, 'The request could not be completed. Please retry.') }
  })
  return <section className="a-wait-card" aria-labelledby={`wait-${scope.agentId}`}>
    <h3 id={`wait-${scope.agentId}`}>{condition?.label || detail.waiting_context?.label || 'Waiting for the provider'}</h3>
    <dl className="a-kv">
      <dt>Provider</dt><dd>{providerLabel(condition?.provider || detail.agent_type)}{condition?.model || detail.model ? ` · ${condition?.model || detail.model}` : ''}</dd>
      {condition?.kind === 'usage_limited' && <><dt>Blocking</dt><dd>{blockingText(condition)}</dd></>}
      {resets.length > 0 && <><dt>Reset</dt><dd>{resets.map((item) => `${item.label}: ${item.reset_at}`).join('; ')} (provider-reported)</dd></>}
      {condition?.observed_at && <><dt>Recorded</dt><dd>{condition.observed_at}</dd></>}
    </dl>
    {condition && <p>{conditionText(condition)}</p>}
    <p className="a-fine">{preservedText(condition, resume)}</p>
    {resume?.blocking?.some((item) => item.fresh === false) && <p className="a-fine">The last usage reading is stale and is not shown as current.</p>}
    <p className={`a-fine${resume && !resume.available ? ' a-tone-alert' : ''}`} role="status">{resumeText(resume) || 'Resume availability has not been reported yet.'}</p>
    {resume?.available && resume.mode === 'explicit_retry' && <Check tone="warn" checked={explicitRetry} disabled={busy} onChange={setExplicitRetry}>
      Retry explicitly: availability is not guaranteed and the provider may report the limit again.</Check>}
    {defaultUnverified && <Check tone="warn" checked={defaultAttempt} disabled={busy} onChange={setDefaultAttempt}>
      The agent’s default model is not verified for this account. Try it explicitly; it is never retried or replaced automatically.</Check>}
    <div className="a-act-row">
      <Button variant="primary" icon="play" disabled={busy || blocked} aria-busy={runner.busy === 'resume' || undefined}
        onClick={() => call('resume', () => agentActions.resume(scope.agentId, { explicit_retry: resume?.mode === 'explicit_retry' && explicitRetry,
          ...(defaultUnverified && defaultAttempt ? { attempt_unverified: true } : {}) }),
        'Resume requested. Every readiness, billing and admission check runs again first.')}>
        {runner.busy === 'resume' ? 'Resuming…' : 'Resume task'}</Button>
      {condition?.provider === 'claude' && <Button disabled={busy} icon="refresh"
        onClick={() => call('refresh-usage', () => agentActions.refreshClaudeUsage(), 'Usage refreshed from Claude Code.')}>
        {runner.busy === 'refresh-usage' ? 'Refreshing…' : 'Refresh usage'}</Button>}
    </div>
    {(condition?.provider || detail.agent_type) !== 'mock' && <p className="a-fine">Usage, limits and sign-in of {providerLabel(condition?.provider || detail.agent_type)}:{' '}
      <a href={formatHash('connections', { provider: condition?.provider || detail.agent_type, section: 'usage' })}>open it in Connections</a>.</p>}
    <ActionResult result={runner.result} keys={['resume', 'refresh-usage']} />
  </section>
}

// Switching provider, model or access while the agent waits at a safe boundary (classic rules).
export function ProviderChange({ scope }) {
  const { detail, runner } = scope
  const [provider, setProvider] = useState(detail.agent_type === 'mock' ? 'codex' : detail.agent_type)
  const [pick, setPick] = useState(() => initialChoice(detail.model, detail.effort))
  const [sandbox, setSandbox] = useState(detail.sandbox || 'read-only')
  const target = useModelCatalog(provider)
  const busy = Boolean(runner.busy)
  const ready = selectionReady(target.catalog, pick)
  function change(event) {
    event.preventDefault()
    if (busy || !ready) return
    runner.run('provider', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      const config = chosenConfig(target.catalog, pick)
      const result = await agentActions.changeProvider(scope.agentId, changePayload(provider, config.model || '', sandbox, { effort: config.effort, attempt: config.attempt }))
      return result.ok ? { ok: true, message: 'Provider and model changed. Nothing starts until you continue or reply.' }
        : { ok: false, message: refusalText(result, 'The request could not be completed. Please retry.') }
    })
  }
  return <section className="a-blk" aria-labelledby={`switch-${scope.agentId}`}>
    <h3 id={`switch-${scope.agentId}`}>Tool and model</h3>
    <Disclosure summary="Switch tool or model" className="a-disclosure--card">
      <form onSubmit={change}>
        <p className="a-fine">Allowed only while the agent waits. A different provider starts a new session with the task and workspace as context;
          the previous provider’s internal memory is not transferred. Access can stay or narrow, never widen. Nothing starts until you continue or reply.</p>
        <PlainSelect label="Provider" value={provider} disabled={busy}
          onChange={(value) => { setProvider(value); setPick(initialChoice()) }}
          options={PROVIDER_IDS.filter((value) => value !== 'mock').map((value) => ({ value, label: providerLabel(value) }))} />
        <ModelChoice provider={provider} models={target} value={pick} onChange={setPick} disabled={busy} />
        <PlainSelect label="Access" value={sandbox} disabled={busy} onChange={setSandbox}
          options={accessOptions(detail.sandbox).map((value) => ({ value, label: value === 'read-only' ? 'Read only' : 'Workspace write' }))} />
        <div className="a-act-row">
          <Button type="submit" disabled={busy || !ready} aria-busy={runner.busy === 'provider' || undefined}>{runner.busy === 'provider' ? 'Changing…' : 'Change'}</Button>
        </div>
      </form>
    </Disclosure>
    <ActionResult result={runner.result} keys={['provider']} />
  </section>
}
