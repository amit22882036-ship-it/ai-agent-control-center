import Icon from '../Icon'
import { providerLabel, PROVIDER_NOTES } from '../../../providerPresentation.mjs'
import { accountNotice, billingGuide } from '../../../providerGuidance.mjs'
import { TOOLS } from '../../vocabulary.mjs'
import { conditionOf, readinessFacts, usageView } from '../../model/connections.mjs'
import { formatHash } from '../../router.mjs'

const TOOL_COLOR = { claude: 'blue', codex: 'green', gemini: 'yellow', mock: 'neutral' }
const MARK_ICON = { yes: 'check', no: 'close', unknown: 'info' }
const MARK_TEXT = { yes: 'Yes', no: 'No', unknown: 'Not known' }

export function ToolMark({ provider, size }) {
  return <span className={`a-tool-mark${size === 'sm' ? ' a-tool-mark--sm' : ''}`} data-color={TOOL_COLOR[provider]} aria-hidden="true">{TOOLS[provider].mark}</span>
}

// The tool's one condition, with a shape as well as a colour.
export function ConditionChip({ condition }) {
  if (!condition) return null
  const icon = { ok: 'check', work: 'refresh', alert: 'warn', ask: 'clock', muted: 'info' }[condition.tone]
  return <span className={`a-cx-cond a-cx-cond--${condition.tone}`}><Icon name={icon} size={13} stroke={2.2} />{condition.label}</span>
}

// Installed, signed in, turned on, allowed for tasks and ready, each on its own line.
export function ReadinessFacts({ entry, compact = false }) {
  return <ul className={`a-cx-facts${compact ? ' a-cx-facts--compact' : ''}`}>
    {readinessFacts(entry).map((fact) => <li key={fact.key} className={`a-cx-fact a-cx-fact--${fact.mark}`}>
      <span className="a-cx-fact-mark"><Icon name={MARK_ICON[fact.mark]} size={12} stroke={2.6} /><span className="a-sr">{MARK_TEXT[fact.mark]}: </span></span>
      <span className="a-cx-fact-label">{fact.label}</span>
      <span className="a-cx-fact-value">{fact.value}</span>
    </li>)}
  </ul>
}

// A provider-reported meter. The bar is drawn only from a reported percentage.
export function UsageMeter({ row }) {
  return <div className={`a-cx-meter${row.exhausted ? ' is-exhausted' : ''}${row.stale ? ' is-stale' : ''}`}>
    <div className="a-cx-meter-head"><b>{row.label}</b><span>{row.used || row.status}</span></div>
    {row.usedPercent !== null && <div className="a-cx-bar" role="meter" aria-label={`${row.label} used`} aria-valuemin={0} aria-valuemax={100}
      aria-valuenow={Math.round(row.usedPercent)} aria-valuetext={row.used}><i style={{ width: `${row.usedPercent}%` }} /></div>}
    <small>{row.status !== 'Unknown' && row.used ? `${row.status} · ` : ''}{row.resetText}{row.scope ? ` · applies to ${row.scope}` : ''}</small>
    <small className={row.stale ? 'a-tone-ask-text' : undefined}>{row.observedAt ? `Observed ${row.observedAt}${row.stale ? ' · stale, not current' : ''}` : 'Never observed'}</small>
  </div>
}

// What the provider currently offers to which accounts, apart from what the Control Center allows.
export function AccountNotice({ provider, brief = false }) {
  const notice = accountNotice(provider)
  if (!notice) return null
  if (brief) return <p className="a-fine a-cx-account"><Icon name="info" size={13} />{notice.title}.</p>
  return <section className="a-notice a-notice--info a-cx-account-notice" aria-labelledby={`cx-account-${provider}`}>
    <span className="a-notice-icon"><Icon name="info" size={17} /></span>
    <div className="a-notice-text"><strong id={`cx-account-${provider}`}>{notice.title}</strong>
    <ul className="a-cx-list">{notice.lines.map((line) => <li key={line}>{line}</li>)}</ul>
    <ul className="a-cx-links">{notice.links.map((link) => <li key={link.url}>
      <a href={link.url} target="_blank" rel="noreferrer noopener" className="a-cx-ext">{link.label}<span className="a-sr"> (opens your browser)</span></a></li>)}</ul></div>
  </section>
}

// One tool on the overview: is it ready, what usage is known, and where paid use is checked.
export function ProviderSummary({ entry, onOpen }) {
  const provider = entry.provider
  const condition = conditionOf(entry)
  const usage = usageView(entry.status)
  const guide = billingGuide(provider)
  const metered = usage.rows.find((row) => row.usedPercent !== null)
  const href = formatHash('connections', { provider })
  return <article className={`a-card a-cx-card a-cx-card--${condition.tone}`} aria-labelledby={`cx-${provider}`}>
    <header className="a-cx-card-head">
      <ToolMark provider={provider} />
      <div className="a-cx-card-name">
        <h2 id={`cx-${provider}`}><a href={href} onClick={(event) => { event.preventDefault(); onOpen(provider) }}>{providerLabel(provider)}</a></h2>
        <small>{PROVIDER_NOTES[provider]}</small>
      </div>
      <ConditionChip condition={condition} />
    </header>
    {condition.detail && <p className="a-fine a-cx-card-detail">{condition.detail}</p>}
    <AccountNotice provider={provider} brief />
    <section className="a-cx-q" aria-label={`Is ${providerLabel(provider)} ready?`}>
      <h3>Ready for tasks?</h3>
      <ReadinessFacts entry={entry} compact />
    </section>
    <section className="a-cx-q" aria-label={`${providerLabel(provider)} usage`}>
      <h3>Usage</h3>
      <p className="a-cx-usage-line"><b>{usage.headline}</b>{usage.lastObserved ? <span> · observed {usage.lastObserved}{usage.stale ? ' (stale)' : ''}</span> : null}</p>
      {metered && <div className="a-cx-bar a-cx-bar--sm" aria-hidden="true"><i style={{ width: `${metered.usedPercent}%` }} /></div>}
      <p className="a-fine">{usage.source}. Remaining: not available.</p>
    </section>
    <section className="a-cx-q" aria-label={`Paid use with ${providerLabel(provider)}`}>
      <h3>Paid use beyond your plan</h3>
      <p className="a-fine">{guide.short}</p>
    </section>
    <footer className="a-cx-card-foot">
      <a className="a-btn a-btn--secondary a-btn--sm" href={href} onClick={(event) => { event.preventDefault(); onOpen(provider) }}>
        Open {providerLabel(provider)}<Icon name="right" size={14} /></a>
    </footer>
  </article>
}
