import Icon from './Icon'
import { displayColor } from './agentIdentity.mjs'
import { agentName, agentContext, statusLabels } from './agentPresentation.mjs'
function Branches({ nodes, selectedAgentId, onSelect, collapsed, onToggle, depth = 0 }) {
  return (
    <ul className={`agent-tree${depth > 0 && depth <= 4 ? ' agent-children' : ''}`}>
      {nodes.map(({ agent, children, contextOnly }) => (
        <li key={agent.agent_id}>
          <article data-display-color={displayColor(agent.display_color)} className={`agent-card${agent.status === 'waiting' ? ' agent-waiting' : ''}${agent.status === 'running' ? ' agent-running' : ''}${selectedAgentId === agent.agent_id ? ' selected' : ''}${contextOnly ? ' context-only' : ''}`}>
            {children.length > 0 && (
              <button type="button" className="close-button branch-toggle" disabled={!onToggle} aria-expanded={!collapsed.has(agent.agent_id)}
                aria-label={`${collapsed.has(agent.agent_id) ? 'Expand' : 'Collapse'} descendants of ${agentName(agent)}`}
                onClick={() => onToggle(agent.agent_id)}>
                <Icon name={collapsed.has(agent.agent_id) ? 'chevron' : 'down'} size={14} />
              </button>
            )}
            <span className="agent-avatar"><Icon name="agent" size={20} /></span>
            <h2 className="task">
              <button className="agent-select" type="button" aria-pressed={selectedAgentId === agent.agent_id}
                onClick={() => onSelect(agent.agent_id)}><span className="agent-name">{agentName(agent)}</span>{agentContext(agent) && <span className="task-preview">{agentContext(agent)}</span>}</button>
            </h2>
            <span className={`status status-${agent.status}`}>{statusLabels[agent.status] || agent.status}</span>
            {depth > 4 && <span className="relationship" title={`Nesting level ${depth}`}><Icon name="chevron" size={12} />{depth}</span>}
            {contextOnly && <span className="sr-only">Ancestor of a matching agent</span>}


          </article>
          {children.length > 0 && !collapsed.has(agent.agent_id) && <Branches nodes={children} selectedAgentId={selectedAgentId} onSelect={onSelect} collapsed={collapsed} onToggle={onToggle} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  )
}

export default function AgentTree(props) {
  return <Branches {...props} />
}
