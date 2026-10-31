import { useEffect, useRef, useState } from 'react'
import { agentActions } from './api/agentClient.mjs'
import { announcement, browserNotification, createNotifyTracker, notifyEntries } from './model/notify.mjs'
import { HEARTBEAT_MS, deliveryEligible, heartbeatDue, locksAvailable } from '../notificationCoordination.mjs'
import useNotificationLeadership, { useFreshRead } from '../useNotificationLeadership'

// Browser notifications from Atlas, under the rules Classic view shares (notificationCoordination):
// one page of either view shows them, the one holding the shared lock, and it asks for the lock
// only while it could show every notification itself:
//
// - the preference is on and the browser allows notifications;
// - showing one has not failed in this page;
// - the backend answers and the canonical Task list is current, and every page of Tasks is loaded,
//   so no Task's change can go unseen here.
//
// The holder sends the backend's heartbeat only within a few seconds of its last successful read,
// so the backend holds back its Windows notification only for events this page has just seen.
// Every other page sends none: the Windows notification is the fallback when it is available.
// Nothing is promised about delivery: Windows settings, Focus assist, the backend being reachable
// and the notifier itself all decide whether one appears.
export { HEARTBEAT_MS }

export function useBrowserNotifications({ feed, live, preference, onOpen, announce }) {
  const [tracker] = useState(() => createNotifyTracker())
  const [failed, setFailed] = useState(false)
  // Turning the preference off and on again is a new chance to show them.
  const [wasEnabled, setWasEnabled] = useState(preference.enabled)
  if (wasEnabled !== preference.enabled) {
    setWasEnabled(preference.enabled)
    if (!preference.enabled) setFailed(false)
  }
  const open = useRef(new Set())
  const openTask = useRef(onOpen)
  const say = useRef(announce)
  useEffect(() => { openTask.current = onOpen; say.current = announce }, [onOpen, announce])
  const store = feed.store
  // The last read must have succeeded, be younger than LEADERSHIP_FRESH_MS, and the backend answer.
  const fresh = useFreshRead(feed.stale ? null : feed.readAt || null)
  const current = feed.phase === 'ready' && !feed.stale && fresh && live.reachable === true
  const granted = typeof Notification !== 'undefined' && preference.permission === 'granted'
  const complete = Boolean(store?.complete)
  const eligible = deliveryEligible({ enabled: preference.enabled, granted, failed, current, complete })
  const leader = useNotificationLeadership(eligible)
  const delivering = eligible && leader
  const deliveringRef = useRef(delivering)
  useEffect(() => { deliveringRef.current = delivering }, [delivering])
  // The last read this page showed notifications for while holding the lock (heartbeats follow it).
  const lastClaimed = useRef(null)

  // Each new store is compared with the previous one. Every transition is recorded, shown or not.
  useEffect(() => {
    if (!store || feed.phase !== 'ready') return
    const events = tracker.observe(notifyEntries(store))
    lastClaimed.current = deliveringRef.current ? Date.now() : null
    if (!events.length) return
    // Screen readers hear new questions from a polite live region; focus never moves.
    const text = announcement(events)
    if (text) say.current?.(text)
    if (!deliveringRef.current) return
    for (const event of events) {
      try {
        const { title, options } = browserNotification(event)
        const notification = new Notification(title, options)
        open.current.add(notification)
        notification.onclose = () => open.current.delete(notification)
        notification.onerror = () => { open.current.delete(notification); setFailed(true) }
        // The Task, never the Agent: a click opens whatever holds the Task now.
        notification.onclick = () => {
          try { window.focus() } catch { /* Focus is the browser's decision. */ }
          openTask.current?.(event.taskId)
          notification.close()
        }
      } catch {
        // Recorded after this pass, like a failure the browser reports later.
        queueMicrotask(() => setFailed(true))
      }
    }
  }, [store, feed.phase, tracker])

  // The heartbeat runs only while this page holds the lock, stays eligible and has just read the
  // Task list; a page whose live updates stopped soon stops suppressing the Windows notification.
  useEffect(() => {
    if (!delivering) return undefined
    const beat = () => {
      if (heartbeatDue({ leader: true, eligible: true, lastRead: lastClaimed.current })) agentActions.notificationHeartbeat()
    }
    beat()
    const timer = setInterval(beat, HEARTBEAT_MS)
    return () => clearInterval(timer)
  }, [delivering])

  useEffect(() => () => {
    for (const notification of open.current) {
      try { notification.close() } catch { /* Closing must not affect the page. */ }
    }
  }, [])

  // Why this page isn't showing them, as a code; model/notify.mjs words it.
  let cause = null
  if (preference.enabled && granted && !delivering) {
    cause = live.reachable === false ? 'offline' : failed ? 'failed' : !current ? 'stale' : !complete ? 'partial'
      : !locksAvailable() ? 'no-locks' : 'follower'
  }
  return { delivering, leader, failed, cause }
}
