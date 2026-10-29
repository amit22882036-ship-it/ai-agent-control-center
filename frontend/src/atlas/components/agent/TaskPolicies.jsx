import { useId, useState } from 'react'
import { Button, Notice } from '../primitives'
import { ActionResult, Check, Disclosure, PlainSelect } from './fields'
import useModelCatalog from '../../../useModelCatalog'
import { useLiveResource } from '../../hooks'
import { API_BASE } from '../../../apiClient.mjs'
import { agentActions, refusalText } from '../../api/agentClient.mjs'
import { AUTO_PROVIDERS, AUTO_SUMMARY, adderSelection, catalogChoices, decisionLines, decisionTitle, draftErrors, draftFromView,
  draftPayload, formatSpec, heldChildren, modelMode, modelRestrictionText, moveItem, parseSpec, policyText, scopeText,
  specLabel } from '../../../allocationPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { REAL_PROVIDERS, restrictionPayload, restrictionText } from '../../../onboardingPresentation.mjs'

const encode = encodeURIComponent
const PROVIDER_DEFAULT = 'provider_default'

// The model allowlist of one allowed provider, from its catalog. Allowing every eligible model and
// allowing only the selected ones are two explicit choices; an empty selection never means "every".
function ProviderModels({ provider, names, mode, onChange, disabled }) {
  const id = useId()
  const models = useModelCatalog(provider)
  const restricted = mode === 'only'
  const choices = catalogChoices(models.catalog, names)
  const toggle = (value, on) => onChange(on ? [...names, value] : names.filter((item) => item !== value), 'only')
  return <fieldset className="a-policy-models">
    <legend>{providerLabel(provider)} models</legend>
    <label className="a-check"><input type="radio" name={`${id}-scope`} checked={!restricted} disabled={disabled}
      onChange={() => onChange([], 'all')} /><span>All otherwise eligible models are allowed</span></label>
    <label className="a-check"><input type="radio" name={`${id}-scope`} checked={restricted} disabled={disabled}
      onChange={() => onChange(names, 'only')} /><span>Only the models selected below</span></label>
    {restricted && <ul className="a-plain-list">
      <li><Check checked={names.includes(PROVIDER_DEFAULT)} disabled={disabled}
        onChange={(on) => toggle(PROVIDER_DEFAULT, on)}>Provider default model</Check></li>
      {choices.map((choice) => {
        const checked = names.includes(choice.id)
        return <li key={choice.id}><Check checked={checked} disabled={disabled || (!choice.selectable && !checked)}
          onChange={(on) => toggle(choice.id, on)}>{choice.label}{choice.selectable ? '' : ` · ${choice.status}`}</Check></li>
      })}
    </ul>}
    {restricted && !names.length && <p className="a-fine a-tone-alert" role="alert">No model is selected: select at least one, or choose "All otherwise eligible models are allowed".</p>}
    <p className="a-fine">{restricted ? (names.length ? modelRestrictionText(names) : 'No model selected yet.') : modelRestrictionText([])}
      {models.catalog && !models.catalog.refresh?.fresh ? ' The model list is stale; availability is rechecked at launch.' : ''}</p>
    {models.error && <p className="a-fine a-tone-alert">{models.error}</p>}
  </fieldset>
}

// Adds one provider/model/effort entry from the provider's current catalog.
function SpecAdder({ providers, onAdd, disabled, anyModel, label }) {
  const [state, setState] = useState({ provider: providers[0] || '', model: '', effort: '' })
  const preliminary = adderSelection(providers, state)
  const models = useModelCatalog(preliminary.provider || null)
  const choices = catalogChoices(models.catalog).filter((choice) => choice.selectable)
  const { provider, model, effort } = adderSelection(providers, state, models.catalog ? choices : null)
  const efforts = model ? (choices.find((choice) => choice.id === model)?.efforts || []) : []
  if (!providers.length) return <p className="a-fine">Allow a provider first.</p>
  return <div className="a-policy-adder" role="group" aria-label={label}>
    <PlainSelect label="Provider" value={provider} disabled={disabled}
      onChange={(value) => setState({ provider: value, model: '', effort: '' })}
      options={providers.map((value) => ({ value, label: providerLabel(value) }))} />
    <PlainSelect label="Model" value={model} disabled={disabled || provider === 'gemini'}
      onChange={(value) => setState({ provider, model: value, effort: '' })}
      options={[{ value: '', label: anyModel ? 'Any model' : 'Provider default model' }, ...choices.map((choice) => ({ value: choice.id, label: choice.label }))]} />
    <PlainSelect label="Effort" value={effort} disabled={disabled || efforts.length === 0}
      onChange={(value) => setState({ provider, model, effort: value })}
      options={[{ value: '', label: anyModel ? 'Any effort' : 'Default effort' }, ...efforts.map((value) => ({ value, label: value }))]} />
    <Button size="sm" disabled={disabled || !provider}
      onClick={() => onAdd(formatSpec({ provider, model: model || null, effort: effort || null }))}>Add</Button>
  </div>
}

