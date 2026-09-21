import { useId, useState } from 'react'

export default function StartAgentForm({ parentId = null, onStarted, disabled = false, onStartingChange }) {
  const id = useId()
  const [task, setTask] = useState('')
  const [agentType, setAgentType] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const unavailable = disabled || starting
  const child = parentId !== null

  async function handleStart(event) {
    event.preventDefault()
    if (!task.trim() || unavailable) return
    setStarting(true)
    onStartingChange?.(true)
    setError('')
    try {
      const path = child ? `/agents/${encodeURIComponent(parentId)}/children/start` : '/agents/start'
      const response = await fetch(`http://127.0.0.1:8000${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task: task.trim(), agent_type: agentType, sandbox }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to start agent. Check the backend and try again.')
      }
      setTask('')
      if (child) {
        setAgentType('mock')
        setSandbox('read-only')
      }
      onStarted()
    } catch (err) {
      setError(err.message || 'Unable to start agent. Please try again.')
    } finally {
      setStarting(false)
      onStartingChange?.(false)
    }
  }

  return (
    <section className="start-agent" aria-labelledby={`${id}-heading`}>
      <h2 id={`${id}-heading`}>{child ? 'Start child agent' : 'Start new agent'}</h2>
      <form onSubmit={handleStart}>
        <div className="agent-type-field">
          <label htmlFor={`${id}-type`}>Agent Type</label>
          <select id={`${id}-type`} value={agentType} onChange={(event) => setAgentType(event.target.value)} disabled={unavailable}>
            <option value="mock">Mock</option><option value="codex">Codex</option>
          </select>
        </div>
        {agentType === 'codex' && (
          <div className="agent-type-field">
            <label htmlFor={`${id}-sandbox`}>Sandbox</label>
            <select id={`${id}-sandbox`} value={sandbox} onChange={(event) => setSandbox(event.target.value)}
              disabled={unavailable} aria-describedby={sandbox === 'workspace-write' ? `${id}-warning` : undefined}>
              <option value="read-only">Read only</option><option value="workspace-write">Workspace write</option>
            </select>
            {sandbox === 'workspace-write' && (
              <p id={`${id}-warning`} className="agent-type-note" role="status">Codex can modify files in this project.</p>
            )}
          </div>
        )}
        <label htmlFor={`${id}-task`}>Task</label>
        <div className="start-controls">
          <input id={`${id}-task`} type="text" value={task} onChange={(event) => setTask(event.target.value)}
            placeholder="Refactor authentication module" required disabled={unavailable} />
          <button className="start-button" type="submit" disabled={unavailable || !task.trim()}>
            {starting ? 'Starting...' : child ? 'Start child agent' : 'Start Agent'}
          </button>
        </div>
      </form>
      {error && <p className="message error" role="alert">{error}</p>}
    </section>
  )
}
