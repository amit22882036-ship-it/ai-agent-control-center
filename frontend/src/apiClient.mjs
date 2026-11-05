// The backend's origin. The desktop app serves this interface from the backend itself and is built
// with VITE_API_BASE=same-origin; development and the browser tests use the fixed local port.
const configuredBase = typeof import.meta !== 'undefined' && import.meta.env ? import.meta.env.VITE_API_BASE : undefined
export const API_BASE = configuredBase === 'same-origin' && typeof window !== 'undefined' && /^https?:/.test(window.location?.origin || '')
  ? window.location.origin
  : (configuredBase && configuredBase !== 'same-origin' ? configuredBase : 'http://127.0.0.1:8000')
// The address as people read it (no scheme), for diagnostics.
export const API_HOST = API_BASE.replace(/^https?:\/\//, '')
export const CSRF_HEADER = 'X-Control-Center-CSRF'
export const CSRF_TOKEN_PATH = '/security/csrf-token'
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])
const TOKEN_REJECTIONS = new Set(['csrf_missing', 'csrf_invalid'])

// The token lives only in memory, is sent only as a header, and is never put
// in a URL or storage. The backend issues a new one on every restart.
export function createApiClient({ fetcher = (...args) => globalThis.fetch(...args) } = {}) {
  const tokens = new Map()

  function token(origin) {
    if (!tokens.has(origin)) {
      const pending = fetcher(`${origin}${CSRF_TOKEN_PATH}`, { cache: 'no-store', credentials: 'omit' })
        .then(async response => {
          const data = await response.json().catch(() => ({}))
          if (!response.ok || typeof data.csrf_token !== 'string' || !data.csrf_token) {
            throw new Error('Could not obtain the local API security token. Check the backend and retry.')
          }
          return data.csrf_token
        })
      pending.catch(() => { if (tokens.get(origin) === pending) tokens.delete(origin) })
      tokens.set(origin, pending)
    }
    return tokens.get(origin)
  }

  // Only an explicit pre-execution rejection is retried: the backend refuses
  // a missing or expired token with 403 before any handler runs. Network
  // errors, timeouts, aborts and every other response propagate untouched and
  // are never replayed.
  async function rejectedBeforeExecution(response) {
    if (response.status !== 403) return false
    const data = await response.clone().json().catch(() => null)
    return TOKEN_REJECTIONS.has(data?.code)
  }

  return async function apiFetch(url, init = {}) {
    const method = (init.method || 'GET').toUpperCase()
    if (!UNSAFE_METHODS.has(method)) return fetcher(url, init)
    const origin = new URL(url).origin
    const send = value => fetcher(url, { ...init, headers: { ...(init.headers || {}), [CSRF_HEADER]: value } })
    const pending = token(origin)
    const response = await send(await pending)
    if (!(await rejectedBeforeExecution(response))) return response
    if (tokens.get(origin) === pending) tokens.delete(origin)
    return send(await token(origin))
  }
}

export const apiFetch = createApiClient()
