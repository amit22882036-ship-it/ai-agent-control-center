import { useEffect } from 'react'
import PageHeader from '../shell/PageHeader'
import Icon from '../components/Icon'
import { Button, Card, Notice, Skeleton } from '../components/primitives'
import { ActionResult } from '../components/agent/fields'
import useModelCatalog from '../../useModelCatalog'
import { agentActions } from '../api/agentClient.mjs'
import { useActionRunner } from '../hooks'
import { PROVIDER_NOTES, providerLabel } from '../../providerPresentation.mjs'
import { REAL_PROVIDERS, attentionOf, conditionOf, entryOf, routeProvider, setupView } from '../model/connections.mjs'
import { AccountNotice, ConditionChip, ProviderSummary, ReadinessFacts, ToolMark } from '../components/connections/ProviderParts'
import { SetupCard } from '../components/connections/SetupSections'
import { AdvancedCard, BillingCard, ModelsCard, UsageCard } from '../components/connections/UseSections'
import { PermissionsCard } from '../components/connections/PermissionsCard'
import { STALE, UNREACHABLE, outcome } from '../components/connections/outcome.mjs'

// Connections (M8): the AI tools on this computer, with Classic's AI Providers view's full
// functionality. `connections` is the shared GET /providers/onboarding read (hooks.useConnections).
// Changing anything waits while the backend is unreachable or the last read failed.
export default function ConnectionsPage({ live, connections, route, navigate }) {
  const provider = routeProvider(route.params)
  const section = route.params?.section || null
  const runner = useActionRunner(connections.reload)
  const view = connections.data
  const blocked = live.reachable === false ? UNREACHABLE : connections.error && view ? STALE : null

  // A link to a section (from New Task, an agent or another page) brings it into view.
  useEffect(() => {
    if (!section || !view) return
    const target = document.getElementById(`cx-sec-${section}`)
    target?.scrollIntoView({ block: 'start' })
    target?.querySelector('h2')?.focus({ preventScroll: true })
  }, [section, provider, view])

  const failed = connections.error && !view
  const status = <>
    {blocked && <Notice tone="warn" role="status" title={live.reachable === false ? 'Backend unavailable' : 'Showing the last answer'}
      actions={<Button size="sm" icon="refresh" onClick={connections.reload}>Retry</Button>}>
      {blocked} {view ? 'What you see below is from the last successful read and may be out of date.' : ''}</Notice>}
    {failed && <Notice tone="error" role="alert" title="Your tools couldn’t be read"
      actions={<Button size="sm" icon="refresh" onClick={connections.reload}>Retry</Button>}>
      {connections.error === 'unreachable' ? 'The backend isn’t reachable.' : `The backend answered ${connections.error}.`} Nothing is assumed about any tool.</Notice>}
  </>
  if (provider) return <ProviderPage provider={provider} view={view} runner={runner} blocked={blocked} status={status} navigate={navigate} />
  return <Overview view={view} loading={!view && !connections.error} runner={runner} blocked={blocked} status={status} navigate={navigate} reload={connections.reload} />
}

function Overview({ view, loading, runner, blocked, status, navigate, reload }) {
  const attention = attentionOf(view)
  const off = Boolean(runner.busy) || Boolean(blocked)
  const checkAll = () => runner.run('check-all', async () => outcome(await agentActions.checkProviders(), {
    done: 'Checked installation, version, sign-in and billing route of every tool, locally. No model was asked anything.',
    refused: 'The tools could not be checked.', prefix: '', unknown: 'The results below may be out of date.' }))
  const checkUpdates = () => runner.run('updates', async () => outcome(await agentActions.checkUpdates(), {
    done: 'Release information read. Nothing was installed.', refused: 'Updates could not be checked.', prefix: '',
    unknown: 'The update information below may be out of date.' }))
  return <div className="a-page a-cx-page">
    <PageHeader title="Connections" subtitle="The AI tools on this computer that your agents use, each with your own sign-in."
      actions={<div className="a-cx-head-acts">
        <Button icon="refresh" disabled={off} onClick={checkAll}>{runner.busy === 'check-all' ? 'Checking…' : 'Check all'}</Button>
        <Button disabled={off} onClick={checkUpdates} title="Reads published release information; installs nothing">
          {runner.busy === 'updates' ? 'Checking…' : 'Check for updates'}</Button>
      </div>} />
    {status}
    <ActionResult result={runner.result} keys={['check-all', 'updates']} />
    {attention?.any && <Notice tone="warn" title="Needs your attention">
      {attention.providers.map((item) => `${item.label}: ${item.condition.label.toLowerCase()}`).join('; ')}
      {attention.defaultProblem ? `${attention.providers.length ? '; t' : 'T'}he default for new tasks can’t be used.` : '.'}</Notice>}
    {loading && <Skeleton lines={4} label="Reading your tools" />}
    {view && <>
      <div className="a-cx-grid">{REAL_PROVIDERS.map((provider) => {
        const entry = entryOf(view, provider)
        return entry ? <ProviderSummary key={provider} entry={entry} onOpen={(id) => navigate('connections', { provider: id })} />
          : <Card key={provider} className="a-cx-card" title={providerLabel(provider)}><p className="a-fine">Not reported by the backend.</p></Card>
      })}</div>
      <div className="a-cx-lower">
        <PermissionsCard policy={view.policy} runner={runner} blocked={blocked} onReload={reload} />
        <Card className="a-cx-sec a-cx-mock" title="Practice (Mock)">
          <p className="a-fine">Simulated work for trying the Control Center. It needs no installation or sign-in, is never limited by allowed tools and is never
            used in place of another tool.</p>
          <ul className="a-cx-list">
            <li>Ready for tasks: always.</li>
            <li>Usage: not applicable. No AI tool runs.</li>
            <li>Paid use: none.</li>
          </ul>
        </Card>
      </div>
    </>}
  </div>
}

