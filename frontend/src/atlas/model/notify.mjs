// Which changes become a browser notification. Pure, so it is testable without a browser.
//
// It follows the same rule as Classic's tracker and the backend's Windows notifier: an Agent that
// starts waiting for the person's reply, and an Agent that finishes, are announced once per
// transition; every other change is only recorded. The difference from Classic is identity:
// Atlas reads Tasks a page at a time, so an Agent seen for the first time is announced only when
// its Task was already being watched (a new assignment, for example after a replacement) or the
// Task was created after watching began. An older page loaded later never replays old states.
import { NOTIFY_TITLES, notificationTag } from '../../notificationCoordination.mjs'

export const NOTIFY_TEXT = {
  'waiting:user': { heading: NOTIFY_TITLES['waiting:user'], announce: 'needs your reply' },
  finished: { heading: NOTIFY_TITLES.finished, announce: 'finished' },
}
// A little leeway for a Task created just before the first read.
const CREATED_LEEWAY_MS = 5000

// One entry per loaded Task that has an Agent: its current Agent, or the last one when it ended.
export function notifyEntries(store) {
  const entries = []
  for (const [taskId, task] of Object.entries(store?.tasks || {})) {
    const assignment = store.assignments[taskId]
    const agentId = assignment?.current?.agentId || assignment?.lastEnded?.agentId
    const agent = agentId ? store.agents[agentId] : null
    if (!agent || !agent.status) continue
    const state = agent.status === 'waiting' ? `waiting:${task.waitingContext?.kind || 'unknown'}` : agent.status
    entries.push({ agentId, taskId, state, task: task.title || 'Untitled task', name: agent.name || null,
      createdAt: Date.parse(task.createdAt || '') || 0 })
  }
  return entries
}

export function createNotifyTracker({ startedAt = Date.now() } = {}) {
  const states = new Map()
  const tasks = new Set()
  let initialized = false
  return {
    // Returns the transitions worth announcing. It always records, whether or not anything is
    // shown, so turning notifications on never replays earlier events.
    observe(entries) {
      const events = []
      for (const entry of entries) {
        const previous = states.get(entry.agentId)
        states.set(entry.agentId, entry.state)
        const watched = tasks.has(entry.taskId)
        tasks.add(entry.taskId)
        if (!initialized || previous === entry.state || !NOTIFY_TEXT[entry.state]) continue
        if (previous === undefined && !watched && entry.createdAt < startedAt - CREATED_LEEWAY_MS) continue
        events.push({ ...entry, key: `${entry.agentId}:${entry.state}`, ...NOTIFY_TEXT[entry.state] })
      }
      initialized = true
      return events
    },
    get size() { return states.size },
  }
}

// The browser notification for one event, worded as Classic words it (title, then the Task, at most
// 180 characters). The tag is the one Classic uses for the same Agent transition, so the same event
// shown by two pages replaces itself instead of stacking.
export function browserNotification(event) {
  return { title: event.heading, options: { body: event.task.slice(0, 180), tag: notificationTag(event.agentId, event.state) } }
}

// How notifications reach the person now, in words that promise nothing the page can't know.
// `cause` comes from useBrowserNotifications. A Windows notification is the backend's to try; it
// depends on Windows settings, Focus assist and the backend itself, so it is only ever "the
// fallback when available".
export const FALLBACK = 'Windows notifications are the fallback when available.'
const CAUSES = {
  offline: 'The backend isn’t answering, so nothing can be sent until it returns.',
  failed: `This browser couldn’t show one here. ${FALLBACK}`,
  'no-locks': `This browser can’t coordinate Control Center tabs, so none shows them. ${FALLBACK}`,
  follower: 'Another Control Center tab in this browser shows them.',
  stale: `The task list isn’t current here. ${FALLBACK}`,
  partial: `Not every task is loaded here. ${FALLBACK}`,
}
export function deliveryView({ supported, permission, enabled }, delivery) {
  if (!supported) return { short: 'This browser can’t show notifications.', detail: 'This browser can’t show notifications.' }
  if (permission === 'denied') return { short: 'Blocked by the browser.', detail: 'Blocked by the browser. Nothing is shown here.' }
  if (!enabled) return { short: 'Off.', detail: 'Nothing is shown while notifications are off.' }
  if (delivery?.delivering) return { short: 'On: shown by this browser.', detail: 'As browser notifications from this page; choosing one opens its task here.' }
  const why = CAUSES[delivery?.cause] || FALLBACK
  return { short: `On. ${why}`, detail: `${why} A Windows notification can’t open a task.` }
}

// What a screen reader hears, politely, when something new needs the person.
export function announcement(events) {
  const asking = events.filter((event) => event.state === 'waiting:user')
  if (!asking.length) return ''
  if (asking.length === 1) return `${asking[0].name || 'An agent'} on “${asking[0].task.slice(0, 80)}” needs your reply.`
  return `${asking.length} agents need your reply.`
}
