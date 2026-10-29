// Reads GET /workspace/tasks into the canonical store. Network access is injected, so the reader
// is testable without a backend; Atlas passes the shared `apiFetch` client. It opens no event
// connection: the shell's single connection decides when to read again.
import { applyPage, emptyStore } from './tasks.mjs'

export const PAGE_SIZE = 200
export const MAX_PAGES = 50 // 10,000 Tasks; beyond that the store reports that more exist

export function tasksUrl(base, { limit = PAGE_SIZE, after = null, root = null } = {}) {
  const params = new URLSearchParams({ limit: String(limit) })
  if (after) params.set('after', after)
  if (root) params.set('root', root)
  return `${base}/workspace/tasks?${params}`
}

// Read every page (up to `maxPages`) and return a fresh store. Each page is one consistent backend
// snapshot; a change between pages is announced by the event connection, which starts a new read.
// Throws on an HTTP or network failure; the caller keeps its previous store.
export async function readTasks({ fetcher, base, root = null, limit = PAGE_SIZE, maxPages = MAX_PAGES, signal } = {}) {
  let store = emptyStore()
  let after = null
  for (let page = 0; page < maxPages; page += 1) {
    const response = await fetcher(tasksUrl(base, { limit, after, root }), { signal, cache: 'no-store' })
    if (!response.ok) throw new Error(`Task list unavailable (${response.status})`)
    store = applyPage(store, await response.json(), { append: page > 0 })
    after = store.nextAfter
    if (!after) return store
    if (signal?.aborted) break
  }
  return { ...store, complete: false }
}
