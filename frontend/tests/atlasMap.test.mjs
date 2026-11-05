import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace, flatRows, inBranch, descendantCount, visibleRows } from '../src/atlas/model/workList.mjs'
import { MAP, boxOf, branchIds, edgeTone, mapKey, mapLayout } from '../src/atlas/model/mapLayout.mjs'
import { ZOOM, centreOn, ensureVisible, fitCamera, followAnchor, intersects, worldRect, zoomAt } from '../src/atlas/model/mapCamera.mjs'

const contract = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const store = applyPage(emptyStore(), contract.all)
const derived = deriveWorkspace(store)
const allOpen = new Set(Object.keys(store.tasks))

test('the map shows exactly the rows the list shows, with edges only from parent_task_id', () => {
  const view = visibleRows(store, derived, { expanded: allOpen })
  const layout = mapLayout(view, derived)
  assert.deepEqual(Object.keys(layout.nodes).sort(), flatRows(view).map((row) => row.id).sort())
  // Edges: release -> docs, release -> tests, tests -> flaky, and nothing for cycles or a missing parent.
  assert.equal(layout.edgeCount, 3)
  assert.deepEqual(layout.parent, { docs: 'release', tests: 'release', flaky: 'tests' })
  assert.equal(layout.nodes.stray.parentId, null) // its parent is missing: no edge is invented
  assert.equal(layout.nodes['loop-a'].parentId, null)
  // A lead is centred over its helpers.
  const { release, docs, tests } = layout.nodes
  const middle = (docs.x + docs.w / 2 + tests.x + tests.w / 2) / 2
  assert.ok(Math.abs(release.x + release.w / 2 - middle) < 0.5)
  assert.ok(docs.y > release.y + release.h)
})

test('collapsed branches show no children, and the layout is deterministic', () => {
  const view = visibleRows(store, derived, { expanded: new Set() })
  const layout = mapLayout(view, derived)
  assert.equal(layout.nodes.docs, undefined)
  assert.equal(layout.edgeCount, 0)
  assert.deepEqual(mapLayout(view, derived), layout)
  assert.equal(descendantCount(derived.forest, 'release'), 3)
})

test('families follow the list sections in bands, oldest first, and wrap instead of growing endlessly', () => {
  const view = visibleRows(store, derived, { expanded: allOpen })
  const layout = mapLayout(view, derived, { maxRow: 600 })
  assert.deepEqual(layout.bands.map((band) => band.key), view.sections.map((section) => section.key))
  for (let i = 1; i < layout.bands.length; i += 1) assert.ok(layout.bands[i].y > layout.bands[i - 1].y)
  // Inside a band, the oldest family (the list's last) comes first.
  const active = view.sections.find((section) => section.key === 'active').families.map((family) => family.topId)
  const activeTops = layout.tops.filter((id) => active.includes(id))
  assert.deepEqual(activeTops, [...active].reverse())
  // Wrapping: with a narrow row limit, families move to new rows (several distinct y positions).
  const ys = new Set(activeTops.map((id) => layout.nodes[id].y))
  assert.ok(ys.size > 1)
  for (const id of Object.keys(layout.nodes)) assert.ok(layout.nodes[id].x + layout.nodes[id].w <= layout.width)
})

