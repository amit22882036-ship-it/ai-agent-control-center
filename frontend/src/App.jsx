import { useEffect, useRef, useState } from 'react'
import AgentDetails from './AgentDetails'
import StartAgentForm from './StartAgentForm'
import AgentList from './AgentList'
import useAgentNotifications from './useAgentNotifications'
import { connectAgentEvents, createRefreshQueue } from './agentEventStream.mjs'
import WorkspaceBar from './WorkspaceBar'
import AttentionPanel from './AttentionPanel'
import ProvidersView from './ProvidersView'
import useProviderNotices from './useProviderNotices'
import ResizeHandle from './ResizeHandle'
import { readLayout, layoutKey } from './panelLayout.mjs'
import './App.css'
import './hierarchy.css'

function App() {
  const [layout, setLayout] = useState(() => readLayout())
  useEffect(() => {
    try { localStorage.setItem(layoutKey, JSON.stringify(layout)) } catch { /* Resizing still works for this session. */ }
  }, [layout])
  function resize(side, width) { setLayout((current) => ({ ...current, [side]: width })) }
  const [search, setSearch] = useState('')
  const [showStart, setShowStart] = useState(false)
  // The AI Providers view replaces the workspace in place; the New Agent composer stays
  // mounted (hidden) so its draft survives a visit to set up a provider.
  const [view, setView] = useState('workspace')
  const [rootStarting, setRootStarting] = useState(false)
  const [agents, setAgents] = useState([])
  const [overview, setOverview] = useState(null)
  const [hasMore, setHasMore] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const pagesRef = useRef(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedAgentId, setSelectedAgentId] = useState(null)
  const [listVersion, setListVersion] = useState(0)
  const [detailsRefresh, setDetailsRefresh] = useState(0)
  const [transport, setTransport] = useState('polling')
  const refreshRef = useRef(null)
  const newAgentButtonRef = useRef(null)
  const notifications = useAgentNotifications(setSelectedAgentId)
  const providerNotices = useProviderNotices(detailsRefresh)
  const { observeAgents } = notifications

  useEffect(() => {
    const controller = new AbortController()

    async function fetchAgents() {
      setRefreshing(true)
      try {
        let after = 0, data
        const collected = []
        for (let page = 0; page < pagesRef.current; page++) {
          const response = await fetch(`http://127.0.0.1:8000/workspace?limit=500&after=${after}`, { signal: controller.signal })
          if (!response.ok) throw new Error('Unable to fetch agents')
          data = await response.json()
          collected.push(...data.agents)
          after = data.next_after
          if (after === null) break
        }
        if (!controller.signal.aborted) {
          setAgents(collected)
          setOverview(data.overview)
          setHasMore(after !== null)
          setError('')
          observeAgents(collected)
        }
      } catch {
        if (!controller.signal.aborted) {
          setError('Backend unavailable. Unable to load agents; retrying automatically.')
        }
        return false
      } finally {
        if (!controller.signal.aborted) { setLoading(false); setRefreshing(false) }
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

  function openView(next) {
    setView(next)
    window.scrollTo?.({ top: 0 })
  }

  function newAgentPressed() {
    if (view !== 'workspace') {
      openView('workspace')
      setShowStart(true)
    } else if (showStart) closeStartComposer()
    else setShowStart(true)
  }

  function loadMoreAgents() {
    if (refreshing) return
    pagesRef.current++
    refreshRef.current?.()
  }

  return (
    <div className={`app-shell${selectedAgentId !== null ? ' has-details' : ''}`} style={{ '--details-width': `${layout.right}px` }}>
      <WorkspaceBar agents={agents} search={search} onSearch={setSearch} notifications={notifications}
        showStart={showStart && view === 'workspace'}
        onStart={newAgentPressed}
        newAgentButtonRef={newAgentButtonRef} startingAgent={rootStarting}
        view={view} onView={openView} providerNotices={providerNotices} />
      {view === 'providers' && <main id="providers" className="dashboard">
        <ProvidersView refreshVersion={detailsRefresh} onClose={() => openView('workspace')} />
      </main>}
      <main id="workspace" className="dashboard" hidden={view !== 'workspace'}>
        <header className="workspace-heading"><div><p className="eyebrow">YOUR AGENTS, IN FOCUS</p><h1>Agent Workspace</h1><p className="subtitle">Monitor, collaborate, and guide your AI agents.</p></div>
          <span className={`connection-state ${transport}`} role="status">{transport === 'live' ? 'Live' : 'Polling fallback'}</span>
        </header>
      {showStart && <StartAgentForm sectionId="new-agent-composer" onCancel={closeStartComposer}
        onStartingChange={setRootStarting} onStarted={rootAgentStarted} onOpenProviders={() => openView('providers')}
        visible={view === 'workspace'} />}
      {overview && <div className="workspace-overview" aria-label="Recorded workspace totals">
        {Object.entries({ active_agents: 'Active agents', completed_tasks: 'Completed tasks', waiting_tasks: 'Waiting tasks', needs_input: 'Need your input', pending_responsibilities: 'Pending responsibilities' }).map(([key, label]) => <div key={key}><strong>{overview[key]}</strong><span>{label}</span></div>)}
      </div>}
      <AttentionPanel agents={agents} onSelect={setSelectedAgentId} />
      {error && <p className="message error" role="alert">{error}</p>}
      {loading && <p className="message" role="status">Loading agents...</p>}
      <AgentList agents={agents} selectedAgentId={selectedAgentId} onSelect={setSelectedAgentId}
        loading={loading} unavailable={Boolean(error)} search={search} onStart={() => setShowStart(true)} />
      {hasMore && <button disabled={refreshing} onClick={loadMoreAgents}>Load more agents (showing {agents.length})</button>}
      </main>
      {selectedAgentId !== null && (
        <div className="details-dock">
        <ResizeHandle side="right" width={layout.right} onChange={(width) => resize('right', width)} />
        <AgentDetails
          key={selectedAgentId}
          agentId={selectedAgentId}
          agents={agents}
          workspace={{ agents, hasMore, loading, refreshing, error, onLoadMore: loadMoreAgents }}
          onSelect={setSelectedAgentId}
          refreshVersion={detailsRefresh}
          onClose={() => setSelectedAgentId(null)}
          onStopped={() => setListVersion((version) => version + 1)}
          onOpenProviders={() => openView('providers')}
        />
        </div>
      )}
    </div>
  )
}

export default App
