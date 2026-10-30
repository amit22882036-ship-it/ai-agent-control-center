import { test, expect } from '@playwright/test'
import { announceChange, openApp } from './harness.mjs'
import { large, showcase } from './scenarios.mjs'

// Measured interactions in the production build with synthetic workspaces. Each one is timed inside
// the page, from the event to the second animation frame after it (React's update, style, layout
// and the first paint), so Playwright's own element queries are not counted. The limits are loose
// so a slow test machine does not fail them; they only catch gross regressions.
const sizes = {
  100: () => large({ families: 5, width: 19 }),
  1000: () => large({ families: 50, width: 19 }),
  5000: () => large({ families: 250, width: 19 }),
}

// Runs `source` in the page and returns the milliseconds until two frames later.
function inPage(page, source, value) {
  return page.evaluate(async ([code, input]) => {
    const start = performance.now()
    new Function('value', code)(input)
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    return Math.round(performance.now() - start)
  }, [source, value])
}
const key = (letter) => `window.dispatchEvent(new KeyboardEvent('keydown', { key: '${letter}', bubbles: true }))`
const click = (selector) => `document.querySelector(${JSON.stringify(selector)}).click()`
const type = `const input = document.querySelector('input[aria-label="Find in loaded tasks"]');
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true }))`

async function measure(page, tasks) {
  await page.goto('about:blank')
  const start = Date.now()
  await openApp(page, { path: '/#/work', tasks })
  await expect(page.locator('.a-row').first()).toBeVisible()
  const result = { tasks: tasks.tasks.length, load: Date.now() - start }
  result.select = await inPage(page, click('.a-outline .a-tcard:nth-of-type(2) .a-row'))
  result.reselect = await inPage(page, click('.a-outline .a-tcard:nth-of-type(3) .a-row'))
  result.toMap = await inPage(page, key('m'))
  result.mapZoom = await inPage(page, "document.querySelector('.a-mappane').dispatchEvent(new KeyboardEvent('keydown', { key: '+', bubbles: true }))")
  result.toList = await inPage(page, key('l'))
  result.search = await inPage(page, type, 'Helper 7 of family 3')
  result.clearSearch = await inPage(page, type, '')
  result.nextNeedsYou = await inPage(page, key('a'))
  result.bell = await inPage(page, click('.a-bell'))
  await page.keyboard.press('Escape')
  result.toSettings = await inPage(page, "window.location.hash = '#/settings'")
  const reads = () => page.evaluate(() => performance.getEntriesByType('resource').filter((entry) => entry.name.includes('/workspace/tasks')).length)
  const before = await reads()
  const refreshStart = Date.now()
  await announceChange(page)
  await expect.poll(reads).toBeGreaterThan(before)
  result.refreshOffPage = Date.now() - refreshStart
  result.toConnections = await inPage(page, "window.location.hash = '#/connections'")
  result.backToWork = await inPage(page, "window.location.hash = '#/work'")
  result.taskPage = await inPage(page, "window.location.hash = '#/task?id=quoted'")
  result.heapMB = await page.evaluate(() => (performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : null))
  return result
}

test('large workspaces: load, List, Map, selection, search, navigation and refreshes stay responsive', async ({ page }) => {
  test.setTimeout(300_000)
  const results = []
  for (const [size, build] of Object.entries(sizes)) {
    const tasks = build()
    tasks.tasks.push(...showcase().tasks.filter((item) => item.task_id === 'quoted' || item.task_id === 'csv'))
    tasks.total = tasks.tasks.length
    const result = await measure(page, tasks)
    results.push({ size: Number(size), ...result })
    expect(result.select, `select at ${size}`).toBeLessThan(4000)
    expect(result.toMap, `map at ${size}`).toBeLessThan(6000)
    await page.unroute('**/*')
  }
  console.log('performance (ms; heap MB)')
  for (const row of results) console.log(JSON.stringify(row))
})
