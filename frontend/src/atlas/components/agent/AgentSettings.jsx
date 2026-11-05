import { useState } from 'react'
import { Avatar } from '../identity'
import { Button } from '../primitives'
import { TextField } from '../controls'
import { ActionResult, Disclosure } from './fields'
import { ModelChoice } from './models'
import { useLiveResource } from '../../hooks'
import { API_BASE } from '../../../apiClient.mjs'
import { agentActions, refusalText } from '../../api/agentClient.mjs'
import { chosenConfig, defaultsPayload, executionFacts, initialChoice, selectionReady } from '../../../modelPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { formatHash } from '../../router.mjs'
import { displayColor, displayColors, nameHistoryTime } from '../../../agentIdentity.mjs'

const encode = encodeURIComponent

// The agent's own default model and effort, its latest effective execution and an explicit
// defaults change (from the next instruction on). Classic rules and words.
export function DefaultsSection({ scope }) {
  const { detail, runner, models } = scope
  const [value, setValue] = useState(() => initialChoice(detail.model, detail.effort, models.catalog))
  const facts = executionFacts(detail.latest_execution)
  const history = (detail.provider_history || []).filter((change) => change.boundary === 'defaults')
  const busy = Boolean(runner.busy)
  const ready = selectionReady(models.catalog, value)
  function save(event) {
    event.preventDefault()
    if (busy || !ready) return
    runner.run('defaults', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      const config = chosenConfig(models.catalog, value)
      const result = await agentActions.saveDefaults(scope.agentId, defaultsPayload(config.model, config.effort, config.attempt))
      return result.ok ? { ok: true, message: 'Defaults saved. They apply from this agent’s next instruction.' }
        : { ok: false, message: refusalText(result, 'The defaults could not be changed. Please retry.') }
    })
  }
  return <section className="a-blk" aria-labelledby={`defaults-${scope.agentId}`}>
    <h3 id={`defaults-${scope.agentId}`}>Model and effort</h3>
    <dl className="a-kv">
      <dt>Agent default</dt><dd>{detail.model || 'Provider default'} · {detail.effort ? `${detail.effort} effort` : 'provider default effort'}</dd>
      {facts.map(([label, text]) => <div key={label} className="a-kv-row"><dt>{label}</dt><dd>{text}</dd></div>)}
    </dl>
    {facts.length > 0 && <p className="a-fine">From the latest execution. Values the provider does not report are never assumed.</p>}
    {detail.agent_type !== 'mock' && <p className="a-fine">{providerLabel(detail.agent_type)}’s sign-in, usage, models and billing:{' '}
      <a href={formatHash('connections', { provider: detail.agent_type })}>open it in Connections</a>.</p>}
    {scope.caps.defaults && <Disclosure summary="Change this agent’s defaults" className="a-disclosure--card">
      <form onSubmit={save}>
        <p className="a-fine">Applies from this agent’s next instruction; a running execution is never changed and other agents keep their own settings.</p>
        <ModelChoice provider={detail.agent_type} models={models} value={value} onChange={setValue} disabled={busy} label="Default model" />
        <div className="a-act-row"><Button type="submit" disabled={busy || !ready}>{runner.busy === 'defaults' ? 'Saving…' : 'Save defaults'}</Button></div>
      </form>
      {history.length > 0 && <ul className="a-plain-list a-fine">{history.slice(-5).map((change) => <li key={change.change_id}>
        {change.changed_at}: {change.from_model || 'provider default'} / {change.from_effort || 'default'} → {change.to_model || 'provider default'} / {change.to_effort || 'default'}
      </li>)}</ul>}
    </Disclosure>}
    <ActionResult result={runner.result} keys={['defaults']} />
  </section>
}

// Saved decision policies. They are turned on from a question (where the decision applies) and
// can be turned off here at any time.
export function DecisionsSection({ scope }) {
  const { caps, runner } = scope
  const busy = Boolean(runner.busy)
  const off = (key, call, message, fallback) => runner.run(key, async () => {
    const stale = scope.guard()
    if (stale) return { ok: false, message: stale, changed: false }
    const result = await call()
    return result.ok ? { ok: true, message } : { ok: false, message: refusalText(result, fallback) }
  })
  return <section className="a-blk" aria-labelledby={`decisions-${scope.agentId}`}>
    <h3 id={`decisions-${scope.agentId}`}>Questions</h3>
    {!caps.alwaysOn && !caps.similarOn && <p className="a-fine">This agent asks you each time. When it asks a question you can let it decide that one,
      similar ones, or always.</p>}
    {caps.alwaysOn && <div className="a-setting-row">
      <div><b>Always decide for this agent: On</b>
        <small>Always Decide takes precedence over Similar Decisions. The agent can still ask for genuinely missing information.</small></div>
      <Button size="sm" disabled={busy} onClick={() => off('always-off', () => agentActions.always(scope.agentId, false), 'Always decide is off.',
        'Unable to change Always Decide. Please try again.')}>{runner.busy === 'always-off' ? 'Turning off…' : 'Turn off'}</Button>
    </div>}
    {caps.similarOn && <div className="a-setting-row">
      <div><b>Auto-decide similar questions: {caps.alwaysOn ? 'Saved (Always Decide is active)' : 'On'}</b>
        <small>Materially similar decisions are made by the agent itself.</small></div>
      <Button size="sm" disabled={busy} onClick={() => off('similar-off', () => agentActions.similar(scope.agentId, false), 'Similar decisions are off.',
        'Unable to change similar decisions. Please try again.')}>{runner.busy === 'similar-off' ? 'Turning off…' : 'Turn off'}</Button>
    </div>}
    <ActionResult result={runner.result} keys={['always-off', 'similar-off']} />
  </section>
}

