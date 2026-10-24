import { useEffect, useId, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { providerLabel, refusalMessage } from './providerPresentation.mjs'
import { REAL_PROVIDERS, restrictionPayload, restrictionText } from './onboardingPresentation.mjs'

// Inspector section (Stage 2G): the hard provider permission of this agent's Task subtree.
// It narrows the global permission and any ancestor restriction; it never widens them,
// and it is separate from the Auto allocation policy.
export default function ProviderRestrictions({ agent, disabled = false, refreshVersion = 0, onChanged, onOpenProviders }) {
  const id = useId()
  const taskId = agent.allocation_task_id
  const permission = agent.provider_permission
  const [view, setView] = useState(null)
  const [selected, setSelected] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!taskId) return undefined
    let active = true
    apiFetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/provider-restrictions`).then(async (response) => {
      const data = await response.json().catch(() => ({}))
      if (!active) return
      if (response.ok) { setView(data); setError('') } else setError(refusalMessage(data, 'Provider restrictions are unavailable.'))
    }).catch(() => { if (active) setError('Provider restrictions are unavailable.') })
    return () => { active = false }
  }, [taskId, refreshVersion])

  async function send(method, body, query = '') {
    if (busy || disabled) return
    setBusy(true)
    setError('')
    try {
      const response = await apiFetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/provider-restrictions${query}`, {
        method, ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(refusalMessage(data, 'The restriction could not be saved. Your selection is kept.'))
      setView(data)
      setSelected(null)
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!taskId) return null
  const inherited = view?.inherited || []
  return <section className="auto-allocation provider-restrictions" aria-labelledby={`${id}-heading`}>
    <h3 id={`${id}-heading`}>Provider permissions</h3>
    {permission && !permission.allowed && <p className="provider-reason" role="status">{permission.reason}</p>}
    {permission && !permission.allowed && onOpenProviders && <button type="button" className="close-button provider-setup-link"
      onClick={onOpenProviders}>Review in AI Providers</button>}
    <p className="agent-type-note">{view ? restrictionText(view) : 'Loading…'}</p>
    {view && selected === null && <div className="provider-actions">
      <button type="button" className="close-button" disabled={disabled || busy}
        onClick={() => setSelected(view.own ? [...view.own.providers] : [...inherited])}>
        {view.own ? 'Edit restriction' : 'Restrict providers for this Task'}</button>
      {view.own && <button type="button" className="close-button" disabled={disabled || busy}
        onClick={() => send('DELETE', null, `?expected_revision=${encodeURIComponent(view.own.revision)}`)}>Remove restriction</button>}
    </div>}
    {view && selected !== null && <form onSubmit={(event) => { event.preventDefault(); send('PUT', restrictionPayload(selected, view.own)) }}>
      <p className="agent-type-note">Applies to this Task and every Task below it, for new launches, children, Auto, retries and
        resumes. Running work is not stopped. Only providers allowed above this Task can be chosen.</p>
      <div className="permission-choices" role="group" aria-label="Providers allowed for this Task">
        {REAL_PROVIDERS.map((provider) => <label key={provider} className="attestation-check">
          <input type="checkbox" checked={selected.includes(provider)} disabled={busy || !inherited.includes(provider)}
            onChange={(event) => setSelected(event.target.checked ? [...selected, provider] : selected.filter((item) => item !== provider))} />
          <span>{providerLabel(provider)}{inherited.includes(provider) ? '' : ' (not allowed above this Task)'}</span></label>)}
      </div>
      <div className="provider-actions">
        <button type="submit" className="start-button" disabled={busy}>{busy ? 'Saving…' : 'Save restriction'}</button>
        <button type="button" className="close-button" disabled={busy} onClick={() => { setSelected(null); setError('') }}>Cancel</button>
      </div>
    </form>}
    {error && <p className="message error" role="alert">{error}</p>}
  </section>
}
