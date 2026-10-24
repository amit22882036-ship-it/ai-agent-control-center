import { useId, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { PROVIDER_IDS, PROVIDER_NOTES, providerLabel, refusalMessage, stateLabel, stateOf, stateTone, statusFor, usageLines,
  usageSummary, windowRows } from './providerPresentation.mjs'

// Stage 2F: compact, inline provider status, usage and enablement. Installation,
// sign-in, updates and permissions live in the Stage 2G AI Providers view, which
// reuses this panel for one provider at a time.
export default function ProviderPanel({ statuses, onChanged, disabled = false, providerIds = PROVIDER_IDS, title = 'Providers and usage' }) {
  const id = useId()
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState({})

  async function act(provider, path, init, label) {
    if (busy) return
    setBusy(`${provider}:${label}`)
    setError('')
    try {
      const response = await apiFetch(`${API_BASE}/providers/${provider}${path}`, init)
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(refusalMessage(data, 'The provider could not be updated. Please retry.'))
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy('')
    }
  }
  const settings = (provider, body, label) => act(provider, '/settings', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, label)
  const working = (provider, label) => busy === `${provider}:${label}`

  return <details className="provider-panel">
    <summary>{title}</summary>
    <ul className="provider-list" aria-label="Agent providers">
      {providerIds.map((provider) => {
        const status = statusFor(statuses, provider)
        const state = stateOf(status)
        const config = status?.settings
        const ready = state === 'ready'
        const enableNeedsConfirmation = ['claude', 'gemini'].includes(provider)
        const acknowledged = config?.billing_confirmed === true
        const blocks = status?.usage?.blocks || []
        const usage = usageSummary(status)
        const rows = windowRows(status)
        return <li key={provider} className="provider-row">
          <div className="provider-row-header">
            <strong>{providerLabel(provider)}</strong>
            <span className={`provider-state provider-${stateTone(state)}`}>{stateLabel(state)}</span>
          </div>
          <p className="agent-type-note">{PROVIDER_NOTES[provider]}{status?.state?.version ? ` · ${status.state.version}` : ''}
            {provider !== 'mock' && config ? ` · ${config.enabled ? 'Enabled' : 'Disabled'}` : ''}</p>
          {!ready && status?.state?.reason && <p className="provider-reason">{status.state.reason}</p>}
          {!ready && status?.state?.guidance && <p className="agent-type-note">{status.state.guidance}</p>}
          {provider !== 'mock' && <section className="provider-usage-section" aria-label={`${providerLabel(provider)} usage`}>
            <p className="provider-usage-head"><span>Usage</span>
              <span className={`provider-state provider-${usage.tone}`}>{usage.overall}</span></p>
            <p className="agent-type-note">{usage.source}. {usage.explanation}</p>
            {rows.length > 0 && <ul className="usage-windows">{rows.map((row) => <li key={row.key} className={row.exhausted ? 'usage-exhausted' : ''}>
              <span className="usage-label">{row.label}</span>
              {row.used && <span className="usage-value">{row.used}</span>}
              {row.usedPercent !== null && <span className="usage-bar" aria-hidden="true"><span style={{ width: `${row.usedPercent}%` }} /></span>}
              <span className="usage-meta">{row.status} · {row.reset}</span>
              <span className={`usage-meta${row.stale ? ' usage-stale' : ''}`}>{row.observed}</span>
            </li>)}</ul>}
            {rows.length === 0 && <p className="agent-type-note">No usage readings{usage.refreshable ? ' yet' : ''}. Unknown usage is neither zero nor confirmed available.</p>}
            <ul className="provider-usage">{usageLines(status).map((line) => <li key={line}>{line}</li>)}</ul>
          </section>}
          {provider !== 'mock' && <div className="provider-actions">
            <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
              onClick={() => act(provider, '/check', { method: 'POST' }, 'check')}>{working(provider, 'check') ? 'Checking…' : 'Check again'}</button>
            {usage.refreshable && <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
              aria-describedby={`${id}-${provider}-refresh`}
              onClick={() => act(provider, '/usage/refresh', { method: 'POST' }, 'refresh')}>{working(provider, 'refresh') ? 'Refreshing…' : 'Refresh usage'}</button>}
            {config && !config.enabled && !enableNeedsConfirmation && <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
              onClick={() => settings(provider, { enabled: true }, 'enable')}>Enable</button>}
            {config?.enabled && <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
              onClick={() => settings(provider, { enabled: false }, 'disable')}>Disable</button>}
            {blocks.length > 0 && <button type="button" className="close-button" disabled={disabled || Boolean(busy)}
              aria-describedby={`${id}-${provider}-clear`}
              onClick={() => act(provider, '/usage/allow', { method: 'POST' }, 'clear')}>Clear recorded limit</button>}
            {status?.docs_url && <a href={status.docs_url} target="_blank" rel="noreferrer">Documentation</a>}
          </div>}
          {usage.refreshable && <p id={`${id}-${provider}-refresh`} className="agent-type-note">Uses the provider's local usage report, never a model request. It updates readings only; no agent resumes.</p>}
          {blocks.length > 0 && <p id={`${id}-${provider}-clear`} className="agent-type-note">Only after the limit has renewed. It resumes no agent, and a limit with a provider-reported reset still ahead cannot be cleared.</p>}
          {config && ((enableNeedsConfirmation && !config.enabled) || (provider === 'codex' && !acknowledged)) && <form className="provider-confirm" onSubmit={(event) => {
            event.preventDefault()
            if (confirming[provider]) settings(provider, provider === 'codex' ? { billing_confirmed: true } : { enabled: true, billing_confirmed: true }, 'confirm')
          }}>
            <label className="attestation-check"><input type="checkbox" checked={Boolean(confirming[provider])} disabled={disabled || Boolean(busy)}
              onChange={(event) => setConfirming((value) => ({ ...value, [provider]: event.target.checked }))} />
              <span>{status?.confirmation_text || 'I confirm the billing route of this provider.'}</span></label>
            <button type="submit" className="close-button" disabled={disabled || Boolean(busy) || !confirming[provider]}>
              {provider === 'codex' ? 'Acknowledge' : 'Enable'}</button>
          </form>}
          {provider !== 'mock' && acknowledged && <p className="agent-type-note">Billing {status?.acknowledgment_kind || 'confirmation'} recorded {config.billing_confirmed_at}. It is your statement, separate from the verified route.</p>}
        </li>
      })}
    </ul>
    {error && <p className="message error" role="alert">{error}</p>}
  </details>
}
