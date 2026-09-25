import test from 'node:test'
import assert from 'node:assert/strict'
import { displayColor, displayColors, nameHistoryTime } from '../src/agentIdentity.mjs'

test('display palette is fixed and untrusted values render neutral', () => {
  assert.deepEqual(displayColors, ['neutral', 'violet', 'blue', 'cyan', 'green', 'yellow', 'orange', 'red', 'pink'])
  for (const color of displayColors) assert.equal(displayColor(color), color)
  for (const value of [undefined, '#fff', 'rgb(0,0,0)', 'BLUE', '']) assert.equal(displayColor(value), 'neutral')
})

test('name history formats valid timestamps and handles unavailable dates', () => {
  const value = '2026-09-25T14:12:00.000Z'
  assert.equal(nameHistoryTime(value), new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value)))
  assert.equal(nameHistoryTime('invalid'), 'Date unavailable')
  assert.equal(nameHistoryTime(null), 'Date unavailable')
})
