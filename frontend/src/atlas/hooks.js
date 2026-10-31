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
// `root` narrows the feed to one Task's branch and the ancestors that place it (a Task page), so a
// page never copies the whole workspace; without it, the workspace is read newest first.
export function useTaskFeed(version, root = null) {
  const feed = useRef(null)
  const [state, setState] = useState({ phase: 'loading', stale: false, error: null, pages: 1, readAt: null, loadingMore: false, store: null, root })
  useEffect(() => {
    const instance = createTaskFeed({
      read: (options) => readTasks({ fetcher: apiFetch, base: API_BASE, root, ...options }),
      onChange: (next) => setState({ ...next, root }),
    })
    feed.current = instance
    // A new root starts from a clean read; the connection's next report keeps it current.
    if (root) instance.request(true)
    return () => { instance.dispose(); feed.current = null }
  }, [root])
  // The connection reports once as soon as it starts (version 1), so version 0 needs no read.
  useEffect(() => { if (version > 0) feed.current?.request() }, [version])
  const loadMore = useCallback(() => feed.current?.loadMore(), [])
  // After an action: read again now rather than waiting for the connection to report it.
  const refresh = useCallback(() => feed.current?.request(true), [])
  // A result from a previous root is never shown for a new one.
  const current = state.root === root ? state : { phase: 'loading', stale: false, error: null, pages: 1, readAt: null, loadingMore: false, store: null, root }
  return { ...current, loadMore, refresh }
}

// A read-only JSON resource that follows the shared connection. Reads of one URL never overlap:
// a change reported while a read is under way does not cancel it (in a busy workspace changes can
// arrive faster than a read answers, and cancelling would leave the data "loading" for ever); it
// asks for one more read when this one ends, however many changes arrived meanwhile. Only a new
// URL, or leaving the page, cancels a read. A null `url` reads nothing. When a later read fails
// for a temporary reason (unreachable or a server error), the last good data stays, marked with
// the error; a refusal such as 404 clears it.
export function useLiveResource(url, version) {
  const [state, setState] = useState({ url: null, data: null, error: null, loading: false })
  // The read for the current URL: its controller, a pending start, and whether to read again.
  const flight = useRef(null)
  // A new URL (or leaving) cancels what the previous one was doing.
  useEffect(() => {
    if (!url) return undefined
    const entry = { url, controller: null, timer: null, again: false }
    flight.current = entry
    return () => {
      clearTimeout(entry.timer)
      entry.controller?.abort()
      if (flight.current === entry) flight.current = null
    }
  }, [url])
  useEffect(() => {
    const entry = flight.current
    if (!url || !entry || entry.url !== url) return
    // A read already waiting to start, or under way, covers this change (the latter reads again).
    if (entry.timer !== null) return
    if (entry.controller) { entry.again = true; return }
    const start = async () => {
      entry.timer = null
      entry.again = false
      entry.controller = new AbortController()
      const { signal } = entry.controller
      setState((current) => (current.url === url ? { ...current, loading: true } : { url, data: null, error: null, loading: true }))
      try {
        const response = await apiFetch(url, { signal, cache: 'no-store' })
        const data = await response.json().catch(() => null)
        if (signal.aborted) return
        setState((current) => (response.ok ? { url, data, error: null, loading: false }
          : { url, data: response.status >= 500 && current.url === url ? current.data : null, error: response.status, loading: false }))
      } catch (error) {
        if (signal.aborted || error?.name === 'AbortError') return
        setState((current) => ({ ...current, url, error: 'unreachable', loading: false }))
      } finally {
        if (!signal.aborted) {
          entry.controller = null
          if (entry.again) entry.timer = setTimeout(start, 60)
        }
      }
    }
    entry.timer = setTimeout(start, 60)
  }, [url, version])
  return state.url === url ? state : { url, data: null, error: null, loading: Boolean(url) }
}

// One agent's own read model (the same as the classic inspector reads). Actions use it as the
// authority for what the agent is doing now; `version` re-reads it after a change.
export function useAgentDetail(agentId, version) {
  return useLiveResource(agentId ? `${API_BASE}/agents/${encodeURIComponent(agentId)}?include_output=false` : null, version)
}

// Runs agent actions one at a time for one inspector. A second action while one is running is
// ignored (no duplicate or conflicting request), and every finished action asks for fresh
// authoritative state instead of assuming what changed. An action returns
// { ok, message, tone?, changed? }; `changed: false` skips the refresh (a request refused before
// anything was sent).
export function useActionRunner(onChanged) {
  const [state, setState] = useState({ busy: null, result: null })
  const busy = useRef(null)
  // Set on every effect setup, not only at creation: StrictMode (and a remount of the same
  // instance) runs cleanup then setup again, and the component must keep receiving results.
  const mounted = useRef(false)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])
  const run = useCallback(async (key, action) => {
    if (busy.current) return null
    busy.current = key
    setState({ busy: key, result: null })
    let outcome
    try {
      outcome = await action()
    } catch (error) {
      outcome = { ok: false, message: error?.message || 'The action could not be completed.' }
    }
    busy.current = null
    if (mounted.current) setState({ busy: null, result: outcome ? { key, ...outcome } : null })
    if (outcome && outcome.changed !== false) onChanged?.()
    return outcome
  }, [onChanged])
  const dismiss = useCallback(() => setState((current) => ({ ...current, result: null })), [])
  return { busy: state.busy, result: state.result, run, dismiss }
}

