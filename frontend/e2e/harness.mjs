// Test harness: serves the production build and a deterministic fixture backend through request
// routing, so no server process is started and no real provider can run.
import { readFile } from 'node:fs/promises'
import { extname } from 'node:path'
import { fileURLToPath } from 'node:url'

const dist = fileURLToPath(new URL('../dist/', import.meta.url))
const workspace = JSON.parse(await readFile(new URL('../tests/fixtures/workspace-sample.json', import.meta.url), 'utf8'))
const TYPES = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.json': 'application/json' }

// A stand-in EventSource that opens and stays open. It counts connections so tests can prove
// there is never more than one.
function fakeEventSource() {
  window.__events = { created: 0, closed: 0 }
  class FakeEventSource extends EventTarget {
    constructor(url) {
      super()
      this.url = url
      this.readyState = 0
      window.__events.created++
      setTimeout(() => { if (this.readyState === 0) { this.readyState = 1; this.onopen?.(new Event('open')) } }, 20)
    }
    close() { if (this.readyState !== 2) { this.readyState = 2; window.__events.closed++ } }
  }
  window.EventSource = FakeEventSource
}

export async function openApp(page, { backend = 'online', path = '/' } = {}) {
  const log = { backend: [], external: [] }
  await page.addInitScript(fakeEventSource)
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.host === 'atlas.preview') {
      const file = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '')
      try {
        const body = await readFile(dist + file)
        return route.fulfill({ status: 200, body, contentType: TYPES[extname(file)] || 'application/octet-stream' })
      } catch {
        return route.fulfill({ status: 404, body: 'not found' })
      }
    }
    if (url.host === '127.0.0.1:8000') {
      log.backend.push(`${request.method()} ${url.pathname}${url.search}`)
      if (backend === 'offline') return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"unavailable"}' })
      if (request.method() === 'GET' && url.pathname === '/workspace') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(workspace) })
      if (request.method() === 'GET' && url.pathname === '/providers/notifications') return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
      // The classic interface synchronises its notification preference on load (an existing behaviour).
      if (request.method() === 'GET' && url.pathname === '/security/csrf-token') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"csrf_token":"fixture"}' })
      if (request.method() === 'POST' && url.pathname.startsWith('/notifications/')) return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
      return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"not in fixture"}' })
    }
    log.external.push(request.url())
    return route.abort()
  })
  await page.goto(path)
  return log
}

export async function activeEventSources(page) {
  return page.evaluate(() => window.__events.created - window.__events.closed)
}
