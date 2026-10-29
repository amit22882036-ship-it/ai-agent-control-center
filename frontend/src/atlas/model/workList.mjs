// The Work list, derived from the canonical store. Pure functions: the List renders what these
// return, and the future Map reads the same derived workspace, so both always agree.
import { buildForest, familySummary, taskAgents } from './tasks.mjs'
import { helpersView, integrationView, plannedView, resultView, taskStatus } from './taskStatus.mjs'
import { TOOLS } from '../vocabulary.mjs'

export const SECTIONS = [
  { key: 'needs', label: 'Needs your attention' },
  { key: 'active', label: 'In progress' },
  { key: 'done', label: 'Completed and history' },
]
export const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'needs', label: 'Needs you' },
  { value: 'active', label: 'In progress' },
  { value: 'done', label: 'Done' },
]
// States in which nothing more happens without someone acting.
const ENDED = new Set(['completed', 'integrated', 'canceled', 'stopped'])

export const PLACEMENT_NOTES = {
  parent_not_loaded: 'Its lead is on a page that isn’t loaded yet',
  parent_unresolved: 'Its lead couldn’t be loaded',
  missing_parent: 'Its lead no longer exists',
  cycle: 'Its recorded hierarchy is inconsistent',
}

function descendants(forest, topId) {
  const out = []
  const stack = [topId]
  while (stack.length) {
    const id = stack.pop()
    out.push(id)
    for (const child of forest.children[id] || []) stack.push(child)
  }
  return out
}

// Everything the List and the inspector need, computed once per store.
export function deriveWorkspace(store) {
  const forest = buildForest(store)
  const status = {}
  const agents = {}
  const integration = {}
  for (const id of Object.keys(store.tasks)) {
    agents[id] = taskAgents(store, id)
    status[id] = taskStatus(store.tasks[id], agents[id])
    integration[id] = integrationView(store.tasks[id])
  }
  const families = forest.tops.map((topId) => {
    const members = descendants(forest, topId)
    const attention = members.filter((id) => status[id].attention || integration[id]?.attention)
    const section = attention.length ? 'needs' : members.every((id) => ENDED.has(status[id].key)) ? 'done' : 'active'
    return { topId, section, members, attention: attention.length, projectId: store.tasks[topId].projectId,
      placement: forest.placement[topId], summary: familySummary(store, forest, topId) }
  })
  // Tasks with something below them that needs the person (shown on collapsed branches).
  const attentionBelow = new Set()
  for (const id of Object.keys(store.tasks)) {
    if (!status[id].attention && !integration[id]?.attention) continue
    let parent = forest.parent[id]
    const seen = new Set([id])
    while (parent && !seen.has(parent)) { attentionBelow.add(parent); seen.add(parent); parent = forest.parent[parent] }
  }
  return { forest, status, agents, integration, families, attentionBelow, byTop: Object.fromEntries(families.map((f) => [f.topId, f])) }
}

export function agentLabel(agent) {
  if (!agent) return null
  if (agent.unknown) return 'Agent details unavailable'
  return agent.name || `${TOOLS[agent.tool]?.label || 'Unknown tool'} agent`
}

function searchText(store, derived, id) {
  const task = store.tasks[id]
  const who = derived.agents[id].current || derived.agents[id].last
  return [task.title, agentLabel(who), TOOLS[who?.tool]?.label, who?.model].filter(Boolean).join(' ').toLowerCase()
}

export function familyCounts(derived, project = null) {
  const families = derived.families.filter((family) => !project || family.projectId === project)
  const counts = { all: families.length, needs: 0, active: 0, done: 0 }
  for (const family of families) counts[family.section] += 1
  // "In progress" lists every family that is not finished, including those that need you.
  counts.active += counts.needs
  return counts
}

function inFilter(family, filter) {
  if (filter === 'all') return true
  if (filter === 'active') return family.section !== 'done'
  return family.section === filter
}

