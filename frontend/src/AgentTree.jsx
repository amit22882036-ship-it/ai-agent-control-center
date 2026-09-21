function Branches({ nodes, selectedAgentId, onSelect, collapsed, onToggle, depth = 0 }) {
  return (
    <ul className={`agent-tree${depth > 0 && depth <= 4 ? ' agent-children' : ''}`}>
      {nodes.map(({ agent, children, contextOnly }) => (
        <li key={agent.agent_id}>
          <article className={`agent-card${agent.status === 'waiting' ? ' agent-waiting' : ''}${agent.status === 'running' ? ' agent-running' : ''}${selectedAgentId === agent.agent_id ? ' selected' : ''}`}>
            {children.length > 0 && (
              <button type="button" className="close-button branch-toggle" disabled={!onToggle} aria-expanded={!collapsed.has(agent.agent_id)}
                aria-label={`${collapsed.has(agent.agent_id) ? 'Expand' : 'Collapse'} descendants of agent ${agent.agent_id}`}
                onClick={() => onToggle(agent.agent_id)}>
                {collapsed.has(agent.agent_id) ? 'Expand' : 'Collapse'}
              </button>
            )}
            <h2 className="task">
              <button className="agent-select" type="button" aria-pressed={selectedAgentId === agent.agent_id}
                onClick={() => onSelect(agent.agent_id)}><span className="task-preview">{agent.task}</span></button>
            </h2>
            <span className={`status status-${agent.status}`}>{agent.status === 'waiting' ? 'waiting for you' : agent.status}</span>
            {depth > 4 && <p className="agent-type-note">Nesting level {depth}</p>}
            {contextOnly && <p className="agent-type-note">Ancestor of a matching agent</p>}
            <dl>
              <dt>Agent ID</dt><dd>{agent.agent_id}</dd>
              <dt>Type</dt><dd>{agent.agent_type === 'codex' ? 'Codex' : 'Mock'}</dd>
              {agent.agent_type === 'codex' && <><dt>Sandbox</dt><dd>{agent.sandbox === 'workspace-write' ? 'Workspace write' : 'Read only'}</dd></>}
            </dl>
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
