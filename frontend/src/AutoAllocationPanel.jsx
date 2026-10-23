import { useEffect, useId, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import AutoPolicyEditor from './AutoPolicyEditor'
import { decisionLines, decisionTitle, draftFromView, draftPayload, heldChildren, policyText } from './allocationPresentation.mjs'
import { refusalMessage } from './providerPresentation.mjs'

// Inspector section: how this agent was allocated (when Auto chose it) and the Auto
// policy for its delegated children. The user's change here is the only way Auto
// is authorized; nothing switches a running agent.
export default function AutoAllocationPanel({ agent, disabled = false, refreshVersion = 0, onChanged }) {
  const id = useId()
  const taskId = agent.allocation_task_id
  const [view, setView] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(null)
  const decision = agent.allocation
  const lines = decisionLines(decision)

  useEffect(() => {
    if (!taskId) return undefined
    let active = true
    apiFetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/auto-allocation`).then(async (response) => {
      const data = await response.json().catch(() => ({}))
      if (!active) return
      if (response.ok) { setView(data); setError('') } else setError(refusalMessage(data, 'Auto allocation details are unavailable.'))
    }).catch(() => { if (active) setError('Auto allocation details are unavailable.') })
    return () => { active = false }
  }, [taskId, refreshVersion])

  // The draft is saved only on an explicit Save; a refusal (including a newer change
  // made elsewhere) keeps the draft so nothing the user entered is lost.
  async function send(method, body, query = '') {
    if (busy || disabled) return
    setBusy(true)
    setError('')
    try {
      const response = await apiFetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/auto-allocation${query}`, {
        method, ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(refusalMessage(data, 'The Auto policy could not be saved. Your changes are kept.'))
      setView(data)
      setEditing(null)
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  if (!taskId || (!decision && !view?.policy && !agent.managed && agent.agent_type === 'mock')) return null
  const policy = view?.policy ? { ...view.policy, inherited: view.inherited } : null
  const held = heldChildren(view)
  return <section className="auto-allocation" aria-labelledby={`${id}-heading`}>
    <h3 id={`${id}-heading`}>Provider allocation</h3>
    {decision && <div className={`allocation-decision allocation-${decision.status}`}>
      <p className="allocation-title">{decisionTitle(decision)}</p>
      <p className="agent-type-note">{lines.summary}</p>
      {lines.details.length > 0 && <details><summary>Why</summary>
        <ul>{lines.details.map((line) => <li key={line}>{line}</li>)}</ul></details>}
    </div>}
    <p className="agent-type-note">{policyText(policy)}</p>
    {held.length > 0 && <ul className="allocation-held" aria-label="Held children">{held.map((child) => <li key={child.proposal_id}>
      <strong>{child.proposal_id}</strong> <span>{decisionLines(child.allocation).summary}</span></li>)}</ul>}
    {held.length > 0 && <p className="agent-type-note">Held work waits; choose a provider explicitly, change the Auto policy or usage, then let the orchestration pass retry. Nothing is downgraded automatically.</p>}
    {!editing && <button type="button" className="close-button" disabled={disabled || busy || !view}
      onClick={() => { setError(''); setEditing(draftFromView(view, agent.agent_type)) }}>
      {policy && !policy.inherited ? 'Edit Auto policy' : policy ? 'Override inherited Auto policy' : 'Set Auto for delegated children'}</button>}
    {editing && <AutoPolicyEditor view={view} draft={editing} onDraft={setEditing} busy={busy} error={error}
      onSave={() => { try { send('PUT', draftPayload(editing)) } catch (err) { setError(err.message) } }} onCancel={() => { setEditing(null); setError('') }}
      onUseInherited={() => send('DELETE', null, `?expected_revision=${encodeURIComponent(view.own_revision)}`)} />}
    {!editing && error && <p className="message error" role="alert">{error}</p>}
  </section>
}
