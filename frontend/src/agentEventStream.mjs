// At most one REST request is in flight, with one trailing refresh if invalidated.
export function createRefreshQueue(refresh, timers = globalThis) {
  let timer = null
  let running = false
  let dirty = false
  let disposed = false
  async function run() {
    timer = null
    if (disposed) return
    running = true
    dirty = false
    let success = true
    try {
      success = await refresh()
    } finally {
      running = false
      if (!disposed && success === false) timer = timers.setTimeout(run, 2000)
      else if (dirty && !disposed) request()
    }
  }
  function request(immediate = false) {
    if (disposed) return
    dirty = true
    if (running || timer !== null) return
    timer = timers.setTimeout(run, immediate ? 0 : 75)
  }
  return {
    request,
    dispose() {
      disposed = true
      timers.clearTimeout(timer)
    },
  }
}

export function connectAgentEvents({ refresh, onStatus = () => {}, EventSourceClass = globalThis.EventSource, timers = globalThis }) {
  let source = null
  let debounce = null
  let polling = null
  let disposed = false
  const flush = () => {
    timers.clearTimeout(debounce)
    debounce = null
    if (!disposed) refresh()
  }
  const fallback = () => {
    if (!disposed && polling === null) {
      polling = timers.setInterval(flush, 2000)
      onStatus('polling')
    }
  }
  refresh()
  fallback()
  if (EventSourceClass) {
    try {
      source = new EventSourceClass('http://127.0.0.1:8000/events')
      source.onopen = () => {
        if (disposed) return
        timers.clearInterval(polling)
        polling = null
        onStatus('live')
        timers.clearTimeout(debounce)
        debounce = null
        flush()
      }
      source.onerror = fallback
      source.addEventListener('agent-change', () => {
        if (!disposed && debounce === null) debounce = timers.setTimeout(flush, 75)
      })
    } catch {
      // Polling already covers unavailable EventSource implementations.
      source?.close()
    }
  }
  return () => {
    disposed = true
    source?.close()
    timers.clearTimeout(debounce)
    timers.clearInterval(polling)
  }
}
