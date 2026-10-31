import { NOTIFY_TITLES, notificationTag } from './notificationCoordination.mjs'

// Keep transition state outside React renders and polling effect restarts. `enabled` on observe
// means "this page shows notifications now" (preference, permission and the shared lock); every
// transition is recorded either way, so taking over later never replays earlier events.
export function createAgentNotifications(onSelect, onError) {
  const statuses = new Map()
  const openNotifications = new Set()
  let initialized = false

  return {
    observe(agents, enabled, NotificationAPI = globalThis.Notification) {
      for (const agent of agents) {
        const previous = statuses.get(agent.agent_id)
        const waitingKind = 'waiting_context' in agent ? agent.waiting_context?.kind : 'user'
        const state = agent.status === 'waiting' ? `waiting:${waitingKind || 'unknown'}` : agent.status
        // Record even when disabled or delivery fails; never replay old events.
        statuses.set(agent.agent_id, state)
        if (!initialized || previous === state || !enabled) continue
        if (agent.status !== 'waiting' && agent.status !== 'finished') continue
        if (agent.status === 'waiting' && waitingKind !== 'user') continue
        try {
          if (!NotificationAPI || NotificationAPI.permission !== 'granted') continue
          // The same event identity and tag as Atlas, so one event never stacks twice.
          const event = agent.status === 'waiting' ? 'waiting:user' : 'finished'
          const notification = new NotificationAPI(NOTIFY_TITLES[event],
            { body: (agent.task || `Agent ${agent.agent_id}`).slice(0, 180), tag: notificationTag(agent.agent_id, event) })
          openNotifications.add(notification)
          notification.onclose = () => openNotifications.delete(notification)
          notification.onerror = () => {
            openNotifications.delete(notification)
            onError('A notification could not be shown. Dashboard polling is still active.')
          }
          notification.onclick = () => {
            try { globalThis.window?.focus() } catch { /* Focus is browser-controlled. */ }
            onSelect(agent.agent_id)
            notification.close()
          }
        } catch {
          onError('A notification could not be shown. Dashboard polling is still active.')
        }
      }
      initialized = true
    },
    close() {
      for (const notification of openNotifications) {
        try { notification.close() } catch { /* Cleanup must not affect the dashboard. */ }
      }
      openNotifications.clear()
    },
  }
}
