import test from 'node:test'
import assert from 'node:assert/strict'
import { agentActions, OFFLINE_MESSAGE, refusalText, send } from '../src/atlas/api/agentClient.mjs'
import { capabilitiesFor } from '../src/atlas/model/agentCapabilities.mjs'

function recorder(response = { ok: true, status: 200, body: { status: 'running' } }) {
  const calls = []
  const fetcher = async (url, init) => {
    calls.push({ url, method: init.method, body: init.body === undefined ? undefined : JSON.parse(init.body), headers: init.headers })
    return { ok: response.ok, status: response.status, json: async () => response.body }
  }
  return { calls, options: { fetcher, base: 'http://api' } }
}

test('every agent action uses the classic endpoint, method and payload', async () => {
  const r = recorder()
  await agentActions.reply('a/1', 'Yes', { confirm_new_session: true }, r.options)
  await agentActions.redirect('a1', 'Use JSON', { model_override: true, model: 'm', effort_override: true, effort: null }, r.options)
  await agentActions.decide('a1', r.options)
  await agentActions.similar('a1', true, r.options)
  await agentActions.similar('a1', false, r.options)
  await agentActions.always('a1', true, r.options)
  await agentActions.always('a1', false, r.options)
  await agentActions.stop('a1', r.options)
  await agentActions.stopBranch('a1', r.options)
  await agentActions.resume('a1', { explicit_retry: false }, r.options)
  await agentActions.changeProvider('a1', { provider: 'codex', model: null, sandbox: 'read-only' }, r.options)
  await agentActions.saveDefaults('a1', { model_set: true, model: 'm', effort_set: true, effort: 'high', attempt_unverified: false }, r.options)
  await agentActions.refreshClaudeUsage(r.options)
  await agentActions.rename('a1', 'Writer', r.options)
  await agentActions.color('a1', 'green', r.options)
  await agentActions.removalPreview('a1', 'single', r.options)
  await agentActions.remove('a1', { request_key: 'k', plan_token: 't', mode: 'single', task_disposition: 'preserve', reason: null }, r.options)
  await agentActions.retryRemoval('rm 1', r.options)
  await agentActions.transfer('t1', { request_key: 'k' }, r.options)
  await agentActions.saveAutoPolicy('t1', { enabled: true }, r.options)
  await agentActions.removeAutoPolicy('t1', 3, r.options)
  await agentActions.saveRestriction('t1', { providers: ['codex'] }, r.options)
  await agentActions.removeRestriction('t1', 'r2', r.options)
  await agentActions.refreshDiagnostics('e1', r.options)
  await agentActions.attest('e1', { request_key: 'k' }, r.options)
  assert.deepEqual(r.calls.map((call) => `${call.method} ${call.url.replace('http://api', '')}`), [
    'POST /agents/a%2F1/reply', 'POST /agents/a1/redirect', 'POST /agents/a1/decide', 'POST /agents/a1/decide-similar',
    'POST /agents/a1/decide-similar/disable', 'POST /agents/a1/decide-always', 'POST /agents/a1/decide-always/disable',
    'POST /agents/a1/stop', 'POST /agents/a1/stop-branch', 'POST /agents/a1/resume', 'POST /agents/a1/provider', 'POST /agents/a1/defaults',
    'POST /providers/claude/usage/refresh', 'POST /agents/a1/rename', 'POST /agents/a1/color', 'POST /agents/a1/removal-preview',
    'POST /agents/a1/remove', 'POST /agent-removals/rm%201/retry', 'POST /tasks/t1/responsibility/transfer', 'PUT /tasks/t1/auto-allocation',
    'DELETE /tasks/t1/auto-allocation?expected_revision=3', 'PUT /tasks/t1/provider-restrictions',
    'DELETE /tasks/t1/provider-restrictions?expected_revision=r2', 'POST /executions/e1/diagnostics', 'POST /executions/e1/attestation',
  ])
  assert.deepEqual(r.calls[0].body, { answer: 'Yes', confirm_new_session: true })
  assert.deepEqual(r.calls[1].body, { instruction: 'Use JSON', model_override: true, model: 'm', effort_override: true, effort: null })
  assert.equal(r.calls[2].body, undefined) // decide sends no body, like classic
  assert.deepEqual(r.calls[13].body, { display_name: 'Writer' })
  assert.deepEqual(r.calls[14].body, { display_color: 'green' })
  assert.deepEqual(r.calls[15].body, { mode: 'single' })
  assert.deepEqual(r.calls[17].body, {})
  assert.equal(r.calls[0].headers['Content-Type'], 'application/json')
})

