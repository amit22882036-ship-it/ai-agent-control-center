export const OUTPUT_BATCH = 300
export const OUTPUT_WINDOW = 2000

export function followOutput(direction, scrollHeight, scrollTop, clientHeight) {
  return direction === 'latest' || (direction === 'live' && scrollHeight - scrollTop - clientHeight < 40)
}

export function anchoredScrollTop(scrollTop, lineTop, panelTop, offset) {
  return scrollTop + lineTop - panelTop - offset
}

export function emptyOutput(agentId) {
  return { agentId, items: [], cursor: null, hasOlder: false, browsing: false }
}

export function outputQuery(state, direction = 'live') {
  const params = new URLSearchParams({ limit: OUTPUT_BATCH })
  if (direction === 'older' && state.items.length) params.set('before', state.items[0].seq)
  else if (direction === 'live' && state.cursor !== null) params.set('after', state.cursor)
  return `http://127.0.0.1:8000/agents/${state.agentId}/output?${params}`
}

export function mergeOutput(state, page, direction = 'live') {
  if (page.agent_id !== state.agentId) return state
  const incoming = page.items
  const cursor = incoming.reduce((last, item) => Math.max(last ?? -1, item.seq), state.cursor)
  // Reading an earlier window must not be displaced by new live output.
  // We still advance the incremental cursor; returning to latest reloads a tail.
  if (direction === 'live' && state.browsing) return { ...state, cursor }
  const existing = direction === 'latest' ? [] : state.items
  const merged = [...new Map([...existing, ...incoming].map((item) => [item.seq, item])).values()]
    .sort((a, b) => a.seq - b.seq)
  const overflow = merged.length > OUTPUT_WINDOW
  const items = direction === 'older' ? merged.slice(0, OUTPUT_WINDOW) : merged.slice(-OUTPUT_WINDOW)
  const extendsOldest = incoming.length && (!existing.length || incoming[0].seq <= existing[0].seq)
  const hasOlder = direction === 'older' && !incoming.length ? page.has_older
    : direction !== 'older' && overflow ? true
    : extendsOldest || !existing.length ? page.has_older : state.hasOlder
  return {
    ...state, items, cursor, hasOlder,
    browsing: direction === 'latest' ? false : state.browsing || (direction === 'older' && overflow),
  }
}

// Serialize older/live requests and derive cursors when they run, not when queued.
export function createOutputLoader(agentId, fetchPage, onChange) {
  let state = emptyOutput(agentId)
  let disposed = false
  let pending = Promise.resolve()
  return {
    load(direction = 'live') {
      const operation = pending.then(async () => {
        if (disposed) return false
        const page = await fetchPage(outputQuery(state, direction))
        if (disposed) return false
        state = mergeOutput(state, page, direction)
        onChange(state, direction)
        return direction === 'live' && page.has_newer && page.items.length > 0
      })
      pending = operation.catch(() => {})
      return operation
    },
    dispose() { disposed = true },
  }
}