// The rows to show. `expanded` holds Task ids; while searching, matches and their loaded path are
// shown expanded. Rows carry tree guides: `guides[i]` says whether a vertical thread continues
// at that ancestor level.
export function visibleRows(store, derived, { expanded = new Set(), filter = 'all', project = null, query = '' } = {}) {
  const q = query.trim().toLowerCase()
  const { forest } = derived
  const keep = new Set()
  if (q) {
    for (const id of Object.keys(store.tasks)) {
      if (!searchText(store, derived, id).includes(q)) continue
      keep.add(id)
      let parent = forest.parent[id]
      const seen = new Set([id])
      while (parent && !seen.has(parent)) { keep.add(parent); seen.add(parent); parent = forest.parent[parent] }
    }
  }
  const sections = SECTIONS.map((section) => ({ ...section, families: [] }))
  const byKey = Object.fromEntries(sections.map((section) => [section.key, section]))
  let shown = 0
  for (const family of derived.families) {
    if (project && family.projectId !== project) continue
    if (!inFilter(family, filter)) continue
    if (q && !keep.has(family.topId)) continue
    const rows = []
    const walk = (id, depth, guides, last, parentId) => {
      const children = (forest.children[id] || []).filter((child) => !q || keep.has(child))
      const open = children.length > 0 && (q ? true : expanded.has(id))
      rows.push({ id, depth, guides, last, parentId, hasChildren: children.length > 0, expanded: open,
        match: q ? searchText(store, derived, id).includes(q) : false, setSize: 0, position: 0 })
      if (!open) return
      children.forEach((child, index) => walk(child, depth + 1, depth > 0 ? [...guides, !last] : [], index === children.length - 1, id))
    }
    walk(family.topId, 0, [], true, null)
    // aria-setsize / aria-posinset among siblings that are shown.
    const siblings = {}
    for (const row of rows) (siblings[row.parentId ?? `top:${family.topId}`] ||= []).push(row)
    for (const list of Object.values(siblings)) list.forEach((row, index) => { row.setSize = list.length; row.position = index + 1 })
    shown += rows.length
    byKey[family.section].families.push({ ...family, rows })
  }
  return { sections: sections.filter((section) => section.families.length), shown, searching: Boolean(q) }
}

// Rows in visual order, for keyboard navigation.
export function flatRows(view) {
  return view.sections.flatMap((section) => section.families.flatMap((family) => family.rows))
}

// WAI-ARIA tree keyboard behaviour over the visible rows. Returns what to do, never mutates.
export function treeKey(rows, currentId, key) {
  if (!rows.length) return null
  const index = Math.max(0, rows.findIndex((row) => row.id === currentId))
  const row = rows[index]
  switch (key) {
    case 'ArrowDown': return { focus: rows[Math.min(rows.length - 1, index + 1)].id }
    case 'ArrowUp': return { focus: rows[Math.max(0, index - 1)].id }
    case 'Home': return { focus: rows[0].id }
    case 'End': return { focus: rows[rows.length - 1].id }
    case 'ArrowRight':
      if (row.hasChildren && !row.expanded) return { expand: row.id }
      if (row.hasChildren && row.expanded) return { focus: rows[index + 1]?.id ?? row.id }
      return null
    case 'ArrowLeft':
      if (row.hasChildren && row.expanded) return { collapse: row.id }
      if (row.parentId) return { focus: row.parentId }
      return null
    case 'Enter':
    case ' ': return { select: row.id }
    default: return null
  }
}

