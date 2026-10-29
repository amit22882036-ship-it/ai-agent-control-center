import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch, API_BASE } from '../apiClient.mjs'
import { connectAgentEvents, createRefreshQueue } from '../agentEventStream.mjs'
import { connectTheme, readTheme } from '../theme.mjs'
import { formatHash, parseHash } from './router.mjs'
import { rememberAtlasHash } from './uiMode.mjs'
import { readTasks } from './model/taskSource.mjs'
import { createTaskFeed } from './model/taskFeed.mjs'

function subscribeLocation(callback) {
  window.addEventListener('hashchange', callback)
  window.addEventListener('popstate', callback)
  return () => {
    window.removeEventListener('hashchange', callback)
    window.removeEventListener('popstate', callback)
  }
}

// The current Atlas route. `navigationRef.current` tells the shell whether the last change was a new
// navigation ("push": start at the top) or a Back/Forward traversal (restore the previous scroll).
export function useAtlasRoute() {
  const hash = useSyncExternalStore(subscribeLocation, () => window.location.hash)
  const route = useMemo(() => parseHash(hash), [hash])
  const navigationRef = useRef('initial')
  useEffect(() => { rememberAtlasHash(hash) }, [hash])
  const navigate = useCallback((name, params = {}) => {
    const next = formatHash(name, params)
    if (next === window.location.hash) return
    navigationRef.current = 'push'
    window.location.hash = next
  }, [])
  const markPush = useCallback(() => { navigationRef.current = 'push' }, [])
  return { route, hash, navigate, markPush, navigationRef }
}

// Appearance is shared with the classic interface (same stored preference and the same
// data-theme attribute on <html>), so switching interfaces never changes it.
export function useAppearance() {
  const [mode, setMode] = useState(() => readTheme())
  useEffect(() => connectTheme(mode, document.documentElement, window.matchMedia('(prefers-color-scheme: dark)')), [mode])
  useEffect(() => {
    const sync = (event) => { if (event.key === 'control-center.theme') setMode(readTheme()) }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])
  const choose = useCallback((value) => {
    setMode(value)
    try { localStorage.setItem('control-center.theme', value) } catch { /* The choice still applies to this session. */ }
  }, [])
  return { mode, choose }
}

// One shared event connection for the whole Atlas shell (never one per view or per node). Each
// change notification refreshes the workspace summary counts and bumps `version`, which pages
// watch to re-read what they show.
export function useLiveStatus() {
  const [state, setState] = useState({ transport: 'polling', reachable: null, overview: null, receivedAt: null, version: 0 })
  useEffect(() => {
    const controller = new AbortController()
    async function read() {
      try {
        const response = await apiFetch(`${API_BASE}/workspace?limit=1&after=0`, { signal: controller.signal, cache: 'no-store' })
        if (!response.ok) throw new Error('Workspace summary unavailable')
        const data = await response.json()
        if (!controller.signal.aborted) {
          setState((current) => ({ ...current, reachable: true, overview: data?.overview ?? null, receivedAt: new Date() }))
        }
      } catch {
        if (!controller.signal.aborted) setState((current) => ({ ...current, reachable: false }))
        return false
      }
      return true
    }
    const queue = createRefreshQueue(read)
    const disconnect = connectAgentEvents({
      refresh: () => {
        queue.request(true)
        setState((current) => ({ ...current, version: current.version + 1 }))
      },
      onStatus: (transport) => setState((current) => (current.transport === transport ? current : { ...current, transport })),
    })
    return () => { disconnect(); queue.dispose(); controller.abort() }
  }, [])
  return state
}

// The canonical Task store for Work. It re-reads when the shared connection reports a change
// (`version`); bursts and changes during a read collapse into one follow-up read.
export function useTaskFeed(version) {
  const feed = useRef(null)
  const [state, setState] = useState({ phase: 'loading', stale: false, error: null, pages: 1, readAt: null, loadingMore: false, store: null })
  useEffect(() => {
    const instance = createTaskFeed({
      read: (options) => readTasks({ fetcher: apiFetch, base: API_BASE, ...options }),
      onChange: setState,
    })
    feed.current = instance
    return () => { instance.dispose(); feed.current = null }
  }, [])
  // The connection reports once as soon as it starts (version 1), so version 0 needs no read.
  useEffect(() => { if (version > 0) feed.current?.request() }, [version])
  const loadMore = useCallback(() => feed.current?.loadMore(), [])
  return { ...state, loadMore }
}

// A read-only JSON resource that follows the shared connection. A response for an earlier URL or
// an earlier version never replaces a newer one. A null `url` reads nothing. When a later read
// fails for a temporary reason (unreachable or a server error), the last good data stays, marked
// with the error; a refusal such as 404 clears it.
export function useLiveResource(url, version) {
  const [state, setState] = useState({ url: null, data: null, error: null, loading: false })
  const latest = useRef(0)
  useEffect(() => {
    if (!url) return undefined
    const id = ++latest.current
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      setState((current) => (current.url === url ? { ...current, loading: true } : { url, data: null, error: null, loading: true }))
      try {
        const response = await apiFetch(url, { signal: controller.signal, cache: 'no-store' })
        const data = await response.json().catch(() => null)
        if (id !== latest.current) return
        setState((current) => (response.ok ? { url, data, error: null, loading: false }
          : { url, data: response.status >= 500 && current.url === url ? current.data : null, error: response.status, loading: false }))
      } catch (error) {
        if (id !== latest.current || error?.name === 'AbortError') return
        setState((current) => ({ ...current, url, error: 'unreachable', loading: false }))
      }
    }, 60)
    return () => { clearTimeout(timer); controller.abort() }
  }, [url, version])
  return state.url === url ? state : { url, data: null, error: null, loading: Boolean(url) }
}
