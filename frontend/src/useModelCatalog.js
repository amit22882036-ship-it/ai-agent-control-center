import { useCallback, useEffect, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { refusalMessage } from './providerPresentation.mjs'

async function request(provider, refresh = false, staleOnly = false) {
  const base = `${API_BASE}/providers/${encodeURIComponent(provider)}/models`
  const response = refresh
    ? await apiFetch(`${base}/refresh${staleOnly ? '?stale_only=true' : ''}`, { method: 'POST' })
    : await apiFetch(base)
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(refusalMessage(data, 'The model list could not be loaded. Please retry.'))
  return data
}

// The cached, account-scoped model catalog of one provider. A stale or missing
// list is refreshed once on open; discovery is non-billable and the backend
// coalesces and rate limits it. `version` re-reads after related changes.
export default function useModelCatalog(provider, version = 0) {
  const [state, setState] = useState({ provider: null, catalog: null, error: '' })
  const [refreshing, setRefreshing] = useState(false)
  const real = Boolean(provider) && provider !== 'mock'

  useEffect(() => {
    if (!real) return undefined
    let active = true
    request(provider).then(async (catalog) => {
      if (!active) return
      setState({ provider, catalog, error: '' })
      if (!catalog.refresh?.fresh) {
        const fresh = await request(provider, true, true)
        if (active) setState({ provider, catalog: fresh, error: '' })
      }
    }).catch((err) => {
      if (active) setState((current) => ({ provider, catalog: current.provider === provider ? current.catalog : null, error: err.message }))
    })
    return () => { active = false }
  }, [provider, real, version])

  const refresh = useCallback(async () => {
    if (!real) return
    setRefreshing(true)
    try {
      const catalog = await request(provider, true)
      setState({ provider, catalog, error: '' })
    } catch (err) {
      setState((current) => ({ ...current, provider, error: err.message }))
    } finally {
      setRefreshing(false)
    }
  }, [provider, real])

  const current = state.provider === provider
  return { catalog: current ? state.catalog : null, error: current ? state.error : '', refreshing, refresh }
}
