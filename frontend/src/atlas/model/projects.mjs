// Registered projects come from GET /projects, the authoritative list; loaded tasks never add to
// it or stand in for it. Pure functions over that response (and its loading or error state).

const isProject = (item) => item && typeof item.project_id === 'string' && item.project_id.length > 0

// `resource` is a useLiveResource result for GET /projects.
export function projectDirectory(resource) {
  const list = Array.isArray(resource?.data?.projects) ? resource.data.projects.filter(isProject) : null
  if (list) {
    const projects = list
      .map((item) => ({ id: item.project_id, name: typeof item.name === 'string' && item.name.trim() ? item.name.trim() : 'Untitled project',
        path: typeof item.root_path === 'string' ? item.root_path : '' }))
      .sort((a, b) => a.name.localeCompare(b.name) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0))
    // A failed later read keeps the last list; `stale` says so.
    return { state: projects.length ? 'ready' : 'empty', projects, byId: Object.fromEntries(projects.map((p) => [p.id, p.name])),
      stale: Boolean(resource.error) }
  }
  if (resource?.error) return { state: 'unavailable', projects: [], byId: {}, stale: false }
  return { state: 'loading', projects: [], byId: {}, stale: false }
}

// How to name a task's project in a row or the inspector.
export function projectLabel(directory, id) {
  if (!id) return 'No project'
  if (directory.byId[id]) return directory.byId[id]
  if (directory.state === 'loading') return 'Loading project…'
  if (directory.state === 'unavailable') return 'Project name unavailable'
  return 'Unregistered project'
}

// The project filter menu: every registered project, whether or not a loaded task uses it. A
// selected project that is not (or no longer) registered stays visible so the filter is honest.
export function projectMenu(directory, selected) {
  const items = [{ value: null, label: 'All projects', checked: !selected }, { separator: true }]
  if (directory.state === 'loading') items.push({ value: undefined, label: 'Loading projects…', disabled: true })
  else if (directory.state === 'unavailable') items.push({ value: undefined, label: 'Projects couldn’t be loaded', disabled: true })
  else if (directory.state === 'empty') items.push({ value: undefined, label: 'No projects registered yet', disabled: true })
  for (const project of directory.projects) items.push({ value: project.id, label: project.name, checked: project.id === selected })
  if (selected && !directory.byId[selected] && directory.state !== 'loading') {
    items.push({ value: selected, label: projectLabel(directory, selected), checked: true })
  }
  return items
}
