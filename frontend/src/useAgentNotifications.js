import { useCallback, useEffect, useRef, useState } from 'react'
import { createAgentNotifications } from './agentNotifications'
import { HEARTBEAT_INTERVAL_MS, nextSyncDelay, syncSystemNotifications } from './systemNotificationSync'
import { deliveryEligible, heartbeatDue, locksAvailable } from './notificationCoordination.mjs'
import useNotificationLeadership, { useFreshRead } from './useNotificationLeadership'
import { desktopNotificationPrerequisite, notificationStatus } from './notificationGuidance'

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
  // The last read this page showed notifications for while holding the lock (heartbeats follow it).
  const lastClaimed = useRef(null)
  const [browserDeliveryFailed, setBrowserDeliveryFailed] = useState(false)
  // The backend's preference is shared by every browser. "On" is told to it on every sync (so it
  // survives a backend restart); "off" only once, after the person switched it off here, so a
  // browser where notifications were never turned on doesn't keep switching them off for another.
  const offPending = useRef(false)
  // When the last workspace read succeeded (null after a failed one) and whether it covered every
  // agent (no "Load more" pending). Only a recent, complete read lets this page take over
  // notifications from the Windows fallback; an old one gives the lock up (useFreshRead).
  const [coverage, setCoverage] = useState({ at: null, complete: false })
  const fresh = useFreshRead(coverage.at)
  const granted = supported && permission === 'granted'
  const eligible = deliveryEligible({ enabled, granted, failed: browserDeliveryFailed, current: fresh, complete: coverage.complete })
  const leader = useNotificationLeadership(eligible)
  const showing = useRef({ leader: false, eligible: false })
  useEffect(() => { showing.current = { leader, eligible } }, [leader, eligible])
  const [tracker] = useState(() => createAgentNotifications(onSelect, (error) => {
    setBrowserDeliveryFailed(true)
    setMessage(error)
  }))

  useEffect(() => () => tracker.close(), [tracker])

  useEffect(() => {
    const controller = new AbortController()
    let syncing = false
    let failures = 0
    let notBefore = 0
    async function sync() {
      // After a failed sync (backend unreachable) the next attempts wait a little longer each
      // time, capped so the lease resumes quickly once the backend is back.
      if (syncing || Date.now() < notBefore) return
      syncing = true
      try {
        const sendPreference = enabled || offPending.current
        await syncSystemNotifications({
          enabled,
          sendPreference,
          // Only the page holding the shared lock, eligible and recently read, claims the event.
          browserActive: supported && Notification.permission === 'granted' && !browserDeliveryFailed
            && heartbeatDue({ leader: showing.current.leader, eligible: showing.current.eligible, lastRead: lastClaimed.current }),
          signal: controller.signal,
        })
        failures = 0
        notBefore = 0
        if (!enabled && sendPreference) offPending.current = false
        if (!controller.signal.aborted) setSyncError('')
      } catch {
        failures += 1
        notBefore = Date.now() + nextSyncDelay(failures) - HEARTBEAT_INTERVAL_MS
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

  const observeAgents = useCallback((agents, { complete = true } = {}) => {
    // Native permission is checked again by the tracker on every transition. Only the page that
    // holds the shared lock shows them; every page records transitions.
    const claiming = enabledRef.current && showing.current.leader && showing.current.eligible && complete
    tracker.observe(agents, claiming)
    const now = Date.now()
    lastClaimed.current = claiming ? now : null
    setCoverage({ at: now, complete })
  }, [tracker])
  // A failed read: this page's view is no longer current, so it stops claiming notifications.
  const observeFailure = useCallback(() => {
    lastClaimed.current = null
    setCoverage((current) => (current.at === null ? current : { ...current, at: null }))
  }, [])

  function savePreference(value) {
    offPending.current = !value
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

  const delivery = !enabled || !granted ? undefined : leader ? 'here' : eligible && locksAvailable() ? 'other-tab' : 'fallback'
  const status = notificationStatus({ supported, permission, enabled, delivery })

  return { observeAgents, observeFailure, toggleNotifications, enabled, supported, permission, requesting, status, delivery,
    prerequisite: desktopNotificationPrerequisite,
    message: [message, syncError].filter(Boolean).join(' ') }
}
