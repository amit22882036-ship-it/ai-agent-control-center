// What the Settings page says, from what the browser and the backend report. Pure.
import { desktopNotificationPrerequisite } from '../../notificationGuidance.js'
import { API_HOST } from '../../apiClient.mjs'

export const SETTINGS_SECTIONS = [['appearance', 'Appearance'], ['interface', 'Interface'], ['notifications', 'Notifications'],
  ['keyboard', 'Keyboard'], ['accessibility', 'Accessibility'], ['about', 'About and diagnostics']]

export function settingsSection(params) {
  const value = params?.section
  return SETTINGS_SECTIONS.some(([id]) => id === value) ? value : null
}

export const KEY_GROUPS = [
  ['Everywhere', [
    ['New task', 'N'],
    ['Move between controls', 'Tab · Shift Tab'],
    ['Activate a button or link', 'Enter · Space'],
    ['Move within tabs, choices and menus', '← → ↑ ↓'],
    ['First or last item', 'Home · End'],
    ['Close a menu, dialog or panel', 'Esc'],
    ['Previous or next page', 'Alt ← · Alt →'],
  ]],
  ['Work', [
    ['Show the List', 'L'],
    ['Show the Map', 'M'],
    ['Focus on the selected task’s branch', 'F'],
    ['Open the selected task’s page', 'O'],
    ['Leave a focused branch', 'Esc'],
    ['Next or previous task that needs you', 'A · Shift A'],
    ['Reply to the selected task’s question', 'R'],
    ['Map: zoom in or out', '+ · −'],
    ['Map: move the view', 'Shift ← → ↑ ↓'],
    ['Map: move between tasks, expand or collapse', '← → ↑ ↓'],
    ['Details panel: resize (on its edge)', '← → · Home · End'],
  ]],
  ['New task', [
    ['Start the task', 'Ctrl Enter'],
  ]],
]
export const KEY_NOTE = 'Single-letter keys work only when no text field, menu, dialog or panel has focus or is open, and never with Ctrl, Alt or ⌘.'

// The notification preference with the browser's permission and the operating system's part.
export function notificationView({ supported, permission, enabled, synced, message }) {
  let state
  let tone = 'muted'
  if (!supported) state = 'This browser can’t show notifications.'
  else if (permission === 'denied') { state = 'Blocked by the browser. Allow notifications for this site in its site settings, then turn them on here.'; tone = 'alert' }
  else if (enabled) { state = 'On'; tone = 'ok' }
  else state = permission === 'default' ? 'Off. Turning them on asks your browser for permission.' : 'Off'
  const backend = !enabled ? null : synced === 'ok' ? 'The backend has your preference.'
    : synced === 'offline' || synced === 'refused' ? 'The backend couldn’t be told yet; it is told again when it answers.' : 'Telling the backend…'
  return {
    state, tone, backend, message: message || null,
    permission: { granted: 'Allowed', denied: 'Blocked', default: 'Not decided yet', unsupported: 'Not supported' }[permission] || 'Not known',
    prerequisite: desktopNotificationPrerequisite,
  }
}

export function supervisionView(resource) {
  if (resource.error) return { text: resource.error === 'unreachable' ? 'Not known: the backend isn’t reachable.' : 'Not known: the backend didn’t answer.', tone: 'muted' }
  const status = resource.data?.status
  if (!status) return { text: 'Reading…', tone: 'muted' }
  if (status === 'supervised') return { text: 'Supervised: every agent and setup process runs in its own process tree that the Control Center can stop.', tone: 'ok' }
  if (status === 'not_applicable') return { text: 'Not applicable on this operating system.', tone: 'muted' }
  return { text: `Unavailable${resource.data.reason ? `: ${resource.data.reason}` : ''}. Real tools can’t be started until it works.`, tone: 'alert' }
}

// A plain-text summary to paste into a bug report: states only, never task text, paths or names.
export function diagnosticsText({ live, connection, supervision, notifications, connections, appearance, userAgent, now = new Date() }) {
  return [
    'Control Center diagnostics (Lumen interface)',
    `Time: ${now.toISOString()}`,
    `Backend: ${API_HOST} · ${connection.label} · live updates ${live.transport === 'live' ? 'connected' : 'not connected'}`,
    `Last summary read: ${live.receivedAt ? new Date(live.receivedAt).toISOString() : 'none'}`,
    `Process supervision: ${supervision.data?.status || 'not known'}`,
    `Tools read: ${connections.data ? 'yes' : 'no'}${connections.error ? ` (last read failed: ${connections.error})` : ''}`,
    `Notifications: ${notifications.enabled ? 'on' : 'off'} · browser permission ${notifications.permission}`,
    `Appearance: ${appearance}`,
    `Browser: ${userAgent || 'not known'}`,
  ].join('\n')
}
