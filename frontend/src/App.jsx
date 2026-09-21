import { useEffect, useState } from 'react'
import AgentDetails from './AgentDetails'
import StartAgentForm from './StartAgentForm'
import AgentTree from './AgentTree'
import useAgentNotifications from './useAgentNotifications'
import './App.css'

function App() {
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedAgentId, setSelectedAgentId] = useState(null)
  const [listVersion, setListVersion] = useState(0)
  const notifications = useAgentNotifications(setSelectedAgentId)
  const { observeAgents } = notifications

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
          observeAgents(data.agents)
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
  }, [listVersion, observeAgents])

  return (
    <main className="dashboard">
      <header>
        <h1>AI Agent Control Center</h1>
        <p className="subtitle">Agents refresh automatically every 2 seconds.</p>
      </header>
      <section className="notification-controls" aria-label="Browser notifications">
        <button className="close-button" type="button"
          onClick={notifications.toggleNotifications}
          disabled={!notifications.supported || notifications.requesting}>
          {notifications.requesting ? 'Requesting permission...'
            : notifications.enabled ? 'Disable notifications' : 'Enable notifications'}
        </button>
        <div role="status">
          <p>{notifications.status}</p>
          {notifications.message && <p>{notifications.message}</p>}
        </div>
      </section>
      <StartAgentForm onStarted={() => setListVersion((version) => version + 1)} />
      {error && <p className="message error" role="alert">{error}</p>}
      {loading && <p className="message" role="status">Loading agents...</p>}
      {!loading && !error && agents.length === 0 && (
        <p className="message">No agents yet</p>
      )}
      <section aria-label="Agents">
        <AgentTree agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId} />
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
