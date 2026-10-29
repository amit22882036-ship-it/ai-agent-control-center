import { useEffect, useRef, useState } from 'react'
import { Button, Skeleton } from '../primitives'
import { apiFetch } from '../../../apiClient.mjs'
import { createOutputLoader, emptyOutput } from '../../../outputHistory.mjs'

// An agent's output, with earlier pages on request and new lines as the shared connection reports
// changes (classic paging rules: a window of up to 2,000 lines; browsing earlier output is never
// displaced by new lines).
export function OutputHistory({ agentId, version, historical }) {
  const [state, setState] = useState(() => emptyOutput(agentId))
  const [status, setStatus] = useState('loading')
  const loader = useRef(null)
  useEffect(() => {
    const instance = createOutputLoader(agentId, async (url) => {
      const response = await apiFetch(url, { cache: 'no-store' })
      if (!response.ok) { const error = new Error('unavailable'); error.status = response.status; throw error }
      return response.json()
    }, (next) => { setState(next); setStatus('ready') })
    loader.current = instance
    instance.load('latest').catch((error) => setStatus(error?.status === 404 ? 'missing' : 'error'))
    return () => { instance.dispose(); loader.current = null }
  }, [agentId])
  useEffect(() => {
    if (version > 0) loader.current?.load('live').catch(() => setStatus((current) => (current === 'ready' ? 'ready' : 'error')))
  }, [version])
  const load = (direction) => loader.current?.load(direction).catch(() => setStatus('error'))
  return <section className="a-blk" aria-label="Output">
    {historical && <p className="a-fine a-insp-lead">From its last agent, which is no longer working on this task.</p>}
    {status === 'loading' && <Skeleton lines={4} label="Loading output" />}
    {status === 'missing' && <p className="a-fine">Output isn’t available for this agent.</p>}
    {status === 'error' && state.items.length === 0 && <p className="a-fine">Output is unavailable right now. It is read again with the next update.</p>}
    {status !== 'loading' && status !== 'missing' && <>
      <div className="a-act-row">
        {state.hasOlder && <Button size="sm" onClick={() => load('older')}>Show earlier lines</Button>}
        {state.browsing && <Button size="sm" onClick={() => load('latest')}>Back to the latest</Button>}
      </div>
      {state.items.length === 0 ? (status === 'ready' && <p className="a-fine">No output recorded.</p>)
        : <div className="a-out" tabIndex={0} aria-label="Output lines">{state.items.map((item) =>
          <div key={item.seq}><span className="a-out-k a-num">{item.seq}</span><span className="a-bidi" dir="auto">{item.text}</span></div>)}</div>}
      {state.browsing && <p className="a-fine">You are reading earlier output; new lines are not added until you go back to the latest.</p>}
    </>}
  </section>
}
