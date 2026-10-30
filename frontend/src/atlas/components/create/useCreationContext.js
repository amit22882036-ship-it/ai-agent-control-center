import { useCallback, useEffect, useRef, useState } from 'react'
import { API_BASE, apiFetch } from '../../../apiClient.mjs'
import { agentActions } from '../../api/agentClient.mjs'

// What a new agent may use, as in the classic form: the provider permission (GET /provider-policy;
// `undefined` while loading, `null` when unavailable, which allows no real provider) and local
// readiness (POST /providers/check: installation, version, sign-in, billing route; never a model
// request). Permission is read again just before a request, and only the newest answer counts.
export function useCreationContext(version = 0) {
  const [policy, setPolicy] = useState(undefined)
  const [statuses, setStatuses] = useState(null)
  const [statusError, setStatusError] = useState('')
  const latest = useRef(0)
  const refreshPolicy = useCallback(async () => {
    const request = ++latest.current
    let result
    try {
      const response = await apiFetch(`${API_BASE}/provider-policy`, { cache: 'no-store' })
      const data = await response.json().catch(() => ({}))
      result = response.ok && Array.isArray(data.allowed) ? data : null
    } catch { result = null }
    if (request === latest.current) setPolicy(result)
    return result
  }, [])
  const refreshProviders = useCallback(async () => {
    const result = await agentActions.checkProviders()
    if (result.ok && Array.isArray(result.data?.providers)) { setStatuses(result.data.providers); setStatusError('') }
    else setStatusError('Provider status is unavailable. Mock agents can still start.')
  }, [])
  useEffect(() => {
    let active = true
    agentActions.checkProviders().then((result) => {
      if (!active) return
      if (result.ok && Array.isArray(result.data?.providers)) setStatuses(result.data.providers)
      else setStatusError('Provider status is unavailable. Mock agents can still start.')
    })
    return () => { active = false }
  }, [])
  // Re-read on mount and whenever the shared connection reports a change.
  useEffect(() => {
    let active = true
    const request = ++latest.current
    apiFetch(`${API_BASE}/provider-policy`, { cache: 'no-store' })
      .then(async (response) => { const data = await response.json().catch(() => ({})); return response.ok && Array.isArray(data.allowed) ? data : null })
      .catch(() => null)
      .then((result) => { if (active && request === latest.current) setPolicy(result) })
    return () => { active = false }
  }, [version])
  return { policy, statuses, statusError, refreshPolicy, refreshProviders }
}
