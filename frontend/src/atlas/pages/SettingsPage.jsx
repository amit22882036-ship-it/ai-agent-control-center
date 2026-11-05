import { useEffect, useRef, useState } from 'react'
import PageHeader from '../shell/PageHeader'
import Icon from '../components/Icon'
import { Button, Card, Kbd } from '../components/primitives'
import { Switch } from '../components/controls'
import { rovingTarget } from '../a11y.mjs'
import { connectionView } from '../status.mjs'
import { THEME_LABELS } from '../vocabulary.mjs'
import { exitAtlas } from '../uiMode.mjs'
import { API_BASE, API_HOST } from '../../apiClient.mjs'
import { useLiveResource } from '../hooks'
import { INSPECTOR, readInspectorWidth, saveInspectorWidth } from '../layout.mjs'
import { KEY_GROUPS, KEY_NOTE, SETTINGS_SECTIONS, diagnosticsText, notificationView, settingsSection, supervisionView } from '../model/settings.mjs'
import { attentionOf } from '../model/connections.mjs'
import { deliveryView } from '../model/notify.mjs'
import { Disclosure } from '../components/agent/fields'

const THEMES = [['automatic', 'monitor', 'auto'], ['light', 'sun', 'light'], ['dark', 'moon', 'dark']]

function ThemeTiles({ mode, onChoose }) {
  const root = useRef(null)
  function onKeyDown(event, index) {
    const next = rovingTarget(index, event.key, THEMES.length)
    if (next === null) return
    event.preventDefault()
    onChoose(THEMES[next][0])
    root.current?.querySelectorAll('[role="radio"]')[next]?.focus()
  }
  return <div className="a-themes" role="radiogroup" aria-label="Appearance" ref={root}>
    {THEMES.map(([value, icon, preview], index) => <button key={value} type="button" role="radio" className="a-theme"
      aria-checked={mode === value} tabIndex={mode === value ? 0 : -1} onClick={() => onChoose(value)} onKeyDown={(event) => onKeyDown(event, index)}>
      <span className={`a-theme-preview a-theme-preview--${preview}`} aria-hidden="true">
        <span className="a-theme-side"><i /><i style={{ width: '70%' }} /><i /><i style={{ width: '55%' }} /></span>
        <span className="a-theme-canvas"><i style={{ left: '46%', top: 16, background: 'var(--a-agent-blue)' }} />
          <i style={{ left: '24%', top: 52, background: 'var(--a-agent-cyan)' }} /><i style={{ left: '68%', top: 52, background: 'var(--a-agent-violet)' }} /></span>
      </span>
      <span className="a-theme-label"><Icon name={icon} size={15} />{THEME_LABELS[value]}</span>
    </button>)}
  </div>
}

const media = (query) => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(query).matches : false)

