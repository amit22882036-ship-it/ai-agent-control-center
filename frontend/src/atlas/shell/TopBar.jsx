import Icon from '../components/Icon'
import { IconButton } from '../components/primitives'
import { StatusBead } from '../components/identity'
import { NAV_ROUTES, ROUTES, formatHash, navSection } from '../router.mjs'
import { connectionView, needsYouCount, needsYouLabel } from '../status.mjs'
import { THEME_LABELS, nextTheme } from '../vocabulary.mjs'
import { attentionOf, attentionText } from '../model/connections.mjs'

const THEME_ICONS = { automatic: 'monitor', light: 'sun', dark: 'moon' }

function BrandMark() {
  return <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true" focusable="false">
    <rect width="26" height="26" rx="8" fill="var(--a-ink)" />
    <path d="M13 9.5v3.2M13 12.7c0 2-4.6 1.4-4.6 4.2M13 12.7c0 2 4.6 1.4 4.6 4.2" stroke="#CBB494" strokeWidth="1.6" fill="none" strokeLinecap="round" />
    <circle cx="13" cy="7.6" r="2.4" fill="var(--a-on-ink)" /><circle cx="8.4" cy="18.3" r="2.2" fill="var(--a-on-ink)" /><circle cx="17.6" cy="18.3" r="2.2" fill="#F0B232" />
  </svg>
}

// Route links are real anchors (so they can be opened in a new tab); `markPush` tells the shell a
// click is a new navigation, which starts the page at the top.
export default function TopBar({ route, live, appearance, markPush, onClassic, connections }) {
  const section = navSection(route)
  const needs = needsYouCount(live.overview)
  const connection = connectionView(live)
  const next = nextTheme(appearance.mode)
  // A mark, not a number: which tools need attention is said in words (title and screen readers).
  const attention = attentionText(attentionOf(connections?.data))
  return <header className="a-topbar">
    <a className="a-brand" href={formatHash('work')} onClick={markPush}><BrandMark />Control Center<span className="a-brand-tag">Atlas preview</span></a>
    <nav className="a-nav" aria-label="Main">
      {NAV_ROUTES.map((name) => <a key={name} className="a-nav-link" href={formatHash(name)} onClick={markPush}
        aria-current={section === name ? 'page' : undefined} title={name === 'connections' && attention ? attention : undefined}>{ROUTES[name].title}
        {name === 'connections' && attention && <><i className="a-nav-mark" aria-hidden="true" /><span className="a-sr">. {attention}</span></>}</a>)}
    </nav>
    <span className="a-sp" />
    {needs > 0 && <a className="a-needs" href={formatHash('work')} onClick={markPush}>
      <StatusBead state="ask" decorative /><span className="a-num">{needsYouLabel(needs)}</span></a>}
    <span className={`a-conn a-conn--${connection.tone}`} role="status" title={connection.detail}>
      <i className="a-conn-dot" aria-hidden="true" /><span className="a-conn-label">{connection.label}</span>
      <span className="a-sr">{connection.label}. {connection.detail}</span>
    </span>
    <IconButton icon={THEME_ICONS[appearance.mode]} label={`Appearance: ${THEME_LABELS[appearance.mode]}. Switch to ${THEME_LABELS[next]}`}
      onClick={() => appearance.choose(next)} />
    <button type="button" className="a-btn a-btn--ghost a-btn--sm" onClick={onClassic} aria-label="Classic view"
      title="Return to the classic interface"><Icon name="swap" size={14} /><span className="a-classic-label" aria-hidden="true">Classic view</span></button>
    <a className="a-btn a-btn--primary" href={formatHash('new')} onClick={markPush}><Icon name="plus" size={15} stroke={2.2} />New task</a>
  </header>
}