// An ordered list with keyboard-operable reordering and removal.
function SpecList({ items, onChange, disabled, anyModel, label }) {
  if (!items.length) return <p className="a-fine">None.</p>
  return <ol className="a-spec-list" aria-label={label}>
    {items.map((value, index) => {
      const text = specLabel(parseSpec(value), { anyModel })
      return <li key={`${value}-${index}`}>
        <span>{text}</span>
        <span className="a-spec-actions">
          <Button size="sm" variant="ghost" disabled={disabled || index === 0} aria-label={`Move ${text} up`} onClick={() => onChange(moveItem(items, index, -1))}>↑</Button>
          <Button size="sm" variant="ghost" disabled={disabled || index === items.length - 1} aria-label={`Move ${text} down`} onClick={() => onChange(moveItem(items, index, 1))}>↓</Button>
          <Button size="sm" variant="ghost" disabled={disabled} aria-label={`Remove ${text}`} onClick={() => onChange(items.filter((_, i) => i !== index))}>Remove</Button>
        </span>
      </li>
    })}
  </ol>
}

// The Auto policy editor: an isolated draft, saved only on an explicit Save.
function PolicyEditor({ view, draft, onDraft, busy, onSave, onCancel, onUseInherited }) {
  const categories = view?.categories || []
  const [category, setCategory] = useState(categories[0] || 'general')
  const update = (patch) => onDraft({ ...draft, ...patch })
  const levels = draft.profile[category] || { preferred: [], acceptable: [] }
  const setLevel = (level, items) => update({ profile: { ...draft.profile, [category]: { ...levels, [level]: items } } })
  const configured = Object.entries(draft.profile).filter(([, value]) => value.preferred?.length || value.acceptable?.length)
  const errors = draftErrors(draft)
  return <form className="a-policy-editor" onSubmit={(event) => { event.preventDefault(); onSave() }}>
    <p className="a-fine" role="status">{scopeText(view)}</p>
    <Check checked={draft.enabled} disabled={busy} onChange={(enabled) => update({ enabled })}>
      Auto: best suitable provider and model for delegated children</Check>
    <p className="a-fine">{AUTO_SUMMARY}</p>
    {draft.enabled && <>
      <fieldset className="a-policy-providers"><legend>Allowed providers</legend>
        {AUTO_PROVIDERS.map((provider) => <Check key={provider} checked={draft.providers.includes(provider)} disabled={busy}
          onChange={(on) => update({ providers: on ? [...draft.providers, provider] : draft.providers.filter((item) => item !== provider) })}>
          {providerLabel(provider)}</Check>)}
      </fieldset>
      <p className="a-fine">Allowed is not the same as available: readiness, sign-in, billing and usage are checked before every launch.</p>
      <Disclosure summary="Allowed models">
        {draft.providers.map((provider) => <ProviderModels key={provider} provider={provider} disabled={busy}
          names={draft.models[provider] || []} mode={modelMode(draft, provider)}
          onChange={(names, mode) => update({ models: { ...draft.models, [provider]: names }, modes: { ...draft.modes, [provider]: mode } })} />)}
      </Disclosure>
      <Disclosure summary={`Preference order (${draft.preferences.length})`}>
        <p className="a-fine">Tie-breakers only, among candidates that are equally suitable for the work. They never override eligibility, your allowlists,
          subscription or billing limits, or a more suitable candidate.</p>
        <SpecList items={draft.preferences} anyModel disabled={busy} label="Preference order" onChange={(preferences) => update({ preferences })} />
        <SpecAdder providers={draft.providers} anyModel disabled={busy} label="Add a preference" onAdd={(value) => update({ preferences: [...draft.preferences, value] })} />
      </Disclosure>
      <Disclosure summary={`Suitability profiles (${configured.length} configured)`}>
        <p className="a-fine">Used only when the parent does not give its own assessment. Preferred candidates are considered before acceptable ones;
          usage only separates equally suitable candidates. This is your configured judgment, not a guarantee that a model is objectively better.</p>
        <PlainSelect label="Task category" value={category} disabled={busy} onChange={setCategory}
          options={categories.map((value) => ({ value, label: `${value.replace('_', ' ')}${draft.profile[value]?.preferred?.length || draft.profile[value]?.acceptable?.length ? ' (configured)' : ''}` }))} />
        {['preferred', 'acceptable'].map((level) => <div key={level} className="a-policy-level">
          <h4>{level === 'preferred' ? 'Preferred' : 'Acceptable'}</h4>
          <SpecList items={levels[level] || []} disabled={busy} label={`${level} candidates`} onChange={(items) => setLevel(level, items)} />
          <SpecAdder providers={draft.providers} disabled={busy} label={`Add a ${level} candidate`} onAdd={(value) => setLevel(level, [...(levels[level] || []), value])} />
        </div>)}
      </Disclosure>
    </>}
    {errors.length > 0 && <ul className="a-plain-list a-tone-alert">{errors.map((text) => <li key={text}>{text}</li>)}</ul>}
    <div className="a-act-row">
      <Button type="submit" variant="primary" size="sm" disabled={busy || errors.length > 0}>{busy ? 'Saving…' : view?.inherited ? 'Save local override' : 'Save'}</Button>
      <Button size="sm" disabled={busy} onClick={onCancel}>Cancel</Button>
      {view?.own_revision && onUseInherited && <Button size="sm" variant="ghost" disabled={busy} onClick={onUseInherited}>Remove this task’s policy</Button>}
    </div>
    {view?.own_revision && <p className="a-fine">Removing this task’s policy makes it follow its parent task’s policy, if any, or no Auto at all.</p>}
  </form>
}

