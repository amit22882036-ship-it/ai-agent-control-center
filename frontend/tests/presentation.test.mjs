import test from 'node:test'
import assert from 'node:assert/strict'
import { readTheme, applyTheme, connectTheme } from '../src/theme.mjs'

test('theme preference is validated and unavailable storage falls back safely', () => {
  assert.equal(readTheme({ getItem: () => 'dark' }), 'dark')
  assert.equal(readTheme({ getItem: () => 'unknown' }), 'automatic')
  assert.equal(readTheme({ getItem() { throw Error() } }), 'automatic')
})
test('automatic theme tracks live system changes and cleans up listener', () => {
  const root = { dataset: {} }
  let listener
  const media = { matches: false, addEventListener(_, fn) { listener = fn }, removeEventListener(_, fn) { assert.equal(fn, listener); listener = null } }
  const cleanup = connectTheme('automatic', root, media)
  assert.equal(root.dataset.theme, 'light')
  media.matches = true; listener()
  assert.equal(root.dataset.theme, 'dark')
  cleanup(); assert.equal(listener, null)
  assert.equal(applyTheme('light', true), 'light')
  assert.equal(applyTheme('dark', false), 'dark')
})