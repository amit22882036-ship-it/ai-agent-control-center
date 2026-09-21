import { useMemo, useState } from 'react'
import AgentTree from './AgentTree'
import { buildAgentTree } from './buildAgentTree.mjs'
import { statuses, countStatuses, filterAgentTree, parentIds, effectiveCollapsedIds, visibleAgentIds, listEmptyMessage } from './agentListView.mjs'

export default function AgentList({ agents, selectedAgentId, onSelect, loading, unavailable }) {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const tree = useMemo(() => buildAgentTree(agents), [agents])
  const counts = useMemo(() => countStatuses(agents), [agents])
  const parents = useMemo(() => parentIds(tree), [tree])
  const filtered = useMemo(() => filterAgentTree(tree, search, status), [tree, search, status])
  const filtering = Boolean(search.trim()) || status !== 'all'
  const effectiveCollapsed = useMemo(() => effectiveCollapsedIds(filtered, collapsed, filtering), [filtered, collapsed, filtering])
  const visible = useMemo(() => visibleAgentIds(filtered, effectiveCollapsed), [filtered, effectiveCollapsed])
  const emptyMessage = listEmptyMessage(agents, filtered, search, status)

  function toggleBranch(id) {
    setCollapsed((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <section aria-label="Agents">
      <div className="list-controls">
        <label htmlFor="agent-search">Search agents</label>
        <input id="agent-search" type="search" value={search} onChange={(event) => setSearch(event.target.value)}
          placeholder="Task, ID, type, or status" />
        <div className="list-buttons" role="group" aria-label="Filter by status">
          {statuses.map((value) => (
            <button key={value} type="button" className="close-button" aria-pressed={status === value}
              onClick={() => setStatus(value)}>
              {value[0].toUpperCase() + value.slice(1)}: {counts[value]}
            </button>
          ))}
        </div>
        <div className="list-buttons">
          <button type="button" className="close-button" disabled={filtering || !parents.length}
            onClick={() => setCollapsed(new Set(parents))}>Collapse all</button>
          <button type="button" className="close-button" disabled={filtering || !collapsed.size}
            onClick={() => setCollapsed(new Set())}>Expand all</button>
        </div>
        {filtering && <p className="agent-type-note">Matching paths are temporarily expanded. Clear search and select All to restore your collapsed branches.</p>}
      </div>
      {selectedAgentId !== null && !visible.has(selectedAgentId) && (
        <p className="agent-type-note" role="status">The selected agent is hidden by the search, status filter, or a collapsed branch. Its details remain open below.</p>
      )}
      {!loading && !unavailable && emptyMessage && <p className="message" role="status">{emptyMessage}</p>}
      {unavailable && agents.length > 0 && <p className="agent-type-note">Showing the last received agents.</p>}
      <AgentTree nodes={filtered} collapsed={effectiveCollapsed} onToggle={filtering ? undefined : toggleBranch}
        selectedAgentId={selectedAgentId} onSelect={onSelect} />
    </section>
  )
}