// How this agent was allocated (when Auto chose it) and the Auto policy for its delegated children.
// A refused save keeps the draft so nothing entered is lost.
export function AutoAllocationSection({ scope, taskId, version }) {
  const { detail, runner } = scope
  const view = useLiveResource(taskId ? `${API_BASE}/tasks/${encode(taskId)}/auto-allocation` : null, version)
  const [editing, setEditing] = useState(null)
  const busy = Boolean(runner.busy)
  const decision = detail.allocation
  const lines = decisionLines(decision)
  const data = view.data
  if (!taskId || (!decision && !data?.policy && !detail.managed && detail.agent_type === 'mock')) return null
  const policy = data?.policy ? { ...data.policy, inherited: data.inherited } : null
  const held = heldChildren(data)
  const send = (key, request, success, fallback) => runner.run(key, async () => {
    const result = await request()
    if (!result.ok) return { ok: false, message: refusalText(result, fallback) }
    setEditing(null)
    return { ok: true, message: success }
  })
  return <section className="a-blk" aria-labelledby={`auto-${scope.agentId}`}>
    <h3 id={`auto-${scope.agentId}`}>Helpers: provider allocation</h3>
    {decision && <div className="a-note-box">
      <b>{decisionTitle(decision)}</b>
      <p className="a-fine">{lines.summary}</p>
      {lines.details.length > 0 && <Disclosure summary="Why"><ul className="a-plain-list">{lines.details.map((line) => <li key={line}>{line}</li>)}</ul></Disclosure>}
    </div>}
    {view.error && !data && <p className="a-fine a-tone-alert">Auto allocation details are unavailable.</p>}
    <p className="a-fine">{policyText(policy)}</p>
    {held.length > 0 && <>
      <ul className="a-plain-list" aria-label="Held children">{held.map((child) => <li key={child.proposal_id}><b>{child.proposal_id}</b> <span className="a-fine">{decisionLines(child.allocation).summary}</span></li>)}</ul>
      <p className="a-fine">Held work waits; choose a provider explicitly, change the Auto policy or usage, then let the orchestration pass retry. Nothing is downgraded automatically.</p>
    </>}
    {!editing && <Button size="sm" disabled={busy || !data} onClick={() => setEditing(draftFromView(data, detail.agent_type))}>
      {policy && !policy.inherited ? 'Edit Auto policy' : policy ? 'Override inherited Auto policy' : 'Set Auto for delegated children'}</Button>}
    {editing && <PolicyEditor view={data} draft={editing} onDraft={setEditing} busy={busy}
      onCancel={() => setEditing(null)}
      onSave={() => {
        let payload
        try { payload = draftPayload(editing) } catch (error) { runner.run('auto', async () => ({ ok: false, message: error.message, changed: false })); return }
        send('auto', () => agentActions.saveAutoPolicy(taskId, payload), 'Auto policy saved.', 'The Auto policy could not be saved. Your changes are kept.')
      }}
      onUseInherited={() => send('auto', () => agentActions.removeAutoPolicy(taskId, data.own_revision), 'This task’s Auto policy was removed.',
        'The Auto policy could not be saved. Your changes are kept.')} />}
    <ActionResult result={runner.result} keys={['auto']} />
  </section>
}

