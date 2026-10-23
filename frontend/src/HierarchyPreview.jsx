import { useState } from 'react'
import { agentName, executionLabel } from './agentPresentation.mjs'
import HierarchyExplorer from './HierarchyExplorer'
import Icon from './Icon'
import { isRemoved } from './agentRemoval.mjs'

export default function HierarchyPreview({ agent, refreshVersion, onSelect, workspace }) {
  const [open, setOpen] = useState(false)
  const known = workspace?.agents || []
  const parent = known.find((item) => item.agent_id === agent.parent_id)
  const children = agent.child_ids || known.filter((item) => item.parent_id === agent.agent_id).map((item) => item.agent_id)
  return <section className="detail-section hierarchy-preview" aria-label="Hierarchy preview">
    <div className="preview-heading"><h3>Hierarchy</h3><span>{children.length} direct {children.length === 1 ? 'child' : 'children'}</span></div>
    {agent.parent_id && <button className={`preview-relative${isRemoved(parent) ? ' agent-removed' : ''}`} onClick={() => onSelect(agent.parent_id)}><Icon name={isRemoved(parent) ? 'removed' : 'agent'} size={15} className={isRemoved(parent) ? 'removal-mark' : undefined} /><span className="agent-name">{parent ? agentName(parent) : 'Parent outside loaded list'}</span><small>{isRemoved(parent) ? 'Parent · removed' : 'Parent'}</small></button>}
    {isRemoved(parent) && !isRemoved(agent) && <p className="agent-type-note">Its parent was removed. This agent keeps working on its own task; anything that needs the parent, such as reviewing its result, waits until the parent task has a new owner.</p>}
    <div className={`preview-current${agent.parent_id ? ' has-parent' : ''}`}><Icon name="agent" size={17} /><div><strong>{agentName(agent)}</strong><small>{executionLabel(agent)}</small></div><span className="sr-only">Selected agent</span></div>
    {children.length > 0 && <ul className="preview-children">{children.slice(0, 3).map((id) => <li key={id}><button onClick={() => onSelect(id)}>{known.some((item) => item.agent_id === id) ? agentName(known.find((item) => item.agent_id === id)) : 'Child outside loaded list'}</button></li>)}</ul>}
    {children.length > 3 && <p className="agent-type-note">+{children.length - 3} more direct children</p>}
    {!agent.parent_id && !children.length && <p className="agent-type-note">An independent agent. No relatives recorded.</p>}
    <button className="explore-hierarchy" onClick={() => setOpen(true)}>Explore Hierarchy <Icon name="arrow" size={16} /></button>
    {open && <HierarchyExplorer initialAgent={agent} workspace={workspace} refreshVersion={refreshVersion} onClose={() => setOpen(false)} onFullDetails={(id) => { setOpen(false); onSelect(id) }} />}
  </section>
}
