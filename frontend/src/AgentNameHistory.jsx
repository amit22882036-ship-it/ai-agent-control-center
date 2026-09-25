import { useEffect, useState } from 'react'
import { nameHistoryTime } from './agentIdentity.mjs'

export default function AgentNameHistory({ agentId, refreshVersion }) {
  const [open, setOpen] = useState(false)
  const [history, setHistory] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    async function load() {
      setLoading(true)
      setError('')
      try {
        const response = await fetch(`http://127.0.0.1:8000/agents/${encodeURIComponent(agentId)}/name-history`, { signal: controller.signal })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail || 'Unable to load name history.')
        if (!controller.signal.aborted) setHistory(data.history)
      } catch (err) {
        if (!controller.signal.aborted) setError(err.message)
      } finally { if (!controller.signal.aborted) setLoading(false) }
    }
    load()
    return () => controller.abort()
  }, [agentId, open, refreshVersion])
  return <details className="detail-section" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>Name history</summary>
    {loading && <p role="status" className="agent-type-note">Loading names...</p>}
    {error && <p role="alert" className="error">{error} Close and reopen to retry.</p>}
    {!loading && !error && !history.length && <p className="agent-type-note">No name history yet.</p>}
    {!error && <ol className="name-history">{history.map((entry, index) => <li key={index}>
      <span>{entry.name}</span><time dateTime={entry.changed_at}>{nameHistoryTime(entry.changed_at)}</time>
    </li>)}</ol>}
  </details>
}