test('a lead whose helpers are all leaves stacks them under a shared thread', () => {
  // A synthetic family: one lead with three leaf helpers.
  const page = { tasks: ['a', 'b', 'c'].map((id, index) => ({ ...contract.all.tasks.find((t) => t.task_id === 'docs'), task_id: id, parent_task_id: 'lead',
    title: `Helper ${id}`, created_at: `2026-10-29T10:0${index}:00Z`, assignment: { current: null, last_ended: null, count: 0, previous_agent_ids: [] }, agent: null, delegation: null }))
    .concat([{ ...contract.all.tasks.find((t) => t.task_id === 'solo'), task_id: 'lead', parent_task_id: null, child_count: 3, title: 'Lead' }]),
  ancestors: [], ancestors_truncated: false, next_after: null, total: 4, root: null }
  const s = applyPage(emptyStore(), page)
  const d = deriveWorkspace(s)
  const layout = mapLayout(visibleRows(s, d, { expanded: new Set(['lead']) }), d)
  assert.equal(layout.nodes.a.kind, 'row')
  assert.ok(layout.nodes.lead.spine !== undefined)
  assert.ok(layout.nodes.b.y > layout.nodes.a.y && layout.nodes.c.y > layout.nodes.b.y)
  assert.equal(layout.nodes.a.x, layout.nodes.c.x)
  assert.equal(layout.edgeCount, 4) // three branches off the spine, plus the spine itself
})

test('a focused branch is the only family, at depth 0, and inBranch follows the loaded parents', () => {
  const view = visibleRows(store, derived, { expanded: allOpen, focus: 'tests', filter: 'done' })
  assert.equal(view.sections.length, 1)
  assert.equal(view.sections[0].key, 'focus')
  assert.deepEqual(flatRows(view).map((row) => [row.id, row.depth]), [['tests', 0], ['flaky', 1]])
  assert.ok(inBranch(derived.forest, 'release', 'flaky'))
  assert.ok(!inBranch(derived.forest, 'tests', 'docs'))
  const layout = mapLayout(view, derived)
  assert.deepEqual(layout.tops, ['tests'])
  assert.deepEqual(branchIds(layout, 'tests').sort(), ['flaky', 'tests'])
})

test('edge tones come from the task below: removed agents, questions and work', () => {
  assert.equal(edgeTone(derived, 'orphaned-work'), 'gone')
  assert.equal(edgeTone(derived, 'docs'), 'plain')
})

test('keyboard movement follows what is drawn', () => {
  const view = visibleRows(store, derived, { expanded: allOpen })
  const layout = mapLayout(view, derived)
  const collapsed = () => false
  assert.deepEqual(mapKey(layout, 'docs', 'ArrowUp', { collapsed }), { focus: 'release' })
  assert.deepEqual(mapKey(layout, 'release', 'ArrowDown', { collapsed }), { focus: layout.children.release[0] })
  const [first, second] = layout.children.release
  assert.deepEqual(mapKey(layout, first, 'ArrowRight', { collapsed }), { focus: second })
  assert.equal(mapKey(layout, second, 'ArrowRight', { collapsed }), null)
  assert.deepEqual(mapKey(layout, 'release', 'Enter', { collapsed }), { select: 'release' })
  assert.deepEqual(mapKey(layout, 'release', ' ', { collapsed }), { collapse: 'release' })
  // Between families at the top level.
  const i = layout.tops.indexOf('release')
  if (i > 0) assert.deepEqual(mapKey(layout, 'release', 'ArrowLeft', { collapsed }), { focus: layout.tops[i - 1] })
  // A collapsed branch opens on the way down.
  const closed = mapLayout(visibleRows(store, derived, { expanded: new Set() }), derived)
  assert.deepEqual(mapKey(closed, 'release', 'ArrowDown', { collapsed: (id) => id === 'release' }), { expand: 'release', descend: true })
  assert.deepEqual(mapKey(closed, 'release', ' ', { collapsed: (id) => id === 'release' }), { expand: 'release' })
})

