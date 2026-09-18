// Keep transition state outside React renders and polling effect restarts.
export function createAgentNotifications(onSelect, onError) {
  const statuses = new Map()
  const openNotifications = new Set()
  let initialized = false

  return {
    observe(agents, enabled, NotificationAPI = globalThis.Notification) {
      for (const agent of agents) {
        const previous = statuses.get(agent.agent_id)
        // Record even when disabled or delivery fails; never replay old events.
        statuses.set(agent.agent_id, agent.status)
        if (!initialized || previous === agent.status || !enabled) continue
        if (agent.status !== 'waiting' && agent.status !== 'finished') continue
        try {
          if (!NotificationAPI || NotificationAPI.permission !== 'granted') continue
          const notification = new NotificationAPI(
            agent.status === 'waiting' ? 'Agent needs your input' : 'Agent finished',
            { body: (agent.task || `Agent ${agent.agent_id}`).slice(0, 180) },
          )
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
