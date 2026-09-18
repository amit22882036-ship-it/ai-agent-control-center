import { useCallback, useEffect, useRef, useState } from 'react'
import { createAgentNotifications } from './agentNotifications'
import { HEARTBEAT_INTERVAL_MS, RECENT_POLL_MS, syncSystemNotifications } from './systemNotificationSync'

const preferenceKey = 'ai-agent-control-center.notifications'

function savedPreference() {
  try { return localStorage.getItem(preferenceKey) === 'enabled' } catch { return false }
}

export default function useAgentNotifications(onSelect) {
  const supported = typeof Notification !== 'undefined'
  const [enabled, setEnabled] = useState(() => savedPreference() && supported && Notification.permission === 'granted')
  const enabledRef = useRef(enabled)
  const [permission, setPermission] = useState(() => supported ? Notification.permission : 'unsupported')
  const [requesting, setRequesting] = useState(false)
  const [message, setMessage] = useState('')
  const [syncError, setSyncError] = useState('')
  const lastPoll = useRef(null)
  const [browserDeliveryFailed, setBrowserDeliveryFailed] = useState(false)
  const [tracker] = useState(() => createAgentNotifications(onSelect, (error) => {
    setBrowserDeliveryFailed(true)
    setMessage(error)
  }))

  useEffect(() => () => tracker.close(), [tracker])

  useEffect(() => {
    const controller = new AbortController()
    let syncing = false
    async function sync() {
      if (syncing) return
      syncing = true
      try {
        await syncSystemNotifications({
          enabled,
          browserActive: supported && Notification.permission === 'granted'
            && !browserDeliveryFailed && lastPoll.current !== null
            && Date.now() - lastPoll.current < RECENT_POLL_MS,
          signal: controller.signal,
        })
        if (!controller.signal.aborted) setSyncError('')
      } catch {
        if (!controller.signal.aborted) {
          setSyncError('System notification settings could not be synced. Retrying automatically.')
        }
      } finally {
        syncing = false
      }
    }
    sync()
    const interval = setInterval(sync, HEARTBEAT_INTERVAL_MS)
    return () => {
      clearInterval(interval)
      controller.abort()
      // Closing a tab ends the lease, not the backend notification preference.
    }
  }, [enabled, supported, browserDeliveryFailed])

  useEffect(() => {
    function refreshPermission() {
      if (!supported) return
      setPermission(Notification.permission)
      if (Notification.permission !== 'granted') {
        enabledRef.current = false
        setEnabled(false)
      }
    }
    window.addEventListener('focus', refreshPermission)
    function syncStoredPreference(event) {
      if (event.key !== preferenceKey) return
      const value = event.newValue === 'enabled' && supported && Notification.permission === 'granted'
      enabledRef.current = value
      setEnabled(value)
    }
    window.addEventListener('storage', syncStoredPreference)
    return () => {
      window.removeEventListener('focus', refreshPermission)
      window.removeEventListener('storage', syncStoredPreference)
    }
  }, [supported])

  const observeAgents = useCallback((agents) => {
    // Native permission is checked again by the tracker on every transition.
    tracker.observe(agents, enabledRef.current)
    lastPoll.current = Date.now()
  }, [tracker])

  function savePreference(value) {
    setBrowserDeliveryFailed(false)
    enabledRef.current = value
    setEnabled(value)
    try {
      localStorage.setItem(preferenceKey, value ? 'enabled' : 'disabled')
    } catch {
      setMessage('Your preference could not be saved. It will apply for this page only.')
    }
  }

  async function toggleNotifications() {
    if (requesting || !supported) return
    setMessage('')
    if (enabled) {
      savePreference(false)
      return
    }
    setRequesting(true)
    try {
      // Permission requests happen only in this explicit button handler.
      const result = Notification.permission === 'default'
        ? await Notification.requestPermission()
        : Notification.permission
      setPermission(result)
      savePreference(result === 'granted')
      if (result === 'default') setMessage('Permission was not granted. You can try again when ready.')
    } catch {
      setMessage('Unable to request notifications. Check your browser’s site settings.')
    } finally {
      setRequesting(false)
    }
  }

  const status = !supported
    ? 'This browser does not support notifications.'
    : permission === 'denied'
      ? 'Notifications are blocked. Allow them in your browser’s site settings to enable them.'
      : enabled && permission === 'granted'
        ? 'Notifications enabled. Windows alerts continue with the dashboard closed while the backend runs.'
        : 'Notifications are off.'

  return { observeAgents, toggleNotifications, enabled, supported, requesting, status,
    message: [message, syncError].filter(Boolean).join(' ') }
}
