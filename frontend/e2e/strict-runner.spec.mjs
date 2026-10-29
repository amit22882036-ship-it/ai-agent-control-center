import { test, expect } from '@playwright/test'
import { mkdtemp, readFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { extname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { build } from 'vite'
import react from '@vitejs/plugin-react'

// The action runner under React StrictMode with React's development build: StrictMode replays
// effect setup and cleanup, which is exactly what left the runner unable to show results before.
const TYPES = { '.html': 'text/html', '.js': 'text/javascript' }
let out

test.beforeAll(async () => {
  out = await mkdtemp(join(tmpdir(), 'atlas-strict-'))
  await build({
    configFile: false, logLevel: 'error', mode: 'development', root: fileURLToPath(new URL('./strict/', import.meta.url)),
    plugins: [react()], define: { 'process.env.NODE_ENV': '"development"' },
    build: { outDir: out, emptyOutDir: true, minify: false },
  })
})
test.afterAll(async () => { if (out) await rm(out, { recursive: true, force: true }) })

async function openProbe(page) {
  await page.route('http://probe.local/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    const file = path === '/' ? 'index.html' : path.slice(1)
    try { return route.fulfill({ status: 200, body: await readFile(join(out, file)), contentType: TYPES[extname(file)] || 'text/plain' }) }
    catch { return route.fulfill({ status: 404, body: '' }) }
  })
  await page.goto('http://probe.local/')
  await expect(page.locator('#busy')).toHaveText('idle')
}

test('StrictMode replays the effect, and a mounted runner still shows results and clears busy', async ({ page }) => {
  await openProbe(page)
  // React's development StrictMode ran effect setup twice: the replay really happened.
  expect(await page.evaluate(() => window.__probe.effectSetups)).toBe(2)
  await page.evaluate(() => { window.__probe.pending = window.__probe.start('reply') })
  await expect(page.locator('#busy')).toHaveText('reply')
  await page.evaluate(() => window.__probe.release())
  await expect(page.locator('#busy')).toHaveText('idle')
  await expect(page.locator('#result')).toHaveText('reply:reply finished')
  expect(await page.evaluate(() => window.__probe.changes)).toBe(1) // fresh data was requested
})

test('a second action while one runs is not started, so no duplicate request is made', async ({ page }) => {
  await openProbe(page)
  const second = await page.evaluate(async () => {
    window.__probe.start('stop')
    const result = await window.__probe.runner.run('stop', async () => { window.__probe.calls += 1; return { ok: true, message: 'again' } })
    return result
  })
  expect(second).toBeNull()
  await page.evaluate(() => window.__probe.release())
  await expect(page.locator('#result')).toHaveText('stop:stop finished')
  expect(await page.evaluate(() => window.__probe.calls)).toBe(1)
})

test('an unmounted runner receives nothing, still requests fresh data, and a remount starts clean', async ({ page }) => {
  await openProbe(page)
  const errors = []
  page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()) })
  await page.evaluate(() => { window.__probe.pending = window.__probe.start('remove') })
  await expect(page.locator('#busy')).toHaveText('remove')
  await page.evaluate(() => window.__probe.unmount())
  await expect(page.locator('#gone')).toBeVisible()
  await page.evaluate(() => window.__probe.release())
  await page.evaluate(() => window.__probe.pending)
  expect(await page.evaluate(() => window.__probe.changes)).toBe(1) // the mutation still asks for fresh data
  await page.evaluate(() => window.__probe.remount())
  await expect(page.locator('#busy')).toHaveText('idle')
  await expect(page.locator('#result')).toHaveText('none') // no stale result from the earlier instance
  expect(errors).toEqual([])
})
