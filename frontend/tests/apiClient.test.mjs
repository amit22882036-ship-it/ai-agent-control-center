import assert from 'node:assert/strict'
import test from 'node:test'
import { API_BASE, CSRF_HEADER, CSRF_TOKEN_PATH, createApiClient } from '../src/apiClient.mjs'

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
const tokenUrl = `${API_BASE}${CSRF_TOKEN_PATH}`

function backend({ tokens = ['first-token'], responses = [] } = {}) {
  const calls = []
  const fetcher = async (url, init = {}) => {
    calls.push({ url, init })
    if (url === tokenUrl) {
      const next = tokens.shift()
      if (next instanceof Error) throw next
      return next === undefined ? json({ detail: 'down' }, 503) : json({ csrf_token: next })
    }
    const next = responses.shift()
    if (next instanceof Error) throw next
    return next ?? json({ ok: true })
  }
  return { calls, fetcher, actions: () => calls.filter(call => call.url !== tokenUrl) }
}

test('safe requests pass through untouched and never fetch a token', async () => {
  const server = backend()
  const apiFetch = createApiClient({ fetcher: server.fetcher })
  const init = { signal: new AbortController().signal }
  await apiFetch(`${API_BASE}/agents`, init)
  await apiFetch(`${API_BASE}/agents`, { ...init, method: 'get' })
  assert.equal(server.calls.length, 2)
  assert.equal(server.calls[0].init, init)
  assert.ok(server.calls.every(call => !JSON.stringify(call.init.headers ?? {}).includes(CSRF_HEADER)))
})

test('every unsafe method sends the token header, never in the URL', async () => {
  const server = backend()
  const apiFetch = createApiClient({ fetcher: server.fetcher })
  for (const method of ['POST', 'put', 'PATCH', 'DELETE']) {
    await apiFetch(`${API_BASE}/agents/a/stop`, { method, headers: { 'Content-Type': 'application/json' }, body: '{}' })
  }
  const actions = server.actions()
  assert.equal(actions.length, 4)
  for (const action of actions) {
    assert.equal(action.init.headers[CSRF_HEADER], 'first-token')
    assert.equal(action.init.headers['Content-Type'], 'application/json')
    assert.ok(!action.url.includes('first-token'))
  }
  const tokenCall = server.calls.find(call => call.url === tokenUrl)
  assert.equal(tokenCall.init.cache, 'no-store')
  assert.equal(tokenCall.init.credentials, 'omit')
})

test('body-less POSTs are protected too', async () => {
  const server = backend()
  await createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/decide-always`, { method: 'POST' })
  assert.equal(server.actions()[0].init.headers[CSRF_HEADER], 'first-token')
})

test('concurrent callers share one token request', async () => {
  const server = backend()
  const apiFetch = createApiClient({ fetcher: server.fetcher })
  await Promise.all([1, 2, 3].map(() => apiFetch(`${API_BASE}/agents/a/stop`, { method: 'POST' })))
  assert.equal(server.calls.filter(call => call.url === tokenUrl).length, 1)
  assert.equal(server.actions().length, 3)
})

for (const code of ['csrf_missing', 'csrf_invalid']) {
  test(`an explicit ${code} rejection refreshes the token and retries exactly once`, async () => {
    const rejected = json({ detail: 'expired', code }, 403)
    const server = backend({ tokens: ['old', 'new'], responses: [rejected, json({ status: 'stopped' })] })
    const response = await createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/stop`, { method: 'POST', body: '{"a":1}' })
    assert.equal(response.status, 200)
    const actions = server.actions()
    assert.deepEqual(actions.map(call => call.init.headers[CSRF_HEADER]), ['old', 'new'])
    assert.equal(actions[1].init.body, '{"a":1}')
  })
}

test('a second token rejection is returned, not retried again', async () => {
  const rejected = () => json({ detail: 'expired', code: 'csrf_invalid' }, 403)
  const server = backend({ tokens: ['old', 'new'], responses: [rejected(), rejected()] })
  const response = await createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/stop`, { method: 'POST' })
  assert.equal(response.status, 403)
  assert.equal(server.actions().length, 2)
})

test('ambiguous outcomes are never replayed', async () => {
  const cases = [
    json({ detail: 'Cross-site requests are not allowed.', code: 'origin_rejected' }, 403),
    json({ detail: 'Forbidden' }, 403),
    json({ detail: 'Cross-site reads require an allowed Origin.', code: 'cross_site_forbidden' }, 403),
    json({ detail: 'old code', code: 'csrf_token_invalid' }, 403),
    json({ detail: 'wrong status', code: 'csrf_invalid' }, 401),
    json({ detail: 'wrong status', code: 'csrf_missing' }, 500),
    new Response('not json', { status: 403 }),
    json({ detail: 'Agent not found' }, 404),
    json({ detail: 'busy' }, 409),
    json({ detail: 'error' }, 500),
    json({ detail: 'unavailable' }, 503),
  ]
  for (const reply of cases) {
    const server = backend({ responses: [reply] })
    const response = await createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/stop`, { method: 'POST' })
    assert.equal(response.status, reply.status)
    assert.equal(server.actions().length, 1)
  }
})

test('network errors and timeouts propagate without retry', async () => {
  for (const error of [new TypeError('Failed to fetch'), new DOMException('Timed out', 'TimeoutError'), new DOMException('Aborted', 'AbortError')]) {
    const server = backend({ responses: [error] })
    await assert.rejects(createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/stop`, { method: 'POST' }), error)
    assert.equal(server.actions().length, 1)
  }
})

test('the caller can still read the rejected response body', async () => {
  const server = backend({ responses: [json({ detail: 'Agent not found' }, 404)] })
  const response = await createApiClient({ fetcher: server.fetcher })(`${API_BASE}/agents/a/stop`, { method: 'POST' })
  assert.deepEqual(await response.json(), { detail: 'Agent not found' })
})

test('token failures surface and a later call fetches a fresh token', async () => {
  const server = backend({ tokens: [new TypeError('Failed to fetch'), undefined, 'later'] })
  const apiFetch = createApiClient({ fetcher: server.fetcher })
  await assert.rejects(apiFetch(`${API_BASE}/agents/a/stop`, { method: 'POST' }), /Failed to fetch/)
  await assert.rejects(apiFetch(`${API_BASE}/agents/a/stop`, { method: 'POST' }), /security token/)
  assert.equal(server.actions().length, 0)
  await apiFetch(`${API_BASE}/agents/a/stop`, { method: 'POST' })
  assert.equal(server.actions()[0].init.headers[CSRF_HEADER], 'later')
})

test('tokens are kept per backend origin', async () => {
  const urls = []
  const apiFetch = createApiClient({ fetcher: async (url) => {
    urls.push(url)
    return url.endsWith(CSRF_TOKEN_PATH) ? json({ csrf_token: new URL(url).host }) : json({})
  } })
  await apiFetch('http://127.0.0.1:8000/agents/a/stop', { method: 'POST' })
  await apiFetch('http://localhost:8000/agents/a/stop', { method: 'POST' })
  assert.deepEqual(urls, ['http://127.0.0.1:8000/security/csrf-token', 'http://127.0.0.1:8000/agents/a/stop',
    'http://localhost:8000/security/csrf-token', 'http://localhost:8000/agents/a/stop'])
})
