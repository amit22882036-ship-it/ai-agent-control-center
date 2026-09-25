import assert from 'node:assert/strict'
import test from 'node:test'
import { desktopNotificationPrerequisite, notificationStatus } from '../src/notificationGuidance.js'

test('notification guidance describes granted on and off states without claiming Windows permission', () => {
  assert.equal(notificationStatus({ supported: true, permission: 'granted', enabled: true }), 'Control Center notifications: On')
  assert.equal(notificationStatus({ supported: true, permission: 'granted', enabled: false }), 'Control Center notifications: Off')
  assert.match(desktopNotificationPrerequisite, /browser in Windows Settings/)
})

test('default permission explains the browser prompt and denied permission gives recovery guidance', () => {
  assert.equal(
    notificationStatus({ supported: true, permission: 'default', enabled: false }),
    'Control Center notifications: Off. Your browser may ask for permission when you enable them.',
  )
  assert.equal(
    notificationStatus({ supported: true, permission: 'denied', enabled: false }),
    'Notifications are blocked in your browser. Allow notifications for this site in browser permissions, then try again.',
  )
})

test('unsupported browsers receive an honest unavailable state', () => {
  assert.equal(
    notificationStatus({ supported: false, permission: 'unsupported', enabled: false }),
    'Notifications are unavailable in this browser.',
  )
})
