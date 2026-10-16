import { agentName, executionLabel, agentContext } from './agentPresentation.mjs'
import { displayColor } from './agentIdentity.mjs'
import { focusedEvidence, assignmentLabel } from './hierarchyContext.mjs'
import useReadProjection from './useReadProjection'
import Icon from './Icon'

export default function FocusedAgentCard({ agent, parent, childrenCount, relationshipsPartial, refreshVersion, onFullDetails, onSelect }) {
  const { data, error } = useReadProjection(`/agents/${encodeURIComponent(agent.agent_id)}/activity`, refreshVersion)
  const evidence = focusedEvidence(data, agent.task_status)
  const label = assignmentLabel(agent)
  return <section className="focused-agent-card" data-display-color={displayColor(agent.display_color)} aria-labelledby="focused-agent-heading">
    <header className="focus-header"><span className="agent-avatar" aria-hidden="true"><Icon name="agent" size={22} /></span><div><p className="eyebrow">IN FOCUS</p><h3 id="focused-agent-heading">{agentName(agent)}</h3></div></header>
    <span className={`status status-${agent.status}`}>{executionLabel(agent)}</span>
    <div className="focus-section"><div className="focus-section-heading"><h4>{label}</h4>{label === 'Last assignment' && <span className="focus-tag">Historical</span>}</div>
      <p>{agentContext(agent) || agent.task || 'No assignment recorded.'}</p>
      {agent.task && <details className="focus-instructions"><summary>Full assignment instructions</summary><p>{agent.task}</p></details>}
    </div>
    <div className="focus-section focus-glance"><dl>
      {agent.task_status && <div><dt>Task</dt><dd>{agent.task_status.replaceAll('_', ' ')}</dd></div>}
      <div><dt>Parent</dt><dd>{parent ? <button onClick={() => onSelect(parent.agent_id)}>{agentName(parent)}</button> : agent.parent_id ? 'Unavailable in this snapshot' : 'Independent root'}</dd></div>
      <div><dt>Children</dt><dd>{childrenCount === undefined ? 'Not available' : `${childrenCount}${relationshipsPartial ? '+' : ''} direct ${childrenCount === 1 ? 'child' : 'children'}`}</dd></div>
    </dl>{relationshipsPartial && <p className="focus-footnote">Relationships are incomplete or awaiting refresh.</p>}</div>
    {error && <p role="status" className="focus-error">Activity is unavailable. {data ? 'Showing last received records.' : 'Retrying automatically.'}</p>}
    {!data && !error && <p role="status" className="focus-footnote">Loading recorded work…</p>}
    {data && <>
      <div className="focus-section"><h4>Recorded progress</h4>{evidence.recorded.length ? <ul className="focus-milestones">{evidence.recorded.map((text, i) => <li key={i}><Icon name="history" size={14} /><span>{text}</span></li>)}</ul> : <p className="focus-empty">No recorded milestones yet.</p>}</div>
      <div className="focus-section"><h4>Known remaining work</h4>{evidence.remaining.length ? <ul>{evidence.remaining.map((text, i) => <li key={i}>{text}</li>)}</ul> : <p className="focus-empty">No confirmed next step recorded.</p>}</div>
    </>}
    <div className="focus-section focus-actions"><p>Controls, activity and output history</p><button className="focus-details-link" onClick={() => onFullDetails(agent.agent_id)}>View full details <Icon name="arrow" size={15} /></button></div>
    {data?.events.length > 0 && <div className="focus-section"><h4>Recent activity</h4><ol className="focus-recent">{data.events.slice(0, 3).map((event) => <li key={event.id}><span>{event.label}</span>{event.at && <time dateTime={event.at}>{new Date(event.at).toLocaleString()}</time>}</li>)}</ol><p className="focus-footnote">Recent recorded events, not a complete history.</p></div>}
    <details className="focus-technical"><summary>Technical details</summary><dl><dt>Agent ID</dt><dd>{agent.agent_id}</dd>{agent.task_id && <><dt>Task ID</dt><dd>{agent.task_id}</dd></>}<dt>Provider</dt><dd>{agent.agent_type}</dd>{agent.sandbox && <><dt>Sandbox</dt><dd>{agent.sandbox}</dd></>}</dl></details>
  </section>
}
