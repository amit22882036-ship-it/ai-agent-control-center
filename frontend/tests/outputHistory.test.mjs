import assert from 'node:assert/strict'
import test from 'node:test'
import { emptyOutput, mergeOutput, outputQuery, createOutputLoader, followOutput, anchoredScrollTop, OUTPUT_BATCH, OUTPUT_WINDOW } from '../src/outputHistory.mjs'

const items = (start, end) => Array.from({ length: end - start }, (_, i) => ({ seq: start + i, text: `line ${start + i}` }))
const page = (start, end, extra = {}) => ({ agent_id: 'agent-a', items: items(start, end), has_older: start > 0, has_newer: false, ...extra })

test('scrolling follows live output only near bottom and preserves position when older entries prepend', () => {
  assert.equal(followOutput('live', 1000, 790, 200), true)
  assert.equal(followOutput('live', 1000, 300, 200), false)
  assert.equal(followOutput('older', 1000, 790, 200), false)
  assert.equal(followOutput('latest', 1000, 0, 200), true)
  assert.equal(anchoredScrollTop(300, 720, 100, 20), 900)
  assert.equal(anchoredScrollTop(900, 120, 100, 20), 900)
})

test('selection starts with a recent bounded tail request', async () => {
  const urls = []
  let state
  const loader = createOutputLoader('agent-a', async (url) => {
    urls.push(url)
    return page(500, 800)
  }, (next) => { state = next })
  await loader.load()
  assert.equal(urls[0], `http://127.0.0.1:8000/agents/agent-a/output?limit=${OUTPUT_BATCH}`)
  assert.equal(state.items.length, OUTPUT_BATCH)
  assert.equal(state.cursor, 799)
  assert.equal(state.hasOlder, true)
})

test('incremental items append in order with deduplication across reconnect snapshots', () => {
  let state = mergeOutput(emptyOutput('agent-a'), page(0, 5))
  state = mergeOutput(state, page(3, 8))
  state = mergeOutput(state, page(3, 8))
  assert.deepEqual(state.items, items(0, 8))
  assert.equal(state.cursor, 7)
  assert.equal(state.hasOlder, false)
})

test('out-of-order responses never regress cursor or corrupt ordering', () => {
  let state = mergeOutput(emptyOutput('agent-a'), page(0, 5))
  state = mergeOutput(state, page(5, 10))
  state = mergeOutput(state, { ...page(3, 7), items: items(3, 7).reverse() })
  assert.deepEqual(state.items, items(0, 10))
  assert.equal(state.cursor, 9)
})

test('load older prepends without duplicates and tracks has_older', () => {
  let state = mergeOutput(emptyOutput('agent-a'), page(300, 600))
  assert.match(outputQuery(state, 'older'), /before=300/)
  state = mergeOutput(state, page(100, 310), 'older')
  assert.deepEqual(state.items, items(100, 600))
  assert.equal(state.hasOlder, true)
  state = mergeOutput(state, page(0, 100), 'older')
  assert.deepEqual(state.items, items(0, 600))
  assert.equal(state.hasOlder, false)
  assert.equal(state.cursor, 599)
})

test('new agent state resets cursors and ignores old agent responses', () => {
  const old = mergeOutput(emptyOutput('agent-a'), page(100, 200))
  const fresh = emptyOutput('agent-b')
  assert.equal(fresh.cursor, null)
  assert.deepEqual(fresh.items, [])
  assert.equal(mergeOutput(fresh, page(0, 500)), fresh)
  assert.equal(old.agentId, 'agent-a')
})

test('SSE and reconnect refreshes request only entries after last loaded sequence', async () => {
  const urls = []
  const pages = [page(100, 400), page(400, 700, { has_newer: true }), page(700, 800), page(0, 0)]
  const loader = createOutputLoader('agent-a', async (url) => { urls.push(url); return pages.shift() }, () => {})
  assert.equal(await loader.load(), false)
  assert.equal(await loader.load(), true)
  await loader.load()
  await loader.load()
  assert.match(urls[1], /after=399/)
  assert.match(urls[2], /after=699/)
  assert.match(urls[3], /after=799/)
})

test('large live histories retain only the newest configured window', () => {
  let state = emptyOutput('agent-a')
  for (let start = 0; start < 12000; start += OUTPUT_BATCH) {
    state = mergeOutput(state, page(start, start + OUTPUT_BATCH))
    assert.ok(state.items.length <= OUTPUT_WINDOW)
  }
  assert.deepEqual(state.items, items(12000 - OUTPUT_WINDOW, 12000))
  assert.equal(state.cursor, 11999)
  assert.equal(state.hasOlder, true)
})

test('older browsing is bounded and return to latest resumes live output without gaps', () => {
  let state = mergeOutput(emptyOutput('agent-a'), page(4000, 6000))
  state = mergeOutput(state, page(3700, 4000), 'older')
  assert.equal(state.browsing, true)
  assert.deepEqual(state.items, items(3700, 5700))
  state = mergeOutput(state, page(6000, 6300))
  assert.deepEqual(state.items, items(3700, 5700))
  assert.equal(state.cursor, 6299)
  assert.doesNotMatch(outputQuery(state, 'latest'), /after|before/)
  state = mergeOutput(state, page(6000, 6300), 'latest')
  assert.equal(state.browsing, false)
  state = mergeOutput(state, page(6300, 6305))
  assert.deepEqual(state.items, items(6000, 6305))
})

test('output navigation leaves selected details and hierarchy/filter state untouched', () => {
  const dashboard = Object.freeze({ selectedAgentId: 'agent-a', search: 'Review', status: 'waiting', parentId: 'root' })
  let state = mergeOutput(emptyOutput(dashboard.selectedAgentId), page(300, 600))
  state = mergeOutput(state, page(0, 300), 'older')
  assert.equal(state.agentId, dashboard.selectedAgentId)
  assert.deepEqual(dashboard, { selectedAgentId: 'agent-a', search: 'Review', status: 'waiting', parentId: 'root' })
})

test('queued older/live requests derive up-to-date cursors and cleanup ignores pending responses', async () => {
  const urls = []
  const changes = []
  let finish
  const loader = createOutputLoader('agent-a', (url) => {
    urls.push(url)
    return new Promise((resolve) => { finish = resolve })
  }, (state) => changes.push(state))
  const first = loader.load()
  const second = loader.load('older')
  await Promise.resolve()
  assert.equal(urls.length, 1)
  finish(page(300, 600))
  await first
  await Promise.resolve()
  assert.equal(urls.length, 2)
  assert.match(urls[1], /before=300/)
  loader.dispose()
  finish(page(0, 300))
  await second
  assert.equal(changes.length, 1)
  await loader.load()
  assert.equal(urls.length, 2)
})

test('failed output requests do not advance cursor and can be retried', async () => {
  let fail = true
  let state
  const urls = []
  const loader = createOutputLoader('agent-a', async (url) => {
    urls.push(url)
    if (fail) throw new Error('Unavailable')
    return page(0, 2)
  }, (next) => { state = next })
  await assert.rejects(loader.load(), /Unavailable/)
  fail = false
  await loader.load()
  assert.equal(urls[0], urls[1])
  assert.equal(state.cursor, 1)
})
