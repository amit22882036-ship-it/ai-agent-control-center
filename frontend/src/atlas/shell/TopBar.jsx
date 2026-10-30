import { useCallback, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { IconButton } from '../components/primitives'
import { StatusBead } from '../components/identity'
import { NAV_ROUTES, ROUTES, formatHash, navSection } from '../router.mjs'
import { connectionView, needsYouLabel } from '../status.mjs'
import { THEME_LABELS, nextTheme } from '../vocabulary.mjs'
import { attentionOf, attentionText } from '../model/connections.mjs'
import { bellLabel } from '../model/attention.mjs'
import NotificationsPanel from './NotificationsPanel'

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
export default function TopBar({ route, live, appearance, markPush, onClassic, connections, attention: watch }) {
  const section = navSection(route)
  const { summary, onNext, panel } = watch
  // The backend's own workspace count of questions; the button moves to the next one.
  const needs = summary.reported
  const connection = connectionView(live)
  const next = nextTheme(appearance.mode)
  // A mark, not a number: which tools need attention is said in words (title and screen readers).
  const attention = attentionText(attentionOf(connections?.data))
  const [open, setOpen] = useState(false)
  const bell = useRef(null)
  const close = useCallback((returnFocus) => { setOpen(false); if (returnFocus) bell.current?.focus() }, [])
  const label = bellLabel(summary, { updates: panel.notices.notices.length, tools: panel.tools.length })
  const urgent = summary.loaded > 0 || (summary.reported ?? 0) > 0 || panel.tools.length > 0
  const calm = !urgent && panel.notices.notices.length > 0
  return <header className="a-topbar">
    <a className="a-brand" href={formatHash('work')} onClick={markPush}><BrandMark />Control Center<span className="a-brand-tag">Atlas preview</span></a>
    <nav className="a-nav" aria-label="Main">
      {NAV_ROUTES.map((name) => <a key={name} className="a-nav-link" href={formatHash(name)} onClick={markPush}
        aria-current={section === name ? 'page' : undefined} title={name === 'connections' && attention ? attention : undefined}>{ROUTES[name].title}
        {name === 'connections' && attention && <><i className="a-nav-mark" aria-hidden="true" /><span className="a-sr">. {attention}</span></>}</a>)}
    </nav>
    <span className="a-sp" />
    {needs > 0 && <button type="button" className="a-needs" onClick={onNext} title="Go to the next task waiting for your reply"
      aria-label={`${needsYouLabel(needs)}. Go to the next one`}>
      <StatusBead state="ask" decorative /><span className="a-num">{needsYouLabel(needs)}</span><Icon name="right" size={13} stroke={2.4} /></button>}
    <div className="a-bell-wrap">
      <button ref={bell} type="button" className={`a-iconbtn a-bell${open ? ' is-open' : ''}`} aria-label={label} title={label}
        aria-expanded={open} aria-haspopup="dialog" onClick={() => setOpen((value) => !value)}>
        <Icon name="bell" size={17} />{(urgent || calm) && <i className={`a-bell-dot${calm ? ' a-bell-dot--calm' : ''}`} aria-hidden="true" />}
      </button>
      {open && <NotificationsPanel {...panel} summary={summary} live={live} onClose={close} triggerRef={bell} />}
    </div>
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
