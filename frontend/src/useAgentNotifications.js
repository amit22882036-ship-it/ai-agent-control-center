import { useCallback, useEffect, useRef, useState } from 'react'
import { createAgentNotifications } from './agentNotifications'

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
  const [tracker] = useState(() => createAgentNotifications(onSelect, setMessage))

  useEffect(() => () => tracker.close(), [tracker])

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
    return () => window.removeEventListener('focus', refreshPermission)
  }, [supported])

  const observeAgents = useCallback((agents) => {
    // Native permission is checked again by the tracker on every transition.
    tracker.observe(agents, enabledRef.current)
  }, [tracker])

  function savePreference(value) {
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
        ? 'Notifications enabled. Keep this dashboard open to receive updates.'
        : 'Notifications are off.'

  return { observeAgents, toggleNotifications, enabled, supported, requesting, status, message }
}
