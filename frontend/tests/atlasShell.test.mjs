import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { DEFAULT_ROUTE, documentTitle, formatHash, NAV_ROUTES, navSection, parseHash, routeKey } from '../src/atlas/router.mjs'
import { connectionView, needsYouCount, needsYouLabel, overviewView, OVERVIEW_FIELDS } from '../src/atlas/status.mjs'
import { initials, rovingTarget, tabPanelProps } from '../src/atlas/a11y.mjs'
import { clampInspector, dragInspectorWidth, INSPECTOR, keyboardInspectorWidth, readInspectorWidth, saveInspectorWidth } from '../src/atlas/layout.mjs'
import { AGENT_COLORS, colorLabel, nextTheme, STATES, toolMark, TOOLS } from '../src/atlas/vocabulary.mjs'
import { PROVIDER_IDS } from '../src/providerPresentation.mjs'
import { themeModes } from '../src/theme.mjs'

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial))
  return { getItem: (key) => values.has(key) ? values.get(key) : null, setItem: (key, value) => values.set(key, String(value)), values }
}
const brokenStorage = { getItem() { throw new Error('denied') }, setItem() { throw new Error('denied') } }

test('routes parse, format and round-trip; unknown routes are explicit', () => {
  assert.deepEqual(parseHash('#/work'), { name: 'work', params: {} })
  assert.deepEqual(parseHash('#/'), { name: DEFAULT_ROUTE, params: {} })
  assert.deepEqual(parseHash('#/settings?section=appearance'), { name: 'settings', params: { section: 'appearance' } })
  assert.deepEqual(parseHash('#/nope?x=1'), { name: 'not-found', requested: 'nope', params: { x: '1' } })
  assert.deepEqual(parseHash('#/work?bad=%E0%A4%A'), { name: 'work', params: {} })
  assert.deepEqual(parseHash('#/constructor'), { name: 'not-found', requested: 'constructor', params: {} })
  assert.equal(formatHash('work'), '#/work')
  assert.equal(formatHash('gallery', { section: 'type', empty: '', none: null }), '#/gallery?section=type')
  assert.equal(formatHash('settings', { b: '2', a: 'x y' }), '#/settings?a=x%20y&b=2')
  for (const name of ['work', 'projects', 'connections', 'settings', 'new', 'gallery']) {
    assert.equal(parseHash(formatHash(name, { q: 'a&b=c' })).params.q, 'a&b=c')
  }
  assert.deepEqual(NAV_ROUTES, ['work', 'projects', 'connections', 'settings'])
  assert.equal(navSection(parseHash('#/new')), 'work')
  assert.equal(navSection(parseHash('#/gallery')), null)
  assert.equal(routeKey(parseHash('#/zzz')), 'not-found:zzz')
  assert.equal(documentTitle(parseHash('#/settings')), 'Settings · AI Agent Control Center')
  assert.equal(documentTitle(parseHash('#/zzz')), 'Page not found · AI Agent Control Center')
})

test('workspace totals show only reported values; missing counts are never zero', () => {
  assert.equal(overviewView(null), null)
  assert.equal(overviewView('broken'), null)
  const view = overviewView({ needs_input: 2, active_agents: 0, waiting_tasks: -1, completed_tasks: 1.5 })
  assert.deepEqual(view.map((item) => item.key), OVERVIEW_FIELDS.map(([key]) => key))
  assert.deepEqual(Object.fromEntries(view.map((item) => [item.key, item.value])),
    { needs_input: 2, active_agents: 0, waiting_tasks: null, pending_responsibilities: null, completed_tasks: null })
  assert.equal(needsYouCount({ needs_input: 3 }), 3)
  assert.equal(needsYouCount({}), null)
  assert.equal(needsYouLabel(1), '1 needs you')
  assert.equal(needsYouLabel(2), '2 need you')
})

