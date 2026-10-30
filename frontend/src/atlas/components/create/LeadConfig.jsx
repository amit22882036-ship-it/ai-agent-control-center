import { useId } from 'react'
import { Button } from '../primitives'
import { TextField } from '../controls'
import { Check, GroupedSelect } from '../agent/fields'
import { GROUP_LABELS, OTHER_MODEL, effortChoices, freshness, groupModels, modelLimitation, optionLabel,
  selection } from '../../../modelPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { LEAD_TOOLS, toolState } from '../../model/newTask.mjs'
import { TOOLS } from '../../vocabulary.mjs'

const TOOL_COLOR = { claude: 'blue', codex: 'green', gemini: 'yellow', mock: 'neutral' }

// A small radio group drawn as segments (native radios, so arrow keys and screen readers work).
function Choice({ label, value, options, onChange, disabled }) {
  const id = useId()
  return <div className="a-lc-choice" role="radiogroup" aria-labelledby={`${id}-l`}>
    <span className="a-lc-l" id={`${id}-l`}>{label}</span>
    <div className="a-segc">{options.map((option) => <label key={option.value} className={value === option.value ? 'is-on' : undefined}>
      <input type="radio" name={id} value={option.value} checked={value === option.value} disabled={disabled}
        onChange={() => onChange(option.value)} /><span>{option.label}</span></label>)}</div>
  </div>
}

// The tools a lead (or a new agent) can run on: permission first, then readiness, as reported.
export function ToolCards({ value, onChange, policy, statuses, disabled, tools = LEAD_TOOLS, label = 'Lead agent’s tool' }) {
  const id = useId()
  return <div className="a-tools" role="radiogroup" aria-label={label}>
    {tools.map((provider) => {
      const state = toolState(provider, policy, statuses)
      return <label key={provider} className={`a-tool${value === provider ? ' is-on' : ''}${state.allowed ? '' : ' is-off'}`}>
        <input type="radio" name={`${id}-tool`} value={provider} checked={value === provider} disabled={disabled || !state.allowed}
          aria-describedby={`${id}-${provider}`} onChange={() => onChange(provider)} />
        <span className="a-tool-mark" data-color={TOOL_COLOR[provider]} aria-hidden="true">{TOOLS[provider].mark}</span>
        <b>{provider === 'mock' ? 'Practice (Mock)' : providerLabel(provider)}</b>
        <small id={`${id}-${provider}`}>{state.note}</small>
      </label>
    })}
  </div>
}

// Model, effort and file access for one agent, from its provider's catalog. Unavailable models
// stay visible but cannot be chosen; an unverified one needs an explicit attempt.
export function LeadSettings({ provider, models, picker, onPicker, sandbox, onSandbox, disabled }) {
  if (provider === 'mock') return null
  const catalog = models.catalog
  const groups = groupModels(catalog)
  const chosen = selection(catalog, picker.choice, picker.typed)
  const efforts = effortChoices(provider, catalog, chosen.model)
  const fresh = freshness(catalog)
  const limitation = modelLimitation(provider, chosen.model)
  const update = (patch) => onPicker({ ...picker, ...patch })
  const items = (list, off = false) => list.map((model) => ({ value: model.model_id, label: optionLabel(model), disabled: off }))
  return <div className="a-leadcfg">
    <div className="a-lc">
      <GroupedSelect label="Model" value={picker.choice} disabled={disabled} onChange={(choice) => update({ choice, effort: '', attempt: false })}
        hint={provider === 'gemini' ? 'Gemini CLI picks the model itself.' : chosen.text}
        hintTone={chosen.kind === 'unavailable' ? 'alert' : chosen.needsAttempt ? 'warn' : undefined}
        before={[{ value: '', label: 'Provider default' }]}
        groups={[{ label: GROUP_LABELS.available, items: items(groups.available) },
          { label: `${GROUP_LABELS.unverified} (explicit attempt)`, items: items(groups.unverified) },
          { label: GROUP_LABELS.unavailable, items: items(groups.unavailable, true) }]}
        after={provider !== 'gemini' ? [{ value: OTHER_MODEL, label: 'Other model identifier…' }] : []} />
      {picker.choice === OTHER_MODEL && <TextField label="Model identifier" mono value={picker.typed} maxLength={128} disabled={disabled}
        onChange={(event) => update({ typed: event.target.value, attempt: false })} />}
    </div>
    <div className="a-lc">
      {efforts.levels.length ? <Choice label="Reasoning effort" value={picker.effort} disabled={disabled} onChange={(effort) => update({ effort })}
        options={[{ value: '', label: 'Default' }, ...efforts.levels.map((level) => ({ value: level, label: level[0].toUpperCase() + level.slice(1) }))]} />
        : <div className="a-lc-choice"><span className="a-lc-l">Reasoning effort</span><span className="a-lc-v">Provider default</span></div>}
      <small className="a-fine">{efforts.note}</small>
    </div>
    <div className="a-lc">
      <Choice label="File access" value={sandbox} disabled={disabled} onChange={onSandbox}
        options={[{ value: 'read-only', label: 'Read only' }, { value: 'workspace-write', label: 'Can edit' }]} />
      <small className="a-fine">Only in its private copy of the project.</small>
    </div>
    {(limitation || chosen.needsAttempt) && <div className="a-lc-wide">
      {limitation && <p className="a-fine">{limitation}</p>}
      {chosen.needsAttempt && <Check tone="warn" checked={picker.attempt} disabled={disabled} onChange={(attempt) => update({ attempt })}>
        Try this model explicitly. Access is not verified for this account: it runs on your approved subscription route, may use included
        usage, and is never retried or replaced automatically.</Check>}
    </div>}
    <div className="a-lc-wide a-catalog" role="status">
      <span className={fresh.stale ? 'a-catalog-stale' : undefined}>{fresh.text || (models.error ? models.error : 'Loading the model list…')}</span>
      {catalog && <span className="a-catalog-access">{fresh.verified ? 'Listed for this account' : 'Account access not verified'}</span>}
      <Button size="sm" variant="ghost" icon="refresh" disabled={disabled || models.refreshing} onClick={models.refresh}>
        {models.refreshing ? 'Refreshing…' : 'Refresh models'}</Button>
    </div>
  </div>
}
