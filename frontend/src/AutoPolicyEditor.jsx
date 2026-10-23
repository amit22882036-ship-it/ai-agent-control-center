import { useId, useState } from 'react'
import useModelCatalog from './useModelCatalog'
import { AUTO_PROVIDERS, AUTO_SUMMARY, adderSelection, catalogChoices, draftErrors, formatSpec, modelMode, modelRestrictionText,
  moveItem, parseSpec, scopeText, specLabel } from './allocationPresentation.mjs'
import { providerLabel } from './providerPresentation.mjs'

const PROVIDER_DEFAULT = 'provider_default'

// One allowed provider's model allowlist, from its current catalog. "All" and "only these"
// are explicit modes: unchecking the last model never switches to "all".
function ProviderModels({ provider, names, mode, onChange, disabled }) {
  const id = useId()
  const models = useModelCatalog(provider)
  const restricted = mode === 'only'
  const choices = catalogChoices(models.catalog, names)
  const toggle = (value, on) => onChange(on ? [...names, value] : names.filter((item) => item !== value), 'only')
  return <fieldset className="policy-models">
    <legend>{providerLabel(provider)} models</legend>
    <label className="attestation-check"><input type="radio" name={`${id}-scope`} checked={!restricted} disabled={disabled}
      onChange={() => onChange([], 'all')} /><span>All otherwise eligible models are allowed</span></label>
    <label className="attestation-check"><input type="radio" name={`${id}-scope`} checked={restricted} disabled={disabled}
      onChange={() => onChange(names, 'only')} /><span>Only the models selected below</span></label>
    {restricted && <ul className="policy-model-list">
      <li><label className="attestation-check"><input type="checkbox" checked={names.includes(PROVIDER_DEFAULT)} disabled={disabled}
        onChange={(event) => toggle(PROVIDER_DEFAULT, event.target.checked)} /><span>Provider default model</span></label></li>
      {choices.map((choice) => {
        const checked = names.includes(choice.id)
        // A restricted or unknown model is never newly allowed, but a saved one stays removable.
        return <li key={choice.id}><label className={`attestation-check${choice.selectable ? '' : ' policy-unavailable'}`}>
          <input type="checkbox" checked={checked} disabled={disabled || (!choice.selectable && !checked)}
            onChange={(event) => toggle(choice.id, event.target.checked)} />
          <span>{choice.label}{choice.selectable ? '' : ` · ${choice.status}`}</span></label></li>
      })}
    </ul>}
    {restricted && !names.length && <p className="message error" role="alert">No model is selected: select at least one, or choose "All otherwise eligible models are allowed".</p>}
    <p className="agent-type-note">{restricted ? (names.length ? modelRestrictionText(names) : 'No model selected yet.') : modelRestrictionText([])}{models.catalog && !models.catalog.refresh?.fresh ? ' The model list is stale; availability is rechecked at launch.' : ''}</p>
    {models.error && <p className="agent-type-note provider-reason">{models.error}</p>}
  </fieldset>
}

// Adds one provider/model/effort entry from the provider's current catalog.
function SpecAdder({ providers, onAdd, disabled, anyModel, label }) {
  const id = useId()
  const [state, setState] = useState({ provider: providers[0] || '', model: '', effort: '' })
  const preliminary = adderSelection(providers, state)
  const models = useModelCatalog(preliminary.provider || null)
  const choices = catalogChoices(models.catalog).filter((choice) => choice.selectable)
  // Derived on every render, so a provider that stopped being allowed is never kept.
  const { provider, model, effort } = adderSelection(providers, state, models.catalog ? choices : null)
  const efforts = model ? (choices.find((choice) => choice.id === model)?.efforts || []) : []
  const setProvider = (value) => setState({ provider: value, model: '', effort: '' })
  const setModel = (value) => setState({ provider, model: value, effort: '' })
  const setEffort = (value) => setState({ provider, model, effort: value })
  if (!providers.length) return <p className="agent-type-note">Allow a provider first.</p>
  return <div className="policy-adder" role="group" aria-label={label}>
    <label htmlFor={`${id}-provider`}>Provider</label>
    <select id={`${id}-provider`} value={provider} disabled={disabled}
      onChange={(event) => setProvider(event.target.value)}>
      {providers.map((value) => <option key={value} value={value}>{providerLabel(value)}</option>)}
    </select>
    <label htmlFor={`${id}-model`}>Model</label>
    <select id={`${id}-model`} value={model} disabled={disabled || provider === 'gemini'}
      onChange={(event) => setModel(event.target.value)}>
      <option value="">{anyModel ? 'Any model' : 'Provider default model'}</option>
      {choices.map((choice) => <option key={choice.id} value={choice.id}>{choice.label}</option>)}
    </select>
    <label htmlFor={`${id}-effort`}>Effort</label>
    <select id={`${id}-effort`} value={effort} disabled={disabled || efforts.length === 0} onChange={(event) => setEffort(event.target.value)}>
      <option value="">{anyModel ? 'Any effort' : 'Default effort'}</option>
      {efforts.map((value) => <option key={value} value={value}>{value}</option>)}
    </select>
    <button type="button" className="close-button" disabled={disabled || !provider}
      onClick={() => onAdd(formatSpec({ provider, model: model || null, effort: effort || null }))}>Add</button>
  </div>
}

