import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { refusalMessage } from './providerPresentation.mjs'
import { retryDelay } from './onboardingPresentation.mjs'

// Stage 2G in-app update notices (shown on the bell). Reads what the backend's
// once-a-day check recorded; dismissing or snoozing is stored per version.
export default function useProviderNotices(refreshVersion = 0) {
  const [notices, setNotices] = useState([])
  const [error, setError] = useState('')
  // One read at a time; while the backend is unreachable, refreshes back off instead of
  // repeating every couple of seconds.
  const reading = useRef({ running: false, failures: 0, notBefore: 0 })

  useEffect(() => {
    const state = reading.current
    if (state.running || Date.now() < state.notBefore) return
    state.running = true
    apiFetch(`${API_BASE}/providers/notifications`).then(async (response) => {
      const data = await response.json().catch(() => null)
      if (response.ok && Array.isArray(data)) setNotices(data)
      state.failures = 0
      state.notBefore = 0
    }).catch(() => {
      // The bell keeps the last notices; the backend may be restarting.
      state.failures += 1
      state.notBefore = Date.now() + retryDelay(state.failures)
    }).finally(() => { state.running = false })
  }, [refreshVersion])

  const dismiss = useCallback(async (notice, mode) => {
    setError('')
    try {
      const response = await apiFetch(`${API_BASE}/providers/${encodeURIComponent(notice.provider)}/notifications/dismiss`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ version: notice.available_version, mode }) })
      const data = await response.json().catch(() => null)
      if (!response.ok) throw new Error(refusalMessage(data, 'The notice could not be updated.'))
      setNotices(Array.isArray(data) ? data : [])
    } catch (err) {
      setError(err.message)
    }
  }, [])

  return { notices, dismiss, error }
}
