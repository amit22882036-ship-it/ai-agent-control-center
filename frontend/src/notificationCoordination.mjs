// Browser notifications shared by Classic view and Atlas (one origin, so one set of rules).
//
// At most one page of the Control Center in one browser profile shows browser notifications at a
// time, whichever view it is in: the page that holds the Web Lock below. Web Locks are shared only
// by pages of one origin in one browser profile, so two browsers (Edge and Chrome) or two profiles
// are not coordinated with each other: each may have its own holder, and the shared tag does not
// merge notifications across browsers either. A page asks for the lock only while it could
// really show every notification itself (eligibility), and gives it up as soon as it can't. While
// it holds the lock and has read the workspace recently, it sends the backend's heartbeat, which
// makes the backend hold back its Windows notification for the same event. Every other page sends
// no heartbeat, so a page that can't see an event never suppresses the Windows notification for
// it. Without Web Locks no page takes the role: the Windows notification is the fallback.
//
// Nothing here guarantees delivery: the browser, Windows settings and the backend still decide.
// One known window remains: a browser that reports a failure seconds after the heartbeat was sent
// can leave that event shown by neither (closing it needs a backend acknowledgement per event).
export const NOTIFICATION_LOCK = 'control-center-notifications'
export const HEARTBEAT_MS = 2000
// A heartbeat is sent only within this long after the last read this page showed notifications
// for (it held the lock when the read arrived), so a page whose live updates silently stopped soon
// stops suppressing the Windows notification, and a read seen before taking the lock (whose events
// this page did not show) never leads to a heartbeat.
export const RECENT_READ_MS = 4000
// A page keeps (or asks for) the lock only while its last successful read is younger than this.
// Live updates read only when something changes, so a quiet minute is normal; after it, the page
// gives the lock up, and its next read makes it eligible again. Meanwhile the Windows notification
// is the fallback, and an eligible page in another tab can take the lock.
export const LEADERSHIP_FRESH_MS = 60000

// The events both views announce, with the same identity: the Agent and its new state.
export const NOTIFY_STATES = ['waiting:user', 'finished']
export const NOTIFY_TITLES = { 'waiting:user': 'Agent needs your input', finished: 'Agent finished' }

// One tag per Agent transition, identical in both views, so the same event shown by two pages
// (for example in the moment a lock changes hands) replaces itself instead of stacking.
export function notificationTag(agentId, state) {
  return `control-center:${agentId}:${state}`
}

// Whether a page may ask for the lock: it could show every notification itself right now.
//   enabled   the person's preference is on
//   granted   the browser allows notifications
//   failed    showing one has failed in this page
//   current   the last read of the workspace succeeded, is younger than LEADERSHIP_FRESH_MS,
//             and the backend answers
//   complete  every page of the workspace is loaded, so no Agent's change can go unseen
export function deliveryEligible({ enabled, granted, failed = false, current, complete }) {
  return Boolean(enabled && granted && !failed && current && complete)
}

// Whether a successful read at `lastRead` still counts as current.
export function readFresh(lastRead, now = Date.now()) {
  return lastRead !== null && lastRead !== undefined && now - lastRead < LEADERSHIP_FRESH_MS
}

// Whether to send a heartbeat now: the page holds the lock, is still eligible, and `lastRead`
// (the last read it showed notifications for, as the lock holder) is within RECENT_READ_MS.
export function heartbeatDue({ leader, eligible, lastRead, now = Date.now() }) {
  return Boolean(leader && eligible && lastRead !== null && lastRead !== undefined && now - lastRead < RECENT_READ_MS)
}

export function locksAvailable(nav = globalThis.navigator) {
  return typeof nav?.locks?.request === 'function'
}

// Asks for the lock and reports holding it; returns a release function. Without Web Locks, or if
// the request fails, the page never holds it. Closing the page releases it (the browser does).
export function requestLeadership(onChange, nav = globalThis.navigator) {
  if (!locksAvailable(nav)) {
    onChange(false)
    return () => {}
  }
  let release = () => {}
  const held = new Promise((resolve) => { release = resolve })
  const controller = new AbortController()
  let active = true
  nav.locks.request(NOTIFICATION_LOCK, { signal: controller.signal }, () => {
    if (!active) return undefined
    onChange(true)
    return held
  }).catch(() => { if (active) onChange(false) })
  return () => {
    active = false
    controller.abort()
    release()
    onChange(false)
  }
}
