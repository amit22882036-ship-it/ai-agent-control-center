import { useCallback, useEffect, useState } from 'react'
import { apiFetch, API_BASE } from './apiClient.mjs'
import { refusalMessage } from './providerPresentation.mjs'

// Stage 2G in-app update notices (shown on the bell). Reads what the backend's
// once-a-day check recorded; dismissing or snoozing is stored per version.
export default function useProviderNotices(refreshVersion = 0) {
  const [notices, setNotices] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    apiFetch(`${API_BASE}/providers/notifications`).then(async (response) => {
      const data = await response.json().catch(() => null)
      if (active && response.ok && Array.isArray(data)) setNotices(data)
    }).catch(() => { /* The bell keeps the last notices; the backend may be restarting. */ })
    return () => { active = false }
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
