import test from 'node:test'
import assert from 'node:assert/strict'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace, flatRows, markedPaths, taskCategory, taskCounts, visibleRows } from '../src/atlas/model/workList.mjs'
import { ROW, changedSince, dayLabel, familyTime, rememberRecent, rowAt, sheetOffsets, sheetRows, sheetWindow, teamCounts } from '../src/atlas/model/workSheet.mjs'
import { busy, showcase } from '../e2e/scenarios.mjs'

const NOW = Date.parse('2026-10-29T12:00:00.000Z')
const load = (page) => {
  const store = applyPage(emptyStore(), page)
  return { store, derived: deriveWorkspace(store) }
}

test('every task has exactly one state category, and the counts add up to the loaded tasks', () => {
  const { store, derived } = load(busy({ families: 120 }))
  const counts = taskCounts(store, derived)
  const sum = ['needs', 'working', 'waiting', 'held', 'pending', 'done', 'canceled'].reduce((total, key) => total + counts[key], 0)
  assert.equal(sum, counts.tasks)
  assert.equal(counts.tasks, Object.keys(store.tasks).length)
  for (const key of ['needs', 'working', 'waiting', 'held', 'done', 'canceled']) assert.ok(counts[key] > 0, key)
  // Working means a running agent on a running task, nothing else.
  for (const id of Object.keys(store.tasks)) {
    if (taskCategory(derived, id) !== 'working') continue
    assert.equal(derived.agents[id].current?.status, 'running')
  }
})

test('the showcase states land in the categories a person would expect', () => {
  const { derived } = load(showcase())
  assert.equal(taskCategory(derived, 'quoted'), 'needs') // asked a question
  assert.equal(taskCategory(derived, 'migrate'), 'needs') // needs a new owner
  assert.equal(taskCategory(derived, 'report'), 'working')
  assert.equal(taskCategory(derived, 'csv'), 'waiting') // waiting for its helpers
  assert.equal(taskCategory(derived, 'limits'), 'waiting') // waiting for provider usage
  assert.equal(taskCategory(derived, 'backlog'), 'pending')
  assert.equal(taskCategory(derived, 'rename'), 'done')
})

test('a task scope keeps the tasks in that state and the path to them, nothing else', () => {
  const { store, derived } = load(busy({ families: 60 }))
  for (const scope of ['needs', 'working', 'waiting', 'held']) {
    const view = visibleRows(store, derived, { filter: scope })
    const rows = flatRows(view)
    assert.ok(rows.length > 0, scope)
    for (const row of rows) {
      if (row.match) assert.equal(taskCategory(derived, row.id), scope)
      else assert.ok(rows.some((other) => other.parentId === row.id), `${row.id} is shown only as a path`)
    }
  }
})

test('pruned branches show only the marked paths and say how many tasks are not shown', () => {
  const { store, derived } = load(showcase())
  const marked = markedPaths(store, derived)
  assert.ok(marked.has('quoted') && marked.has('csv') && !marked.has('report'))
  const view = visibleRows(store, derived, { expanded: new Set(['csv']), prune: new Set(['csv']), marked })
  const csv = view.sections.flatMap((section) => section.families).find((family) => family.topId === 'csv')
  assert.deepEqual(csv.rows.map((row) => row.id), ['csv', 'quoted'])
  assert.equal(csv.rows[0].hiddenChildren, 2)
  // Without pruning the whole team is there.
  const full = visibleRows(store, derived, { expanded: new Set(['csv', 'cli']) })
  assert.ok(flatRows(full).some((row) => row.id === 'report'))
})

test('sheet rows: sections, history by day, hidden-task rows, and sheet edges', () => {
  const { store, derived } = load(busy({ families: 40 }))
  const marked = markedPaths(store, derived)
  const expanded = new Set([...marked])
  const prune = new Set([...marked])
  const view = visibleRows(store, derived, { expanded, prune, marked })
  const rows = sheetRows(view, store, derived, { now: NOW, finishedOpen: true })
  assert.deepEqual(rows.filter((row) => row.kind === 'section').map((row) => row.section), view.sections.map((section) => section.key))
  assert.ok(rows.some((row) => row.kind === 'day'))
  assert.ok(rows.some((row) => row.kind === 'more' && row.hidden > 0))
  // Each run between headings is one sheet with a first and a last row.
  const first = rows.filter((row) => row.first).length
  const last = rows.filter((row) => row.last).length
  assert.equal(first, last)
  assert.equal(first, rows.filter((row) => row.kind === 'section').length)
  // Keys are unique: a task is drawn once.
  assert.equal(new Set(rows.map((row) => row.key)).size, rows.length)
  // Folded history offers itself instead of listing its families.
  const folded = sheetRows(view, store, derived, { now: NOW, finishedOpen: false })
  assert.ok(folded.some((row) => row.kind === 'reveal'))
  assert.ok(!folded.some((row) => row.kind === 'day'))
})

