export const desktopNotificationPrerequisite = 'Desktop alerts also require notifications to be allowed for this site in your browser and enabled for your browser in Windows Settings.'

// `delivery` (optional) says who shows them while they are on: 'here', 'other-tab' or 'fallback'.
export function notificationStatus({ supported, permission, enabled, delivery }) {
  if (!supported) return 'Notifications are unavailable in this browser.'
  if (permission === 'denied') {
    return 'Notifications are blocked in your browser. Allow notifications for this site in browser permissions, then try again.'
  }
  if (permission === 'default') {
    return 'Control Center notifications: Off. Your browser may ask for permission when you enable them.'
  }
  if (enabled && delivery === 'other-tab') return 'Control Center notifications: On. Another Control Center tab in this browser shows them.'
  if (enabled && delivery === 'fallback') return 'Control Center notifications: On. Windows notifications are the fallback when available.'
  return `Control Center notifications: ${enabled ? 'On' : 'Off'}`
}
