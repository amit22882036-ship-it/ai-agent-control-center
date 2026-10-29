import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { apiFetch, API_BASE } from '../apiClient.mjs'
import { connectAgentEvents, createRefreshQueue } from '../agentEventStream.mjs'
import { connectTheme, readTheme } from '../theme.mjs'
import { formatHash, parseHash } from './router.mjs'
import { rememberAtlasHash } from './uiMode.mjs'

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
// change notification refreshes only the workspace summary counts in M1.
export function useLiveStatus() {
  const [state, setState] = useState({ transport: 'polling', reachable: null, overview: null, receivedAt: null })
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
      refresh: () => queue.request(true),
      onStatus: (transport) => setState((current) => (current.transport === transport ? current : { ...current, transport })),
    })
    return () => { disconnect(); queue.dispose(); controller.abort() }
  }, [])
  return state
}
