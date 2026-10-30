import { OFFLINE_MESSAGE, refusalText } from '../../api/agentClient.mjs'

// The outcome of one Connections request, in words. A lost connection or a backend failure while
// handling the request is an unknown outcome (`ok: null`, a warning): it may or may not have taken
// effect, the state is read again, and nothing is retried by itself. A refusal changed nothing.
export function outcome(result, { done, refused, unknown, prefix = 'Nothing was changed. ' }) {
  if (result.offline) return { ok: null, tone: 'warn', message: `${OFFLINE_MESSAGE} ${unknown}` }
  if (result.status === 503 || result.status === 500) return { ok: null, tone: 'warn', message: `The backend couldn’t finish this. ${unknown}` }
  if (!result.ok) return { ok: false, message: `${prefix}${refusalText(result, refused)}` }
  return { ok: true, message: done }
}

export const UNREACHABLE = 'The backend isn’t reachable, so nothing can be changed right now. These actions return when it answers.'
export const STALE = 'The latest state of your tools couldn’t be read, so changes wait until it can.'
