import test from 'node:test'
import assert from 'node:assert/strict'
import { initialAutonomousChoice, autonomyEligible, creationPayload } from '../src/autonomousCreation.mjs'

const agent = (id, parent = null, extra = {}) => ({ agent_id: id, parent_id: parent, task: id, status: 'running', ...extra })

test('eligible new Codex defaults on; initial Mock stays inactive', () => {
  assert.equal(initialAutonomousChoice, true)
  assert.equal(creationPayload(' Work ', 'mock', 'read-only', initialAutonomousChoice).autonomous, false)
  assert.deepEqual(creationPayload(' Work ', 'codex', 'workspace-write', initialAutonomousChoice), { task: 'Work', agent_type: 'codex', sandbox: 'workspace-write', autonomous: true })
})
test('explicit off intent survives provider changes; children and unsupported providers cannot opt in', () => {
  const choice = false
  for (const provider of ['codex', 'mock', 'unknown', 'codex']) assert.equal(creationPayload('Work', provider, 'read-only', choice).autonomous, false)
  assert.equal(autonomyEligible('unknown'), false)
  assert.equal(creationPayload('Work', 'codex', 'read-only', true, 'parent').autonomous, false)
})