test('refusals keep the backend’s words; network failures say nothing is assumed', async () => {
  const refused = recorder({ ok: false, status: 409, body: { detail: 'Agent is not waiting for a reply' } })
  const result = await agentActions.reply('a1', 'x', {}, refused.options)
  assert.deepEqual([result.ok, result.status], [false, 409])
  assert.equal(refusalText(result, 'fallback'), 'Agent is not waiting for a reply')
  assert.equal(refusalText({ data: { detail: { code: 'x', message: 'Structured refusal' } } }, 'fallback'), 'Structured refusal')
  assert.equal(refusalText({ data: { detail: [{ msg: 'answer too long' }] } }, 'fallback'), 'answer too long')
  assert.equal(refusalText({ data: {} }, 'Unable to send reply. Please try again.'), 'Unable to send reply. Please try again.')
  const offline = await send('POST', '/agents/a1/stop', undefined, { fetcher: async () => { throw new TypeError('Failed to fetch') }, base: '' })
  assert.deepEqual([offline.ok, offline.offline], [false, true])
  assert.equal(refusalText(offline, 'fallback'), OFFLINE_MESSAGE)
})

const base = { agent_id: 'a1', agent_type: 'codex', status: 'running', waiting_context: null, child_ids: [], session_id: 's',
  similar_decisions_enabled: false, always_decide_enabled: false, sandbox: 'workspace-write', allocation_task_id: 't1' }

test('controls follow the classic inspector’s conditions', () => {
  const running = capabilitiesFor(base)
  assert.deepEqual([running.stop, running.redirect, running.redirectReady, running.reply, running.stopBranch, running.providerWait, running.providerChange],
    [true, true, true, false, false, false, false])
  assert.equal(capabilitiesFor({ ...base, session_id: null }).redirectReady, false)
  const asking = capabilitiesFor({ ...base, status: 'waiting', waiting_context: { kind: 'user' } })
  assert.deepEqual([asking.reply, asking.decide, asking.enableSimilar, asking.enableAlways, asking.stop, asking.redirect, asking.providerChange],
    [true, true, true, true, true, false, true])
  assert.equal(capabilitiesFor({ ...base, status: 'waiting', waiting_context: { kind: 'user' }, always_decide_enabled: true }).enableAlways, false)
  const provider = capabilitiesFor({ ...base, status: 'waiting', waiting_context: { kind: 'provider' } })
  assert.deepEqual([provider.providerWait, provider.reply, provider.providerChange], [true, false, true])
  // Managed (autonomous) agents cannot switch providers; mock agents have no models.
  assert.equal(capabilitiesFor({ ...base, status: 'waiting', waiting_context: { kind: 'user' }, managed: true }).providerChange, false)
  assert.equal(capabilitiesFor({ ...base, agent_type: 'mock' }).defaults, false)
  assert.equal(capabilitiesFor({ ...base, agent_type: 'mock', status: 'waiting', waiting_context: { kind: 'user' } }).reply, false)
  const finished = capabilitiesFor({ ...base, status: 'finished', child_ids: ['c1'] })
  assert.deepEqual([finished.stop, finished.stopBranch, finished.redirect], [false, true, false])
  assert.deepEqual([capabilitiesFor({ ...base, similar_decisions_enabled: true, always_decide_enabled: true }).similarOn,
    capabilitiesFor({ ...base, always_decide_enabled: true }).alwaysOn], [true, true])
  assert.equal(capabilitiesFor({ ...base, removal: { state: 'blocked' } }).removal, 'blocked')
  assert.equal(capabilitiesFor(null), null)
  assert.equal(capabilitiesFor({ status: 'running' }), null)
})
