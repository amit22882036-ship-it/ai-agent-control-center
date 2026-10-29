import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { applyPage, emptyStore } from '../src/atlas/model/tasks.mjs'
import { deriveWorkspace, durationText, familyCounts, flatRows, nowView, timeView, treeKey, visibleRows, whoView } from '../src/atlas/model/workList.mjs'
import { createTaskFeed } from '../src/atlas/model/taskFeed.mjs'

const contract = JSON.parse(readFileSync(new URL('./fixtures/workspace-tasks.contract.json', import.meta.url), 'utf8'))
const store = applyPage(emptyStore(), contract.all)
const derived = deriveWorkspace(store)
const allOpen = new Set(Object.keys(store.tasks))

test('families are grouped by what they need, from the status model', () => {
  const sections = Object.fromEntries(derived.families.map((family) => [family.topId, family.section]))
  assert.equal(sections.release, 'needs') // a helper asks the person
  assert.equal(sections['orphaned-work'], 'needs') // needs a new owner
  assert.equal(sections.solo, 'done') // applied to the project
  assert.equal(sections.limited, 'active') // a provider wait is not the person's to answer
  assert.equal(sections.paused, 'active')
  assert.deepEqual(familyCounts(derived), { all: 9, needs: 2, active: 8, done: 1 })
  assert.ok(derived.attentionBelow.has('release'))
  assert.ok(!derived.attentionBelow.has('tests'))
})

test('completed execution is not presented as success applied to the project', () => {
  const solo = nowView(store, derived, 'solo')
  assert.equal(solo.line, 'Applied to the project')
  assert.equal(solo.second, 'Not independently verified')
  const docs = nowView(store, derived, 'docs')
  assert.equal(docs.line, 'Integrated into the lead')
})

test('rows follow parent_task_id only, with guides, levels and sibling positions', () => {
  const view = visibleRows(store, derived, { expanded: allOpen })
  const rows = flatRows(view)
  const byId = Object.fromEntries(rows.map((row) => [row.id, row]))
  assert.equal(byId.release.depth, 0)
  assert.equal(byId.docs.depth, 1)
  assert.equal(byId.flaky.depth, 2)
  assert.equal(byId.flaky.parentId, 'tests')
  assert.deepEqual([byId.docs.position, byId.docs.setSize, byId.tests.position], [1, 2, 2])
  assert.deepEqual(byId.flaky.guides, [false])
  assert.equal(byId.tests.last, true)
  assert.equal(rows.length, 12)
  assert.deepEqual(view.sections.map((section) => section.key), ['needs', 'active', 'done'])
})

test('collapsed branches hide their descendants; default expansion is the caller’s', () => {
  const rows = flatRows(visibleRows(store, derived, { expanded: new Set() }))
  assert.ok(!rows.some((row) => row.id === 'docs'))
  assert.equal(rows.find((row) => row.id === 'release').expanded, false)
  assert.equal(rows.find((row) => row.id === 'release').hasChildren, true)
})

test('filters, project scope and search', () => {
  const needs = visibleRows(store, derived, { expanded: allOpen, filter: 'needs' })
  assert.deepEqual(needs.sections.map((section) => section.key), ['needs'])
  const done = flatRows(visibleRows(store, derived, { expanded: allOpen, filter: 'done' }))
  assert.deepEqual(done.map((row) => row.id), ['solo'])
  assert.equal(flatRows(visibleRows(store, derived, { expanded: allOpen, project: 'other' })).length, 0)
  const found = flatRows(visibleRows(store, derived, { expanded: new Set(), query: 'FLAKY' }))
  assert.deepEqual(found.map((row) => row.id), ['release', 'tests', 'flaky']) // the match and its loaded path, opened
  assert.deepEqual(found.map((row) => row.match), [false, false, true])
  const byAgent = flatRows(visibleRows(store, derived, { expanded: new Set(), query: 'tester' }))
  assert.ok(byAgent.some((row) => row.id === 'tests'))
})

test('keyboard behaviour follows the WAI-ARIA tree pattern', () => {
  const rows = flatRows(visibleRows(store, derived, { expanded: allOpen }))
  assert.deepEqual(treeKey(rows, 'release', 'ArrowDown'), { focus: 'docs' })
  assert.deepEqual(treeKey(rows, 'docs', 'ArrowUp'), { focus: 'release' })
  assert.deepEqual(treeKey(rows, 'release', 'ArrowLeft'), { collapse: 'release' })
  assert.deepEqual(treeKey(rows, 'docs', 'ArrowLeft'), { focus: 'release' })
  assert.deepEqual(treeKey(rows, 'release', 'ArrowRight'), { focus: 'docs' })
  assert.equal(treeKey(rows, 'docs', 'ArrowRight'), null)
  assert.deepEqual(treeKey(rows, 'tests', 'Enter'), { select: 'tests' })
  assert.deepEqual(treeKey(rows, 'docs', 'Home'), { focus: rows[0].id })
  assert.deepEqual(treeKey(rows, 'docs', 'End'), { focus: rows.at(-1).id })
  const closed = flatRows(visibleRows(store, derived, { expanded: new Set() }))
  assert.deepEqual(treeKey(closed, 'release', 'ArrowRight'), { expand: 'release' })
  assert.equal(treeKey([], 'x', 'ArrowDown'), null)
})

