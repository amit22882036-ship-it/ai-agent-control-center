// What the Atlas shell may say about the backend. Only values the backend reported are shown: a
// missing or malformed count is "not available", never zero.
export const OVERVIEW_FIELDS = [
  ['needs_input', 'Need your input'],
  ['active_agents', 'Active agents'],
  ['waiting_tasks', 'Waiting tasks'],
  ['pending_responsibilities', 'Pending responsibilities'],
  ['completed_tasks', 'Completed tasks'],
]

const count = (value) => Number.isInteger(value) && value >= 0 ? value : null

export function overviewView(overview) {
  if (!overview || typeof overview !== 'object') return null
  return OVERVIEW_FIELDS.map(([key, label]) => ({ key, label, value: count(overview[key]) }))
}

export function needsYouCount(overview) {
  return count(overview?.needs_input)
}

export function needsYouLabel(value) {
  return value === 1 ? '1 needs you' : `${value} need you`
}

// `transport` mirrors connectAgentEvents ("live" or "polling"); `reachable` is whether the last
// overview read succeeded (null before the first answer).
export function connectionView({ transport, reachable }) {
  if (reachable === null || reachable === undefined) {
    return { tone: 'pending', label: 'Connecting', detail: 'Contacting the Control Center backend.' }
  }
  if (!reachable) {
    return { tone: 'offline', label: 'Backend unavailable', detail: 'The backend at 127.0.0.1:8000 is not answering. Retrying automatically.' }
  }
  if (transport === 'live') return { tone: 'live', label: 'Live', detail: 'Changes arrive as they happen.' }
  return { tone: 'polling', label: 'Polling', detail: 'Live updates are unavailable, so the dashboard checks every 2 seconds.' }
}