// A version for reads that only some pages follow closely. While `active`, it follows every change
// the shared connection reports; otherwise it moves at most every 15 seconds, and only after a
// reported change (never on a timer of its own).
export const CONNECTIONS_QUIET_MS = 15000
function useQuietVersion(live, active) {
  const [followed, setFollowed] = useState(0)
  const last = useRef(0)
  useEffect(() => {
    if (live.version === 0) return undefined
    const wait = active ? 0 : Math.max(0, last.current + CONNECTIONS_QUIET_MS - Date.now())
    const timer = setTimeout(() => { last.current = Date.now(); setFollowed(live.version) }, wait)
    return () => clearTimeout(timer)
  }, [live.version, active])
  return followed
}

// Connections (GET /providers/onboarding, the read Classic's AI Providers view uses). On the
// Connections page it follows every change the shared connection reports; elsewhere it only feeds
// the navigation's attention mark. `reload` reads again after an action, because some provider
// changes are not announced on the event connection.
export function useConnections(live, active) {
  const [nonce, setNonce] = useState(0)
  const followed = useQuietVersion(live, active)
  // Opening the page always reads afresh.
  const resource = useLiveResource(`${API_BASE}/providers/onboarding`, `${followed}:${nonce}:${active ? 'page' : 'nav'}`)
  const reload = useCallback(() => setNonce((value) => value + 1), [])
  return { ...resource, reload }
}

// Provider update notices (GET /providers/notifications, what Classic's bell shows): what the
// backend's daily release check recorded, minus versions dismissed or snoozed. The backend
// announces a check and a dismissal on the event connection, so this follows it like Connections
// does: closely while the notifications panel or Connections is open, quietly otherwise.
export function useProviderNotices(live, active) {
  const [nonce, setNonce] = useState(0)
  const followed = useQuietVersion(live, active)
  const resource = useLiveResource(`${API_BASE}/providers/notifications`, `${followed}:${nonce}:${active ? 'open' : 'quiet'}`)
  const reload = useCallback(() => setNonce((value) => value + 1), [])
  const notices = Array.isArray(resource.data) ? resource.data : []
  return { ...resource, notices, reload }
}

// The Control Center notification preference, shared with Classic view (same stored key and the
// same rule: it is on only while the browser allows notifications). It is told to the backend, so
// the backend's Windows notifications follow it; Atlas's own browser notifications follow it too
// (notifications.js). The backend keeps the preference in memory, so an "on" preference is
// sent again whenever the backend answers after being unreachable. Nothing is sent for an "off"
// preference the person did not change here.
const NOTIFICATION_KEY = 'ai-agent-control-center.notifications'
function storedNotifications() {
  try { return localStorage.getItem(NOTIFICATION_KEY) === 'enabled' } catch { return false }
}
const browserPermission = () => (typeof Notification === 'undefined' ? 'unsupported' : Notification.permission)

export function useNotificationPreference(reachable, send) {
  const supported = typeof Notification !== 'undefined'
  const [state, setState] = useState(() => ({ enabled: storedNotifications() && browserPermission() === 'granted',
    permission: browserPermission(), requesting: false, message: '', synced: null, changed: false }))
  useEffect(() => {
    const refresh = () => setState((current) => {
      const permission = browserPermission()
      const enabled = current.enabled && permission === 'granted'
      return permission === current.permission && enabled === current.enabled ? current : { ...current, permission, enabled, changed: current.changed || enabled !== current.enabled }
    })
    const sync = (event) => { if (event.key === NOTIFICATION_KEY) setState((current) => ({ ...current, enabled: event.newValue === 'enabled' && browserPermission() === 'granted' })) }
    window.addEventListener('focus', refresh)
    window.addEventListener('storage', sync)
    return () => { window.removeEventListener('focus', refresh); window.removeEventListener('storage', sync) }
  }, [])
  useEffect(() => {
    if (reachable !== true || (!state.enabled && !state.changed)) return undefined
    let live = true
    send(state.enabled).then((result) => {
      if (!live) return
      setState((current) => ({ ...current, synced: result.ok ? 'ok' : result.offline ? 'offline' : 'refused' }))
    })
    return () => { live = false }
  }, [reachable, state.enabled, state.changed, send])

  const save = (enabled, message = '') => {
    try { localStorage.setItem(NOTIFICATION_KEY, enabled ? 'enabled' : 'disabled') } catch {
      message = 'Your preference could not be saved; it applies to this page only.'
    }
    setState((current) => ({ ...current, enabled, changed: true, requesting: false, message, permission: browserPermission(), synced: null }))
  }
  async function toggle() {
    if (!supported || state.requesting) return
    if (state.enabled) { save(false); return }
    setState((current) => ({ ...current, requesting: true, message: '' }))
    try {
      // The browser asks only from this explicit click, never on its own.
      const result = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission
      if (result === 'granted') save(true)
      else save(false, result === 'denied' ? '' : 'Permission was not granted. You can try again when ready.')
    } catch {
      save(false, 'The browser could not ask for permission. Check this site’s notification settings.')
    }
  }
  return { ...state, supported, toggle }
}