test('the camera fits, zooms around a point, follows a moved task and only moves when needed', () => {
  const view = { l: 0, w: 1000, h: 700 }
  const small = { minX: 0, minY: 0, maxX: 400, maxY: 300 }
  const fit = fitCamera(small, view)
  assert.equal(fit.k, 1) // never enlarged past 100%
  assert.ok(fit.x > 0 && fit.x + 400 < 1000)
  // A huge box keeps text legible and centres on the anchor.
  const huge = { minX: 0, minY: 0, maxX: 20000, maxY: 400 }
  const anchored = fitCamera(huge, view, { anchor: { minX: 10000, minY: 0, maxX: 10168, maxY: 120 } })
  assert.equal(anchored.k, ZOOM.legible)
  assert.ok(Math.abs(anchored.x + 10084 * anchored.k - 500) < 1)
  // Only too tall: centred across, with the anchor in view vertically.
  const tall = { minX: 0, minY: 0, maxX: 500, maxY: 3000 }
  const across = fitCamera(tall, view, { anchor: { minX: 0, minY: 2000, maxX: 168, maxY: 2120 } })
  assert.equal(across.k, ZOOM.legible)
  assert.ok(Math.abs(across.x + 250 * across.k - 500) < 1)
  assert.ok(Math.abs(across.y + 2060 * across.k - 350) < 1)
  // "Show everything" may go smaller.
  assert.ok(fitCamera(huge, view, { allowSmall: true }).k < ZOOM.legible)
  // Zooming keeps the point under the cursor in place.
  const z = zoomAt({ k: 1, x: 0, y: 0 }, 1.2, 300, 200)
  assert.ok(Math.abs((300 - z.x) / z.k - 300) < 1e-9 && Math.abs((200 - z.y) / z.k - 200) < 1e-9)
  assert.equal(zoomAt({ k: 1.5, x: 0, y: 0 }, 4, 0, 0).k, ZOOM.max)
  // ensureVisible leaves a visible box alone and centres one that is off screen, along the axis
  // that needs it only.
  const camera = { k: 1, x: 0, y: 0 }
  assert.equal(ensureVisible(camera, { minX: 100, minY: 100, maxX: 268, maxY: 220 }, view), camera)
  const moved = ensureVisible(camera, { minX: 3000, minY: 100, maxX: 3168, maxY: 220 }, view)
  assert.deepEqual(moved, { ...centreOn(camera, { minX: 3000, minY: 100, maxX: 3168, maxY: 220 }, view), y: 0 })
  const both = { minX: 3000, minY: 3000, maxX: 3168, maxY: 3120 }
  assert.deepEqual(ensureVisible(camera, both, view), centreOn(camera, both, view))
  // UAT: a task near the top of the graph, scrolled out above the view, comes back without a
  // large empty area opening above the graph; the deliberate horizontal position is kept.
  const graph = { minX: 0, minY: 0, maxX: 4000, maxY: 3000 }
  const scrolled = { k: 1, x: -50, y: -800 }
  const near = ensureVisible(scrolled, { minX: 200, minY: 20, maxX: 368, maxY: 140 }, view, 36, graph)
  assert.equal(near.x, -50)
  assert.equal(near.y, 64)  // The graph's top sits at the usual top padding, not at mid-screen.
  assert.ok(near.y + 20 >= 36 && near.y + 140 <= view.h - 36)
  // Near the bottom of a tall graph the view never shows empty space below it.
  const low = ensureVisible({ k: 1, x: 0, y: 0 }, { minX: 200, minY: 2900, maxX: 368, maxY: 2990 }, view, 36, graph)
  assert.equal(low.y, view.h - 28 - 3000)
  // A side list covering the left edge counts as not visible.
  assert.notEqual(ensureVisible(camera, { minX: 10, minY: 100, maxX: 178, maxY: 220 }, { l: 300, w: 700, h: 700 }), camera)
  // After a refresh moves the selected task, the camera follows so it stays put on screen.
  const after = followAnchor({ k: 0.5, x: 10, y: 20 }, { x: 100, y: 100 }, { x: 300, y: 100 })
  assert.deepEqual(after, { k: 0.5, x: -90, y: 20 })
  // The drawn region.
  const rect = worldRect({ k: 2, x: -100, y: -2 }, { l: 0, w: 400, h: 300 })
  assert.deepEqual(rect, { minX: 50, maxX: 250, minY: 1, maxY: 151 })
  assert.ok(intersects(rect, { minX: 240, maxX: 260, minY: 10, maxY: 20 }))
  assert.ok(!intersects(rect, { minX: 260, maxX: 280, minY: 10, maxY: 20 }))
})

