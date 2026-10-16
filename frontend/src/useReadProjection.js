import { useEffect, useRef, useState } from 'react'
import { createRefreshQueue } from './agentEventStream.mjs'
import { readProjection } from './readProjection.mjs'

// Reuse shared invalidation; no EventSource or polling connection per node.
export default function useReadProjection(path, refreshVersion) {
  const [state, setState] = useState({ path, data: null, error: false })
  const refresh = useRef(null)
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    const queue = createRefreshQueue(() => readProjection(path, controller.signal, setState))
    refresh.current = queue.request
    queue.request(true)
    return () => { refresh.current = null; queue.dispose(); controller.abort() }
  }, [path])
  useEffect(() => { refresh.current?.() }, [refreshVersion])
  return state.path === path ? state : { data: null, error: false }
}
