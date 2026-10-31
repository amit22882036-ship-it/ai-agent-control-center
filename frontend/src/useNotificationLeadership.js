import { useEffect, useState } from 'react'
import { LEADERSHIP_FRESH_MS, requestLeadership } from './notificationCoordination.mjs'

// Holds the shared notification lock while `wanted` is true (the page is eligible to show every
// notification itself) and gives it up as soon as it is not, so an eligible page in another tab,
// Classic or Atlas, can take over. Returns whether this page holds it.
export default function useNotificationLeadership(wanted) {
  const [leader, setLeader] = useState(false)
  useEffect(() => {
    if (!wanted) return undefined
    return requestLeadership(setLeader)
  }, [wanted])
  return wanted && leader
}

// Whether the last successful read (a timestamp, or null) is still current. One timer, set for
// the moment that read becomes too old; a newer read replaces it. No request is made: a page
// whose read has expired is simply not eligible until its next read.
export function useFreshRead(lastReadAt) {
  const [expired, setExpired] = useState(null)
  useEffect(() => {
    if (!lastReadAt) return undefined
    const timer = setTimeout(() => setExpired(lastReadAt), Math.max(0, lastReadAt + LEADERSHIP_FRESH_MS - Date.now()))
    return () => clearTimeout(timer)
  }, [lastReadAt])
  return Boolean(lastReadAt) && expired !== lastReadAt
}
