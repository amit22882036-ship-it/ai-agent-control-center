// What needs the person, derived from the canonical Task store and the backend's own counts. Pure.
//
// Rules:
// - An item is a Task, never an Agent: the Task id is the stable identity, and the Agent shown is
//   the Task's current one at the time of reading (a replaced Agent is never named as the asker).
// - Only Tasks in the loaded pages can be listed. The backend's workspace count of questions
//   (overview.needs_input) is the only count that covers the whole workspace; anything else is a
//   count of loaded Tasks and is said to be one.
// - The order is the List's own order (sections, families newest first, helpers by creation), so
//   moving to the next item is predictable and matches what the person sees.
import { flatRows, visibleRows, agentLabel } from './workList.mjs'

// Kinds, in the words the person sees. `reply` is the only kind the backend counts workspace-wide.
export const ATTENTION_KINDS = {
  reply: { label: 'Waiting for your reply', short: 'Asked you a question', action: 'Reply' },
  owner: { label: 'Waiting for a new owner', short: 'Needs a new owner', action: 'Choose an owner' },
  recovery: { label: 'Needs inspection', short: 'Stopped short of guessing', action: 'Review' },
  integration: { label: 'Work couldn’t be combined', short: 'Combining needs you', action: 'Review' },
  // Its agent stopped for a reason the person must see (a provider error, a refused control message, a restart).
  stopped: { label: 'Stopped: needs attention', short: 'Stopped: needs attention', action: 'Review' },
}
export const KIND_ORDER = ['reply', 'owner', 'recovery', 'integration', 'stopped']
// A question that is a plan waiting for approval is still a reply (counted with questions), named as a plan.
export const PLAN_APPROVAL_LABEL = 'Plan waiting for approval'
export function attentionLabel(entry) {
  return entry.plan ? PLAN_APPROVAL_LABEL : ATTENTION_KINDS[entry.kind].label
}

// The kind of attention a loaded Task needs, or null.
export function attentionKind(derived, taskId) {
  const status = derived.status[taskId]
  if (!status) return null
  if (status.key === 'needs_input') return 'reply'
  if (status.key === 'needs_owner') return 'owner'
  if (status.key === 'recovery') return 'recovery'
  if (derived.integration[taskId]?.attention) return 'integration'
  if (status.key === 'refused') return 'stopped'
  return null
}

function item(store, derived, taskId, kind) {
  const task = store.tasks[taskId]
  const { current } = derived.agents[taskId]
  const agent = current && !current.unknown ? current : null
  return {
    taskId,
    kind,
    title: task.title || 'Untitled task',
    // Only the current Agent is named: a question is answered by whoever holds the Task now.
    agentId: agent?.id || null,
    agentName: agent ? agentLabel(agent) : null,
    plan: kind === 'reply' && agent?.awaitsPlanApproval === true,
    projectId: task.projectId,
    updatedAt: task.updatedAt,
  }
}

// Every loaded Task that needs the person, in the List's order (everything expanded, no filters).
export function attentionItems(store, derived) {
  if (!store || !derived) return []
  const all = new Set(Object.keys(store.tasks))
  const view = visibleRows(store, derived, { expanded: all })
  const items = []
  for (const row of flatRows(view)) {
    const kind = attentionKind(derived, row.id)
    if (kind) items.push(item(store, derived, row.id, kind))
  }
  return items
}

// The items reachable from the Work view the person has now: the same project, focus, filter,
// colours and search as the List, with every branch counted as open (moving to an item opens the
// path to it). Returns the ids in order.
export function attentionInView(store, derived, { filter = 'all', project = null, query = '', colors = null, focus = null } = {}) {
  if (!store || !derived) return []
  const all = new Set(Object.keys(store.tasks))
  const view = visibleRows(store, derived, { expanded: all, filter, project, query, colors, focus })
  return flatRows(view).map((row) => row.id).filter((id) => attentionKind(derived, id))
}

// The next (or previous) id after `currentId` in `ids`, wrapping around. When the current Task is
// not in the list, the first (or last) one. Null when there is nothing to move to.
export function stepAttention(ids, currentId, direction = 1) {
  if (!ids.length) return null
  const index = ids.indexOf(currentId)
  if (index === -1) return direction > 0 ? ids[0] : ids[ids.length - 1]
  if (ids.length === 1) return ids[0]
  return ids[(index + direction + ids.length) % ids.length]
}

const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null)

// What can honestly be said about how many things need the person.
//   reported    the backend's workspace-wide count of agents waiting for a reply (or null)
//   loadedReply questions among the loaded Tasks
//   loaded      every loaded item, all kinds
//   complete    every page of Tasks is loaded
//   unlisted    questions the backend counts that the loaded pages don't show (never negative)
export function attentionSummary(items, overview, store) {
  const reported = count(overview?.needs_input)
  const loadedReply = items.filter((entry) => entry.kind === 'reply').length
  const complete = Boolean(store?.complete)
  const unlisted = reported === null ? null : Math.max(0, reported - loadedReply)
  const byKind = Object.fromEntries(KIND_ORDER.map((kind) => [kind, items.filter((entry) => entry.kind === kind)]))
  return { reported, loadedReply, loaded: items.length, complete, unlisted, byKind }
}

// One sentence on what the list covers, or null when it covers everything the backend counts.
export function coverageText(summary) {
  if (summary.reported === null) {
    return summary.complete ? 'The backend’s count isn’t available right now; this list is from the tasks loaded.' : 'From the tasks loaded so far; older tasks aren’t included.'
  }
  if (summary.unlisted > 0) {
    const n = summary.unlisted
    return summary.complete
      ? `The backend counts ${summary.reported} waiting for a reply; ${n} ${n === 1 ? 'isn’t' : 'aren’t'} in this list yet.`
      : `The backend counts ${summary.reported} waiting for a reply; ${n} may be on older pages that aren’t loaded.`
  }
  return summary.complete ? null : 'Other kinds are listed from the loaded tasks only.'
}

// The bell's accessible name: what it covers, in words, with counts only where they are known.
export function bellLabel(summary, { updates = 0, tools = 0 } = {}) {
  const parts = []
  if (summary.known === false) parts.push('what needs you isn’t known yet')
  if (summary.reported !== null && summary.reported > 0) parts.push(`${summary.reported} waiting for your reply`)
  else if (summary.reported === null && summary.loadedReply > 0) parts.push(`${summary.loadedReply} waiting for your reply among loaded tasks`)
  const others = summary.loaded - summary.loadedReply
  if (others > 0) parts.push(`${others} other task${others === 1 ? '' : 's'} need${others === 1 ? 's' : ''} you`)
  if (tools > 0) parts.push(`${tools} tool${tools === 1 ? '' : 's'} need${tools === 1 ? 's' : ''} attention`)
  if (updates > 0) parts.push(`${updates} tool update${updates === 1 ? '' : 's'} available`)
  return parts.length ? `Notifications: ${parts.join(', ')}` : 'Notifications: nothing needs you'
}
