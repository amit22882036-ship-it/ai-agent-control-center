import { useState } from 'react'
import Icon from '../Icon'
import { Button, Card } from '../primitives'
import { ActionResult, Check, ConfirmDialog } from '../agent/fields'
import { agentActions } from '../../api/agentClient.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { billingGuide, GUIDANCE_CHECKED } from '../../../providerGuidance.mjs'
import { effortChoices, freshness, groupModels } from '../../../modelPresentation.mjs'
import { billingView, humanTimes, usageView } from '../../model/connections.mjs'
import { formatHash } from '../../router.mjs'
import { outcome } from './outcome.mjs'
import { UsageMeter } from './ProviderParts'

// Turned on, billing route and the person's own statement, and where paid use is checked.
export function BillingCard({ entry, runner, blocked }) {
  const provider = entry.provider
  const view = billingView(entry)
  const guide = billingGuide(provider)
  const [agreed, setAgreed] = useState(false)
  const off = Boolean(runner.busy) || Boolean(blocked)
  const label = providerLabel(provider)
  const settings = (key, payload, done) => runner.run(key, async () => {
    const result = await agentActions.providerSettings(provider, payload)
    if (result.ok) setAgreed(false)
    return outcome(result, { done, refused: `${label} could not be changed.`, unknown: 'The change may or may not have been saved. Its state is read again; check it before trying again.' })
  })
  return <Card className="a-cx-sec" id="cx-sec-billing" titleId="cx-sec-billing-h" title="Turned on and billing">
    <dl className="a-kv">
      <dt>Turned on</dt><dd>{entry.status?.settings?.enabled ? 'On: new work may start with it' : 'Off: no new work starts with it'}</dd>
      <dt>Sign-in route</dt><dd>{view.authRoute || 'Not verified yet'}</dd>
      <dt>Billing route</dt><dd>{view.route ? `${view.route} (verified by the last check)` : 'Not verified yet'}</dd>
      <dt>Your {view.kind}</dt><dd>{view.recorded ? `Recorded${view.recorded.at ? ` ${view.recorded.at}` : ''}` : 'Not recorded'}</dd>
    </dl>
    {view.recorded && <p className="a-fine">Your {view.kind} is your own statement about your account. It is not proof of an account setting and is separate from the
      route the check verified.</p>}
    {view.form && <form className="a-cx-confirm" onSubmit={(event) => {
      event.preventDefault()
      if (!agreed || off) return
      settings('confirm', view.form.payload, view.form.action === 'enable' ? `${label} is on, with your confirmation recorded.` : 'Your acknowledgment is recorded.')
    }}>
      <Check checked={agreed} disabled={off} onChange={setAgreed}>{view.text}</Check>
      <div className="a-act-row"><Button type="submit" variant="primary" disabled={!agreed || off}>{runner.busy === 'confirm' ? 'Saving…' : view.form.button}</Button></div>
    </form>}
    {(view.canEnable || view.canDisable) && <div className="a-act-row">
      {view.canEnable && <Button disabled={off} onClick={() => settings('enable', { enabled: true }, `${label} is on.`)}>Turn on</Button>}
      {view.canDisable && <Button disabled={off} onClick={() => settings('disable', { enabled: false }, `${label} is off. No new work starts with it.`)}>
        {runner.busy === 'disable' ? 'Turning off…' : 'Turn off'}</Button>}
    </div>}
    <div className="a-cx-guide">
      <h3><Icon name="info" size={14} />Where to check paid use beyond your plan</h3>
      <p>{guide.summary}</p>
      <ul className="a-cx-list">{guide.where.map((line) => <li key={line}>{line}</li>)}</ul>
      <ul className="a-cx-links">{guide.links.map((link) => <li key={link.url}>
        <a href={link.url} target="_blank" rel="noreferrer noopener" className="a-cx-ext">{link.label}<span className="a-sr"> (opens your browser)</span></a></li>)}</ul>
      <p className="a-fine">Places checked against the providers’ official pages on {GUIDANCE_CHECKED}. The Control Center can’t read or change these account settings.</p>
      {view.guidance && <p className="a-fine">Setup note from the Control Center: {view.guidance}</p>}
    </div>
    <ActionResult result={runner.result} keys={['confirm', 'enable', 'disable']} />
  </Card>
}