test('the selected task keeps its row: a held task outlives its scope and pruning, and a folded history still lists its family', () => {
  const { store, derived } = load(showcase())
  // "report" is working; in the "Need you" scope it is not listed unless it is held.
  const scoped = visibleRows(store, derived, { filter: 'needs' })
  assert.ok(!flatRows(scoped).some((row) => row.id === 'report'))
  const held = visibleRows(store, derived, { filter: 'needs', hold: 'report' })
  const rows = flatRows(held)
  assert.ok(rows.some((row) => row.id === 'report'))
  // With its path, so the tree stays whole.
  for (const row of rows) if (row.parentId) assert.ok(rows.some((other) => other.id === row.parentId), `${row.id} has its lead`)
  // Pruning to what needs you keeps the held task too.
  const marked = markedPaths(store, derived)
  const pruned = visibleRows(store, derived, { expanded: new Set(['csv', 'cli']), prune: new Set(['csv']), marked, hold: 'report' })
  assert.ok(flatRows(pruned).some((row) => row.id === 'report'))
  // A finished family is listed under a folded History when it holds the selection; the rest stays folded.
  const view = visibleRows(store, derived, {})
  const done = view.sections.find((section) => section.key === 'done')
  const folded = sheetRows(view, store, derived, { now: NOW, finishedOpen: false, keep: 'rename' })
  assert.ok(folded.some((row) => row.id === 'rename'))
  const reveal = folded.find((row) => row.kind === 'reveal')
  if (done.families.length > 1) assert.equal(reveal.count, done.families.length - 1)
  else assert.equal(reveal, undefined)
  assert.ok(!folded.some((row) => row.kind === 'day'))
  // Held in the section it was in when the hold began: a family that finished stays in Open, marked
  // as kept there, and Open's count doesn't include it.
  const pinned = visibleRows(store, derived, { hold: 'rename', holdSection: 'active' })
  const open = pinned.sections.find((section) => section.key === 'active')
  assert.ok(open.families.some((family) => family.topId === 'rename' && family.kept))
  assert.ok(!pinned.sections.some((section) => section.key === 'done' && section.families.some((family) => family.topId === 'rename')))
  const sheet = sheetRows(pinned, store, derived, { now: NOW })
  assert.equal(sheet.find((row) => row.key === 'section:active').count, open.families.length - 1)
})

test('history days are named plainly and families sort by their latest recorded change', () => {
  const day = (iso) => dayLabel(Date.parse(iso), NOW)
  assert.equal(day('2026-10-29T09:00:00Z').label, 'Today')
  assert.equal(day('2026-10-28T09:00:00Z').label, 'Yesterday')
  assert.equal(day('2026-10-26T09:00:00Z').label, 'Monday')
  assert.equal(day('2026-09-02T09:00:00Z').label, '2 September')
  assert.equal(dayLabel(null, NOW).label, 'Date not recorded')
  const { store, derived } = load(showcase())
  const family = derived.byTop.rename
  assert.equal(familyTime(store, family), Date.parse(store.tasks.rename.updatedAt))
})

test('offsets, the visible window and the row under a point agree for thousands of rows', () => {
  const { store, derived } = load(busy({ families: 600 }))
  const view = visibleRows(store, derived, { expanded: new Set() })
  const rows = sheetRows(view, store, derived, { now: NOW })
  const { tops, total } = sheetOffsets(rows, 1)
  assert.equal(tops[0], 0)
  assert.equal(total, tops[rows.length - 1] + ROW[rows[rows.length - 1].kind] + ROW.end)
  for (const y of [0, 333, 5000, total - 1]) {
    const index = rowAt(tops, y)
    assert.ok(tops[index] <= y && (index === rows.length - 1 || tops[index + 1] > y))
  }
  const { start, end } = sheetWindow(tops, total, 5000, 900, 480)
  assert.ok(end - start < 60, `${end - start} rows drawn`)
  assert.ok(tops[start] <= 5000 - 480 + ROW.reveal && tops[end - 1] >= 5000 + 900)
  // Twice the text size gives twice the heights.
  assert.equal(sheetOffsets(rows, 2).total, rows.reduce((sum, row) => sum + Math.round(ROW[row.kind] * 2), 0) + ROW.end * 2)
})

test('team counts cover the whole family, lead included', () => {
  const { derived } = load(showcase())
  const team = teamCounts(derived, derived.byTop.csv)
  assert.equal(team.size, 5)
  assert.equal(team.needs + team.working + team.waiting + team.held + team.pending + team.done + team.canceled, 5)
})

test('changed since you looked: after the baseline, until the task is opened', () => {
  const { store } = load(showcase())
  const updated = (id) => store.tasks[id].updatedAt
  const before = new Date(Date.parse(updated('csv')) - 60000).toISOString()
  const after = new Date(Date.parse(updated('csv')) + 60000).toISOString()
  assert.equal(changedSince(store, null)('csv'), false)
  assert.equal(changedSince(store, { since: after, opened: {} })('csv'), false)
  assert.equal(changedSince(store, { since: before, opened: {} })('csv'), true)
  assert.equal(changedSince(store, { since: before, opened: { csv: after } })('csv'), false)
})

test('recently opened: newest first, no repeats, bounded', () => {
  let list = []
  for (const id of ['a', 'b', 'c', 'a']) list = rememberRecent(list, id, 3)
  assert.deepEqual(list, ['a', 'c', 'b'])
  assert.deepEqual(rememberRecent('not a list', 'x'), ['x'])
})