const MINUTE = 60000
export function durationText(ms) {
  const minutes = Math.max(0, Math.floor(ms / MINUTE))
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h${minutes % 60 ? ` ${minutes % 60}m` : ''}`
  const days = Math.floor(hours / 24)
  return `${days}d`
}

function parse(value) {
  const time = Date.parse(value || '')
  return Number.isFinite(time) ? time : null
}

// A short, honest time for a row: how long the current agent has held the Task, when the last one
// ended, or when the Task was created. The title says which.
export function timeView(store, id, now) {
  const assignment = store.assignments[id]
  const task = store.tasks[id]
  const held = parse(assignment?.current?.assignedAt)
  if (held !== null) return { text: durationText(now - held), title: 'Time since its current agent took this task' }
  const ended = parse(assignment?.lastEnded?.endedAt)
  if (ended !== null) {
    const span = durationText(now - ended)
    return { text: span === 'just now' ? span : `${span} ago`, title: 'Time since its last agent finished' }
  }
  const created = parse(task?.createdAt)
  if (created !== null) {
    const span = durationText(now - created)
    return { text: span === 'just now' ? span : `${span} ago`, title: 'Time since the task was created' }
  }
  return { text: '—', title: 'No time recorded' }
}

const humanize = (code) => String(code || '').replace(/_/g, ' ')

// What is happening now, in a sentence, from reported facts only.
export function nowView(store, derived, id) {
  const task = store.tasks[id]
  const status = derived.status[id]
  const helpers = helpersView(task)
  const planned = plannedView(task)
  const into = derived.integration[id]
  const result = resultView(task)
  const who = derived.agents[id].current || derived.agents[id].last
  const plannedText = planned ? `${planned.count} more planned, not started` : ''
  let line = status.label
  let second = status.detail || ''
  let tone = status.tone
  switch (status.key) {
    case 'needs_input': line = 'Asked you a question'; second = 'Waiting for your reply'; break
    case 'waiting_helpers':
      // Only helpers still working count; a delivered result is waiting for the lead, not the other way round.
      if (!status.detail && task.delegations.materialized) line = `Waiting for ${task.delegations.materialized} helper${task.delegations.materialized === 1 ? '' : 's'}`
      second = second || plannedText || 'Continues when their results arrive'
      break
    case 'reviewing': second = helpers.resultsReady ? `${helpers.resultsReady} result${helpers.resultsReady === 1 ? '' : 's'} ready` : plannedText; break
    case 'running': second = task.delegations.materialized ? `${task.delegations.materialized} helper${task.delegations.materialized === 1 ? '' : 's'} working` : plannedText; break
    case 'provider_wait': {
      const reset = who?.providerCondition?.blocking?.find((item) => item.resetAt)?.resetAt
      second = reset ? `Reset reported by the provider: ${new Date(reset).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' })}` : 'Nothing resumes by itself'
      break
    }
    case 'blocked': second = task.blockers.length ? humanize(task.blockers[0]) + (task.blockers.length + task.blockersMore > 1 ? ` and ${task.blockers.length + task.blockersMore - 1} more` : '') : second; break
    case 'needs_owner': line = status.label; second = second || 'Its agent was removed; choose a new owner'; break
    case 'recovery': second = 'Review it in the task’s details'; break
    case 'integrated': second = 'Not independently verified'; break
    case 'completed':
      line = result ? result.label : 'Completed'
      second = into ? (into.scope === 'project' && into.status === 'not_started' ? 'Not applied to the project yet' : into.label) : ''
      tone = into?.attention ? 'alert' : tone
      break
    case 'stopped': if (result?.outcome === 'failed') second = 'Reported failure'; break
    default: break
  }
  return { line, second, tone }
}

export function whoView(derived, id) {
  const { current, last, replaced, previous } = derived.agents[id]
  const agent = current || last
  if (!agent) return { agent: null, name: 'No agent', detail: 'Nobody is assigned', role: 'none', replaced: false }
  const parts = [TOOLS[agent.tool]?.label, agent.model].filter(Boolean)
  return {
    agent,
    name: agentLabel(agent),
    role: current ? 'current' : 'last',
    detail: current ? parts.join(' · ') : `Last agent${agent.removed ? ' · removed' : parts[0] ? ` · ${parts[0]}` : ''}`,
    replaced,
    previousCount: previous.length,
    removed: Boolean(agent.removed),
  }
}
