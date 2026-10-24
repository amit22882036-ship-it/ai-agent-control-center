import { apiFetch } from './apiClient.mjs'

export const HEARTBEAT_INTERVAL_MS = 2000
export const RECENT_POLL_MS = 4000
// Delay before the next sync after `failures` consecutive failed ones: the normal heartbeat,
// doubling while the backend stays unreachable, never more than ten seconds.
export const MAX_SYNC_DELAY_MS = 10000
export function nextSyncDelay(failures) {
  if (failures <= 0) return HEARTBEAT_INTERVAL_MS
  return Math.min(HEARTBEAT_INTERVAL_MS * 2 ** failures, MAX_SYNC_DELAY_MS)
}

export async function syncSystemNotifications({ enabled, browserActive, signal }, request = apiFetch) {
  const preference = await request('http://127.0.0.1:8000/notifications/preferences', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
    signal,
  })
  if (!preference.ok) throw new Error('Notification preference sync failed')
  if (enabled && browserActive && !signal?.aborted) {
    const heartbeat = await request('http://127.0.0.1:8000/notifications/heartbeat', {
      method: 'POST', signal,
    })
    if (!heartbeat.ok) throw new Error('Notification heartbeat failed')
  }
}