function ProviderPage({ provider, view, runner, blocked, status, navigate }) {
  const entry = entryOf(view, provider)
  const update = view?.updates?.[provider] || null
  const models = useModelCatalog(entry ? provider : null)
  const condition = conditionOf(entry)
  const step = entry ? setupView(entry, update).step : null
  const off = Boolean(runner.busy) || Boolean(blocked)
  const label = providerLabel(provider)
  const check = () => runner.run('check', async () => outcome(await agentActions.checkProvider(provider), {
    done: `Checked ${label}’s installation, version, sign-in and billing route, locally. No model was asked anything.`, refused: `${label} could not be checked.`,
    prefix: '', unknown: 'The facts below may be out of date.' }))
  const crumbs = <nav className="a-crumbs a-cx-crumbs" aria-label="Where you are">
    <button type="button" className="a-link" onClick={() => navigate('connections')}><Icon name="left" size={13} stroke={2.4} />Connections</button>
  </nav>
  return <div className="a-page a-cx-page">
    <PageHeader crumbs={crumbs} mark={<ToolMark provider={provider} />} title={label} subtitle={PROVIDER_NOTES[provider]}
      actions={<div className="a-cx-head-acts"><ConditionChip condition={condition} />
        <Button icon="refresh" variant={step?.kind === 'check' ? 'primary' : 'secondary'} disabled={off || !entry} onClick={check}>
          {runner.busy === 'check' ? 'Checking…' : 'Check again'}</Button></div>} />
    {status}
    {!entry && !view && <Skeleton lines={5} label={`Reading ${label}`} />}
    {!entry && view && <Notice tone="error" title={`${label} isn’t reported`}>The backend’s answer doesn’t include this tool.</Notice>}
    {entry && <>
      {step && <div className={`a-cx-next a-cx-next--${step.kind === 'ready' ? 'ok' : condition.tone}`} role="status">
        <b>{step.kind === 'ready' ? 'Ready for new tasks.' : 'Next:'}</b> {step.kind === 'ready' ? '' : step.text}
        {step.also?.length > 0 && <ul>{step.also.map((item) => <li key={item.kind}>{item.text}</li>)}</ul>}
        {entry.status?.state?.reason && condition.key !== 'ready' && entry.status.state.reason !== step.text && <p className="a-fine">{entry.status.state.reason}</p>}
      </div>}
      <AccountNotice provider={provider} />
      <div className="a-cx-detail">
        <div className="a-cx-main">
          <SetupCard entry={entry} update={update} runner={runner} blocked={blocked} />
          <UsageCard entry={entry} runner={runner} blocked={blocked} />
          <ModelsCard provider={provider} models={models} blocked={blocked} />
        </div>
        <div className="a-cx-side">
          <Card className="a-cx-sec" id="cx-sec-ready" titleId="cx-sec-ready-h" title="Ready for tasks?">
            <ReadinessFacts entry={entry} />
            <p className="a-fine">Allowed tools are changed under <button type="button" className="a-link"
              onClick={() => navigate('connections', { section: 'permissions' })}>Allowed tools and defaults</button>.</p>
          </Card>
          <BillingCard key={`${provider}:${entry.status?.settings?.enabled}:${entry.status?.settings?.billing_confirmed}`} entry={entry} runner={runner} blocked={blocked} />
          <AdvancedCard entry={entry} />
        </div>
      </div>
    </>}
  </div>
}
