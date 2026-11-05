import { useCallback, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { IconButton, Kbd } from '../components/primitives'
import { StatusBead } from '../components/identity'
import { formatHash } from '../router.mjs'
import { connectionView, needsYouLabel } from '../status.mjs'
import { THEME_LABELS, nextTheme } from '../vocabulary.mjs'
import { bellLabel } from '../model/attention.mjs'
import NotificationsPanel from './NotificationsPanel'

const THEME_ICONS = { automatic: 'monitor', light: 'sun', dark: 'moon' }

// The mark: a lead and two helpers joined by their threads, one of them lit.
export function BrandMark({ size = 28 }) {
  return <svg className="a-brandmark" width={size} height={size} viewBox="0 0 28 28" aria-hidden="true" focusable="false">
    <defs><radialGradient id="a-brand-glow" cx="70%" cy="72%" r="55%"><stop offset="0" stopColor="#FFC768" stopOpacity=".95" />
      <stop offset="1" stopColor="#FF9D2E" stopOpacity="0" /></radialGradient></defs>
    <rect x="1" y="1" width="26" height="26" rx="8.5" fill="var(--a-ink)" />
    <circle cx="19" cy="19.5" r="9" fill="url(#a-brand-glow)" />
    <path d="M14 10.2v3.3M14 13.5c0 2.1-4.7 1.5-4.7 4.4M14 13.5c0 2.1 4.7 1.5 4.7 4.4" stroke="var(--a-on-ink)" strokeOpacity=".55"
      strokeWidth="1.5" fill="none" strokeLinecap="round" />
    <circle cx="14" cy="8.4" r="2.4" fill="var(--a-on-ink)" /><circle cx="9.3" cy="19.4" r="2.2" fill="var(--a-on-ink)" />
    <circle cx="18.7" cy="19.4" r="2.3" fill="#FFB547" />
  </svg>
}

// Route links are real anchors (so they can be opened in a new tab); `markPush` tells the shell a
// click is a new navigation, which starts the page at the top.
export default function TopBar({ live, appearance, markPush, attention: watch }) {
  const { summary, onNext, panel } = watch
  // The backend's own workspace count of questions; the button moves to the next one.
  const needs = summary.reported
  const connection = connectionView(live)
  const next = nextTheme(appearance.mode)
  const [open, setOpen] = useState(false)
  const bell = useRef(null)
  const close = useCallback((returnFocus) => { setOpen(false); if (returnFocus) bell.current?.focus() }, [])
  const label = bellLabel(summary, { updates: panel.notices.notices.length, tools: panel.tools.length })
  const urgent = summary.loaded > 0 || (summary.reported ?? 0) > 0 || panel.tools.length > 0
  const calm = !urgent && panel.notices.notices.length > 0
  return <header className="a-topbar">
    <a className="a-brand" href={formatHash('work')} onClick={markPush}><BrandMark /><span className="a-brand-name">Control Center</span></a>
    <span className="a-sp" />
    {needs > 0 && <button type="button" className="a-needs" onClick={onNext} title="Go to the next task waiting for your reply"
      aria-label={`${needsYouLabel(needs)}. Go to the next one`}>
      <StatusBead state="ask" decorative /><span className="a-num">{needs}<span className="a-needs-word">{needsYouLabel(needs).slice(String(needs).length)}</span></span>
      <Icon name="right" size={13} stroke={2.4} /></button>}
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
    <a className="a-btn a-btn--primary" href={formatHash('new')} onClick={markPush} title="New task · N" aria-keyshortcuts="N">
      <Icon name="plus" size={15} stroke={2.2} /><span className="a-newtask-label">New task</span><span aria-hidden="true"><Kbd>N</Kbd></span></a>
  </header>
}
