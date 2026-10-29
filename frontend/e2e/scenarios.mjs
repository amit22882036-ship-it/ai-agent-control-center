// Synthetic GET /workspace/tasks responses for browser tests and review screenshots. They use the
// endpoint's exact node shape (checked against the recorded contract in atlas-work.spec.mjs) with
// invented content. Nothing here comes from a real workspace.
const BASE = Date.parse('2026-10-29T12:00:00.000Z')
const at = (minutesAgo) => new Date(BASE - minutesAgo * 60000).toISOString()

export function node(taskId, parent = null, extra = {}) {
  return {
    task_id: taskId, parent_task_id: parent, project_id: 'p1', title: `Task ${taskId}`, status: 'pending', control_intent: 'active',
    stop_required: false, created_at: at(600), updated_at: at(600), root_task_id: null, depth: null, ancestry: 'complete',
    child_count: 0, blockers: [], blockers_more: 0,
    assignment: { current: null, last_ended: null, count: 0, previous_agent_ids: [] }, agent: null, waiting_context: null,
    responsibility_awaiting: false, transfer: null, continuation: null, delegation: null,
    delegations: { requested: 0, materialized: 0, result_ready: 0, acknowledged: 0, canceled: 0 },
    planned: { unmaterialized: 0, by_disposition: {} }, plan_revision: null, rounds: 0, integration: null, ...extra,
  }
}

export function agent(agentId, { status = 'running', name = null, tool = 'claude', model = 'Sonnet 4.6', effort = 'medium', color = 'blue',
  role = 'current', asks = false, removed = false, condition = null, autonomous = false } = {}) {
  return { agent_id: agentId, role, status, display_name: name || '', display_color: color, agent_type: tool, model, effort,
    asks_user: asks, provider_condition: condition, autonomous,
    removed, removal: removed ? { state: 'removed', mode: 'single', failure: null, removed_at: at(90) } : null }
}

const holding = (agentId, minutesAgo) => ({ current: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: at(minutesAgo) },
  last_ended: null, count: 1, previous_agent_ids: [] })
const ended = (agentId, minutesAgo, reason = 'completed', previous = []) => ({ current: null,
  last_ended: { assignment_id: `as-${agentId}`, agent_id: agentId, assigned_at: at(minutesAgo + 40), ended_at: at(minutesAgo), ended_reason: reason },
  count: 1 + previous.length, previous_agent_ids: [agentId, ...previous] })
const child = (id, parent, delegationStatus, extra) => node(id, parent, {
  delegation: { delegation_id: `d-${id}`, parent_task_id: parent, status: delegationStatus, result: null }, ...extra })

