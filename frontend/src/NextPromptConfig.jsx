import ModelPicker from './ModelPicker'
import { chosenConfig, defaultNeedsAttempt, initialChoice, newSessionLikely } from './modelPresentation.mjs'
import { providerLabel } from './providerPresentation.mjs'

// Stage 2F: the next instruction runs with the Agent's defaults, or with a
// one-time override that never changes them unless explicitly saved.
export default function NextPromptConfig({ agent, models, value, onChange, disabled = false }) {
  const provider = agent.agent_type
  if (provider === 'mock') return null
  const update = (patch) => onChange({ ...value, ...patch })
  const requested = value.enabled ? chosenConfig(models.catalog, value.picker) : { model: agent.model || null, effort: agent.effort || null }
  const newSession = newSessionLikely(provider, agent.session_config, [requested.model, requested.effort], models.catalog)
  const defaultUnverified = defaultNeedsAttempt(agent, models.catalog, Boolean(models.error))

  return <div className="next-prompt-config">
    <label className="attestation-check">
      <input type="checkbox" checked={value.enabled} disabled={disabled}
        onChange={(event) => update({ enabled: event.target.checked, picker: initialChoice(agent.model, agent.effort, models.catalog) })} />
      <span>Use a different model or effort for this instruction only</span>
    </label>
    {!value.enabled && <p className="agent-type-note">Runs with this agent's defaults: {agent.model || 'provider default model'}, {agent.effort ? `${agent.effort} effort` : 'provider default effort'}.</p>}
    {!value.enabled && defaultUnverified && <label className="attestation-check model-attempt">
      <input type="checkbox" checked={value.defaultAttempt === true} disabled={disabled} onChange={(event) => update({ defaultAttempt: event.target.checked })} />
      <span>The default model is not verified for this account. Try it explicitly for this instruction; it is never retried or replaced automatically.</span>
    </label>}
    {value.enabled && <>
      <ModelPicker provider={provider} catalog={models.catalog} catalogError={models.error} refreshing={models.refreshing}
        onRefresh={models.refresh} value={value.picker} onChange={(picker) => update({ picker })} disabled={disabled} label="Model for this instruction" />
      <label className="attestation-check">
        <input type="checkbox" checked={value.saveDefault} disabled={disabled} onChange={(event) => update({ saveDefault: event.target.checked })} />
        <span>Also make this the agent's default from now on</span>
      </label>
    </>}
    {newSession && <label className="attestation-check model-new-session">
      <input type="checkbox" checked={value.confirmNewSession} disabled={disabled} onChange={(event) => update({ confirmNewSession: event.target.checked })} />
      <span>{providerLabel(provider)} cannot continue the current session with this model or effort. Start a new session: it receives the task and workspace as context; the previous session memory is not carried over.</span>
    </label>}
  </div>
}
