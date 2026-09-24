import { useEffect, useRef, useState } from 'react'
import AgentDetails from './AgentDetails'
import StartAgentForm from './StartAgentForm'
import AgentList from './AgentList'
import useAgentNotifications from './useAgentNotifications'
import { connectAgentEvents, createRefreshQueue } from './agentEventStream.mjs'
import './App.css'

function App() {
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedAgentId, setSelectedAgentId] = useState(null)
  const [listVersion, setListVersion] = useState(0)
  const [detailsRefresh, setDetailsRefresh] = useState(0)
  const [transport, setTransport] = useState('polling')
  const refreshRef = useRef(null)
  const notifications = useAgentNotifications(setSelectedAgentId)
  const { observeAgents } = notifications

  useEffect(() => {
    const controller = new AbortController()

    async function fetchAgents() {
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
          setError('Backend unavailable. Unable to load agents; retrying automatically.')
        }
        return false
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }

    const queue = createRefreshQueue(fetchAgents)
    const refresh = () => {
      queue.request(true)
      setDetailsRefresh((version) => version + 1)
    }
    refreshRef.current = refresh
    const disconnect = connectAgentEvents({ refresh, onStatus: setTransport })
    return () => {
      refreshRef.current = null
      disconnect()
      queue.dispose()
      controller.abort()
    }
  }, [observeAgents])

  useEffect(() => {
    if (listVersion > 0) refreshRef.current?.()
  }, [listVersion])

  return (
    <main className="dashboard">
      <header>
        <h1>AI Agent Control Center</h1>
        <p className="subtitle" role="status">{transport === 'live' ? 'Live updates' : 'Polling fallback'}</p>
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
      <AgentList agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId}
        loading={loading} unavailable={Boolean(error)} />
      {selectedAgentId !== null && (
        <AgentDetails
          key={selectedAgentId}
          agentId={selectedAgentId}
          refreshVersion={detailsRefresh}
          onClose={() => setSelectedAgentId(null)}
          onStopped={() => setListVersion((version) => version + 1)}
        />
      )}
    </main>
  )
}

export default App
