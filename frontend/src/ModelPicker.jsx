import { useId } from 'react'
import { GROUP_LABELS, OTHER_MODEL, effortChoices, freshness, groupModels, modelLimitation, optionLabel, selection } from './modelPresentation.mjs'

// Stage 2F: Provider → Model → Effort. Models come from the account-scoped
// catalog the backend discovered; unavailable ones stay visible but disabled
// with the provider's reason. The backend validates every choice again.
export default function ModelPicker({ provider, catalog, catalogError = '', refreshing = false, onRefresh, value, onChange,
  disabled = false, label = 'Model' }) {
  const id = useId()
  const groups = groupModels(catalog)
  const chosen = selection(catalog, value.choice, value.typed)
  const efforts = effortChoices(provider, catalog, chosen.model)
  const fresh = freshness(catalog)
  const limitation = modelLimitation(provider, chosen.model)
  const update = (patch) => onChange({ ...value, ...patch })

  return <div className="model-picker">
    <label htmlFor={`${id}-model`}>{label}</label>
    <select id={`${id}-model`} value={value.choice} disabled={disabled} aria-describedby={`${id}-model-help`}
      onChange={(event) => update({ choice: event.target.value, effort: '', attempt: false })}>
      <option value="">Provider default</option>
      {groups.available.length > 0 && <optgroup label={GROUP_LABELS.available}>
        {groups.available.map((model) => <option key={model.model_id} value={model.model_id}>{optionLabel(model)}</option>)}
      </optgroup>}
      {groups.unverified.length > 0 && <optgroup label={`${GROUP_LABELS.unverified} (explicit attempt)`}>
        {groups.unverified.map((model) => <option key={model.model_id} value={model.model_id}>{optionLabel(model)}</option>)}
      </optgroup>}
      {groups.unavailable.length > 0 && <optgroup label={GROUP_LABELS.unavailable}>
        {groups.unavailable.map((model) => <option key={model.model_id} value={model.model_id} disabled>{optionLabel(model)}</option>)}
      </optgroup>}
      {provider !== 'gemini' && <option value={OTHER_MODEL}>Other model identifier…</option>}
    </select>
    {value.choice === OTHER_MODEL && <input aria-label="Model identifier" value={value.typed} maxLength={128} autoComplete="off"
      placeholder="Model identifier" disabled={disabled} onChange={(event) => update({ typed: event.target.value, attempt: false })} />}
    <p id={`${id}-model-help`} className={`agent-type-note model-${chosen.kind}`}>{chosen.text}</p>
    {limitation && <p className="agent-type-note model-limitation">{limitation}</p>}
    {chosen.needsAttempt && <label className="attestation-check model-attempt">
      <input type="checkbox" checked={value.attempt === true} disabled={disabled} onChange={(event) => update({ attempt: event.target.checked })} />
      <span>Try this model explicitly. Access is not verified for this account: it runs on your approved subscription route, may use included usage, and is never retried or replaced automatically.</span>
    </label>}
    <label htmlFor={`${id}-effort`}>Effort</label>
    <select id={`${id}-effort`} value={value.effort} disabled={disabled || efforts.levels.length === 0} aria-describedby={`${id}-effort-help`}
      onChange={(event) => update({ effort: event.target.value })}>
      <option value="">Provider default</option>
      {efforts.levels.map((level) => <option key={level} value={level}>{level}</option>)}
    </select>
    <p id={`${id}-effort-help`} className="agent-type-note">{efforts.note}</p>
    {provider !== 'mock' && <div className="model-catalog-status" role="status">
      <span className={fresh.stale ? 'model-stale' : ''}>{fresh.text || 'Loading the model list…'}</span>
      {catalog && <span className={`model-entitlement${fresh.verified ? '' : ' model-unverified'}`}>
        {fresh.verified ? 'Listed for this account' : 'Account access not verified'}</span>}
      <button type="button" className="close-button" disabled={disabled || refreshing} onClick={onRefresh}>
        {refreshing ? 'Refreshing…' : 'Refresh models'}</button>
    </div>}
    {fresh.source && <p className="agent-type-note">{fresh.source}</p>}
    {groups.unavailable.length > 0 && <details className="model-unavailable">
      <summary>{groups.unavailable.length} unavailable with the current account</summary>
      <ul>{groups.unavailable.map((model) => <li key={model.model_id}><strong>{model.display_name || model.model_id}</strong>
        <span>{model.status_label}. {model.explanation}</span></li>)}</ul>
    </details>}
    {catalogError && <p className="agent-type-note provider-reason">{catalogError}</p>}
  </div>
}