export default function SettingsPage({ appearance, live, navigate, route, notifications, connections, delivery }) {
  const current = settingsSection(route.params) || 'appearance'
  const connection = connectionView(live)
  const supervision = useLiveResource(`${API_BASE}/system/process-supervision`, live.reachable ? 'answering' : 'waiting')
  const supervised = supervisionView(supervision)
  const notice = notificationView(notifications)
  const attention = attentionOf(connections.data)
  const [width, setWidth] = useState(() => readInspectorWidth())
  const [copied, setCopied] = useState('')

  // The section is part of the address, so Back and Forward return to it.
  useEffect(() => {
    const id = settingsSection(route.params)
    if (!id) return
    const section = document.getElementById(`atlas-settings-${id}`)
    section?.scrollIntoView({ block: 'start' })
    section?.querySelector('h2')?.focus({ preventScroll: true })
  }, [route.params])

  async function copyDiagnostics() {
    const text = diagnosticsText({ live, connection, supervision, notifications, connections, appearance: appearance.mode, userAgent: navigator.userAgent })
    try { await navigator.clipboard.writeText(text); setCopied('Copied. It holds states only: no task text, paths or names.') } catch { setCopied('The browser didn’t allow copying.') }
  }

  return <div className="a-page a-page--narrow">
    <PageHeader title="Settings" subtitle="How the Control Center looks and behaves on this computer." />
    <div className="a-settings">
      <nav className="a-settings-index" aria-label="Settings sections">
        {SETTINGS_SECTIONS.map(([id, label]) => <button key={id} type="button" aria-current={current === id}
          onClick={() => navigate('settings', { section: id })}>{label}</button>)}
      </nav>
      <div>
        <Card className="a-section" id="atlas-settings-appearance" titleId="atlas-settings-appearance-h" title="Appearance">
          <p className="a-fine a-section-lead">Automatic follows your system’s light or dark setting. The choice is saved in this browser and shared with Classic view.</p>
          <ThemeTiles mode={appearance.mode} onChoose={appearance.choose} />
        </Card>
        <Card className="a-section" id="atlas-settings-interface" titleId="atlas-settings-interface-h" title="Interface">
          <div className="a-rows">
            <div className="a-row"><div><b>Classic interface</b><small>The earlier interface stays available as a fallback with every function. It
              opens in this window; its own button brings you back here.</small></div>
              <Button icon="swap" onClick={() => exitAtlas()}>Open Classic interface</Button></div>
            <div className="a-row"><div><b>Details panel width</b><small>{width} px{width === INSPECTOR.default ? ' (default)' : ''}. Drag the panel’s edge in Work, or use
              the arrow keys on it. Saved in this browser.</small></div>
              <Button disabled={width === INSPECTOR.default} onClick={() => { saveInspectorWidth(INSPECTOR.default); setWidth(INSPECTOR.default) }}>Reset to default</Button></div>
            <div className="a-row"><div><b>Connections</b><small>{attention?.any ? 'Some tools need your attention.' : 'Installation, sign-in, allowed tools, usage and billing of your AI tools.'}</small></div>
              <Button onClick={() => navigate('connections')}>Open Connections</Button></div>
          </div>
        </Card>
        <Card className="a-section" id="atlas-settings-notifications" titleId="atlas-settings-notifications-h" title="Notifications">
          <div className="a-rows">
            <div className="a-row"><div><b id="atlas-notify-label">Control Center notifications</b>
              <small id="atlas-notify-state" className={notice.tone === 'alert' ? 'a-tone-alert-text' : undefined}>{notice.state}</small></div>
              <Switch checked={notifications.enabled} labelledBy="atlas-notify-label" describedBy="atlas-notify-state"
                disabled={!notifications.supported || notifications.requesting || notifications.permission === 'denied'} onChange={() => notifications.toggle()} /></div>
          </div>
          {notice.message && <p className="a-fine" role="status">{notice.message}</p>}
          {notice.backend && <p className="a-fine" role="status">{notice.backend}</p>}
          <dl className="a-kv a-settings-kv">
            <dt>When</dt><dd>When an agent needs your reply, and when an agent finishes.</dd>
            <dt>Browser permission</dt><dd>{notice.permission}</dd>
            <dt>How they arrive</dt><dd>{deliveryView(notifications, delivery).detail}</dd>
          </dl>
          {/* The rules behind it, for whoever wants them; the preference above is what matters. */}
          <Disclosure summary="How notifications are coordinated" className="a-settings-more">
          <dl className="a-kv a-settings-kv">
            <dt>One at a time</dt><dd>While this page shows them, it tells the backend so, and the backend holds back its Windows notification for
              the same event. Otherwise the backend may try a Windows notification; Windows settings and Focus assist decide whether it appears.
              In this browser, only one Control Center tab shows them, Atlas or Classic view, and only while it has every task loaded and current; otherwise the Windows notification is the fallback when available. Another browser or browser profile open at the same time is not coordinated with this one and can show the same event.</dd>
            <dt>In Atlas</dt><dd>The bell lists what needs you now, from the live task list; it keeps no history.</dd>
            <dt>Also needed</dt><dd>{notice.prerequisite} Windows Focus assist (Do not disturb) can hide them.</dd>
            <dt>Shared</dt><dd>The same preference as Classic view’s bell in this browser. The backend keeps one setting for its Windows notifications: turning them on in any browser turns that on; turning them off here turns it off until a browser that has them on says so again.</dd>
          </dl>
          </Disclosure>
        </Card>
        <Card className="a-section" id="atlas-settings-keyboard" titleId="atlas-settings-keyboard-h" title="Keyboard">
          {KEY_GROUPS.map(([group, keys]) => <div key={group} className="a-keygroup">
            <h3>{group}</h3>
            <div className="a-keys">{keys.map(([action, combo]) => <div key={action}><span>{action}</span><Kbd>{combo}</Kbd></div>)}</div>
          </div>)}
          <p className="a-fine">{KEY_NOTE}</p>
        </Card>
        <Card className="a-section" id="atlas-settings-accessibility" titleId="atlas-settings-accessibility-h" title="Accessibility">
          <dl className="a-kv a-settings-kv">
            <dt>Motion</dt><dd>{media('(prefers-reduced-motion: reduce)') ? 'Reduced: your system asks for less motion, so transitions are instant.'
              : 'Transitions are short. If you turn off “Animation effects” in Windows, Atlas makes them instant.'}</dd>
            <dt>Contrast</dt><dd>{media('(forced-colors: active)') ? 'Windows contrast themes are on; Atlas uses their colours.' : 'Colour is never the only signal: every state also has a shape or words.'}</dd>
            <dt>Right-to-left text</dt><dd>Task titles and agent text in Hebrew or Arabic are shown right to left automatically.</dd>
            <dt>Screen readers</dt><dd>Each page moves focus to its heading. Changes and results are announced.</dd>
          </dl>
        </Card>
        <Card className="a-section" id="atlas-settings-about" titleId="atlas-settings-about-h" title="About and diagnostics">
          <dl className="a-kv">
            <dt>Interface</dt><dd>Lumen. The Classic interface remains available as a fallback.</dd>
            <dt>Backend</dt><dd>{API_HOST} · {connection.label}. {connection.detail}</dd>
            <dt>Process supervision</dt><dd className={supervised.tone === 'alert' ? 'a-tone-alert-text' : undefined}>{supervised.text}</dd>
            <dt>Tools</dt><dd>{connections.data ? (attention?.any ? 'Some need attention (see Connections).' : 'Read; none needs attention.') : connections.error ? 'Couldn’t be read.' : 'Reading…'}</dd>
            <dt>Design reference</dt><dd><button type="button" className="a-btn a-btn--ghost a-btn--sm" onClick={() => navigate('gallery')}>Open the design gallery</button></dd>
          </dl>
          <div className="a-act-row"><Button icon="info" onClick={copyDiagnostics}>Copy diagnostics</Button></div>
          {copied && <p className="a-fine" role="status">{copied}</p>}
        </Card>
      </div>
    </div>
  </div>
}