// A workspace that resembles daily use: a lead with helpers several levels deep, a question for
// the person, replaced and removed agents, a provider limit, finished and applied work, and a
// Hebrew task name.
export function showcase() {
  const tasks = [
    node('csv', null, { title: 'Build the CSV import pipeline', status: 'in_progress', created_at: at(64), child_count: 3, root_task_id: 'csv', depth: 0,
      assignment: holding('lead', 64), agent: agent('lead', { name: 'Atlas lead', color: 'violet', autonomous: true, model: 'Opus 4.8', effort: 'high' }),
      continuation: { phase: 'waiting', mode: null, current_assignment: true },
      waiting_context: { kind: 'delegation', label: 'Waiting for delegated results' },
      delegations: { requested: 1, materialized: 2, result_ready: 1, acknowledged: 0, canceled: 0 },
      planned: { unmaterialized: 1, by_disposition: { pending: 1 } }, plan_revision: 2, rounds: 1 }),
    child('quoted', 'csv', 'materialized', { title: 'Handle quoted fields and embedded newlines', status: 'waiting', created_at: at(60), depth: 1,
      assignment: holding('quoter', 41), agent: agent('quoter', { status: 'waiting', name: 'Quoted fields', color: 'yellow', asks: true }),
      waiting_context: { kind: 'user', label: 'Waiting for you' } }),
    child('cli', 'csv', 'result_ready', { title: 'JSON command-line interface', status: 'completed', created_at: at(59), depth: 1, child_count: 1,
      assignment: ended('cli-2', 12, 'completed', ['cli-1']), agent: agent('cli-2', { status: 'finished', role: 'last', name: 'CLI writer', tool: 'codex', model: 'GPT-5.5', color: 'cyan' }),
      delegation: { delegation_id: 'd-cli', parent_task_id: 'csv', status: 'result_ready',
        result: { outcome: 'completed', phase: 'delivered', integration: { integration_id: 'i-cli', status: 'applied', applied_at: at(11) } } } }),
    child('cli-docs', 'cli', 'acknowledged', { title: 'Document the CLI flags', status: 'completed', created_at: at(40), depth: 2,
      assignment: ended('docs', 20), agent: agent('docs', { status: 'finished', role: 'last', name: 'Docs', color: 'green', model: 'Haiku 4.5', effort: 'low' }),
      delegation: { delegation_id: 'd-docs', parent_task_id: 'cli', status: 'acknowledged',
        result: { outcome: 'completed', phase: 'accepted', integration: { integration_id: 'i-docs', status: 'applied', applied_at: at(18) } } } }),
    child('report', 'csv', 'materialized', { title: 'HTML summary report', status: 'in_progress', created_at: at(55), depth: 1,
      assignment: { current: { assignment_id: 'as-rep2', agent_id: 'rep-2', assigned_at: at(30) },
        last_ended: { assignment_id: 'as-rep1', agent_id: 'rep-1', assigned_at: at(55), ended_at: at(31), ended_reason: 'removed' }, count: 2, previous_agent_ids: ['rep-1'] },
      agent: agent('rep-2', { name: 'Report builder', color: 'orange', tool: 'codex', model: 'GPT-5.5' }) }),
    node('limits', null, { title: 'Summarize last week’s error logs', status: 'waiting', created_at: at(150), root_task_id: 'limits', depth: 0,
      assignment: holding('logs', 150), agent: agent('logs', { status: 'waiting', name: 'Log reader', color: 'pink',
        condition: { kind: 'usage_limited', label: 'Waiting for provider usage', blocking: [{ label: '5-hour limit', reset_at: '2026-10-29T14:30:00.000Z' }] } }),
      waiting_context: { kind: 'provider', label: 'Waiting for provider usage', condition: 'usage_limited' } }),
    node('hebrew', null, { title: 'לתרגם את מסך ההגדרות לעברית (Settings page)', status: 'in_progress', created_at: at(35), root_task_id: 'hebrew', depth: 0,
      assignment: holding('he', 35), agent: agent('he', { name: 'מתרגם', color: 'green', model: 'Sonnet 4.6' }) }),
    node('migrate', null, { title: 'Migrate the settings database', status: 'in_progress', created_at: at(240), root_task_id: 'migrate', depth: 0,
      assignment: ended('mig', 95, 'removed'), agent: agent('mig', { status: 'stopped', role: 'last', name: 'Migrator', color: 'red', removed: true }),
      responsibility_awaiting: true }),
    node('rename', null, { title: 'Rename the settings page', status: 'completed', created_at: at(1500), root_task_id: 'rename', depth: 0,
      assignment: ended('ren', 1380), agent: agent('ren', { status: 'finished', role: 'last', name: 'Renamer', tool: 'codex', model: 'GPT-5.5', color: 'blue' }),
      integration: { integration_id: 'i-ren', destination_kind: 'project', status: 'applied', validation_status: 'not_run', created_at: at(1381), applied_at: at(1380) } }),
    node('backlog', null, { title: 'Profile the slow dashboard load', status: 'pending', created_at: at(20), root_task_id: 'backlog', depth: 0 }),
  ]
  return { tasks: [...tasks].sort((a, b) => (a.created_at < b.created_at ? 1 : -1)), ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null }
}

// `families` roots, each with `width` children; every fifth child has `depth` more levels.
export function large({ families = 50, width = 19, depth = 0 } = {}) {
  const tasks = []
  let minute = 0
  for (let f = 0; f < families; f += 1) {
    const root = `r${String(f).padStart(3, '0')}`
    tasks.push(node(root, null, { title: `Family ${f} lead task with a reasonably long descriptive name`, status: 'in_progress', created_at: at(5000 - (minute += 1)),
      child_count: width, assignment: holding(`a-${root}`, 30), agent: agent(`a-${root}`, { name: `Lead ${f}`, color: ['violet', 'blue', 'green'][f % 3] }) }))
    for (let c = 0; c < width; c += 1) {
      const id = `${root}-c${c}`
      tasks.push(node(id, root, { title: `Helper ${c} of family ${f}`, status: c % 4 ? 'completed' : 'in_progress', created_at: at(5000 - (minute += 1)),
        child_count: depth && c % 5 === 0 ? 1 : 0,
        assignment: c % 4 ? ended(`a-${id}`, 10) : holding(`a-${id}`, 10), agent: agent(`a-${id}`, { status: c % 4 ? 'finished' : 'running', role: c % 4 ? 'last' : 'current' }) }))
      let parent = id
      for (let d = 0; depth && c % 5 === 0 && d < depth; d += 1) {
        const deep = `${id}-d${d}`
        tasks.push(node(deep, parent, { title: `Level ${d + 2} under ${id}`, created_at: at(5000 - (minute += 1)), child_count: d < depth - 1 ? 1 : 0 }))
        parent = deep
      }
    }
  }
  return { tasks: [...tasks].reverse(), ancestors: [], ancestors_truncated: false, next_after: null, total: tasks.length, root: null }
}
