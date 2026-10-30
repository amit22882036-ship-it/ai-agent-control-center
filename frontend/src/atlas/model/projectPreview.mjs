// The small hierarchy picture on a project card, kept small however large the project is. It is
// built straight from the loaded forest (parent_task_id only), breadth first from each family's
// top, within fixed limits: a few families, a few dozen Tasks in all, a few helpers under one lead
// and a few levels. What is left out is counted where it hangs ("+N" next to its lead) and in the
// totals, never dropped silently; nothing is read from the backend to draw it. Pure.
import { mapLayout } from './mapLayout.mjs'

export const PREVIEW = { families: 6, tasks: 36, children: 6, depth: 4 }
const SECTION_ORDER = { needs: 0, active: 1, done: 2 }

export function projectPreview(derived, projectId, limits = PREVIEW) {
  const families = derived.families.filter((family) => family.projectId === projectId)
  const loaded = families.reduce((sum, family) => sum + family.members.length, 0)
  if (!families.length) return { families: 0, shownFamilies: 0, loaded: 0, shown: 0, truncated: false, more: {}, layout: null }
  // What needs the person first, then work in progress, then finished work; inside, the List's order.
  const chosen = families.map((family, index) => [family, index])
    .sort((a, b) => SECTION_ORDER[a[0].section] - SECTION_ORDER[b[0].section] || a[1] - b[1])
    .slice(0, limits.families).map(([family]) => family)
  const { children } = derived.forest
  const more = {}
  const picked = []
  let budget = limits.tasks
  for (const family of chosen) {
    if (budget <= 0) break
    const rows = [{ id: family.topId, depth: 0, parentId: null, hasChildren: (children[family.topId] || []).length > 0 }]
    budget -= 1
    for (let head = 0; head < rows.length; head += 1) {
      const row = rows[head]
      const kids = children[row.id] || []
      let kept = 0
      if (row.depth + 1 < limits.depth) {
        for (const child of kids) {
          if (kept >= limits.children || budget <= 0) break
          rows.push({ id: child, depth: row.depth + 1, parentId: row.id, hasChildren: (children[child] || []).length > 0 })
          kept += 1
          budget -= 1
        }
      }
      // Helpers not drawn under this Task (each with its own branch).
      if (kept < kids.length) more[row.id] = kids.length - kept
    }
    picked.push({ ...family, rows })
  }
  const shown = picked.reduce((sum, family) => sum + family.rows.length, 0)
  const layout = mapLayout({ sections: [{ key: 'all', label: '', families: picked }] }, derived,
    { maxRow: Infinity, bands: false, chips: () => false })
  return { families: families.length, shownFamilies: picked.length, loaded, shown, truncated: shown < loaded, more, layout }
}
