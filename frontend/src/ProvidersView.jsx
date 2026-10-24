import { useCallback, useEffect, useId, useRef, useState } from 'react'
import Icon from './Icon'
import ModelPicker from './ModelPicker'
import ProviderPanel from './ProviderPanel'
import useModelCatalog from './useModelCatalog'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { createViewLoader } from './providersViewLoader.mjs'
import { chosenConfig, selectionReady } from './modelPresentation.mjs'
import { providerLabel, refusalMessage } from './providerPresentation.mjs'
import { ACTION_LABELS, BACKEND_UNAVAILABLE, CHANNELS, REAL_PROVIDERS, allowedText, approvalRequest, availableActions, entryFor,
  isConnectionFailure, nextStep, operationText, operationTone, planCommand, planRequest, planSource, policyDraft, policyPayload,
  readinessBadge, summaryRows } from './onboardingPresentation.mjs'

async function call(path, init = {}) {
  let response
  try {
    response = await apiFetch(`${API_BASE}${path}`, init)
  } catch (cause) {
    // No HTTP answer at all: the backend is down or restarting. This is never a provider's own
    // sign-in or setup problem, and it is reported as such.
    const error = new Error(BACKEND_UNAVAILABLE, { cause })
    error.offline = true
    throw error
  }
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(refusalMessage(data, 'The request could not be completed. Please retry.'))
    error.status = response.status
    throw error
  }
  return data
}
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

