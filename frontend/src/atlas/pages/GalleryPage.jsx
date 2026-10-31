import { useRef, useState } from 'react'
import PageHeader from '../shell/PageHeader'
import Icon from '../components/Icon'
import { Button, Card, Chip, EmptyState, IconButton, Kbd, Notice, Skeleton } from '../components/primitives'
import { Segmented, SelectField, Switch, Tabs, TextArea, TextField } from '../components/controls'
import { Avatar, StatusBead } from '../components/identity'
import { Dialog, MenuButton, ToastRegion } from '../components/overlays'
import Inspector from '../components/Inspector'
import { tabPanelProps } from '../a11y.mjs'
import { readInspectorWidth, saveInspectorWidth } from '../layout.mjs'
import { AGENT_COLORS, STATES, TOOLS } from '../vocabulary.mjs'

const SECTIONS = [['colours', 'Colours'], ['type', 'Typography'], ['buttons', 'Buttons'], ['choices', 'Choices and fields'],
  ['feedback', 'Chips and notices'], ['identity', 'Agents and status'], ['overlays', 'Menus and dialogs'],
  ['layout', 'Inspector layout'], ['states', 'Loading and empty'], ['bidi', 'Mixed-direction text']]
const SWATCHES = [['--a-bg', 'Background'], ['--a-rail', 'Top bar'], ['--a-surface', 'Surface'], ['--a-sunken', 'Sunken'], ['--a-line', 'Line'],
  ['--a-text', 'Text'], ['--a-text-2', 'Secondary text'], ['--a-text-3', 'Tertiary text'], ['--a-accent', 'Accent'], ['--a-work', 'Working'],
  ['--a-ask-fill', 'Needs you'], ['--a-block', 'Blocked'], ['--a-fail', 'Didn’t finish'], ['--a-thread', 'Hierarchy thread']]
const SAMPLE_AGENTS = [['Toolkit lead', 'claude'], ['Report dates', 'codex'], ['License audit', 'gemini'], ['Practice run', 'mock']]

function Section({ id, title, children }) {
  return <Card className="a-section" id={`atlas-gallery-${id}`} titleId={`atlas-gallery-${id}-h`} title={title}>{children}</Card>
}

