import { buildAgentTree } from './buildAgentTree.mjs'

function Branches({ nodes, selectedAgentId, onSelect, depth = 0 }) {
  return (
    <ul className={`agent-tree${depth > 0 && depth <= 4 ? ' agent-children' : ''}`}>
      {nodes.map(({ agent, children }) => (
        <li key={agent.agent_id}>
          <article className={`agent-card${agent.status === 'waiting' ? ' agent-waiting' : ''}${selectedAgentId === agent.agent_id ? ' selected' : ''}`}>
            <h2 className="task">
              <button className="agent-select" type="button" aria-pressed={selectedAgentId === agent.agent_id}
                onClick={() => onSelect(agent.agent_id)}>{agent.task}</button>
            </h2>
            <span className={`status status-${agent.status}`}>{agent.status === 'waiting' ? 'waiting for you' : agent.status}</span>
            {depth > 4 && <p className="agent-type-note">Nesting level {depth}</p>}
            <dl>
              <dt>Agent ID</dt><dd>{agent.agent_id}</dd>
              <dt>Type</dt><dd>{agent.agent_type === 'codex' ? 'Codex' : 'Mock'}</dd>
              {agent.agent_type === 'codex' && <><dt>Sandbox</dt><dd>{agent.sandbox === 'workspace-write' ? 'Workspace write' : 'Read only'}</dd></>}
            </dl>
          </article>
          {children.length > 0 && <Branches nodes={children} selectedAgentId={selectedAgentId} onSelect={onSelect} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  )
}

export default function AgentTree({ agents, selectedAgentId, onSelect }) {
  return <Branches nodes={buildAgentTree(agents)} selectedAgentId={selectedAgentId} onSelect={onSelect} />
}
