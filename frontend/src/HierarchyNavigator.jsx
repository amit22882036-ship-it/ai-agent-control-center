import { agentName, executionLabel } from './agentPresentation.mjs'
import { displayColor } from './agentIdentity.mjs'
import { navigatorRows } from './hierarchyContext.mjs'
import Icon from './Icon'

export default function HierarchyNavigator({ tree, collapsed, count, partial, loading, error, selectedId, onSelect, onToggle, search, onSearch, onReveal }) {
  const rows = navigatorRows(tree, collapsed)
  const branched = count > 1
  return <nav className="explorer-navigator" aria-label="This hierarchy">
    <div className="navigator-heading"><h3>This hierarchy <span>{count}{partial ? '+' : ''}</span></h3></div>
    <label className="navigator-search"><span className="sr-only">Search this hierarchy</span><Icon name="search" size={15} /><input type="search" placeholder="Search this hierarchy" value={search} onChange={(event) => onSearch(event.target.value)} /></label>
    {partial && <p className="navigator-note">Partial hierarchy. Showing known relationships only.</p>}
    {error && <p role="status" className="navigator-note">Hierarchy unavailable. {count ? 'Showing last received agents.' : 'Retrying automatically.'}</p>}
    {loading && <p role="status" className="navigator-note">Loading hierarchy…</p>}
    {count > 0 && !rows.some(({ agent }) => agent.agent_id === selectedId) && <p className="navigator-note">Selection hidden in this view. <button onClick={onReveal}>Reveal selected</button></p>}
    <ul className="navigator-list">{rows.map(({ agent, children, depth, contextOnly }) => <li key={agent.agent_id} style={{ '--depth': depth }} data-context={Boolean(contextOnly)}>
      {branched && (children.length ? <button className="navigator-toggle" disabled={!onToggle} aria-expanded={!collapsed.has(agent.agent_id)} aria-label={`${collapsed.has(agent.agent_id) ? 'Expand' : 'Collapse'} ${agentName(agent)} in navigator`} onClick={() => onToggle?.(agent.agent_id)}><Icon name={collapsed.has(agent.agent_id) ? 'chevron' : 'down'} size={12} /></button> : <span className="navigator-branch-space" aria-hidden="true" />)}
      <button className="navigator-agent" title={agentName(agent)} aria-label={`${agentName(agent)}, ${executionLabel(agent)}${depth ? `, level ${depth + 1}` : ''}`} aria-pressed={agent.agent_id === selectedId} onClick={() => onSelect(agent.agent_id)} data-display-color={displayColor(agent.display_color)}>
        <span className="navigator-identity"><strong>{agentName(agent)}</strong><span className={`status status-${agent.status}`}>{executionLabel(agent)}</span></span>
      </button>
    </li>)}</ul>
    {!loading && !rows.length && <p className="navigator-note">No matching agents.</p>}
  </nav>
}
