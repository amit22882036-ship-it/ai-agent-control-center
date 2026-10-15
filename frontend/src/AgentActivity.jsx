export default function AgentActivity({ activity, onSelect }) {
  if (!activity) return null
  const { context, events, delegations, blockers } = activity
  return <details className="detail-section execution-context" open><summary>Work & activity</summary>
    <p className="agent-type-note">{context.autonomous ? 'Autonomous execution' : 'Standard execution'}{context.historical_assignment ? ' · Past assignment' : ''}</p>
    {context.task_status && <p>Task now: <strong>{context.task_status.replaceAll('_', ' ')}</strong>{context.plan_revision ? ` · Plan revision ${context.plan_revision}` : ''}</p>}
    {activity.result && <p>Result: {activity.result.outcome} · {activity.result.phase}<br />Integration: {activity.result.integration_status || 'Not recorded'}</p>}
    {context.waiting_context && <p>{context.waiting_context.label}</p>}
    {blockers.length > 0 && <details><summary>Current blockers ({blockers.length})</summary><ul>{blockers.map((code) => <li key={code}>{code.replaceAll('_', ' ')}</li>)}</ul></details>}
    {delegations.length > 0 && <details><summary>Delegated responsibilities</summary><ul className="responsibilities">{delegations.map((item) => <li key={item.delegation_id}>
      {item.agent_id ? <button className="relationship-link" onClick={() => onSelect(item.agent_id)}>{item.responsibility}</button> : <strong>{item.responsibility}</strong>}
      <p>{item.status.replaceAll('_', ' ')}{item.admission ? ` · Admission: ${item.admission.replaceAll('_', ' ')}` : ''}</p>
      {item.materialization && <p>Execution preparation: {item.materialization.replaceAll('_', ' ')}</p>}
      {item.result_phase && <p>Result: {item.outcome} · {item.result_phase}</p>}
      {item.integration_status && <p>Integration: {item.integration_status.replaceAll('_', ' ')}</p>}
    </li>)}</ul><p className="agent-type-note">Most recent 30 responsibilities. A completed run does not imply integrated work.</p></details>}
    <details open><summary>Recent activity</summary>{events.length ? <ol className="activity-timeline">{events.map((event) => <li key={event.id}><span>{event.label}</span>{event.at && <time dateTime={event.at}>{new Date(event.at).toLocaleString()}</time>}</li>)}</ol> : <p className="agent-type-note">No recorded activity.</p>}
      <p className="agent-type-note">Up to 30 recorded events. Undated transitions describe their current recorded phase.</p></details>
  </details>
}
