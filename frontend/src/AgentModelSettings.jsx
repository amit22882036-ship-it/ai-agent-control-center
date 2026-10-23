import { useId, useState } from 'react'
import ModelPicker from './ModelPicker'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { chosenConfig, defaultsPayload, executionFacts, initialChoice, selectionReady } from './modelPresentation.mjs'
import { refusalMessage } from './providerPresentation.mjs'

// Stage 2F: one Agent's own default model and effort, its latest effective
// execution, and an explicit defaults change (applies from the next instruction).
export default function AgentModelSettings({ agent, agentId, models, disabled = false, onChanged }) {
  const id = useId()
  const [value, setValue] = useState(() => initialChoice(agent.model, agent.effort, models.catalog))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const facts = executionFacts(agent.latest_execution)
  const history = (agent.provider_history || []).filter((change) => change.boundary === 'defaults')

  async function save(event) {
    event.preventDefault()
    if (busy || disabled || !selectionReady(models.catalog, value)) return
    const config = chosenConfig(models.catalog, value)
    setBusy(true)
    setError('')
    try {
      const response = await apiFetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}/defaults`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(defaultsPayload(config.model, config.effort, config.attempt)) })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) throw new Error(refusalMessage(data, 'The defaults could not be changed. Please retry.'))
      onChanged?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return <section className="agent-model-settings" aria-labelledby={`${id}-heading`}>
    <h3 id={`${id}-heading`}>Model and effort</h3>
    <dl className="model-facts">
      <dt>Agent default</dt><dd>{agent.model || 'Provider default'} · {agent.effort ? `${agent.effort} effort` : 'provider default effort'}</dd>
      {facts.map(([label, text]) => <div key={label} className="model-fact"><dt>{label}</dt><dd>{text}</dd></div>)}
    </dl>
    {facts.length > 0 && <p className="agent-type-note">From the latest execution. Values the provider does not report are never assumed.</p>}
    <details className="provider-change">
      <summary>Change this agent's defaults</summary>
      <form className="redirect-form" onSubmit={save}>
        <p className="agent-type-note">Applies from this agent's next instruction; a running execution is never changed and other agents keep their own settings.</p>
        <ModelPicker provider={agent.agent_type} catalog={models.catalog} catalogError={models.error} refreshing={models.refreshing}
          onRefresh={models.refresh} value={value} onChange={setValue} disabled={disabled || busy} label="Default model" />
        <button type="submit" className="close-button" disabled={disabled || busy || !selectionReady(models.catalog, value)}>
          {busy ? 'Saving…' : 'Save defaults'}</button>
      </form>
      {history.length > 0 && <ul className="model-history">{history.slice(-5).map((change) => <li key={change.change_id}>
        {change.changed_at}: {change.from_model || 'provider default'} / {change.from_effort || 'default'} → {change.to_model || 'provider default'} / {change.to_effort || 'default'}
      </li>)}</ul>}
    </details>
    {error && <p className="message error" role="alert">{error}</p>}
  </section>
}