export default function GalleryPage() {
  const [current, setCurrent] = useState('colours')
  const [lens, setLens] = useState('list')
  const [filter, setFilter] = useState('all')
  const [on, setOn] = useState(true)
  const [tab, setTab] = useState('now')
  const [dialog, setDialog] = useState(false)
  const [toasts, setToasts] = useState([])
  const [inspector, setInspector] = useState(true)
  const [width, setWidth] = useState(() => readInspectorWidth())
  const counter = useRef(0)
  function toast(message) {
    const id = ++counter.current
    setToasts((items) => [...items, { id, message }])
    setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 4000)
  }
  function jump(id) {
    setCurrent(id)
    const section = document.getElementById(`atlas-gallery-${id}`)
    section?.scrollIntoView({ block: 'start' })
    section?.querySelector('h2')?.focus({ preventScroll: true })
  }
  function resize(value) { setWidth(value); saveInspectorWidth(value) }

  return <div className="a-page">
    <PageHeader title="Design gallery" subtitle="The Atlas building blocks, for design review." actions={<span className="a-gallery-sample">Sample content</span>} />
    <Notice tone="info" title="Nothing on this page is live data">Names, counts and actions here are samples for checking the design. Choosing an
      action only shows a confirmation on this page.</Notice>
    <div className="a-gallery a-gallery-body">
      <nav className="a-settings-index" aria-label="Gallery sections">
        {SECTIONS.map(([id, label]) => <button key={id} type="button" aria-current={current === id} onClick={() => jump(id)}>{label}</button>)}
      </nav>
      <div>
        <Section id="colours" title="Colours">
          <div className="a-swatches">{SWATCHES.map(([token, label]) => <div key={token} className="a-swatch">
            <i style={{ background: `var(${token})` }} /><span>{label}</span><code>{token}</code></div>)}</div>
        </Section>
        <Section id="type" title="Typography">
          <div className="a-type">
            <div className="a-type-row"><code>Page title</code><span className="a-title">Prepare the 2.0 release</span></div>
            <div className="a-type-row"><code>Section</code><span className="a-card-title">How it’s going</span></div>
            <div className="a-type-row"><code>Body</code><span>The lead planned three parts and handed two of them to helpers.</span></div>
            <div className="a-type-row"><code>Label</code><span className="a-field-label">Reasoning effort</span></div>
            <div className="a-type-row"><code>Fine print</code><span className="a-fine">Totals cover all recorded work.</span></div>
            <div className="a-type-row"><code>Monospace</code><span className="a-mono">tests/test_export.py · 9 passed</span></div>
          </div>
        </Section>
        <Section id="buttons" title="Buttons">
          <div className="a-demo">
            <Button variant="primary" icon="plus" kbd="N" onClick={() => toast('Sample: primary action')}>New task</Button>
            <Button icon="map" onClick={() => toast('Sample: secondary action')}>Show in map</Button>
            <Button variant="ghost" onClick={() => toast('Sample: quiet action')}>Cancel</Button>
            <Button variant="ask" icon="reply" onClick={() => toast('Sample: reply')}>Send reply</Button>
            <Button variant="danger" icon="stop" onClick={() => toast('Sample: stop')}>Stop</Button>
            <Button disabled>Unavailable</Button>
          </div>
          <div className="a-demo">
            <Button size="sm" onClick={() => toast('Sample: small action')}>Small</Button>
            <Button size="sm" variant="primary" onClick={() => toast('Sample: small primary action')}>Small primary</Button>
            <IconButton icon="focus" label="Focus this branch (sample)" onClick={() => toast('Sample: focus')} />
            <IconButton icon="refresh" label="Refresh (sample)" onClick={() => toast('Sample: refresh')} />
            <span className="a-fine">Keyboard hint: <Kbd>Ctrl ⏎</Kbd></span>
          </div>
        </Section>
        <Section id="choices" title="Choices and fields">
          <div className="a-demo">
            <span className="a-demo-label">Segmented choice</span>
            <Segmented label="View as" value={lens} onChange={setLens} options={[{ value: 'list', label: 'List', icon: 'list' }, { value: 'map', label: 'Map', icon: 'map' }]} />
            <Segmented label="Show" value={filter} onChange={setFilter} options={[{ value: 'all', label: 'All', count: 7 }, { value: 'needs', label: 'Needs you', count: 2 },
              { value: 'active', label: 'Open', count: 4 }, { value: 'done', label: 'Done', count: 3 }]} />
          </div>
          <div className="a-demo">
            <span className="a-demo-label">Switch</span>
            <span id="atlas-gallery-switch-label">Let the lead bring in helpers</span>
            <Switch checked={on} onChange={setOn} labelledBy="atlas-gallery-switch-label" />
            <span className="a-fine">{on ? 'On' : 'Off'}</span>
          </div>
          <div className="a-demo">
            <span className="a-demo-label">Tabs</span>
            <div style={{ width: '100%' }}>
              <Tabs label="Agent details (sample)" idBase="atlas-gallery-tabs" value={tab} onChange={setTab}
                tabs={[{ id: 'now', label: 'Now' }, { id: 'activity', label: 'Activity' }, { id: 'output', label: 'Output' }, { id: 'settings', label: 'Settings' }]} />
              <div className="a-tabpanel" {...tabPanelProps('atlas-gallery-tabs', tab)}>Sample panel: {tab}.</div>
            </div>
          </div>
          <div className="a-demo a-demo-fields">
            <TextField label="Project name" defaultValue="docs-site" hint="Shown in lists and on task pages." />
            <TextField label="Folder path" mono defaultValue="C:\Work\notes" error="This folder is not a Git repository." />
            <SelectField label="Model" defaultValue="sonnet" options={[{ value: 'sonnet', label: 'Sonnet 4.5' }, { value: 'opus', label: 'Opus 4.1' },
              { value: 'pro', label: 'Example model (not available with your plan)', disabled: true }]} />
            <TextArea label="What should be done?" placeholder="For example: fix the flaky timeout in test_exporter.py." />
          </div>
        </Section>
        <Section id="feedback" title="Chips and notices">
          <div className="a-demo"><Chip>Always available</Chip><Chip tone="work">Ready</Chip><Chip tone="ask">Needs your OK</Chip>
            <Chip tone="fail">Didn’t finish</Chip><Chip tone="accent">Arrives in M5</Chip></div>
          <div className="a-stack a-demo">
            <Notice tone="info" title="Information">Helpers appear only if the lead brings them in.</Notice>
            <Notice tone="ok" title="Done">The helper’s part merged into the lead’s copy with no conflicts.</Notice>
            <Notice tone="warn" title="Needs attention">Codex reported a usage limit. Nothing resumes by itself.</Notice>
            <Notice tone="error" title="Didn’t finish" actions={<Button size="sm" onClick={() => toast('Sample: details')}>Details</Button>}>
              The Control Center stopped this agent. Nothing was created.</Notice>
          </div>
        </Section>
        <Section id="identity" title="Agents and status">
          <div className="a-demo">
            <span className="a-demo-label">Colour labels and tools</span>
            {AGENT_COLORS.map((color, index) => <Avatar key={color} name={`${color} agent`} color={color} tool={Object.keys(TOOLS)[index % 4]} size={34} />)}
          </div>
          <div className="a-demo">
            <span className="a-demo-label">Sizes and removed agents</span>
            {SAMPLE_AGENTS.map(([name, tool], index) => <Avatar key={name} name={name} tool={tool} color={['blue', 'orange', 'yellow', 'neutral'][index]} size={[60, 48, 30, 24][index]} />)}
            <Avatar name="XML export" tool="codex" color="neutral" size={30} removed />
          </div>
          <div className="a-demo">
            <span className="a-demo-label">Status (each state has its own shape)</span>
            <div className="a-beads" style={{ width: '100%' }}>{Object.entries(STATES).map(([state, label]) => <div key={state}><StatusBead state={state} decorative /><span>{label}</span></div>)}</div>
          </div>
        </Section>
        <Section id="overlays" title="Menus and dialogs">
          <div className="a-demo">
            <MenuButton label="More actions (sample)" items={[
              { label: 'Rename', icon: 'edit', onSelect: () => toast('Sample: rename') },
              { label: 'Colour label', icon: 'layers', onSelect: () => toast('Sample: colour label') },
              { separator: true },
              { label: 'Stop branch…', icon: 'stop', danger: true, onSelect: () => toast('Sample: stop branch') }]} />
            <MenuButton label="Sort (sample)" text="Sort" icon="sliders" align="start" items={[
              { label: 'Newest first', onSelect: () => toast('Sample: newest first') }, { label: 'Needs you first', onSelect: () => toast('Sample: needs you first') }]} />
            <Button onClick={() => setDialog(true)}>Open a dialog</Button>
            <Button onClick={() => toast('Sample: this is a confirmation message')}>Show a confirmation</Button>
          </div>
        </Section>
        <Section id="layout" title="Inspector layout">
          <p className="a-fine a-section-lead">The docked panel used for agent details. Drag its edge or focus it and use the arrow keys to resize; Home resets it.</p>
          <div className="a-frame-demo"><div className="a-workframe">
            <div className="a-workframe-main">
              {SAMPLE_AGENTS.slice(0, 3).map(([name, tool], index) => <div key={name} className="a-demo-row">
                <StatusBead state={['work', 'ask', 'paused'][index]} /><span className="a-ell">{name}</span>
                <Avatar name={name} tool={tool} color={['blue', 'orange', 'yellow'][index]} size={24} decorative /></div>)}
              {!inspector && <Button size="sm" onClick={() => setInspector(true)}>Open the inspector</Button>}
            </div>
            {inspector && <Inspector label="Agent details (sample)" width={width} onResize={resize} onClose={() => setInspector(false)}
              header={<div className="a-demo-head"><Avatar name="Toolkit lead" tool="claude" color="blue" size={40} decorative />
                <div><h3 className="a-demo-name">Toolkit lead</h3><p className="a-fine">Lead · Claude Code · Sonnet 4.5</p></div></div>}
              footer={<><Button size="sm" icon="redirect">Redirect</Button><Button size="sm" variant="danger" icon="stop">Stop</Button></>}>
              <p className="a-fine">Sample content. In Work, this panel shows the selected agent.</p>
            </Inspector>}
          </div></div>
        </Section>
        <Section id="states" title="Loading and empty">
          <div className="a-demo a-demo-states">
            <div className="a-demo-box"><Skeleton lines={4} label="Loading (sample)" /></div>
            <div className="a-demo-box"><EmptyState icon="layers" title="No tasks yet" actions={<Button size="sm" variant="primary" icon="plus">New task</Button>}>
              Describe what you want done and an agent starts on it.</EmptyState></div>
          </div>
        </Section>
        <Section id="bidi" title="Mixed-direction text">
          <div className="a-bidi-sample">
            <p dir="auto" className="a-bidi">תקן את ייצוא הדוחות כך שתאריכים יוצגו בפורמט DD/MM/YYYY</p>
            <p dir="auto" className="a-bidi">Review the README in the docs-site project</p>
            <p dir="auto" className="a-bidi">סקירת קוד ל-billing-service לפני גרסה 2.0</p>
            <p className="a-fine">Text people write keeps its own direction (<span className="a-mono">dir=auto</span>); the interface around it stays left to right.</p>
          </div>
        </Section>
      </div>
    </div>
    <Dialog open={dialog} onClose={() => setDialog(false)} title="Stop this branch?"
      description="Sample dialog. In the product, this lists exactly what would stop before anything happens."
      actions={<><Button variant="ghost" onClick={() => setDialog(false)}>Cancel</Button>
        <Button variant="primary" onClick={() => { setDialog(false); toast('Sample: confirmed') }}>Stop branch</Button></>}>
      <p className="a-fine"><Icon name="info" size={14} /> Nothing happens on this page; it only shows a confirmation.</p>
    </Dialog>
    <ToastRegion toasts={toasts} onDismiss={(id) => setToasts((items) => items.filter((item) => item.id !== id))} />
  </div>
}
