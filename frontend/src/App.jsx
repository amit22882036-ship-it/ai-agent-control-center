import { useEffect, useRef, useState } from 'react'
import AgentDetails from './AgentDetails'
import StartAgentForm from './StartAgentForm'
import AgentList from './AgentList'
import useAgentNotifications from './useAgentNotifications'
import { connectAgentEvents, createRefreshQueue } from './agentEventStream.mjs'
import WorkspaceBar from './WorkspaceBar'
import AttentionPanel from './AttentionPanel'
import ResizeHandle from './ResizeHandle'
import { readLayout, layoutKey } from './panelLayout.mjs'
import './App.css'

function App() {
  const [layout, setLayout] = useState(() => readLayout())
  useEffect(() => {
    try { localStorage.setItem(layoutKey, JSON.stringify(layout)) } catch { /* Resizing still works for this session. */ }
  }, [layout])
  function resize(side, width) { setLayout((current) => ({ ...current, [side]: width })) }
  const [search, setSearch] = useState('')
  const [showStart, setShowStart] = useState(false)
  const [rootStarting, setRootStarting] = useState(false)
  const [agents, setAgents] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedAgentId, setSelectedAgentId] = useState(null)
  const [listVersion, setListVersion] = useState(0)
  const [detailsRefresh, setDetailsRefresh] = useState(0)
  const [transport, setTransport] = useState('polling')
  const refreshRef = useRef(null)
  const newAgentButtonRef = useRef(null)
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

  function restoreNewAgentFocus() {
    requestAnimationFrame(() => newAgentButtonRef.current?.focus())
  }

  function closeStartComposer() {
    if (rootStarting) return
    setShowStart(false)
    restoreNewAgentFocus()
  }

  function rootAgentStarted() {
    setListVersion((version) => version + 1)
    setShowStart(false)
    restoreNewAgentFocus()
  }

  return (
    <div className={`app-shell${selectedAgentId !== null ? ' has-details' : ''}`} style={{ '--details-width': `${layout.right}px` }}>
      <WorkspaceBar agents={agents} search={search} onSearch={setSearch} notifications={notifications}
        showStart={showStart} onStart={() => showStart ? closeStartComposer() : setShowStart(true)}
        newAgentButtonRef={newAgentButtonRef} startingAgent={rootStarting} />
      <main id="workspace" className="dashboard">
        <header className="workspace-heading"><div><p className="eyebrow">YOUR AGENTS, IN FOCUS</p><h1>Agent Workspace</h1><p className="subtitle">Monitor, collaborate, and guide your AI agents.</p></div>
          <span className={`connection-state ${transport}`} role="status">{transport === 'live' ? 'Live' : 'Polling fallback'}</span>
        </header>
      {showStart && <StartAgentForm sectionId="new-agent-composer" onCancel={closeStartComposer}
        onStartingChange={setRootStarting} onStarted={rootAgentStarted} />}
      <AttentionPanel agents={agents} onSelect={setSelectedAgentId} />
      {error && <p className="message error" role="alert">{error}</p>}
      {loading && <p className="message" role="status">Loading agents...</p>}
      <AgentList agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId}
        loading={loading} unavailable={Boolean(error)} search={search} onStart={() => setShowStart(true)} />
      </main>
      {selectedAgentId !== null && (
        <div className="details-dock">
        <ResizeHandle side="right" width={layout.right} onChange={(width) => resize('right', width)} />
        <AgentDetails
          key={selectedAgentId}
          agentId={selectedAgentId}
          agents={agents}
          onSelect={setSelectedAgentId}
          refreshVersion={detailsRefresh}
          onClose={() => setSelectedAgentId(null)}
          onStopped={() => setListVersion((version) => version + 1)}
        />
        </div>
      )}
    </div>
  )
}

export default App