// The hard provider permission of this agent's task subtree. It narrows the global permission and
// any ancestor restriction; it never widens them, and it is separate from the Auto policy.
export function RestrictionsSection({ scope, taskId, version }) {
  const { detail, runner } = scope
  const view = useLiveResource(taskId ? `${API_BASE}/tasks/${encode(taskId)}/provider-restrictions` : null, version)
  const [selected, setSelected] = useState(null)
  const busy = Boolean(runner.busy)
  if (!taskId) return null
  const data = view.data
  const inherited = data?.inherited || []
  const permission = detail.provider_permission
  const send = (request, success) => runner.run('restriction', async () => {
    const result = await request()
    if (!result.ok) return { ok: false, message: refusalText(result, 'The restriction could not be saved. Your selection is kept.') }
    setSelected(null)
    return { ok: true, message: success }
  })
  return <section className="a-blk" aria-labelledby={`perm-${scope.agentId}`}>
    <h3 id={`perm-${scope.agentId}`}>Provider permissions</h3>
    {permission && !permission.allowed && <Notice tone="warn" title="Provider not allowed">{permission.reason}</Notice>}
    <p className="a-fine">{data ? restrictionText(data) : view.error ? 'Provider restrictions are unavailable.' : 'Loading…'}</p>
    {data && selected === null && <div className="a-act-row">
      <Button size="sm" disabled={busy} onClick={() => setSelected(data.own ? [...data.own.providers] : [...inherited])}>
        {data.own ? 'Edit restriction' : 'Restrict providers for this task'}</Button>
      {data.own && <Button size="sm" variant="ghost" disabled={busy}
        onClick={() => send(() => agentActions.removeRestriction(taskId, data.own.revision), 'The restriction was removed.')}>Remove restriction</Button>}
    </div>}
    {data && selected !== null && <form onSubmit={(event) => { event.preventDefault(); send(() => agentActions.saveRestriction(taskId, restrictionPayload(selected, data.own)), 'Restriction saved.') }}>
      <p className="a-fine">Applies to this task and every task below it, for new launches, children, Auto, retries and resumes. Running work is not
        stopped. Only providers allowed above this task can be chosen.</p>
      <div role="group" aria-label="Providers allowed for this task">
        {REAL_PROVIDERS.map((provider) => <Check key={provider} checked={selected.includes(provider)} disabled={busy || !inherited.includes(provider)}
          onChange={(on) => setSelected(on ? [...selected, provider] : selected.filter((item) => item !== provider))}>
          {providerLabel(provider)}{inherited.includes(provider) ? '' : ' (not allowed above this task)'}</Check>)}
      </div>
      <div className="a-act-row">
        <Button type="submit" variant="primary" size="sm" disabled={busy}>{runner.busy === 'restriction' ? 'Saving…' : 'Save restriction'}</Button>
        <Button size="sm" disabled={busy} onClick={() => setSelected(null)}>Cancel</Button>
      </div>
    </form>}
    <ActionResult result={runner.result} keys={['restriction']} />
  </section>
}
