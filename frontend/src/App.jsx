import { useEffect, useState } from 'react'
import AgentDetails from './AgentDetails'
import './App.css'

function App() {
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedAgentId, setSelectedAgentId] = useState(null)
  const [task, setTask] = useState('')
  const [agentType, setAgentType] = useState('mock')
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState('')
  const [listVersion, setListVersion] = useState(0)

  async function handleStart(event) {
    event.preventDefault()
    const trimmedTask = task.trim()
    if (!trimmedTask || starting) return

    setStarting(true)
    setStartError('')
    try {
      const response = await fetch('http://127.0.0.1:8000/agents/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task: trimmedTask, agent_type: agentType }),
      })
      if (!response.ok) throw new Error('Unable to start agent')
      setTask('')
      setListVersion((version) => version + 1)
    } catch {
      setStartError('Unable to start agent. Check the backend and try again.')
    } finally {
      setStarting(false)
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    let fetching = false

    async function fetchAgents() {
      if (fetching) return
      fetching = true
      try {
        const response = await fetch('http://127.0.0.1:8000/agents', {
          signal: controller.signal,
        })
        if (!response.ok) throw new Error('Unable to fetch agents')
        const data = await response.json()
        if (!controller.signal.aborted) {
          setAgents(data.agents)
          setError('')
        }
      } catch {
        if (!controller.signal.aborted) {
          setError('Unable to load agents. Check that the backend is running.')
        }
      } finally {
        fetching = false
        if (!controller.signal.aborted) setLoading(false)
      }
    }

    fetchAgents()
    const interval = setInterval(fetchAgents, 2000)
    return () => {
      clearInterval(interval)
      controller.abort()
    }
  }, [listVersion])

  return (
    <main className="dashboard">
      <header>
        <h1>AI Agent Control Center</h1>
        <p className="subtitle">Agents refresh automatically every 2 seconds.</p>
      </header>
      <section className="start-agent" aria-labelledby="start-heading">
        <h2 id="start-heading">Start new agent</h2>
        <form onSubmit={handleStart}>
          <div className="agent-type-field">
            <label htmlFor="agent-type">Agent Type</label>
            <select
              id="agent-type"
              value={agentType}
              onChange={(event) => setAgentType(event.target.value)}
              disabled={starting}
              aria-describedby={agentType === 'codex' ? 'codex-note' : undefined}
            >
              <option value="mock">Mock</option>
              <option value="codex">Codex</option>
            </select>
            {agentType === 'codex' && (
              <p id="codex-note" className="agent-type-note">Codex currently runs in read-only mode.</p>
            )}
          </div>
          <label htmlFor="agent-task">Task</label>
          <div className="start-controls">
            <input
              id="agent-task"
              type="text"
              value={task}
              onChange={(event) => setTask(event.target.value)}
              placeholder="Refactor authentication module"
              required
              disabled={starting}
            />
            <button className="start-button" type="submit" disabled={starting || !task.trim()}>
              {starting ? 'Starting...' : 'Start Agent'}
            </button>
          </div>
        </form>
        {startError && <p className="message error" role="alert">{startError}</p>}
      </section>
      {error && <p className="message error" role="alert">{error}</p>}
      {loading && <p className="message" role="status">Loading agents...</p>}
      {!loading && !error && agents.length === 0 && (
        <p className="message">No agents yet</p>
      )}
      <section className="agent-grid" aria-label="Agents">
        {agents.map((agent) => (
          <article
            className={`agent-card${selectedAgentId === agent.agent_id ? ' selected' : ''}`}
            key={agent.agent_id}
          >
            <h2>
              <button
                className="agent-select"
                type="button"
                aria-pressed={selectedAgentId === agent.agent_id}
                onClick={() => setSelectedAgentId(agent.agent_id)}
              >
                Agent {agent.agent_id}
              </button>
            </h2>
            <dl>
              <dt>Type</dt>
              <dd>{agent.agent_type === 'codex' ? 'Codex' : 'Mock'}</dd>
              <dt>Task</dt>
              <dd className="task">{agent.task}</dd>
              <dt>Status</dt>
              <dd><span className={`status status-${agent.status}`}>{agent.status}</span></dd>
            </dl>
          </article>
        ))}
      </section>
      {selectedAgentId !== null && (
        <AgentDetails
          key={selectedAgentId}
          agentId={selectedAgentId}
          onClose={() => setSelectedAgentId(null)}
          onStopped={() => setListVersion((version) => version + 1)}
        />
      )}
    </main>
  )
}

export default App