// Stage 2G: the AI Providers view. Everything that installs, updates or signs in runs in
// the backend from a fixed operation the user reviews and approves here; nothing is typed
// as a command. Opening the view probes nothing.
export default function ProvidersView({ refreshVersion = 0, onClose, focus = null }) {
  const id = useId()
  const [view, setView] = useState(null)
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState(null)  // The last failed read of the view, if any.
  const [busy, setBusy] = useState('')
  // One read at a time, backing off while the backend is unreachable (providersViewLoader). Only
  // the Retry button is a manual read that skips the backoff; live events, polling fallback,
  // operation polling and reloads after an action are automatic.
  const [loader] = useState(() => createViewLoader({
    fetchView: () => call('/providers/onboarding'),
    onData: (data) => { setView(data); setLoadError(null) },
    onError: setLoadError,
  }))
  const reload = useCallback(() => loader.request(), [loader])
  useEffect(() => {
    loader.activate()
    return () => loader.dispose()
  }, [loader])
  useEffect(() => { loader.request() }, [refreshVersion, loader])

  // While an operation runs, re-read it even when live updates are unavailable.
  const running = (view?.providers || []).some((entry) => entry.operations?.some((operation) => operation.status === 'running'))
  useEffect(() => {
    if (!running) return undefined
    const timer = setInterval(reload, 3000)
    return () => clearInterval(timer)
  }, [running, reload])

  async function act(label, path, init) {
    if (busy) return
    setBusy(label)
    setError('')
    try {
      await call(path, init)
      reload()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }

  return <section className="providers-view" aria-labelledby={`${id}-heading`}>
    <header className="workspace-heading">
      <div><p className="eyebrow">SETUP</p><h1 id={`${id}-heading`}>AI Providers</h1>
        <p className="subtitle">Install, sign in, update and choose which providers your agents may use.</p></div>
      <div className="providers-view-actions">
        <button type="button" className="close-button" disabled={Boolean(busy)}
          onClick={() => act('check', '/providers/check', { method: 'POST' })}>{busy === 'check' ? 'Checking…' : 'Check all'}</button>
        <button type="button" className="close-button" disabled={Boolean(busy)} title="Reads published release information; installs nothing"
          onClick={() => act('updates', '/providers/updates/check', { method: 'POST' })}>{busy === 'updates' ? 'Checking…' : 'Check for updates'}</button>
        <button type="button" className="start-button" onClick={onClose}>Back to workspace</button>
      </div>
    </header>
    {error && <p className="message error" role="alert">{error}</p>}
    {loadError && <div className="message error provider-unreachable" role="alert">
      <p>{isConnectionFailure(loadError) ? BACKEND_UNAVAILABLE : `AI Providers could not be loaded: ${loadError.message}`}
        {view ? ' The information below is from the last successful load and may be out of date; unsaved changes are kept.' : ''}</p>
      <button type="button" className="close-button" onClick={() => loader.retry()}>Retry</button>
    </div>}
    {!view && !loadError && <p className="message" role="status">Loading providers...</p>}
    {view && <div className={loadError ? 'providers-stale' : undefined}>
      <PermissionsEditor policy={view.policy} onSaved={reload} />
      <div className="provider-cards">
        {REAL_PROVIDERS.map((provider) => <ProviderCard key={provider} entry={entryFor(view, provider)}
          update={view.updates?.[provider]} onChanged={reload} focus={focus?.provider === provider ? focus : null} />)}
        <article className="provider-card provider-card-mock">
          <div className="provider-row-header"><h2>Mock</h2><span className="provider-state provider-ready">Ready</span></div>
          <p className="agent-type-note">Simulated work for development and testing. It needs no installation or sign-in, is never
            restricted by permissions and is never used in place of another provider.</p>
        </article>
      </div>
    </div>}
  </section>
}

function ProviderCard(props) {
  return props.entry ? <ProviderCardBody {...props} /> : null
}

function ProviderCardBody({ entry, update, onChanged, focus = null }) {
  const id = useId()
  const card = useRef(null)
  const handled = useRef(null)
  const [plan, setPlan] = useState(null)
  const [reviewed, setReviewed] = useState(false)
  const [channel, setChannel] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [ended, setEnded] = useState(false)
  const provider = entry.provider
  const label = providerLabel(provider)
  const badge = readinessBadge(entry)
  const step = nextStep(entry, update)
  const rows = summaryRows(entry, update)
  const running = entry.operations?.find((operation) => operation.status === 'running')
  const unverified = entry.operations?.find((operation) => operation.status === 'uncertain')
  const latest = entry.operations?.[0]
  const channels = CHANNELS[provider] || []

  async function run(label, task) {
    if (busy) return
    setBusy(label)
    setError('')
    try {
      await task()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }
  const review = (action) => run(action, async () => {
    setReviewed(false)
    setPlan(await call(`/providers/${provider}/operations/plan`,
      json('POST', planRequest(action, ['install', 'update'].includes(action) ? channel || null : null))))
  })
  const approve = () => run('approve', async () => {
    try {
      await call(`/providers/${provider}/operations`, json('POST', approvalRequest(plan)))
    } finally {
      setPlan(null)  // An approval is used once: a refusal needs a fresh review.
      setReviewed(false)
      onChanged()
    }
  })
  const check = () => run('check', async () => { await call(`/providers/${provider}/check`, { method: 'POST' }); onChanged() })
  const confirmEnded = () => run('resolve', async () => {
    await call(`/provider-operations/${unverified.operation_id}/resolve`, json('POST', { confirm_ended: true }))
    setEnded(false)
    onChanged()
  })
  const cancel = () => run('cancel', async () => { await call(`/provider-operations/${running.operation_id}/cancel`, { method: 'POST' }); onChanged() })

  // "Review update" from a notice: bring this card into view and prepare the update review. It only
  // builds the plan to read; nothing runs until the user approves that exact operation.
  useEffect(() => {
    if (!focus || handled.current === focus.nonce) return
    handled.current = focus.nonce
    card.current?.scrollIntoView?.({ block: 'start' })
    card.current?.querySelector('h2')?.focus()
    if (focus.action === 'update' && availableActions(entry).includes('update') && !plan && !running && !unverified) {
      setTimeout(() => review('update'), 0)  // After this render; the review itself only reads.
    }
  })

  return <article className="provider-card" aria-labelledby={`${id}-name`} ref={card}>
    <div className="provider-row-header"><h2 id={`${id}-name`} tabIndex={-1}>{label}</h2>
      <span className={`provider-state provider-${badge.tone}`}>{badge.label}</span></div>
    {step && <p className={`provider-next provider-next-${step.kind}`}><strong>Next:</strong> {step.text}</p>}
    {step?.also?.length > 0 && <ul className="provider-also" aria-label="Also needed">
      {step.also.map((item) => <li key={item.kind}>{item.text}</li>)}</ul>}
    {entry.status?.state?.reason && badge.tone !== 'ready' && entry.status.state.reason !== step?.text
      && <p className="provider-reason">{entry.status.state.reason}</p>}
    {entry.active_work?.length > 0 && <p className="agent-type-note">Running now: {entry.active_work.map((work) => work.name).join(', ')}. Updates, sign-in
      and sign-out wait until they finish; nothing is stopped for you.</p>}
    {running && <div className={`provider-operation provider-${operationTone(running)}`} role="status">
      <p>{operationText(running)}{running.action === 'login' ? ' Finish signing in in the window that opened.' : ''}</p>
      <button type="button" className="close-button" disabled={Boolean(busy)} onClick={cancel}>{busy === 'cancel' ? 'Canceling…' : 'Cancel'}</button>
    </div>}
    {!running && latest && <p className={`provider-operation-result provider-${operationTone(latest)}`}>{operationText(latest)}</p>}
    {unverified && <div className="operation-review" role="group" aria-label="Unverified setup operation">
      <p className="agent-type-note">The Control Center could not verify that this {ACTION_LABELS[unverified.action]?.toLowerCase()} and
        everything it started have ended. Close its window if one is open and wait for any installer to finish, then confirm.
        It is refused while its processes are still running.</p>
      <label className="attestation-check"><input type="checkbox" checked={ended} disabled={Boolean(busy)}
        onChange={(event) => setEnded(event.target.checked)} /><span>The setup window and installer have ended.</span></label>
      <button type="button" className="close-button" disabled={!ended || Boolean(busy)} onClick={confirmEnded}>
        {busy === 'resolve' ? 'Confirming…' : 'Confirm it ended'}</button>
    </div>}
    <dl className="provider-facts">{rows.map((row) => <div key={row.key}><dt>{row.label}</dt>
      <dd className={`provider-fact-${row.tone}`}>{row.value}</dd></div>)}</dl>
    {entry.discovery?.elsewhere?.length > 0 && <p className="agent-type-note">Also found (reported only, never run by the Control Center):
      {' '}{entry.discovery.elsewhere.join(', ')}</p>}
    {!plan && !running && !unverified && <div className="provider-actions">
      <button type="button" className={step?.kind === 'check' ? 'start-button' : 'close-button'} disabled={Boolean(busy)} onClick={check}>
        {busy === 'check' ? 'Checking…' : 'Check again'}</button>
      {availableActions(entry).map((action) => <button key={action} type="button"
        className={step?.kind === action ? 'start-button' : 'close-button'} disabled={Boolean(busy)} onClick={() => review(action)}>
        {busy === action ? 'Preparing…' : `${ACTION_LABELS[action]}…`}</button>)}
      {channels.length > 1 && <label className="provider-channel">Channel
        <select value={channel} disabled={Boolean(busy)} onChange={(event) => setChannel(event.target.value)}>
          <option value="">{entry.discovery?.state === 'installed' ? 'Current' : 'latest'}</option>
          {channels.map((value) => <option key={value} value={value}>{value}</option>)}
        </select></label>}
    </div>}
    {plan && <OperationReview plan={plan} reviewed={reviewed} onReviewed={setReviewed} busy={busy}
      onApprove={approve} onDismiss={() => { setPlan(null); setReviewed(false) }} />}
    <ProviderPanel statuses={[entry.status]} providerIds={[provider]} title="Enable, billing and usage" onChanged={onChanged}
      disabled={Boolean(busy)} />
    {error && <p className="message error" role="alert">{error}</p>}
  </article>
}

function OperationReview({ plan, reviewed, onReviewed, busy, onApprove, onDismiss }) {
  const id = useId()
  const command = planCommand(plan)
  const blocked = plan.blockers.length > 0
  return <div className="operation-review" role="group" aria-labelledby={`${id}-title`}>
    <h3 id={`${id}-title`}>{ACTION_LABELS[plan.action]} {providerLabel(plan.provider)}</h3>
    <dl className="provider-facts">
      {command && <div><dt>Runs</dt><dd><code>{command}</code></dd></div>}
      {planSource(plan) && <div><dt>Installs</dt><dd>{planSource(plan)}</dd></div>}
      <div><dt>Method</dt><dd>{plan.method.replaceAll('_', ' ')}</dd></div>
      {plan.current_version && <div><dt>Installed</dt><dd>{plan.current_version}</dd></div>}
      {plan.target && <div><dt>Target</dt><dd>{plan.target}{plan.target_version ? ` (resolved to ${plan.target_version} for this approval)` : ''}</dd></div>}
      <div><dt>Source</dt><dd><a href={plan.source} target="_blank" rel="noreferrer">{plan.source}</a></dd></div>
      {plan.console && <div><dt>Window</dt><dd>Opens the provider's own sign-in window; you sign in there and in your browser.</dd></div>}
    </dl>
    {plan.changes.length > 0 && <ul className="operation-list">{plan.changes.map((line) => <li key={line}>{line}</li>)}</ul>}
    {plan.dependencies.length > 0 && <p className="agent-type-note">Needs: {plan.dependencies.join(', ')}.</p>}
    {plan.risks.length > 0 && <ul className="operation-list operation-risks">{plan.risks.map((line) => <li key={line}>{line}</li>)}</ul>}
    {plan.manual.length > 0 && <div className="operation-manual"><strong>Manual steps</strong>
      {plan.manual.map((line) => <p key={line}>{line}</p>)}</div>}
    {blocked && <ul className="operation-list operation-blockers" role="alert">{plan.blockers.map((line) => <li key={line}>{line}</li>)}</ul>}
    {plan.plan_id && !blocked && <label className="attestation-check">
      <input type="checkbox" checked={reviewed} disabled={Boolean(busy)} onChange={(event) => onReviewed(event.target.checked)} />
      <span>I reviewed this exact operation. It runs once, without administrator rights, in its own supervised process tree, and can be canceled.</span></label>}
    <div className="provider-actions">
      {plan.plan_id && !blocked && <button type="button" className="start-button" disabled={!reviewed || Boolean(busy)} onClick={onApprove}>
        {busy === 'approve' ? 'Starting…' : `Run ${ACTION_LABELS[plan.action].toLowerCase()}`}</button>}
      <button type="button" className="close-button" disabled={busy === 'approve'} onClick={onDismiss}>{plan.plan_id && !blocked ? 'Cancel' : 'Close'}</button>
    </div>
  </div>
}

function PermissionsEditor({ policy, onSaved }) {
  const id = useId()
  // Unsaved edits; without any, the form shows the latest policy as loaded.
  const [edits, setEdits] = useState(null)
  const draft = edits || policyDraft(policy)
  const dirty = edits !== null
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [conflict, setConflict] = useState(false)
  const real = draft.defaultProvider && draft.defaultProvider !== 'mock'
  const models = useModelCatalog(real ? draft.defaultProvider : null)
  if (!policy) return <p className="message error">Provider permissions are unavailable.</p>
  const change = (patch) => setEdits({ ...draft, ...patch })
  const config = real ? chosenConfig(models.catalog, draft.pick) : {}

  async function save(event) {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError('')
    try {
      await call('/provider-policy', json('PUT', policyPayload(draft, config)))
      setEdits(null)
      setConflict(false)
      onSaved()
    } catch (err) {
      setConflict(err.status === 409 && /changed since/.test(err.message))
      setError(err.message)  // The draft is kept.
    } finally {
      setBusy(false)
    }
  }

  return <form className="provider-permissions" onSubmit={save} aria-labelledby={`${id}-heading`}>
    <h2 id={`${id}-heading`}>Permissions</h2>
    <p className="agent-type-note">Which real providers any agent may use. This is a hard limit for new agents, children, Auto and
      every later run; an empty selection allows none (Mock still runs). It is separate from installation, sign-in and Enabled.</p>
    <fieldset className="permission-choices"><legend className="sr-only">Allowed providers</legend>
      {REAL_PROVIDERS.map((provider) => <label key={provider} className="attestation-check">
        <input type="checkbox" checked={draft.allowed.includes(provider)} disabled={busy}
          onChange={(event) => change({ allowed: event.target.checked ? [...draft.allowed, provider] : draft.allowed.filter((item) => item !== provider) })} />
        <span>{providerLabel(provider)}</span></label>)}
    </fieldset>
    <p className="agent-type-note" role="status">{allowedText(draft.allowed)}</p>
    <h3>Default for new agents</h3>
    <label htmlFor={`${id}-provider`}>Provider</label>
    <select id={`${id}-provider`} value={draft.defaultProvider} disabled={busy}
      onChange={(event) => change({ defaultProvider: event.target.value, pick: policyDraft(null).pick })}>
      <option value="">No default (choose each time)</option>
      <option value="mock">Mock</option>
      {REAL_PROVIDERS.map((provider) => <option key={provider} value={provider} disabled={!draft.allowed.includes(provider)}>
        {providerLabel(provider)}{draft.allowed.includes(provider) ? '' : ' (not allowed)'}</option>)}
    </select>
    {real && <ModelPicker provider={draft.defaultProvider} catalog={models.catalog} catalogError={models.error} refreshing={models.refreshing}
      onRefresh={models.refresh} value={draft.pick} onChange={(pick) => change({ pick })} disabled={busy} label="Default model" />}
    {policy.default_problem && !dirty && <p className="provider-reason">The saved default cannot be used: {policy.default_problem} It is shown, never replaced.</p>}
    <div className="provider-actions">
      <button type="submit" className="start-button" disabled={busy || !dirty || (real && !selectionReady(models.catalog, draft.pick))}>
        {busy ? 'Saving…' : 'Save permissions'}</button>
      {dirty && <button type="button" className="close-button" disabled={busy}
        onClick={() => { if (conflict) onSaved(); setEdits(null); setConflict(false); setError('') }}>{conflict ? 'Discard and reload' : 'Discard changes'}</button>}
    </div>
    {error && <p className="message error" role="alert">{error}{conflict ? ' Your selection is kept; discard it to load the newer permissions.' : ''}</p>}
    <p className="agent-type-note"><Icon name="info" size={13} /> Task-level restrictions are set per Task in the agent inspector and can only narrow this.</p>
  </form>
}