// Provider-reported usage, with when it was observed. Nothing is estimated.
export function UsageCard({ entry, runner, blocked }) {
  const provider = entry.provider
  const view = usageView(entry.status)
  const [confirmClear, setConfirmClear] = useState(false)
  const off = Boolean(runner.busy) || Boolean(blocked)
  const refresh = () => runner.run('usage', async () => outcome(await agentActions.refreshUsage(provider), {
    done: 'Usage read again from Claude Code’s own report. No agent was resumed.', refused: 'Usage could not be refreshed.', prefix: '',
    unknown: 'The readings below may be out of date.' }))
  const clear = () => runner.run('clear', async () => {
    setConfirmClear(false)
    return outcome(await agentActions.clearUsageLimit(provider), { done: 'The recorded limit is cleared. No agent was resumed; resume a waiting one yourself.',
      refused: 'The limit could not be cleared.', unknown: 'It may or may not have been cleared. Its state is read again.' })
  })
  return <Card className="a-cx-sec" id="cx-sec-usage" titleId="cx-sec-usage-h" title="Usage" aside={<span className={`a-cx-cond a-cx-cond--${view.tone === 'ready' ? 'ok' : view.tone === 'blocked' ? 'alert' : 'muted'}`}>{view.overall}</span>}>
    <p className="a-fine">{view.source}. {view.explanation}</p>
    {view.rows.length > 0 ? <div className="a-cx-meters">{view.rows.map((row) => <UsageMeter key={row.key} row={row} />)}</div>
      : <p className="a-cx-empty">No usage readings{view.refreshable ? ' yet' : ''}. Unknown usage is neither zero nor confirmed available.</p>}
    {view.blocks.length > 0 && <ul className="a-cx-list a-cx-list--block">{view.blocks.map((block) => <li key={block.block_id || block.reason}>
      {block.reason} {block.resetText}.{block.observed_at ? ` Recorded ${block.observed_at}.` : ''}</li>)}</ul>}
    {view.lines.length > 0 && <ul className="a-cx-list">{view.lines.map((line) => <li key={line}>{line}</li>)}</ul>}
    <dl className="a-kv">
      <dt>Remaining</dt><dd>{view.remaining}: {providerLabel(provider)} doesn’t report remaining usage.</dd>
      <dt>Cost</dt><dd>Not available: the Control Center never sees what your account is charged.</dd>
      <dt>Last observation</dt><dd>{view.lastObserved || 'None recorded'}</dd>
    </dl>
    <div className="a-act-row">
      {view.refreshable && <Button icon="refresh" disabled={off} onClick={refresh}>{runner.busy === 'usage' ? 'Refreshing…' : 'Refresh usage'}</Button>}
      {view.clearable && <Button disabled={off} onClick={() => setConfirmClear(true)}>Clear recorded limit…</Button>}
    </div>
    {view.refreshable && <p className="a-fine">Refreshing reads the tool’s local usage report, never a model request. It updates readings only; no agent resumes.</p>}
    <ConfirmDialog open={confirmClear} title="Clear the recorded limit?" confirmLabel="Clear recorded limit" danger={false} busy={runner.busy === 'clear'}
      onConfirm={clear} onCancel={() => setConfirmClear(false)}>
      <p>Do this only after the limit has renewed. It resumes no agent, and a limit whose provider-reported reset is still ahead can’t be cleared.</p>
    </ConfirmDialog>
    <ActionResult result={runner.result} keys={['usage', 'clear']} />
  </Card>
}

