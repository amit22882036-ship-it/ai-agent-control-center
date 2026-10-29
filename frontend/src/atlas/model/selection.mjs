// UI selection and focus over the canonical store, shared by List and Map. Selection is a Task id
// (never an Agent id or a row index), so it survives Agent replacement, reordering and refreshes.
// The selected Task is kept in the address (#/work?task=…) so Back/Forward and reloads restore it.
import { pathTo } from './tasks.mjs'

export function emptySelection() {
  return { taskId: null, focusId: null, expanded: [] }
}

export function selectionFromRoute(route) {
  const taskId = typeof route?.params?.task === 'string' && route.params.task.length <= 200 ? route.params.task : null
  return { ...emptySelection(), taskId, focusId: taskId }
}

export function routeParamsFor(selection, params = {}) {
  return { ...params, task: selection.taskId || undefined }
}

// Select a Task and expand the loaded path to it.
export function select(selection, forest, taskId) {
  const expanded = new Set(selection.expanded)
  if (taskId) for (const ancestor of pathTo(forest, taskId)) expanded.add(ancestor)
  return { taskId, focusId: taskId, expanded: [...expanded].sort() }
}

export function toggleExpanded(selection, taskId) {
  const expanded = new Set(selection.expanded)
  if (expanded.has(taskId)) expanded.delete(taskId)
  else expanded.add(taskId)
  return { ...selection, expanded: [...expanded].sort() }
}

// After a refresh: keep the selection while the Task exists; otherwise say why it is not shown.
// "not_loaded" means more pages exist, so the Task may still be there.
export function reconcile(selection, store) {
  const present = (taskId) => Boolean(taskId && store.tasks[taskId])
  const expanded = selection.expanded.filter(present)
  const status = !selection.taskId ? 'none' : present(selection.taskId) ? 'present' : store.complete ? 'missing' : 'not_loaded'
  return {
    selection: { ...selection, focusId: present(selection.focusId) ? selection.focusId : (status === 'present' ? selection.taskId : null), expanded },
    status,
  }
}
