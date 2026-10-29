import test from 'node:test'
import assert from 'node:assert/strict'
import { readdirSync, readFileSync } from 'node:fs'

const fixturesDir = new URL('./fixtures/', import.meta.url)
const presentation = readFileSync(new URL('../../backend/app/presentation.py', import.meta.url), 'utf8')

// The fields the backend's workspace read model emits for each Agent, read from its source.
function projectedAgentFields() {
  const body = presentation.slice(presentation.indexOf('def _nodes('), presentation.indexOf('def workspace('))
  const base = /for key in \(([^)]*)\)/.exec(body)[1].match(/'([a-z_]+)'/g).map((value) => value.slice(1, -1))
  const assigned = [...body.matchAll(/node\['([a-z_]+)'\]\s*=/g)].map((match) => match[1])
  return new Set([...base, ...assigned])
}

test('the workspace fixture has exactly the fields the backend projection emits', () => {
  const fields = projectedAgentFields()
  assert.ok(fields.has('waiting_context') && fields.has('task_status') && fields.size >= 18)
  const workspace = JSON.parse(readFileSync(new URL('workspace-sample.json', fixturesDir), 'utf8'))
  assert.deepEqual(Object.keys(workspace).sort(), ['agents', 'next_after', 'overview'])
  for (const agent of workspace.agents) assert.deepEqual(new Set(Object.keys(agent)), fields, agent.agent_id)
  const overviewKeys = /'overview': \{([\s\S]*?)\}\}/.exec(presentation)[1].match(/'([a-z_]+)':/g).map((value) => value.slice(1, -2))
  assert.deepEqual(Object.keys(workspace.overview).sort(), overviewKeys.sort())
})

test('fixtures contain no paths, credentials or personal data', () => {
  for (const name of readdirSync(fixturesDir).filter((file) => file.endsWith('.json'))) {
    const text = readFileSync(new URL(name, fixturesDir), 'utf8')
    assert.doesNotMatch(text, /[A-Za-z]:\\|\/Users\/|\/home\/|\\\\/, `${name} contains a filesystem path`)
    assert.doesNotMatch(text, /csrf|token|api[_-]?key|password|secret|@[a-z0-9-]+\.[a-z]{2,}/i, `${name} contains sensitive-looking data`)
  }
})