test('large hierarchies lay out quickly and without overlaps between families', () => {
  const tasks = []
  for (let f = 0; f < 50; f += 1) {
    tasks.push({ ...contract.all.tasks.find((t) => t.task_id === 'solo'), task_id: `r${f}`, parent_task_id: null, child_count: 19, created_at: `2026-10-0${1 + (f % 9)}T00:00:${String(f).padStart(2, '0')}Z` })
    for (let c = 0; c < 19; c += 1) {
      tasks.push({ ...contract.all.tasks.find((t) => t.task_id === 'docs'), task_id: `r${f}-${c}`, parent_task_id: `r${f}`, delegation: null,
        created_at: `2026-10-10T00:${String(c).padStart(2, '0')}:00Z` })
    }
  }
  const s = applyPage(emptyStore(), { tasks, ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null })
  const d = deriveWorkspace(s)
  const view = visibleRows(s, d, { expanded: new Set(Object.keys(s.tasks)) })
  const started = performance.now()
  const layout = mapLayout(view, d)
  const elapsed = performance.now() - started
  assert.equal(Object.keys(layout.nodes).length, 1000)
  assert.ok(elapsed < 250, `layout took ${elapsed.toFixed(1)} ms`)
  // Family zones never overlap.
  const zones = layout.zones.map((z) => ({ minX: z.x, maxX: z.x + z.w, minY: z.y, maxY: z.y + z.h }))
  for (let i = 0; i < zones.length; i += 1) {
    for (let j = i + 1; j < zones.length; j += 1) assert.ok(!intersects(zones[i], zones[j]), `families ${i} and ${j} overlap`)
  }
  assert.ok(boxOf(layout).maxX <= layout.width)
  assert.ok(MAP.MAX_ROW > 0)
})

test('left to right: helpers sit to the right of their lead, families stack, nothing overlaps, and the arrows turn', () => {
  const view = visibleRows(store, derived, { expanded: allOpen })
  const down = mapLayout(view, derived)
  const right = mapLayout(view, derived, { direction: 'right' })
  assert.equal(right.direction, 'right')
  assert.equal(down.direction, 'down')
  assert.deepEqual(Object.keys(right.nodes).sort(), Object.keys(down.nodes).sort())
  assert.equal(right.edgeCount, down.edgeCount)
  const { release, docs, tests, flaky } = right.nodes
  assert.ok(docs.x >= release.x + release.w && tests.x >= release.x + release.w)
  assert.ok(flaky.x >= tests.x + tests.w)
  // The lead sits beside the middle of its helpers' block.
  const lead = release.y + release.h / 2
  assert.ok(lead > Math.min(docs.y, tests.y) && lead < Math.max(docs.y + docs.h, tests.y + tests.h))
  const list = Object.values(right.nodes)
  for (const a of list) {
    assert.ok(a.x + a.w <= right.width && a.y + a.h <= right.height)
    for (const b of list) {
      if (a === b) continue
      assert.ok(a.x + a.w <= b.x || b.x + b.w <= a.x || a.y + a.h <= b.y || b.y + b.h <= a.y, `${a.id} overlaps ${b.id}`)
    }
  }
  // Family tops all start in the same column.
  assert.equal(new Set(right.tops.map((id) => right.nodes[id].x)).size, 1)
  assert.deepEqual(mapKey(right, 'release', 'ArrowRight'), { focus: right.children.release[0] })
  assert.deepEqual(mapKey(right, 'docs', 'ArrowLeft'), { focus: 'release' })
  assert.deepEqual(mapLayout(view, derived, { direction: 'right' }), right)
})
