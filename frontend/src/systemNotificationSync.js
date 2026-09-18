export const HEARTBEAT_INTERVAL_MS = 2000
export const RECENT_POLL_MS = 4000

export async function syncSystemNotifications({ enabled, browserActive, signal }, request = fetch) {
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