// Name and colour label of an agent, and the names it had. Renaming or recolouring never changes
// what the agent does; it is also allowed for an agent that is history, and says so.
export function IdentitySection({ agentId, detail, runner, guardIdentity, historical, version }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [historyOpen, setHistoryOpen] = useState(false)
  const history = useLiveResource(historyOpen ? `${API_BASE}/agents/${encode(agentId)}/name-history` : null, version)
  const busy = Boolean(runner.busy)
  const color = displayColor(detail.display_color)
  const label = detail.display_name || 'Untitled agent'
  function rename(event) {
    event.preventDefault()
    if (busy || !name.trim()) return
    runner.run('rename', async () => {
      const stale = guardIdentity()
      if (stale) return { ok: false, message: stale, changed: false }
      const result = await agentActions.rename(agentId, name.trim())
      if (!result.ok) return { ok: false, message: refusalText(result, 'Unable to save name. Use 1–80 characters.') }
      setEditing(false)
      return { ok: true, message: `Renamed to “${result.data?.display_name || name.trim()}”.` }
    })
  }
  const recolor = (value) => {
    if (busy || value === color) return
    runner.run('color', async () => {
      const stale = guardIdentity()
      if (stale) return { ok: false, message: stale, changed: false }
      const result = await agentActions.color(agentId, value)
      return result.ok ? { ok: true, message: `Colour label set to ${value}.` } : { ok: false, message: refusalText(result, 'Unable to save color.') }
    })
  }
  const items = history.data?.history || []
  return <section className="a-blk" aria-labelledby={`identity-${agentId}`}>
    <h3 id={`identity-${agentId}`}>{historical ? 'Name and colour of its last agent' : 'Name and colour'}</h3>
    {historical && <p className="a-fine">This agent is no longer working on the task. Its name and colour label can still be changed; nothing else is.</p>}
    <div className="a-identity">
      <Avatar name={label} color={color} tool={detail.agent_type} size={36} removed={detail.removed === true} decorative />
      {editing ? <form className="a-rename" onSubmit={rename}>
        <TextField label="Agent name" value={name} maxLength={80} disabled={busy} autoFocus onChange={(event) => setName(event.target.value)} />
        <div className="a-act-row">
          <Button type="submit" variant="primary" size="sm" disabled={busy || !name.trim()}>{runner.busy === 'rename' ? 'Saving…' : 'Save name'}</Button>
          <Button size="sm" disabled={busy} onClick={() => setEditing(false)}>Cancel</Button>
        </div>
      </form> : <div className="a-identity-name"><b className="a-bidi" dir="auto">{label}</b>
        <Button size="sm" variant="ghost" icon="edit" disabled={busy} onClick={() => { setName(detail.display_name || ''); setEditing(true) }}>Rename</Button></div>}
    </div>
    <fieldset className="a-swatches-pick" disabled={busy}>
      <legend>Colour label</legend>
      {displayColors.map((value) => <label key={value} className="a-swatch-pick" data-color={value} title={value[0].toUpperCase() + value.slice(1)}>
        <input type="radio" name={`color-${agentId}`} value={value} checked={color === value} onChange={() => recolor(value)} />
        <span className="a-swatch-dot" aria-hidden="true" /><span className="a-sr">{value[0].toUpperCase() + value.slice(1)}</span>
      </label>)}
    </fieldset>
    <Disclosure summary="Name history" onToggle={setHistoryOpen}>
      {history.loading && !history.data ? <p className="a-fine" role="status">Loading names…</p>
        : history.error ? <p className="a-fine a-tone-alert">Name history is unavailable right now.</p>
          : items.length === 0 ? <p className="a-fine">No name history yet.</p>
            : <ol className="a-plain-list">{items.map((entry, index) => <li key={index}><span className="a-bidi" dir="auto">{entry.name}</span>
              <time className="a-fine" dateTime={entry.changed_at}> · {nameHistoryTime(entry.changed_at)}</time></li>)}</ol>}
    </Disclosure>
    <ActionResult result={runner.result} keys={['rename', 'color']} />
  </section>
}

// Identifiers and provider history for support and troubleshooting.
export function TechnicalSection({ detail, taskId }) {
  const changes = detail.provider_history || []
  return <section className="a-blk" aria-labelledby={`tech-${detail.agent_id}`}>
    <h3 id={`tech-${detail.agent_id}`}>Technical details</h3>
    <dl className="a-kv">
      <dt>Agent</dt><dd className="a-mono">{detail.agent_id}</dd>
      <dt>Task</dt><dd className="a-mono">{taskId}</dd>
      <dt>Provider session</dt><dd className="a-mono">{detail.session_id || 'Not available'}</dd>
      <dt>Provider</dt><dd>{providerLabel(detail.agent_type)}</dd>
      <dt>Model</dt><dd>{detail.model || 'Provider default'}</dd>
      <dt>Access</dt><dd>{detail.sandbox === 'full-access' ? 'Full Access (not sandboxed)' : detail.sandbox === 'workspace-write' ? 'Workspace write' : detail.sandbox === 'read-only' ? 'Read only' : 'Not applicable'}</dd>
      {changes.length > 0 && <><dt>Provider changes</dt><dd>{changes.map((change) =>
        `${providerLabel(change.from_provider)}${change.from_model ? ` (${change.from_model})` : ''} → ${providerLabel(change.to_provider)}${change.to_model ? ` (${change.to_model})` : ''}${change.session_retained ? ', session kept' : ', new session'}`).join('; ')}</dd></>}
      <dt>Internal status</dt><dd>{detail.status}</dd>
    </dl>
  </section>
}