test('the connection label follows the backend and the event transport', () => {
  assert.equal(connectionView({ transport: 'polling', reachable: null }).tone, 'pending')
  assert.equal(connectionView({ transport: 'live', reachable: false }).tone, 'offline')
  assert.equal(connectionView({ transport: 'live', reachable: true }).label, 'Live')
  assert.equal(connectionView({ transport: 'polling', reachable: true }).label, 'Polling')
})

test('roving focus moves within composite widgets, skips disabled items and mirrors in RTL', () => {
  assert.equal(rovingTarget(0, 'ArrowRight', 3), 1)
  assert.equal(rovingTarget(2, 'ArrowRight', 3), 0)
  assert.equal(rovingTarget(0, 'ArrowLeft', 3), 2)
  assert.equal(rovingTarget(1, 'Home', 3), 0)
  assert.equal(rovingTarget(0, 'End', 3), 2)
  assert.equal(rovingTarget(0, 'ArrowRight', 3, { isDisabled: (index) => index === 1 }), 2)
  assert.equal(rovingTarget(0, 'ArrowRight', 3, { dir: 'rtl' }), 2)
  assert.equal(rovingTarget(0, 'ArrowDown', 3, { orientation: 'vertical' }), 1)
  assert.equal(rovingTarget(0, 'ArrowDown', 3), null)
  assert.equal(rovingTarget(0, 'Enter', 3), null)
  assert.equal(rovingTarget(0, 'ArrowRight', 0), null)
  assert.equal(rovingTarget(0, 'ArrowRight', 2, { isDisabled: () => true }), null)
  assert.deepEqual(tabPanelProps('t', 'now'), { role: 'tabpanel', id: 't-panel-now', 'aria-labelledby': 't-tab-now', tabIndex: 0 })
})

test('avatar initials work for one or two words and any script', () => {
  assert.equal(initials('Toolkit lead'), 'TL')
  assert.equal(initials('codex'), 'CO')
  assert.equal(initials('  '), '?')
  assert.equal(initials('דוחות תאריכים'), 'דת')
})

test('the inspector width is clamped, dragged from its left edge and resizable by keyboard', () => {
  assert.equal(clampInspector(10), INSPECTOR.min)
  assert.equal(clampInspector(9999), INSPECTOR.max)
  assert.equal(clampInspector(Number.NaN), INSPECTOR.default)
  assert.equal(dragInspectorWidth(404, 500, 480), 424)
  assert.equal(keyboardInspectorWidth(404, 'ArrowLeft'), 420)
  assert.equal(keyboardInspectorWidth(404, 'ArrowRight'), 388)
  assert.equal(keyboardInspectorWidth(404, 'Home'), INSPECTOR.default)
  assert.equal(keyboardInspectorWidth(404, 'Escape'), null)
  const storage = memoryStorage()
  saveInspectorWidth(9999, storage)
  assert.equal(readInspectorWidth(storage), INSPECTOR.max)
  assert.equal(readInspectorWidth(brokenStorage), INSPECTOR.default)
})

test('Atlas vocabulary matches the backend and the classic interface', () => {
  const persistence = readFileSync(new URL('../../backend/app/persistence.py', import.meta.url), 'utf8')
  const backendColors = /display_color IN \(([^)]*)\)/.exec(persistence)[1].split(',').map((value) => value.trim().replace(/'/g, ''))
  assert.deepEqual(AGENT_COLORS, backendColors)
  assert.deepEqual(Object.keys(TOOLS).sort(), [...PROVIDER_IDS].sort())
  assert.equal(toolMark('nope'), '?')
  assert.equal(colorLabel('magenta'), 'neutral')
  assert.ok(STATES.ask && STATES.review && STATES.removed)
  const seen = new Set()
  let mode = 'automatic'
  for (let i = 0; i < themeModes.length; i++) { seen.add(mode); mode = nextTheme(mode) }
  assert.deepEqual([...seen].sort(), [...themeModes].sort())
  assert.equal(mode, 'automatic')
})