// The account-scoped model list, each model with the effort levels it lists itself. There is no
// effort setting shared by every tool.
export function ModelsCard({ provider, models, blocked }) {
  const groups = groupModels(models.catalog)
  const fresh = freshness(models.catalog)
  const list = [...groups.available.map((model) => [model, 'Available']), ...groups.unverified.map((model) => [model, 'Not verified for this account']),
    ...groups.unavailable.map((model) => [model, model.status_label || 'Unavailable'])]
  return <Card className="a-cx-sec" id="cx-sec-models" titleId="cx-sec-models-h" title="Models">
    <div className="a-catalog" role="status">
      <span className={fresh.stale ? 'a-catalog-stale' : undefined}>{humanTimes(fresh.text) || 'Loading the model list…'}</span>
      {models.catalog && <span className="a-catalog-access">{fresh.verified ? 'Listed for this account' : 'Account access not verified'}</span>}
      <Button size="sm" variant="ghost" icon="refresh" disabled={models.refreshing || Boolean(blocked)} onClick={models.refresh}>
        {models.refreshing ? 'Refreshing…' : 'Refresh models'}</Button>
    </div>
    {list.length > 0 ? <table className="a-cx-models">
      <thead><tr><th scope="col">Model</th><th scope="col">For this account</th><th scope="col">Effort levels</th></tr></thead>
      <tbody>{list.map(([model, status]) => {
        const efforts = effortChoices(provider, models.catalog, model.model_id)
        return <tr key={model.model_id}><th scope="row">{model.display_name || model.model_id}</th><td>{status}</td>
          <td>{efforts.levels.length ? efforts.levels.join(' · ') : provider === 'gemini' ? 'None (no reasoning control)' : 'None listed'}</td></tr>
      })}</tbody>
    </table> : <p className="a-cx-empty">{models.error || 'No models listed yet.'}</p>}
    {fresh.source && <p className="a-fine">{fresh.source}</p>}
    <p className="a-fine">Refreshing lists models without a model request. The default model for new tasks is set under{' '}
      <a href={formatHash('connections', { section: 'permissions' })}>Allowed tools and defaults</a>; changing it never changes an agent that is already running.</p>
  </Card>
}

// Identifiers and capabilities, for troubleshooting.
export function AdvancedCard({ entry }) {
  const status = entry.status || {}
  const capabilities = Object.entries(status.capabilities || {})
  return <Card className="a-cx-sec" id="cx-sec-advanced" titleId="cx-sec-advanced-h" title="Advanced">
    <details className="a-disclosure"><summary><Icon name="right" size={13} stroke={2.2} /><span>Details for troubleshooting</span></summary>
      <div className="a-disclosure-body">
        <dl className="a-kv">
          <dt>State code</dt><dd className="a-mono">{status.state?.state || 'unchecked'}</dd>
          <dt>Location</dt><dd className="a-mono a-cx-wrap">{entry.discovery?.location || 'Not found'}</dd>
          <dt>Install method</dt><dd>{entry.discovery?.method || 'Not known'}</dd>
          <dt>npm</dt><dd className="a-mono a-cx-wrap">{entry.discovery?.prerequisites?.npm || 'Not found'}</dd>
          {status.docs_url && <><dt>Documentation</dt><dd><a href={status.docs_url} target="_blank" rel="noreferrer noopener" className="a-cx-ext">{status.docs_url}</a></dd></>}
        </dl>
        {capabilities.length > 0 && <ul className="a-cx-caps">{capabilities.map(([name, value]) => <li key={name} className={value.supported ? 'is-yes' : 'is-no'}>
          <Icon name={value.supported ? 'check' : 'close'} size={12} stroke={2.4} /><span>{name.replaceAll('_', ' ')}</span>
          {!value.supported && value.reason && <small>{value.reason}</small>}</li>)}</ul>}
      </div>
    </details>
  </Card>
}
