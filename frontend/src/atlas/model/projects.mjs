// Registered projects come from GET /projects, the authoritative list; loaded tasks never add to
// it or stand in for it. Pure functions over that response (and its loading or error state).
//
// The backend returns projects in the person's own order (Projects page, drag or keyboard), with
// hidden projects marked. Every selector keeps that order, so a project is where the person put it
// everywhere. Projects in the Trash are not in this list (GET /projects/trash lists them).

const isProject = (item) => item && typeof item.project_id === 'string' && item.project_id.length > 0

// `resource` is a useLiveResource result for GET /projects.
// A stable hue for a project's monogram, from its id (renaming keeps the colour). Hues come from a
// curated ladder of earth and water tones that stays clear of the inks that mean something: course
// purple for attention (280–350), forest green for work (110–160) and olive for held (90–110).
const HUE_LADDER = [25, 185, 55, 215, 5, 200, 40, 230]
export function projectHue(id) {
  let hash = 2166136261
  for (const char of String(id || '')) hash = Math.imul(hash ^ char.codePointAt(0), 16777619)
  return HUE_LADDER[(hash >>> 0) % HUE_LADDER.length]
}

export function projectDirectory(resource) {
  const list = Array.isArray(resource?.data?.projects) ? resource.data.projects.filter(isProject) : null
  if (list) {
    const projects = list.map((item, index) => ({
      id: item.project_id, name: typeof item.name === 'string' && item.name.trim() ? item.name.trim() : 'Untitled project',
      path: typeof item.root_path === 'string' ? item.root_path : '', hidden: item.hidden === true,
      order: Number.isInteger(item.sort_order) ? item.sort_order : index,
    }))
    // A failed later read keeps the last list; `stale` says so.
    return { state: projects.length ? 'ready' : 'empty', projects, byId: Object.fromEntries(projects.map((p) => [p.id, p.name])),
      hiddenIds: new Set(projects.filter((p) => p.hidden).map((p) => p.id)), stale: Boolean(resource.error) }
  }
  if (resource?.error) return { state: 'unavailable', projects: [], byId: {}, hiddenIds: new Set(), stale: false }
  return { state: 'loading', projects: [], byId: {}, hiddenIds: new Set(), stale: false }
}

// How to name a task's project in a row or the inspector.
export function projectLabel(directory, id) {
  if (!id) return 'No project'
  if (directory.byId[id]) return directory.byId[id]
  if (directory.state === 'loading') return 'Loading project…'
  if (directory.state === 'unavailable') return 'Project name unavailable'
  return 'Unregistered project'
}

// The project filter menu: every registered project in the person's order, whether or not a loaded
// task uses it, with hidden projects after the others under their own heading. A selected project
// that is not (or no longer) registered stays visible so the filter is honest.
export function projectMenu(directory, selected) {
  const items = [{ value: null, label: 'All projects', checked: !selected }, { separator: true }]
  if (directory.state === 'loading') items.push({ value: undefined, label: 'Loading projects…', disabled: true })
  else if (directory.state === 'unavailable') items.push({ value: undefined, label: 'Projects couldn’t be loaded', disabled: true })
  else if (directory.state === 'empty') items.push({ value: undefined, label: 'No projects registered yet', disabled: true })
  const shown = directory.projects.filter((project) => !project.hidden)
  const hidden = directory.projects.filter((project) => project.hidden)
  for (const project of shown) items.push({ value: project.id, label: project.name, checked: project.id === selected })
  if (hidden.length) {
    items.push({ separator: true }, { value: undefined, label: 'Hidden projects', disabled: true })
    for (const project of hidden) items.push({ value: project.id, label: project.name, checked: project.id === selected, hidden: true })
  }
  if (selected && !directory.byId[selected] && directory.state !== 'loading') {
    items.push({ value: selected, label: projectLabel(directory, selected), checked: true })
  }
  return items
}

// The visible projects, in order, for navigation (the rail and project pickers).
export function visibleProjects(directory) {
  return directory.projects.filter((project) => !project.hidden)
}

// Moving one project within the order, for keyboard reordering and the "Move up/down" actions.
// Hidden projects keep their place; the order sent is always the full list.
export function moveProject(ids, id, offset) {
  const index = ids.indexOf(id)
  const target = Math.max(0, Math.min(ids.length - 1, index + offset))
  if (index < 0 || target === index) return ids
  const next = ids.filter((item) => item !== id)
  next.splice(target, 0, id)
  return next
}

// The order after a drag: `id` lands at `to` among the visible projects; hidden ones stay where
// they were relative to the rest.
export function dropProject(ids, visibleIds, id, to) {
  const visible = visibleIds.filter((item) => item !== id)
  const clamped = Math.max(0, Math.min(visible.length, to))
  visible.splice(clamped, 0, id)
  const queue = [...visible]
  return ids.map((item) => (visibleIds.includes(item) ? queue.shift() : item))
}
