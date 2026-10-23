import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { buildAgentTree } from '../src/buildAgentTree.mjs'
import { matchesStatus, filterAgentTree, visibleAgentIds } from '../src/agentListView.mjs'
import { attentionAgents } from '../src/agentPresentation.mjs'
import {
  branchCounts, consequenceText, effectiveRemovalCollapse, failureText, fullyRemovedBranches, integrationGuidance, isRemoved, previewSummary,
  readRemovalError, removalRequest, removalScope, removalState, removedDescendants, responsibilitySummary, transferRequest,
  withoutRemoved,
} from '../src/agentRemoval.mjs'

const removed = (id, parent = null) => ({ agent_id: id, parent_id: parent, status: 'stopped', display_name: id, removed: true,
  removal: { state: 'removed' }, waiting_context: { kind: 'removed', label: 'Removed' } })
const active = (id, parent = null) => ({ agent_id: id, parent_id: parent, status: 'running', display_name: id })
const ids = (nodes) => nodes.map((n) => [n.agent.agent_id, Boolean(n.parentHidden), ids(n.children)])

test('removal state is explicit and removed agents are history, never active work', () => {
  assert.equal(removalState(removed('a')), 'removed')
  assert.equal(removalState({ removal: { state: 'blocked' } }), 'blocked')
  assert.equal(removalState(active('a')), null)
  assert.equal(isRemoved({ removed: true }), true)
  assert.equal(removalScope(removed('a')), 'history')
  assert.equal(removalScope({ removal: { state: 'removing' } }), 'active')
  assert.equal(matchesStatus(removed('a'), 'active'), false)
  assert.equal(matchesStatus(removed('a'), 'history'), true)
  assert.equal(matchesStatus(removed('a'), 'all'), true)
  const blocked = { ...active('b'), status: 'stopped', removal: { state: 'blocked' }, waiting_context: { kind: 'recovery', label: 'Removal blocked' } }
  assert.equal(matchesStatus(blocked, 'active'), true)
  assert.deepEqual(attentionAgents([removed('a'), blocked]), [blocked])  // a blocked removal needs you; history does not
})

test('hiding a removed parent keeps its active children visible without inventing a parent', () => {
  const tree = buildAgentTree([active('root'), removed('parent', 'root'), active('child', 'parent'), removed('done', 'parent'), active('grand', 'child')])
  assert.deepEqual(ids(withoutRemoved(tree)), [
    ['root', false, []],
    ['child', true, [['grand', false, []]]],  // lifted to the top level and marked, not reattached to root
  ])
  const visible = visibleAgentIds(withoutRemoved(tree), new Set())
  assert.equal(visible.has('child') && visible.has('grand'), true)
  assert.equal(visible.has('parent'), false)
  // The underlying tree keeps the real lineage.
  assert.equal(tree[0].children[0].children[0].agent.agent_id, 'child')
})

test('fully removed branches collapse by default and can be expanded again', () => {
  const tree = buildAgentTree([removed('a'), removed('b', 'a'), removed('c', 'b'), removed('d'), active('e', 'd'), removed('leaf')])
  assert.deepEqual(fullyRemovedBranches(tree).sort(), ['a'])  // d still has active work; a lone leaf needs no collapse
  const indexed = tree.find((n) => n.agent.agent_id === 'a')
  assert.equal(removedDescendants(indexed), 2)
  const auto = new Set(fullyRemovedBranches(tree))
  assert.deepEqual([...effectiveRemovalCollapse(new Set(['x']), auto, new Set())].sort(), ['a', 'x'])
  assert.deepEqual([...effectiveRemovalCollapse(new Set(['x']), auto, new Set(['a']))], ['x'])
  const filtered = filterAgentTree(tree, '', 'all')
  assert.equal(visibleAgentIds(filtered, effectiveRemovalCollapse(new Set(), auto, new Set())).has('b'), false)
})

test('branch progress is counted per member', () => {
  const operation = { members: [{ phase: 'removed' }, { phase: 'stopping' }, { phase: 'recovery_required' }, { phase: 'admitted' }] }
  assert.deepEqual(branchCounts(operation), { total: 4, removed: 1, pending: 2, blocked: 1 })
  assert.deepEqual(branchCounts(null), { total: 0, removed: 0, pending: 0, blocked: 0 })
  assert.match(failureText('execution_unresolved: process termination is not established'), /Process ownership/)
  assert.match(failureText('stop_not_confirmed: x'), /stay held/)
  assert.match(consequenceText('children_continue'), /keep working/)
  assert.match(consequenceText('orchestration_disabled'), /Hybrid coordination stops/)
  assert.match(consequenceText('task_waits_hybrid_orchestration_bound'), /Hybrid/)
  assert.match(consequenceText('task_waits_delegation_handoff_unsettled'), /not implemented yet/)
  assert.doesNotMatch(consequenceText('task_waits_delegation_handoff_unsettled'), /Hybrid/)
})

const preview = (mode, available = true) => ({
  mode, plan_token: 'token', agent: { agent_id: 'a', status: 'running' },
  members: [{ agent_id: 'a', status: 'running', unresolved_executions: 0 }, { agent_id: 'b', status: 'stopped', unresolved_executions: 1 }],
  descendants: [{ agent_id: 'b', active: true, removed: false, continuation: 'continues_result_waits' }, { agent_id: 'c', active: false, removed: true }],
  orchestration: { coordinates_delegated_work: true },
  options: { [mode]: { available, reasons: available ? [] : ['descendant_removal_in_progress'] } },
  consequences: ['children_continue'],
})

