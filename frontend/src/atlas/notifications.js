import { useEffect, useRef, useState } from 'react'
import { agentActions } from './api/agentClient.mjs'
import { announcement, browserNotification, createNotifyTracker, notifyEntries } from './model/notify.mjs'

// Browser notifications from Atlas, coordinated with the backend's Windows notifications exactly
// as Classic coordinates them: the backend waits a few seconds before a Windows notification and
// sends it only if no page has sent a heartbeat recently. A page sends the heartbeat only while it
// can really show the same notification itself, so each event is shown by one of the two:
//
// - the preference is on and the browser allows notifications;
// - showing one has not failed in this page;
// - this is the one Atlas tab that shows them (the others leave it to it);
// - the backend answers and the canonical Task list is current, and every page of Tasks is loaded,
//   so no Task's change can go unseen here. Otherwise no heartbeat is sent, and the backend's
//   Windows notification is the fallback when it is available.
//
// The backend keeps the heartbeat for 6 seconds, so a closed or stalled tab never holds back a
// Windows notification for long. Nothing is promised about delivery: Windows settings, Focus
// assist, the backend being reachable and the notifier itself all decide whether one appears.
export const HEARTBEAT_MS = 2000
const LOCK = 'control-center-atlas-notifications'

// One Atlas tab at a time shows browser notifications. The Web Locks API hands the role to
// another tab when this one closes. Without it (an insecure address, an old browser) tabs can't
// agree, so none shows them: the backend's Windows notification is the fallback, rather than
// several tabs showing the same event.
export const LOCKS_AVAILABLE = () => Boolean(globalThis.navigator?.locks?.request)
function useLeadership() {
  const [leader, setLeader] = useState(false)
  useEffect(() => {
    const locks = globalThis.navigator?.locks
    if (!locks?.request) return undefined
    let release
    const held = new Promise((resolve) => { release = resolve })
    const controller = new AbortController()
    locks.request(LOCK, { signal: controller.signal }, () => { setLeader(true); return held }).catch(() => {})
    return () => { controller.abort(); release(); setLeader(false) }
  }, [])
  return leader
}

export function useBrowserNotifications({ feed, live, preference, onOpen, announce }) {
  const leader = useLeadership()
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
  const current = feed.phase === 'ready' && !feed.stale && live.reachable === true
  const granted = typeof Notification !== 'undefined' && preference.permission === 'granted'
  const delivering = Boolean(preference.enabled && granted && !failed && leader && current && store?.complete)
  const deliveringRef = useRef(delivering)
  useEffect(() => { deliveringRef.current = delivering }, [delivering])

  // Each new store is compared with the previous one. Every transition is recorded, shown or not.
  useEffect(() => {
    if (!store || feed.phase !== 'ready') return
    const events = tracker.observe(notifyEntries(store))
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

  // The heartbeat runs only while this page can show the notification itself.
  useEffect(() => {
    if (!delivering) return undefined
    const beat = () => { agentActions.notificationHeartbeat() }
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
    cause = live.reachable === false ? 'offline' : failed ? 'failed' : !LOCKS_AVAILABLE() ? 'no-locks' : !leader ? 'follower'
      : !current ? 'stale' : !store?.complete ? 'partial' : null
  }
  return { delivering, leader, failed, cause }
}
