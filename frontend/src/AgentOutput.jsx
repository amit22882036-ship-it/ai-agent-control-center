import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createRefreshQueue } from './agentEventStream.mjs'
import { createOutputLoader, emptyOutput, followOutput, anchoredScrollTop, OUTPUT_WINDOW } from './outputHistory.mjs'

export default function AgentOutput({ agentId, refreshVersion }) {
  const [output, setOutput] = useState(() => emptyOutput(agentId))
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const panel = useRef(null)
  const anchor = useRef(null)
  const controls = useRef(null)

  useEffect(() => {
    const controller = new AbortController()
    const loader = createOutputLoader(agentId, async (url) => {
      const response = await fetch(url, { signal: controller.signal })
      if (!response.ok) throw new Error('Unable to load output. Please retry.')
      return response.json()
    }, (next, direction) => {
      const element = panel.current
      if (element) {
        const top = element.getBoundingClientRect().top
        const first = [...element.children].find((line) => line.getBoundingClientRect().bottom > top)
        anchor.current = {
          follow: followOutput(direction, element.scrollHeight, element.scrollTop, element.clientHeight),
          seq: first?.dataset.seq,
          offset: first ? first.getBoundingClientRect().top - top : 0,
        }
      }
      setOutput(next)
      setError('')
    })
    async function load(direction) {
      try {
        const more = await loader.load(direction)
        if (more) queue.request()
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message)
      }
    }
    const queue = createRefreshQueue(() => load('live'))
    controls.current = {
      refresh: queue.request,
      async navigate(direction) {
        setLoading(true)
        await load(direction)
        if (!controller.signal.aborted) setLoading(false)
      },
    }
    queue.request(true)
    return () => {
      controls.current = null
      loader.dispose()
      queue.dispose()
      controller.abort()
    }
  }, [agentId])

  useEffect(() => { controls.current?.refresh(true) }, [refreshVersion])

  useLayoutEffect(() => {
    const element = panel.current
    const position = anchor.current
    if (!element || !position) return
    if (position.follow) element.scrollTop = element.scrollHeight
    else {
      const line = [...element.children].find((item) => item.dataset.seq === position.seq)
      if (line) element.scrollTop = anchoredScrollTop(element.scrollTop, line.getBoundingClientRect().top,
        element.getBoundingClientRect().top, position.offset)
    }
    anchor.current = null
  }, [output])

  return (
    <section aria-labelledby="output-heading">
      <h3 id="output-heading">Output</h3>
      <div className="details-actions">
        {output.hasOlder && <button type="button" className="close-button" disabled={loading}
          onClick={() => controls.current?.navigate('older')}>{loading ? 'Loading...' : 'Load older output'}</button>}
        <button type="button" className="close-button" disabled={loading}
          onClick={() => controls.current?.navigate('latest')}>Return to latest output</button>
      </div>
      <p className="agent-type-note">Showing up to {OUTPUT_WINDOW} entries. Complete history remains in SQLite.
        {output.browsing && ' Browsing an older window; return to latest output to follow new lines.'}</p>
      {error && <p className="message error" role="alert">{error}</p>}
      <div ref={panel} className="agent-output" aria-labelledby="output-heading" tabIndex={0}>
        {output.items.length ? output.items.map((item) => <div key={item.seq} data-seq={item.seq}>{item.text || '\u00a0'}</div>) : 'No output yet'}
      </div>
    </section>
  )
}
