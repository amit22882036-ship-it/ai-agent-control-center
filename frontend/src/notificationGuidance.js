export const desktopNotificationPrerequisite = 'Desktop alerts also require notifications to be allowed for this site in your browser and enabled for your browser in Windows Settings.'

export function notificationStatus({ supported, permission, enabled }) {
  if (!supported) return 'Notifications are unavailable in this browser.'
  if (permission === 'denied') {
    return 'Notifications are blocked in your browser. Allow notifications for this site in browser permissions, then try again.'
  }
  if (permission === 'default') {
    return 'Control Center notifications: Off. Your browser may ask for permission when you enable them.'
  }
  return `Control Center notifications: ${enabled ? 'On' : 'Off'}`
}
