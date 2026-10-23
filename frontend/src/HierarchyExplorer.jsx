import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import AgentHierarchy from './AgentHierarchy'
import AgentTree from './AgentTree'
import FocusedAgentCard from './FocusedAgentCard'
import HierarchyNavigator from './HierarchyNavigator'
import useReadProjection from './useReadProjection'
import { familyContext, revealPath } from './hierarchyContext.mjs'
import { filterAgentTree, effectiveCollapsedIds, parentIds, visibleAgentIds } from './agentListView.mjs'
import { withoutRemoved, fullyRemovedBranches, effectiveRemovalCollapse } from './agentRemoval.mjs'
import Icon from './Icon'

export default function HierarchyExplorer({ initialAgent, refreshVersion, onClose, onFullDetails }) {
  const dialog = useRef(null)
  const [focusedId, setFocusedId] = useState(initialAgent.agent_id)
  const treeId = initialAgent.agent_id
  const [search, setSearch] = useState('')
  const [view, setView] = useState('graph')
  const [pane, setPane] = useState('graph')
  const [collapsed, setCollapsed] = useState(() => new Set())
  const projection = useReadProjection(`/agents/${encodeURIComponent(treeId)}/hierarchy`, refreshVersion)
  const [showRemoved, setShowRemoved] = useState(true)
  const [expandedHistory, setExpandedHistory] = useState(() => new Set())
  const family = useMemo(() => familyContext(projection.data?.agents || [], treeId), [projection.data, treeId])
  // Hiding removed Agents lifts their remaining descendants; it never invents a parent.
  const shownTree = useMemo(() => showRemoved ? family.tree : withoutRemoved(family.tree), [family.tree, showRemoved])
  const autoCollapsed = useMemo(() => new Set(fullyRemovedBranches(shownTree)), [shownTree])
  const filtered = useMemo(() => filterAgentTree(shownTree, search, 'all'), [shownTree, search])
  const historyCollapsed = useMemo(() => effectiveRemovalCollapse(collapsed, autoCollapsed, expandedHistory), [collapsed, autoCollapsed, expandedHistory])
  const effective = useMemo(() => effectiveCollapsedIds(filtered, historyCollapsed, Boolean(search.trim())), [filtered, historyCollapsed, search])
  const visible = visibleAgentIds(filtered, effective)
  // Keep the opening identity available while its family loads.
  const focused = family.indexed.get(focusedId)?.agent || (focusedId === initialAgent.agent_id ? initialAgent : null)
  const entry = family.indexed.get(focusedId)
  const parent = [...family.indexed.values()].find((node) => node.children.some((child) => child.agent.agent_id === focusedId))?.agent
  useEffect(() => {
    const surface = dialog.current, previous = document.activeElement
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    surface.showModal()
    return () => { surface.close(); document.body.style.overflow = overflow; if (previous?.isConnected) previous.focus({ preventScroll: true }) }
  }, [])
  function select(id) {
    if (!family.indexed.has(id)) return
    setFocusedId(id)
    setCollapsed((current) => revealPath(family.indexed, id, current))
    setPane('graph')
  }
  function toggle(id) {
    const flip = (current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next }
    if (autoCollapsed.has(id)) setExpandedHistory(flip)
    else setCollapsed(flip)
  }
  return createPortal(<dialog ref={dialog} className="hierarchy-explorer" data-pane={pane} aria-labelledby="explorer-heading"
    onCancel={(event) => { event.preventDefault(); event.stopPropagation(); onClose() }} onKeyDown={(event) => { if (event.key === 'Escape') event.stopPropagation() }}>
    <header className="explorer-heading"><div><p className="eyebrow">AGENT CONNECTIONS</p><h2 id="explorer-heading">Hierarchy Explorer</h2></div><button onClick={onClose} aria-label="Back to workspace"><Icon name="close" />Back to workspace</button></header>
    <div className="explorer-mobile-tabs" role="group" aria-label="Explorer panes">{['agents', 'graph', 'details'].map((name) => <button key={name} aria-pressed={pane === name} onClick={() => setPane(name)}>{name === 'agents' ? 'Agents' : name === 'graph' ? 'Hierarchy' : 'Details'}</button>)}</div>
    <div className="explorer-body">
      <HierarchyNavigator tree={filtered} collapsed={effective} count={family.indexed.size} partial={Boolean(projection.data?.truncated || projection.data?.incomplete_ancestry || family.malformed)} loading={!projection.data && !projection.error} error={projection.error} selectedId={focusedId} onSelect={select} onToggle={search.trim() ? undefined : toggle} search={search} onSearch={setSearch} onReveal={() => { setSearch(''); select(focusedId) }} />
      <section className="explorer-canvas" aria-label="Selected agent hierarchy">
        <div className="explorer-tree-heading"><h3>Agent hierarchy</h3></div>
        <div className="explorer-controls"><label className="explorer-search"><span className="sr-only">Find in graph</span><Icon name="search" /><input type="search" placeholder="Find in this tree" value={search} onChange={(event) => setSearch(event.target.value)} /></label>
          <div className="hierarchy-view" role="group" aria-label="Hierarchy presentation"><button aria-pressed={view === 'graph'} onClick={() => setView('graph')}>Graph</button><button aria-pressed={view === 'list'} onClick={() => setView('list')}>Tree list</button></div>
          <button disabled={Boolean(search.trim()) || !parentIds(shownTree).length} onClick={() => setCollapsed(collapsed.size ? new Set() : new Set(parentIds(shownTree)))}>{collapsed.size ? 'Expand all' : 'Collapse all'}</button>
          <button aria-pressed={showRemoved} onClick={() => setShowRemoved((value) => !value)}>{showRemoved ? 'Hide removed' : 'Show removed'}</button>
        </div>
        {projection.error && <p className="explorer-notice" role="status">Hierarchy unavailable. {projection.data ? 'Showing last received relationships.' : 'This agent may be unavailable. Retrying automatically.'}</p>}
        {(projection.data?.truncated || projection.data?.incomplete_ancestry || family.malformed) && <p className="explorer-notice">Partial hierarchy · {projection.data?.truncated ? 'Showing up to 500 related agents. ' : ''}{projection.data?.incomplete_ancestry ? 'Some ancestors are unavailable. ' : ''}{family.malformed ? 'A circular historical link is omitted.' : ''}</p>}
        {!projection.data && !projection.error && <p className="explorer-notice" role="status">Loading this agent’s hierarchy…</p>}
        {projection.data && !family.indexed.has(focusedId) && <p role="status" className="explorer-notice">Selected agent is unavailable in this hierarchy snapshot.</p>}
        {projection.data && !filtered.length && <p className="explorer-notice">No agents match this search.</p>}
        {family.tree.length > 0 && <>
          {!visible.has(focusedId) && <p className="explorer-notice">Focused agent is outside this view. <button onClick={() => { setSearch(''); select(focusedId) }}>Reveal focused agent</button></p>}
          <div hidden={view !== 'graph' || !filtered.length} className="explorer-graph"><AgentHierarchy key={treeId} nodes={filtered} allNodes={shownTree} collapsed={effective} onToggle={search.trim() ? undefined : toggle} selectedAgentId={focusedId} onSelect={select} filterKey={search.trim()} /></div>
          {view === 'list' && <div className="explorer-tree" aria-label="Hierarchy tree navigation"><AgentTree nodes={filtered} collapsed={effective} onToggle={search.trim() ? undefined : toggle} selectedAgentId={focusedId} onSelect={select} /></div>}
        </>}
      </section>
      {focused ? <FocusedAgentCard key={focusedId} agent={focused} parent={parent} childrenCount={entry?.children.length} relationshipsPartial={!projection.data || projection.error || projection.data?.truncated || projection.data?.incomplete_ancestry || family.malformed} refreshVersion={refreshVersion} onFullDetails={onFullDetails} onSelect={select} /> : <section className="focused-agent-card"><p role="status">Selected agent is no longer available.</p></section>}
    </div>
  </dialog>, document.body)
}
