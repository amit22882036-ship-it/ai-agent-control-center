import { statusLabels } from './agentPresentation.mjs'
import { useMemo, useState } from 'react'
import AgentTree from './AgentTree'
import AgentColorFilter from './AgentColorFilter'
import Icon from './Icon'
import Popover from './Popover'
import { buildAgentTree } from './buildAgentTree.mjs'
import { shouldExpandFilteredPaths, viewFilters, countStatuses, filterAgentTree, parentIds, effectiveCollapsedIds, visibleAgentIds, listEmptyMessage } from './agentListView.mjs'
import { withoutRemoved, fullyRemovedBranches, effectiveRemovalCollapse } from './agentRemoval.mjs'

const showRemovedKey = 'agent-control-center.show-removed'
function readShowRemoved() {
  try { return localStorage.getItem(showRemovedKey) !== 'false' } catch { return true }
}

export default function AgentList({ agents, selectedAgentId, onSelect, loading, unavailable, search, onStart }) {
  const [status, setStatus] = useState('active')
  const [selectedColors, setSelectedColors] = useState(() => new Set())
  const [collapsed, setCollapsed] = useState(() => new Set())
  // Removed Agents stay visible by default; fully removed branches start collapsed.
  const [showRemoved, setShowRemoved] = useState(readShowRemoved)
  const [expandedHistory, setExpandedHistory] = useState(() => new Set())
  const fullTree = useMemo(() => buildAgentTree(agents), [agents])
  const tree = useMemo(() => showRemoved ? fullTree : withoutRemoved(fullTree), [fullTree, showRemoved])
  const removedCount = useMemo(() => agents.filter((agent) => agent.removed).length, [agents])
  const autoCollapsed = useMemo(() => new Set(fullyRemovedBranches(tree)), [tree])
  const counts = useMemo(() => countStatuses(agents), [agents])
  const parents = useMemo(() => parentIds(tree), [tree])
  const filtered = useMemo(() => filterAgentTree(tree, search, status, selectedColors), [tree, search, status, selectedColors])
  const filtering = shouldExpandFilteredPaths(search, status, selectedColors)
  const historyCollapsed = useMemo(() => effectiveRemovalCollapse(collapsed, autoCollapsed, expandedHistory), [collapsed, autoCollapsed, expandedHistory])
  const effectiveCollapsed = useMemo(() => effectiveCollapsedIds(filtered, historyCollapsed, filtering), [filtered, historyCollapsed, filtering])
  const visible = useMemo(() => visibleAgentIds(filtered, effectiveCollapsed), [filtered, effectiveCollapsed])
  const emptyMessage = listEmptyMessage(agents, filtered, search, status, selectedColors)

  function changeShowRemoved(value) {
    setShowRemoved(value)
    try { localStorage.setItem(showRemovedKey, String(value)) } catch { /* The choice still applies to this session. */ }
  }

  function toggleBranch(id) {
    if (autoCollapsed.has(id)) {
      setExpandedHistory((current) => {
        const next = new Set(current)
        if (next.has(id)) next.delete(id)
        else next.add(id)
        return next
      })
      return
    }
    setCollapsed((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function revealSelected() {
    // An explicit navigation action may expand this path; filtering never mutates it.
    const parentsById = new Map(agents.map((agent) => [agent.agent_id, agent.parent_id]))
    setCollapsed((current) => {
      const next = new Set(current), seen = new Set()
      let id = parentsById.get(selectedAgentId)
      while (id && !seen.has(id)) { seen.add(id); next.delete(id); id = parentsById.get(id) }
      return next
    })
  }

  return (
    <section aria-label="Agents">
      <div className="team-heading">
        <div><p className="eyebrow">WORKSPACE</p><h2>{status === 'active' ? 'Your agent team' : status === 'history' ? 'Past work' : 'Agents'}</h2></div>
        <div className="team-tools">
          <AgentColorFilter selectedColors={selectedColors} onChange={setSelectedColors} />
          <Popover label="Filter agents" trigger={<><Icon name="filter" /><span>{({ active: 'Active', history: 'History', all: 'All agents', ...statusLabels })[status]}</span><Icon name="down" size={14} /></>}>
            <fieldset className="choice-group"><legend>Show agents</legend>
              {viewFilters.map((value) => <label className="choice" key={value}>
                <span>{({ active: 'Active', history: 'History', all: 'All agents', ...statusLabels })[value]}</span>
                <span className="choice-count">{counts[value]}</span>
                <input type="radio" name="agent-view" value={value} checked={status === value} onChange={() => setStatus(value)} />
              </label>)}
            </fieldset>
          </Popover>
          <Popover label="Hierarchy options" trigger={<Icon name="more" />}>
            <button type="button" disabled={filtering || !parents.length} onClick={() => setCollapsed(new Set(parents))}>Collapse all</button>
            <button type="button" disabled={filtering || (!collapsed.size && !autoCollapsed.size)} onClick={() => { setCollapsed(new Set()); setExpandedHistory(new Set(autoCollapsed)) }}>Expand all</button>
            {filtering && <p className="agent-type-note">Clear search and color filters, and select Active or All, to restore collapse choices.</p>}
            <fieldset className="choice-group removal-visibility"><legend>Removed agents{removedCount ? ` (${removedCount})` : ''}</legend>
              <label className="choice"><span>Show removed agents</span><input type="checkbox" checked={showRemoved} onChange={(event) => changeShowRemoved(event.target.checked)} /></label>
              <button type="button" disabled={!autoCollapsed.size} onClick={() => setExpandedHistory(new Set())}>Collapse removed branches</button>
              <button type="button" disabled={!autoCollapsed.size || [...autoCollapsed].every((id) => expandedHistory.has(id))} onClick={() => setExpandedHistory(new Set(autoCollapsed))}>Expand removed branches</button>
              {!showRemoved && <p className="agent-type-note">Agents that continue under a hidden removed parent stay listed and are marked.</p>}
            </fieldset>
          </Popover>
        </div>
      </div>
      {selectedAgentId !== null && !visible.has(selectedAgentId) && (
        <p className="agent-type-note" role="status">Selected agent is outside this view. Details remain open. {!filtering && visibleAgentIds(filtered, new Set()).has(selectedAgentId) && <button onClick={revealSelected}>Reveal selected branch</button>}</p>
      )}
      {!loading && !unavailable && emptyMessage && <div className="workspace-empty" role="status"><span className="empty-symbol"><Icon name="agent" size={28} /></span><h3>{agents.length ? 'Room for what comes next' : 'Your next idea starts here'}</h3><p>{emptyMessage}</p><button className="start-button" onClick={onStart}><Icon name="plus" />New agent</button></div>}
      {unavailable && agents.length > 0 && <p className="agent-type-note">Showing the last received agents.</p>}
      <AgentTree nodes={filtered} collapsed={effectiveCollapsed} onToggle={filtering ? undefined : toggleBranch}
        selectedAgentId={selectedAgentId} onSelect={onSelect} />
      {(counts.history > 0) && <div className="history-access"><Icon name="history" /><div><strong>Past work</strong><span>{counts.history} agents · assignments and output retained</span></div><button onClick={() => setStatus(status === 'history' ? 'active' : 'history')}>{status === 'history' ? 'Back to active' : 'View history'}<Icon name="arrow" size={16} /></button></div>}
    </section>
  )
}