test('current, last, replaced, removed and missing agents', () => {
  assert.deepEqual([whoView(derived, 'tests').role, whoView(derived, 'tests').name], ['current', 'Tester'])
  const docs = whoView(derived, 'docs')
  assert.deepEqual([docs.role, docs.replaced, docs.previousCount], ['last', true, 2])
  assert.match(docs.detail, /^Last agent/)
  const gone = whoView(derived, 'orphaned-work')
  assert.equal(gone.removed, true)
  assert.match(gone.detail, /removed/)
  assert.equal(whoView(derived, 'flaky').agent, null)
})

test('waiting for helpers counts only helpers still working', () => {
  const view = nowView(store, derived, 'release')
  assert.equal(view.line, 'Reviewing helper results')
  assert.equal(view.second, '1 result ready')
})

test('times are honest about what they measure', () => {
  const now = Date.parse('2026-01-01T02:00:00.000Z')
  assert.equal(timeView(store, 'tests', now).title, 'Time since its current agent took this task')
  assert.equal(timeView(store, 'solo', now).title, 'Time since its last agent finished')
  assert.equal(timeView(store, 'flaky', now).title, 'Time since the task was created')
  assert.equal(durationText(30_000), 'just now')
  assert.equal(durationText(125 * 60_000), '2h 5m')
  assert.equal(durationText(3 * 24 * 60 * 60_000), '3d')
})

// A fake clock: `step()` runs the earliest pending timer and waits for its promise chain.
function fakeTimers() {
  let id = 0
  const pending = new Map()
  return {
    setTimeout(fn, ms) { pending.set(++id, { fn, ms }); return id },
    clearTimeout(handle) { pending.delete(handle) },
    get size() { return pending.size },
    delays() { return [...pending.values()].map((item) => item.ms) },
    async step() {
      const next = pending.entries().next().value
      if (!next) return false
      pending.delete(next[0])
      await next[1].fn()
      for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve))
      return true
    },
  }
}
const settle = async () => { for (let i = 0; i < 5; i += 1) await new Promise((resolve) => setImmediate(resolve)) }

test('the feed collapses bursts, never overlaps reads and applies only the newest read', async () => {
  const timers = fakeTimers()
  const calls = []
  const waiting = []
  let running = 0
  let overlapped = false
  const states = []
  const feed = createTaskFeed({
    timers,
    onChange: (state) => states.push(state),
    read: (options) => {
      calls.push(options.maxPages)
      running += 1
      if (running > 1) overlapped = true
      return new Promise((resolve) => waiting.push(() => { running -= 1; resolve(applyPage(emptyStore(), contract.all)) }))
    },
  })
  feed.request()
  feed.request()
  feed.request()
  assert.equal(timers.size, 1) // three requests, one scheduled read
  const first = timers.step()
  await settle()
  assert.equal(calls.length, 1)
  feed.request() // during the read
  feed.request()
  assert.equal(timers.size, 0) // nothing starts while a read runs
  waiting.shift()()
  await first
  assert.equal(timers.size, 1) // one follow-up read for both
  const second = timers.step()
  await settle()
  assert.equal(calls.length, 2)
  waiting.shift()()
  await second
  assert.equal(overlapped, false)
  assert.equal(states.at(-1).phase, 'ready')
  feed.dispose()
})

test('a failed read keeps the last good store, marks it stale and retries; load more keeps the page count', async () => {
  const timers = fakeTimers()
  let fail = false
  const pages = []
  const states = []
  const feed = createTaskFeed({
    timers,
    onChange: (state) => states.push(state),
    read: async ({ maxPages }) => {
      pages.push(maxPages)
      if (fail) throw new Error('Task list unavailable (503)')
      return applyPage(emptyStore(), { ...contract.paged[0] })
    },
  })
  feed.loadMore() // nothing loaded yet: ignored
  assert.equal(feed.state.pages, 1)
  feed.request(true)
  await timers.step()
  assert.equal(feed.state.phase, 'ready')
  const good = feed.state.store
  fail = true
  feed.request(true)
  await timers.step()
  assert.equal(feed.state.stale, true)
  assert.equal(feed.state.phase, 'ready')
  assert.equal(feed.state.store, good)
  assert.deepEqual(timers.delays(), [2000]) // retries on its own
  fail = false
  await timers.step()
  assert.equal(feed.state.stale, false)
  feed.loadMore()
  assert.equal(feed.state.pages, 2)
  await timers.step()
  assert.equal(pages.at(-1), 2)
  feed.dispose()
  assert.equal(timers.size, 0)
})

test('a read that fails before anything loaded is an error, not an empty workspace', async () => {
  const timers = fakeTimers()
  const feed = createTaskFeed({ timers, onChange: () => {}, read: async () => { throw new Error('offline') } })
  feed.request(true)
  await timers.step()
  assert.equal(feed.state.phase, 'error')
  assert.equal(feed.state.stale, true)
  feed.dispose()
})
