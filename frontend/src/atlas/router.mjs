import { TOOLS } from './vocabulary.mjs'

// Atlas routes live in the URL hash ("#/settings?section=appearance") so that the browser's Back
// and Forward buttons, reloads and bookmarks work without a server-side router or a dependency.
export const ROUTES = {
  work: { title: 'Work', nav: true },
  projects: { title: 'Projects', nav: true },
  connections: { title: 'Connections', nav: true },
  settings: { title: 'Settings', nav: true },
  new: { title: 'New task', nav: false },
  task: { title: 'Task', nav: false },
  gallery: { title: 'Design gallery', nav: false },
}
export const DEFAULT_ROUTE = 'work'
export const NAV_ROUTES = Object.keys(ROUTES).filter((name) => ROUTES[name].nav)

export function parseHash(hash) {
  const raw = String(hash || '').replace(/^#/, '')
  const [path, query = ''] = raw.split('?')
  const name = path.replace(/^\/+|\/+$/g, '').split('/')[0] || DEFAULT_ROUTE
  const params = {}
  for (const part of query.split('&')) {
    if (!part) continue
    const [key, value = ''] = part.split('=')
    try { params[decodeURIComponent(key)] = decodeURIComponent(value) } catch { /* A malformed pair is ignored. */ }
  }
  if (!Object.hasOwn(ROUTES, name)) return { name: 'not-found', requested: name, params }
  return { name, params }
}

export function formatHash(name, params = {}) {
  const query = Object.keys(params).sort().filter((key) => params[key] !== undefined && params[key] !== null && params[key] !== '')
    .map((key) => `${encodeURIComponent(key)}=${encodeURIComponent(params[key])}`).join('&')
  return `#/${name}${query ? `?${query}` : ''}`
}

// Each Task page is its own page (focus moves to its heading, it opens at the top); other
// parameters (a selection, a filter) stay on the same page.
export function routeKey(route) {
  if (route.name === 'not-found') return `not-found:${route.requested}`
  if (route.name === 'task') return `task:${route.params?.id || ''}`
  // Each tool's Connections page is its own page too; a section within it is not.
  if (route.name === 'connections') return `connections:${connectionTool(route) || ''}`
  return route.name
}

export function documentTitle(route) {
  const tool = connectionTool(route)
  const title = route.name === 'not-found' ? 'Page not found' : tool ? `${TOOLS[tool].label} · Connections` : ROUTES[route.name].title
  return `${title} · AI Agent Control Center`
}

// Which top-level navigation item a route belongs to (New task and Task pages open from Work).
export function navSection(route) {
  if (route.name === 'new' || route.name === 'task') return 'work'
  return ROUTES[route.name]?.nav ? route.name : null
}

// The tool a Connections page shows, when its address names a real one.
export function connectionTool(route) {
  const tool = route?.name === 'connections' ? route.params?.provider : null
  return ['claude', 'codex', 'gemini'].includes(tool) ? tool : null
}
