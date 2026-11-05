// What the Trash and permanent deletion dialogs say, from GET /projects/{id}/lifecycle and the
// backend's refusals. Pure functions: every fact comes from the backend; nothing is assumed.

const plural = (count, one, many) => `${count} ${count === 1 ? one : many}`

// Refusals from the lifecycle routes carry { code, message, reasons, ... } as their detail.
export function lifecycleRefusal(result) {
  const detail = result?.data?.detail
  if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
    return { code: detail.code, message: detail.message || 'The request was refused.', reasons: Array.isArray(detail.reasons) ? detail.reasons : [],
      suggestion: typeof detail.suggestion === 'string' ? detail.suggestion : null, failures: Array.isArray(detail.failures) ? detail.failures : [] }
  }
  if (typeof detail === 'string') return { code: 'refused', message: detail, reasons: [], suggestion: null, failures: [] }
  return null
}

// The work that blocks a move: a sentence per kind, already worded by the backend.
export function activityLines(preview) {
  return Array.isArray(preview?.activity?.reasons) ? preview.activity.reasons : []
}

// What deleting the folder would lose that exists only on this computer. Remotes are never
// contacted, so "not pushed" means "not on any remote this repository knows".
export function localOnlyWarnings(git) {
  if (!git?.available) return []
  const warnings = []
  if (git.uncommitted_changes > 0) warnings.push(`${plural(git.uncommitted_changes, 'file has', 'files have')} changes that were never committed.`)
  if (git.unpushed_commits > 0) warnings.push(`${plural(git.unpushed_commits, 'commit is', 'commits are')} not on any remote.`)
  if (git.stashes > 0) warnings.push(`${plural(git.stashes, 'stash exists', 'stashes exist')} only here.`)
  if (!git.remotes?.length) warnings.push('This repository has no remote, so its whole history exists only on this computer.')
  return warnings
}

// Where the Trash keeps the folder, in words.
export function trashPlace(preview) {
  const location = preview?.trash_location
  if (!location) return null
  return location.kind === 'beside'
    ? `It is kept next to its original folder, in ${location.container}, because the Control Center Trash is on another drive.`
    : `It is kept in the Control Center Trash (${location.container}).`
}

// A short line for a project in the Trash.
export function trashSummary(project) {
  const trash = project?.trash
  if (!trash) return ''
  if (trash.state === 'deleting') return trash.error || 'Deletion did not finish. Choose Delete permanently again to finish it.'
  if (trash.state === 'moving' || trash.state === 'restoring') return 'A move is in progress.'
  return `Was at ${trash.original_root_path}`
}

export function deletionScope(preview) {
  const lines = []
  const folder = preview?.folder
  if (folder?.exists) lines.push(`The folder ${folder.path}, with all its files and its Git history (.git).`)
  else lines.push('The folder is already gone from the Trash; only the records remain.')
  if (preview?.worktrees) lines.push(`${plural(preview.worktrees, 'private task worktree', 'private task worktrees')} the Control Center made for its tasks.`)
  const tasks = preview?.activity?.tasks || 0
  lines.push(`${plural(tasks, 'task', 'tasks')}, ${plural(preview?.agents || 0, 'agent', 'agents')} and their output history in the Control Center.`)
  return lines
}
