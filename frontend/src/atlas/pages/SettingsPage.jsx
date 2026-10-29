import { useRef, useState } from 'react'
import PageHeader from '../shell/PageHeader'
import Icon from '../components/Icon'
import { Button, Card, Kbd } from '../components/primitives'
import { rovingTarget } from '../a11y.mjs'
import { connectionView } from '../status.mjs'
import { THEME_LABELS } from '../vocabulary.mjs'
import { exitAtlas } from '../uiMode.mjs'

const SECTIONS = [['appearance', 'Appearance'], ['interface', 'Interface'], ['notifications', 'Notifications'], ['keyboard', 'Keyboard'], ['about', 'About']]
const THEMES = [['automatic', 'monitor', 'auto'], ['light', 'sun', 'light'], ['dark', 'moon', 'dark']]
const KEYS = [
  ['Move between controls', 'Tab · Shift Tab'],
  ['Activate a button or link', 'Enter · Space'],
  ['Move within tabs, choices and menus', '← → ↑ ↓'],
  ['First or last item', 'Home · End'],
  ['Close a menu, dialog or panel', 'Esc'],
  ['Previous or next page', 'Alt ← · Alt →'],
]

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

export default function SettingsPage({ appearance, live, navigate }) {
  const [current, setCurrent] = useState('appearance')
  const connection = connectionView(live)
  function jump(id) {
    setCurrent(id)
    const section = document.getElementById(`atlas-settings-${id}`)
    section?.scrollIntoView({ block: 'start' })
    section?.querySelector('h2')?.focus({ preventScroll: true })
  }
  return <div className="a-page a-page--narrow">
    <PageHeader title="Settings" subtitle="How the Control Center looks and behaves on this computer." />
    <div className="a-settings">
      <nav className="a-settings-index" aria-label="Settings sections">
        {SECTIONS.map(([id, label]) => <button key={id} type="button" aria-current={current === id} onClick={() => jump(id)}>{label}</button>)}
      </nav>
      <div>
        <Card className="a-section" id="atlas-settings-appearance" titleId="atlas-settings-appearance-h" title="Appearance">
          <p className="a-fine a-section-lead">Automatic follows your system setting. The choice is shared with Classic view.</p>
          <ThemeTiles mode={appearance.mode} onChoose={appearance.choose} />
        </Card>
        <Card className="a-section" id="atlas-settings-interface" titleId="atlas-settings-interface-h" title="Interface">
          <div className="a-rows">
            <div className="a-row"><div><b>Atlas preview</b><small>You are previewing the new interface. Classic view stays the default: opening the
              app without an Atlas address always shows Classic view.</small></div>
              <Button icon="swap" onClick={() => exitAtlas()}>Return to Classic view</Button></div>
          </div>
        </Card>
        <Card className="a-section" id="atlas-settings-notifications" titleId="atlas-settings-notifications-h" title="Notifications">
          <div className="a-rows"><div className="a-row"><div><b>Agent and provider notifications</b><small>During the preview they run in Classic view:
            turn them on or off from the bell there. Atlas adds its own notification settings in a later milestone.</small></div>
            <span className="a-chip">In Classic view</span></div></div>
        </Card>
        <Card className="a-section" id="atlas-settings-keyboard" titleId="atlas-settings-keyboard-h" title="Keyboard">
          <div className="a-keys">{KEYS.map(([action, keys]) => <div key={action}><span>{action}</span><Kbd>{keys}</Kbd></div>)}</div>
        </Card>
        <Card className="a-section" id="atlas-settings-about" titleId="atlas-settings-about-h" title="About">
          <dl className="a-kv">
            <dt>Interface</dt><dd>Atlas preview (milestone M1: design system and navigation)</dd>
            <dt>Backend</dt><dd>127.0.0.1:8000 · {connection.label}</dd>
            <dt>Design reference</dt><dd><button type="button" className="a-btn a-btn--ghost a-btn--sm" onClick={() => navigate('gallery')}>Open the design gallery</button></dd>
          </dl>
        </Card>
      </div>
    </div>
  </div>
}