test('previews summarize the exact impact for single and branch removal', () => {
  const single = previewSummary(preview('single'))
  assert.equal(single.affected, 1)
  assert.equal(single.continuing, 1)
  assert.equal(single.resultsWait, 1)
  assert.equal(single.orchestration, true)
  const branch = previewSummary(preview('retire_branch'))
  assert.deepEqual([branch.affected, branch.running, branch.unresolved, branch.continuing], [2, 1, 1, 0])
  assert.deepEqual(previewSummary(preview('retire_branch', false)).reasons, ['descendant_removal_in_progress'])
})

test('removal and transfer requests require deliberate confirmation and carry no override', () => {
  assert.equal(removalRequest(preview('single'), { confirmed: false, requestKey: 'k' }), null)
  assert.equal(removalRequest(preview('single', false), { confirmed: true, requestKey: 'k' }), null)
  assert.deepEqual(removalRequest(preview('retire_branch'), { confirmed: true, requestKey: 'k', reason: '  done  ' }),
    { request_key: 'k', plan_token: 'token', mode: 'retire_branch', task_disposition: 'preserve', reason: 'done' })
  const state = { transferable: true, removed_owner: { assignment_id: 'old' } }
  assert.equal(transferRequest({ transferable: false }, { confirmed: true, requestKey: 'k' }), null)
  assert.equal(transferRequest(state, { confirmed: false, requestKey: 'k' }), null)
  assert.deepEqual(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'mock', sandbox: 'workspace-write' }),
    { request_key: 'k', expected_owner_assignment_id: 'old', agent_type: 'mock', sandbox: null, reason: null })
  assert.equal(transferRequest(state, { confirmed: true, requestKey: 'k', agentType: 'codex' }).sandbox, 'read-only')
})

test('integration guidance is shown only for ordinary delegations and never claims acknowledgement', () => {
  const candidates = [
    { child_task_id: 'a', title: 'A', integration_available: true, integration_blockers: [] },
    { child_task_id: 'b', title: 'B', integration_available: false, integration_blockers: [{ code: 'workspace_missing', message: 'No workspace.' }] },
  ]
  const ordinary = integrationGuidance({ orchestration_kind: 'delegation_handoff', integration_candidates: candidates })
  assert.deepEqual(ordinary.available.map((c) => c.child_task_id), ['a'])
  assert.deepEqual(ordinary.unavailable.map((c) => c.child_task_id), ['b'])
  assert.match(ordinary.note, /does not acknowledge the delegation, does not unblock this task and does not complete it/)
  const hybrid = integrationGuidance({ orchestration_kind: 'hybrid', integration_candidates: candidates })
  assert.deepEqual([hybrid.available, hybrid.unavailable], [[], []])
  assert.deepEqual(integrationGuidance(null).available, [])
})

test('responsibility and errors are explained without claiming success', () => {
  assert.match(responsibilitySummary({ awaiting_transfer: true, transferable: true }).text, /choose a new owner/)
  assert.match(responsibilitySummary({ awaiting_transfer: true, transferable: false }).text, /cannot be assigned yet/)
  assert.match(responsibilitySummary({ owner: { agent_id: 'x' } }).text, /active owner/)
  const stale = readRemovalError(409, { detail: { code: 'stale_preview', message: 'changed', preview: { mode: 'single' } } })
  assert.equal(stale.code, 'stale_preview')
  assert.deepEqual(stale.preview, { mode: 'single' })
  assert.equal(readRemovalError(503, { detail: { code: 'removal_recovery_required', message: 'm', removal: { phase: 'recovery_required' } } }).removal.phase, 'recovery_required')
  assert.match(readRemovalError(503, {}).message, /retry/)
})

test('removal UI is integrated in the details panel, accessible and not a modal', () => {
  const source = readFileSync(new URL('../src/RemovalSection.jsx', import.meta.url), 'utf8')
  const details = readFileSync(new URL('../src/AgentDetails.jsx', import.meta.url), 'utf8')
  const tree = readFileSync(new URL('../src/AgentTree.jsx', import.meta.url), 'utf8')
  const graph = readFileSync(new URL('../src/AgentHierarchy.jsx', import.meta.url), 'utf8')
  const css = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')
  assert.match(details, /<RemovalSection /)
  assert.doesNotMatch(source, /role="dialog"|aria-modal|window\.confirm|showModal/)
  assert.match(source, /aria-live="polite"/)
  assert.match(source, /role="alert"/)
  assert.match(source, /type="checkbox"[^>]*required/)
  assert.match(source, /Retry safe removal steps/)
  assert.doesNotMatch(source, /confirm_ended|force/)
  // Removal is marked with an icon and text (screen readers) plus strike-through, not color alone.
  assert.match(tree, /removal-mark/)
  assert.match(tree, /\(removed\)/)
  assert.match(graph, /, removed/)
  assert.match(css, /text-decoration:line-through/)
  assert.match(css, /prefers-reduced-motion:reduce\) \{ \.removal-section/)
})
