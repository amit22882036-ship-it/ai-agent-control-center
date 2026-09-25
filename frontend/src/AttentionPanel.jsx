import { agentName, attentionAgents, attentionContext } from './agentPresentation.mjs'
import Icon from './Icon'
export default function AttentionPanel({ agents, onSelect }) {
  const waiting = attentionAgents(agents)
  if (!waiting.length) return null
  return <section className="attention-panel" aria-labelledby="attention-title">
    <h2 id="attention-title"><span className="attention-dot" />Needs your attention</h2>
    <div className="attention-links">{waiting.map((agent) => <button className="attention-link" key={agent.agent_id} onClick={() => onSelect(agent.agent_id)}>
      <div><strong>{agentName(agent)}</strong><span>{attentionContext(agent)}</span></div>
      <span className="attention-state">Needs you</span><Icon name="arrow" size={16} />
    </button>)}</div>
  </section>
}
