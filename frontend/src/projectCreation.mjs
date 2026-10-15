import { apiFetch } from './apiClient.mjs'

const base = 'http://127.0.0.1:8000'

export async function projectRequest(path, { signal, body } = {}, fetcher = apiFetch) {
  const response = await fetcher(`${base}${path}`, {
    signal, ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Project request failed. Check the backend and retry.')
  return data
}

export async function loadProjects(signal, fetcher = fetch) {
  const data = await projectRequest('/projects', { signal }, fetcher)
  if (!Array.isArray(data.projects) || data.projects.some(project => !project.project_id || !project.name || !project.root_path)) {
    throw new Error('Project list is unavailable. Refresh and try again.')
  }
  return data.projects
}

export function selectedProject(projects, id) {
  return projects.find(project => project.project_id === id) || null
}