// An ordered list of entries with keyboard-operable reordering and removal.
function SpecList({ items, onChange, disabled, anyModel, label }) {
  if (!items.length) return <p className="agent-type-note">None.</p>
  return <ol className="policy-spec-list" aria-label={label}>
    {items.map((value, index) => <li key={`${value}-${index}`}>
      <span>{specLabel(parseSpec(value), { anyModel })}</span>
      <span className="policy-spec-actions">
        <button type="button" className="close-button" disabled={disabled || index === 0}
          aria-label={`Move ${specLabel(parseSpec(value), { anyModel })} up`} onClick={() => onChange(moveItem(items, index, -1))}>↑</button>
        <button type="button" className="close-button" disabled={disabled || index === items.length - 1}
          aria-label={`Move ${specLabel(parseSpec(value), { anyModel })} down`} onClick={() => onChange(moveItem(items, index, 1))}>↓</button>
        <button type="button" className="close-button" disabled={disabled}
          aria-label={`Remove ${specLabel(parseSpec(value), { anyModel })}`} onClick={() => onChange(items.filter((_, i) => i !== index))}>Remove</button>
      </span>
    </li>)}
  </ol>
}

// The full Auto policy editor: an isolated draft, saved only on an explicit Save.
export default function AutoPolicyEditor({ view, draft, onDraft, busy, onSave, onCancel, onUseInherited, error }) {
  const id = useId()
  const categories = view?.categories || []
  const [category, setCategory] = useState(categories[0] || 'general')
  const update = (patch) => onDraft({ ...draft, ...patch })
  const levels = draft.profile[category] || { preferred: [], acceptable: [] }
  const setLevel = (level, items) => update({ profile: { ...draft.profile, [category]: { ...levels, [level]: items } } })
  const configured = Object.entries(draft.profile).filter(([, value]) => value.preferred?.length || value.acceptable?.length)
  const errors = draftErrors(draft)
  return <form className="redirect-form auto-policy-editor" onSubmit={(event) => { event.preventDefault(); onSave() }}>
    <p className="agent-type-note policy-scope" role="status">{scopeText(view)}</p>
    <label className="attestation-check"><input type="checkbox" checked={draft.enabled} disabled={busy}
      onChange={(event) => update({ enabled: event.target.checked })} />
      <span>Auto — best suitable provider/model for delegated children</span></label>
    <p className="agent-type-note">{AUTO_SUMMARY}</p>
    {draft.enabled && <>
      <fieldset className="allocation-providers"><legend>Allowed providers</legend>
        {AUTO_PROVIDERS.map((provider) => <label key={provider} className="attestation-check">
          <input type="checkbox" checked={draft.providers.includes(provider)} disabled={busy}
            onChange={(event) => update({ providers: event.target.checked
              ? [...draft.providers, provider] : draft.providers.filter((item) => item !== provider) })} />
          <span>{providerLabel(provider)}</span></label>)}
      </fieldset>
      <p className="agent-type-note">Allowed is not the same as available: readiness, sign-in, billing and usage are checked before every launch.</p>
      <details className="policy-section"><summary>Allowed models</summary>
        {draft.providers.map((provider) => <ProviderModels key={provider} provider={provider} disabled={busy}
          names={draft.models[provider] || []} mode={modelMode(draft, provider)}
          onChange={(names, mode) => update({ models: { ...draft.models, [provider]: names },
            modes: { ...draft.modes, [provider]: mode } })} />)}
      </details>
      <details className="policy-section"><summary>Preference order ({draft.preferences.length})</summary>
        <p className="agent-type-note">Tie-breakers only, among candidates that are equally suitable for the work. They never override eligibility, your allowlists, subscription or billing limits, or a more suitable candidate.</p>
        <SpecList items={draft.preferences} anyModel disabled={busy} label="Preference order" onChange={(preferences) => update({ preferences })} />
        <SpecAdder providers={draft.providers} anyModel disabled={busy} label="Add a preference"
          onAdd={(value) => update({ preferences: [...draft.preferences, value] })} />
      </details>
      <details className="policy-section"><summary>Suitability profiles ({configured.length} configured)</summary>
        <p className="agent-type-note">Used only when the parent does not give its own assessment. Preferred candidates are considered before acceptable ones; usage only separates equally suitable candidates. This is your configured judgment, not a guarantee that a model is objectively better.</p>
        <label htmlFor={`${id}-category`}>Task category</label>
        <select id={`${id}-category`} value={category} disabled={busy} onChange={(event) => setCategory(event.target.value)}>
          {categories.map((value) => <option key={value} value={value}>{value.replace('_', ' ')}
            {draft.profile[value]?.preferred?.length || draft.profile[value]?.acceptable?.length ? ' (configured)' : ''}</option>)}
        </select>
        {['preferred', 'acceptable'].map((level) => <div key={level} className="policy-level">
          <h4>{level === 'preferred' ? 'Preferred' : 'Acceptable'}</h4>
          <SpecList items={levels[level] || []} disabled={busy} label={`${level} candidates`} onChange={(items) => setLevel(level, items)} />
          <SpecAdder providers={draft.providers} disabled={busy} label={`Add a ${level} candidate`}
            onAdd={(value) => setLevel(level, [...(levels[level] || []), value])} />
        </div>)}
      </details>
    </>}
    <div className="provider-actions">
      <button type="submit" className="close-button" disabled={busy || errors.length > 0} aria-describedby={`${id}-errors`}>
        {busy ? 'Saving…' : view?.inherited ? 'Save local override' : 'Save'}</button>
      <button type="button" className="close-button" disabled={busy} onClick={onCancel}>Cancel</button>
      {view?.own_revision && onUseInherited && <button type="button" className="close-button" disabled={busy}
        onClick={onUseInherited}>Remove this task's policy</button>}
    </div>
    {errors.length > 0 && <ul id={`${id}-errors`} className="policy-errors">{errors.map((text) => <li key={text}>{text}</li>)}</ul>}
    {view?.own_revision && <p className="agent-type-note">Removing this task's policy makes it follow its parent task's policy, if any, or no Auto at all.</p>}
    {error && <p className="message error" role="alert">{error}</p>}
  </form>
}
