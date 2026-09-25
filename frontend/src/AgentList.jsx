import { statusLabels } from './agentPresentation.mjs'
import { useMemo, useState } from 'react'
import AgentTree from './AgentTree'
import AgentColorFilter from './AgentColorFilter'
import Icon from './Icon'
import Popover from './Popover'
import { buildAgentTree } from './buildAgentTree.mjs'
import { shouldExpandFilteredPaths, viewFilters, countStatuses, filterAgentTree, parentIds, effectiveCollapsedIds, visibleAgentIds, listEmptyMessage } from './agentListView.mjs'

export default function AgentList({ agents, selectedAgentId, onSelect, loading, unavailable, search, onStart }) {
  const [status, setStatus] = useState('active')
  const [selectedColors, setSelectedColors] = useState(() => new Set())
  const [collapsed, setCollapsed] = useState(() => new Set())
  const tree = useMemo(() => buildAgentTree(agents), [agents])
  const counts = useMemo(() => countStatuses(agents), [agents])
  const parents = useMemo(() => parentIds(tree), [tree])
  const filtered = useMemo(() => filterAgentTree(tree, search, status, selectedColors), [tree, search, status, selectedColors])
  const filtering = shouldExpandFilteredPaths(search, status, selectedColors)
  const effectiveCollapsed = useMemo(() => effectiveCollapsedIds(filtered, collapsed, filtering), [filtered, collapsed, filtering])
  const visible = useMemo(() => visibleAgentIds(filtered, effectiveCollapsed), [filtered, effectiveCollapsed])
  const emptyMessage = listEmptyMessage(agents, filtered, search, status, selectedColors)

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
      <div className="team-heading">
        <div><p className="eyebrow">WORKSPACE</p><h2>{status === 'active' ? 'Your agent team' : status === 'history' ? 'Past work' : 'Agents'}</h2></div>
        <div className="team-tools">
          <AgentColorFilter selectedColors={selectedColors} onChange={setSelectedColors} />
          <Popover label="Filter agents" trigger={<><Icon name="filter" /><span>{({ active: 'Active', history: 'History', all: 'All agents', ...statusLabels })[status]}</span><Icon name="down" size={14} /></>}>
            <fieldset className="choice-group"><legend>Show agents</legend>
              {viewFilters.map((value) => <label className="choice" key={value}>
                <span>{({ active: 'Active', history: 'History', all: 'All agents', ...statusLabels })[value]}</span>
                <span className="choice-count">{value === 'active' ? counts.running + counts.waiting : value === 'history' ? counts.finished + counts.stopped : counts[value]}</span>
                <input type="radio" name="agent-view" value={value} checked={status === value} onChange={() => setStatus(value)} />
              </label>)}
            </fieldset>
          </Popover>
          <Popover label="Hierarchy options" trigger={<Icon name="more" />}>
            <button type="button" disabled={filtering || !parents.length} onClick={() => setCollapsed(new Set(parents))}>Collapse all</button>
            <button type="button" disabled={filtering || !collapsed.size} onClick={() => setCollapsed(new Set())}>Expand all</button>
            {filtering && <p className="agent-type-note">Clear search and color filters, and select Active or All, to restore collapse choices.</p>}
          </Popover>
        </div>
      </div>
      {selectedAgentId !== null && !visible.has(selectedAgentId) && (
        <p className="agent-type-note" role="status">Selected agent is outside this view. Details remain open.</p>
      )}
      {!loading && !unavailable && emptyMessage && <div className="workspace-empty" role="status"><span className="empty-symbol"><Icon name="agent" size={28} /></span><h3>{agents.length ? 'Room for what comes next' : 'Your next idea starts here'}</h3><p>{emptyMessage}</p><button className="start-button" onClick={onStart}><Icon name="plus" />New agent</button></div>}
      {unavailable && agents.length > 0 && <p className="agent-type-note">Showing the last received agents.</p>}
      <AgentTree nodes={filtered} collapsed={effectiveCollapsed} onToggle={filtering ? undefined : toggleBranch}
        selectedAgentId={selectedAgentId} onSelect={onSelect} />
      {(counts.finished + counts.stopped > 0) && <div className="history-access"><Icon name="history" /><div><strong>Past work</strong><span>{counts.finished + counts.stopped} agents · assignments and output retained</span></div><button onClick={() => setStatus(status === 'history' ? 'active' : 'history')}>{status === 'history' ? 'Back to active' : 'View history'}<Icon name="arrow" size={16} /></button></div>}
    </section>
  )
}
