import { useId, useState } from 'react'
import ModelPicker from './ModelPicker'
import useModelCatalog from './useModelCatalog'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { chosenConfig, defaultNeedsAttempt, initialChoice, selectionReady } from './modelPresentation.mjs'
import { PROVIDER_IDS, accessOptions, blockingText, changeEligible, changePayload, conditionText, preservedText, providerLabel,
  refusalMessage, resumeText } from './providerPresentation.mjs'

// Stage 2F: a provider wait (usage, sign-in, availability) and explicit
// provider/model changes at a safe boundary, inline in the inspector.
export default function ProviderControls({ agent, agentId, models = null, disabled = false, onChanged }) {
  const id = useId()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [provider, setProvider] = useState(agent.agent_type === 'mock' ? 'codex' : agent.agent_type)
  const [pick, setPick] = useState(() => initialChoice(agent.model, agent.effort))
  const [defaultAttempt, setDefaultAttempt] = useState(false)
  const target = useModelCatalog(changeEligible(agent) ? provider : null)
  const [sandbox, setSandbox] = useState(agent.sandbox || 'read-only')
  const [explicitRetry, setExplicitRetry] = useState(false)
  const waiting = agent.status === 'waiting' && agent.waiting_context?.kind === 'provider'
  const condition = agent.provider_condition
  const resume = agent.resume
  // A default model whose access is not verified needs an explicit attempt with every instruction.
  const defaultUnverified = defaultNeedsAttempt(agent, models?.catalog, Boolean(models?.error))

  async function post(path, body, label, base = `${API_BASE}/agents/${encodeURIComponent(agentId)}`) {
    if (busy || disabled) return
    setBusy(label)
    setError('')
    try {
      const response = await apiFetch(`${base}${path}`, {
        method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(refusalMessage(data, 'The request could not be completed. Please retry.'))
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  if (!waiting && !changeEligible(agent)) return null
  return <section className="provider-controls" aria-labelledby={`${id}-heading`}>
    {waiting && <>
      <h3 id={`${id}-heading`}>{condition?.label || agent.waiting_context.label}</h3>
      <dl className="provider-wait-facts">
        <dt>Provider</dt><dd>{providerLabel(condition?.provider || agent.agent_type)}{condition?.model || agent.model ? ` · ${condition?.model || agent.model}` : ''}</dd>
        {condition?.kind === 'usage_limited' && <><dt>Blocking</dt><dd>{blockingText(condition)}</dd></>}
        {condition?.blocking?.some((item) => item.reset_at) && <><dt>Reset</dt><dd>{condition.blocking.filter((item) => item.reset_at)
          .map((item) => `${item.label}: ${item.reset_at}`).join('; ')} (provider-reported)</dd></>}
        {condition?.observed_at && <><dt>Recorded</dt><dd>{condition.observed_at}</dd></>}
      </dl>
      <p>{conditionText(condition)}</p>
      <p className="agent-type-note">{preservedText(condition, resume)}</p>
      {resume?.blocking?.some((item) => item.fresh === false) && <p className="agent-type-note">The last usage reading is stale and is not shown as current.</p>}
      <p className={`agent-type-note${resume && !resume.available ? ' provider-reason' : ''}`} role="status">{resumeText(resume)}</p>
      <div className="provider-actions">
        {condition?.provider === 'claude' && <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
          onClick={() => post('/claude/usage/refresh', null, 'refresh', `${API_BASE}/providers`)}>{busy === 'refresh' ? 'Refreshing…' : 'Refresh usage'}</button>}
        {resume?.available && resume.mode === 'explicit_retry' && <label className="attestation-check">
          <input type="checkbox" checked={explicitRetry} disabled={disabled || Boolean(busy)} onChange={(event) => setExplicitRetry(event.target.checked)} />
          <span>Retry explicitly: availability is not guaranteed and the provider may report the limit again.</span></label>}
        {defaultUnverified && <label className="attestation-check model-attempt">
          <input type="checkbox" checked={defaultAttempt} disabled={disabled || Boolean(busy)} onChange={(event) => setDefaultAttempt(event.target.checked)} />
          <span>The agent's default model is not verified for this account. Try it explicitly; it is never retried or replaced automatically.</span></label>}
        <button type="button" className="start-button" disabled={disabled || Boolean(busy) || !resume?.available
          || (resume.mode === 'explicit_retry' && !explicitRetry) || (defaultUnverified && !defaultAttempt)}
          onClick={() => post('/resume', { explicit_retry: resume?.mode === 'explicit_retry' && explicitRetry,
            ...(defaultUnverified && defaultAttempt ? { attempt_unverified: true } : {}) }, 'resume')}>
          {busy === 'resume' ? 'Resuming…' : 'Resume task'}</button>
      </div>
    </>}
    {!waiting && <h3 id={`${id}-heading`}>Provider</h3>}
    {changeEligible(agent) && <details className="provider-change">
      <summary>Change provider or model</summary>
      <form className="redirect-form" onSubmit={(event) => {
        event.preventDefault()
        const config = chosenConfig(target.catalog, pick)
        post('/provider', changePayload(provider, config.model || '', sandbox, { effort: config.effort, attempt: config.attempt }), 'change')
      }}>
        <p className="agent-type-note">Allowed only while the agent waits. A different provider starts a new session with the task and workspace as context; the previous provider's internal memory is not transferred. Access can stay or narrow, never widen. Nothing starts until you continue or reply.</p>
        <label htmlFor={`${id}-provider`}>Provider</label>
        <select id={`${id}-provider`} value={provider} disabled={disabled || Boolean(busy)}
          onChange={(event) => { setProvider(event.target.value); setPick(initialChoice()) }}>
          {PROVIDER_IDS.filter((value) => value !== 'mock').map((value) => <option key={value} value={value}>{providerLabel(value)}</option>)}
        </select>
        <ModelPicker provider={provider} catalog={target.catalog} catalogError={target.error} refreshing={target.refreshing}
          onRefresh={target.refresh} value={pick} onChange={setPick} disabled={disabled || Boolean(busy)} />
        <label htmlFor={`${id}-access`}>Access</label>
        <select id={`${id}-access`} value={sandbox} disabled={disabled || Boolean(busy)} onChange={(event) => setSandbox(event.target.value)}>
          {accessOptions(agent.sandbox).map((value) => <option key={value} value={value}>{value === 'read-only' ? 'Read only' : 'Workspace write'}</option>)}
        </select>
        <button type="submit" className="close-button" disabled={disabled || Boolean(busy) || !selectionReady(target.catalog, pick)}>{busy === 'change' ? 'Changing…' : 'Change'}</button>
      </form>
    </details>}
    {error && <p className="message error" role="alert">{error}</p>}
  </section>
}
