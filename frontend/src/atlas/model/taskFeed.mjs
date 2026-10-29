// Keeps the canonical store current. The shell's single event connection calls `request()` for
// each change; requests that arrive close together, or while a read is running, collapse into one
// follow-up read. Reads never overlap, and a result is applied only if no newer read was started
// after it, so an older response can never replace newer state. A failed read keeps the last good
// store and marks it stale.
import { emptyStore } from './tasks.mjs'
import { PAGE_SIZE } from './taskSource.mjs'

export function createTaskFeed({ read, onChange, timers = globalThis, delay = 60, pageSize = PAGE_SIZE }) {
  let state = { store: emptyStore(), phase: 'loading', stale: false, error: null, pages: 1, readAt: null, loadingMore: false }
  let running = false
  let dirty = false
  let timer = null
  let disposed = false
  let started = 0
  let controller = null

  const emit = (patch) => {
    state = { ...state, ...patch }
    if (!disposed) onChange(state)
  }

  async function run() {
    timer = null
    if (disposed) return
    running = true
    dirty = false
    const id = ++started
    controller = new AbortController()
    const pages = state.pages
    try {
      const store = await read({ maxPages: pages, limit: pageSize, signal: controller.signal })
      if (disposed || id !== started) return
      emit({ store, phase: 'ready', stale: false, error: null, readAt: Date.now(), loadingMore: false })
    } catch (error) {
      if (disposed || id !== started || error?.name === 'AbortError') return
      emit({ phase: state.readAt ? 'ready' : 'error', stale: true, error: error?.message || 'Task list unavailable', loadingMore: false })
      // Retry on its own; the event connection also asks again when the backend returns.
      if (!dirty) timer = timers.setTimeout(run, 2000)
    } finally {
      running = false
      if (dirty && !disposed && timer === null) timer = timers.setTimeout(run, 0)
    }
  }

  function request(immediate = false) {
    if (disposed) return
    dirty = true
    if (running) return
    if (timer !== null) {
      if (!immediate) return
      timers.clearTimeout(timer)
    }
    timer = timers.setTimeout(run, immediate ? 0 : delay)
  }

  return {
    request,
    // One more page is kept from now on, including on later refreshes.
    loadMore() {
      if (disposed || !state.readAt || state.store.complete) return
      emit({ pages: state.pages + 1, loadingMore: true })
      request(true)
    },
    get state() { return state },
    dispose() {
      disposed = true
      timers.clearTimeout(timer)
      controller?.abort()
    },
  }
}
