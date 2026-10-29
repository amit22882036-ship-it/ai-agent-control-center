import { useId } from 'react'
import { Button } from '../primitives'
import { TextField } from '../controls'
import { Check, GroupedSelect, PlainSelect } from './fields'
import { GROUP_LABELS, OTHER_MODEL, chosenConfig, defaultNeedsAttempt, effortChoices, freshness, groupModels, initialChoice,
  modelLimitation, newSessionLikely, optionLabel, selection } from '../../../modelPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'

// Provider → model → effort, from the account-scoped catalog the backend discovered (the classic
// picker's rules and words). Unavailable models stay visible but cannot be chosen; an unverified
// one needs an explicit attempt. The backend validates every choice again.
export function ModelChoice({ provider, models, value, onChange, disabled, label = 'Model' }) {
  const catalog = models.catalog
  const groups = groupModels(catalog)
  const chosen = selection(catalog, value.choice, value.typed)
  const efforts = effortChoices(provider, catalog, chosen.model)
  const fresh = freshness(catalog)
  const limitation = modelLimitation(provider, chosen.model)
  const update = (patch) => onChange({ ...value, ...patch })
  const items = (list, disabledAll = false) => list.map((model) => ({ value: model.model_id, label: optionLabel(model), disabled: disabledAll }))
  return <div className="a-modelchoice">
    <GroupedSelect label={label} value={value.choice} disabled={disabled}
      onChange={(choice) => update({ choice, effort: '', attempt: false })}
      hint={chosen.text} hintTone={chosen.kind === 'unavailable' ? 'alert' : chosen.needsAttempt ? 'warn' : undefined}
      before={[{ value: '', label: 'Provider default' }]}
      groups={[{ label: GROUP_LABELS.available, items: items(groups.available) },
        { label: `${GROUP_LABELS.unverified} (explicit attempt)`, items: items(groups.unverified) },
        { label: GROUP_LABELS.unavailable, items: items(groups.unavailable, true) }]}
      after={provider !== 'gemini' ? [{ value: OTHER_MODEL, label: 'Other model identifier…' }] : []} />
    {value.choice === OTHER_MODEL && <TextField label="Model identifier" value={value.typed} maxLength={128} autoComplete="off"
      placeholder="Model identifier" disabled={disabled} mono onChange={(event) => update({ typed: event.target.value, attempt: false })} />}
    {limitation && <p className="a-fine">{limitation}</p>}
    {chosen.needsAttempt && <Check tone="warn" checked={value.attempt} disabled={disabled} onChange={(attempt) => update({ attempt })}>
      Try this model explicitly. Access is not verified for this account: it runs on your approved subscription route, may use
      included usage, and is never retried or replaced automatically.</Check>}
    <PlainSelect label="Effort" value={value.effort} disabled={disabled || efforts.levels.length === 0} hint={efforts.note}
      onChange={(effort) => update({ effort })}
      options={[{ value: '', label: 'Provider default' }, ...efforts.levels.map((level) => ({ value: level, label: level }))]} />
    {provider !== 'mock' && <div className="a-catalog" role="status">
      <span className={fresh.stale ? 'a-catalog-stale' : undefined}>{fresh.text || 'Loading the model list…'}</span>
      {catalog && <span className="a-catalog-access">{fresh.verified ? 'Listed for this account' : 'Account access not verified'}</span>}
      <Button size="sm" variant="ghost" icon="refresh" disabled={disabled || models.refreshing} onClick={models.refresh}>
        {models.refreshing ? 'Refreshing…' : 'Refresh models'}</Button>
    </div>}
    {fresh.source && <p className="a-fine">{fresh.source}</p>}
    {groups.unavailable.length > 0 && <details className="a-disclosure a-disclosure--quiet">
      <summary><span>{groups.unavailable.length} unavailable with the current account</span></summary>
      <ul className="a-plain-list">{groups.unavailable.map((model) => <li key={model.model_id}><b>{model.display_name || model.model_id}</b>
        <span className="a-fine"> {model.status_label}. {model.explanation}</span></li>)}</ul>
    </details>}
    {models.error && <p className="a-fine a-tone-alert">{models.error}</p>}
  </div>
}

// The next instruction runs with the agent's defaults, or with a one-time override that never
// changes them unless explicitly saved (the classic rules, words and checkboxes).
export function InstructionOptions({ agent, models, value, onChange, disabled }) {
  const id = useId()
  const provider = agent.agent_type
  if (provider === 'mock') return null
  const update = (patch) => onChange({ ...value, ...patch })
  const requested = value.enabled ? chosenConfig(models.catalog, value.picker) : { model: agent.model || null, effort: agent.effort || null }
  const newSession = newSessionLikely(provider, agent.session_config, [requested.model, requested.effort], models.catalog)
  const defaultUnverified = defaultNeedsAttempt(agent, models.catalog, Boolean(models.error))
  return <div className="a-instr" id={id}>
    <Check checked={value.enabled} disabled={disabled}
      onChange={(enabled) => update({ enabled, picker: initialChoice(agent.model, agent.effort, models.catalog) })}>
      Use a different model or effort for this instruction only</Check>
    {!value.enabled && <p className="a-fine">Runs with this agent’s defaults: {agent.model || 'provider default model'}, {agent.effort ? `${agent.effort} effort` : 'provider default effort'}.</p>}
    {!value.enabled && defaultUnverified && <Check tone="warn" checked={value.defaultAttempt === true} disabled={disabled}
      onChange={(defaultAttempt) => update({ defaultAttempt })}>
      The default model is not verified for this account. Try it explicitly for this instruction; it is never retried or replaced automatically.</Check>}
    {value.enabled && <div className="a-instr-body">
      <ModelChoice provider={provider} models={models} value={value.picker} onChange={(picker) => update({ picker })} disabled={disabled}
        label="Model for this instruction" />
      <Check checked={value.saveDefault} disabled={disabled} onChange={(saveDefault) => update({ saveDefault })}>
        Also make this the agent’s default from now on</Check>
    </div>}
    {newSession && <Check tone="warn" checked={value.confirmNewSession} disabled={disabled} onChange={(confirmNewSession) => update({ confirmNewSession })}>
      {providerLabel(provider)} cannot continue the current session with this model or effort. Start a new session: it receives the task and
      workspace as context; the previous session memory is not carried over.</Check>}
  </div>
}
