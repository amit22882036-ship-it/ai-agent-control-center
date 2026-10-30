import { useState } from 'react'
import { Button, Skeleton } from '../primitives'
import { AgentStartDialog } from '../create/AgentStart'
import { ActionResult } from './fields'
import { ProviderChange, ProviderWaitCard, QuestionCard, RedirectCard } from './AgentNow'
import { DecisionsSection, DefaultsSection, IdentitySection, TechnicalSection } from './AgentSettings'
import { AutoAllocationSection, RestrictionsSection } from './TaskPolicies'
import { RemovalEntry, RemovalProgress } from './Removal'
import { ResponsibilityPanel } from './Responsibility'
import { ProcessOwnership } from './ProcessOwnership'
import StopControls from './StopControls'

// Controls for what the agent is doing now (Now tab, above the task facts).
export function AgentNowPanels({ agent, store, taskId, now }) {
  const { target, detail, detailResource, scope, runner, nameOf, version } = agent
  const task = store.tasks[taskId]
  const lastRemoved = target.kind !== 'current' && target.lastAgentId && store.agents[target.lastAgentId]?.removed
  const panels = []
  if (target.kind === 'current' && !detail) {
    panels.push(detailResource.error && detailResource.error !== 'unreachable' && !detailResource.loading
      ? <p key="unavailable" className="a-fine">This agent’s controls are unavailable right now ({detailResource.error === 404 ? 'the agent was not found' : 'the backend refused the read'}).</p>
      : detailResource.error ? <p key="offline" className="a-fine">This agent’s controls return when the backend answers again.</p>
        : <Skeleton key="loading" lines={2} label="Loading the agent’s controls" />)
  }
  if (scope) {
    if (scope.caps.removal === 'removing' || scope.caps.removal === 'blocked') panels.push(<RemovalProgress key="rm" scope={scope} nameOf={nameOf} version={version} />)
    panels.push(<ProcessOwnership key="own" agentId={scope.agentId} runner={runner} version={version} now={now} />)
    if (scope.caps.reply) panels.push(<QuestionCard key={`q-${taskId}`} scope={scope} />)
    if (scope.caps.providerWait) panels.push(<ProviderWaitCard key="wait" scope={scope} />)
    if (scope.caps.providerChange) panels.push(<ProviderChange key="switch" scope={scope} />)
    if (scope.caps.redirect) panels.push(<RedirectCard key={`r-${taskId}`} scope={scope} />)
  } else if (target.lastAgentId) {
    // Recovery that the backend supports for an agent that is history stays available: settling
    // its runs and retrying its blocked removal. Ordinary execution actions are not offered.
    const state = detail?.removal?.state
    if (state === 'removing' || state === 'blocked') {
      panels.push(<RemovalProgress key="rm-last" scope={{ agentId: target.lastAgentId, detail, runner }} nameOf={nameOf} version={version} />)
    }
    panels.push(<ProcessOwnership key="own-last" agentId={target.lastAgentId} runner={runner} version={version} now={now} historical />)
  }
  if (task.responsibilityAwaiting || lastRemoved) {
    panels.push(<ResponsibilityPanel key="resp" taskId={taskId} runner={runner} version={version} nameOf={nameOf} />)
  }
  // Starting agents: a helper under the current agent, or a new assignment on a task that has no
  // current agent and is not waiting for a new owner (that goes through the transfer above).
  const canStartAgain = target.kind !== 'current' && !task.responsibilityAwaiting && !lastRemoved
    && !['completed', 'canceled'].includes(task.lifecycle) && task.controlIntent !== 'canceled'
  if ((scope && !scope.caps.removal) || canStartAgain) {
    panels.push(<StartPanel key="start" agent={agent} taskId={taskId} title={task.title} mode={scope && !scope.caps.removal ? 'helper' : 'again'} />)
  }
  return panels.length ? <div className="a-blk a-agent-now">{panels}</div> : null
}

// Settings tab: the agent's model, decision policies, helper allocation, permissions, identity,
// technical details and (separated) removal.
export function AgentSettingsPanels({ agent, taskId }) {
  const { target, detail, scope, runner, guardIdentity, version } = agent
  if (!detail) return null
  if (!scope) {
    return <>
      <IdentitySection agentId={detail.agent_id} detail={detail} runner={runner} guardIdentity={guardIdentity} historical version={version} />
      <TechnicalSection detail={detail} taskId={taskId} />
      {target.kind !== 'current' && <p className="a-fine">Other controls apply only to a task’s current agent.</p>}
    </>
  }
  const allocationTask = scope.caps.allocationTask
  return <>
    <DefaultsSection scope={scope} />
    <DecisionsSection scope={scope} />
    <AutoAllocationSection scope={scope} taskId={allocationTask} version={version} />
    <RestrictionsSection scope={scope} taskId={allocationTask} version={version} />
    <IdentitySection agentId={scope.agentId} detail={detail} runner={runner} guardIdentity={guardIdentity} version={version} />
    <TechnicalSection detail={detail} taskId={taskId} />
    {!scope.caps.removal && <RemovalEntry scope={scope} />}
  </>
}

function StartPanel({ agent, taskId, title, mode }) {
  const [open, setOpen] = useState(false)
  const [done, setDone] = useState(null)
  const name = mode === 'helper' ? agent.scope.agentName : title || 'this task'
  return <section className="a-blk" aria-labelledby={`start-${taskId}`}>
    <h3 id={`start-${taskId}`}>{mode === 'helper' ? 'Helpers' : 'Nobody is working on this task'}</h3>
    <div className="a-setting-row">
      <div><small>{mode === 'helper' ? 'Give part of this work to a new helper agent, on a new task under this one.'
        : 'Start a new agent on this task. It does not resume an earlier agent’s session.'}</small></div>
      <Button size="sm" icon="plus" onClick={() => { setDone(null); setOpen(true) }}>{mode === 'helper' ? 'Add a helper…' : 'Start a new agent…'}</Button>
    </div>
    {done && <div className="a-result a-result--ok" role="status"><div>{done}</div></div>}
    {open && <AgentStartDialog mode={mode} open onClose={() => setOpen(false)} parentAgentId={agent.scope?.agentId} parentName={name} taskId={taskId}
      version={agent.version} onStarted={() => {
        setOpen(false)
        setDone(mode === 'helper' ? 'Helper started. Its task appears under this one.' : 'A new agent started on this task.')
        agent.refresh()
      }} />}
  </section>
}

// Outcomes of actions whose controls close or move when they succeed (Stop from the footer, a
// removal dialog, a removal retry), shown above whichever tab is open.
export function AgentStopResult({ agent }) {
  return <ActionResult result={agent.runner.result} keys={['stop', 'stop-branch', 'remove', 'removal-retry']} />
}

// Stop controls for the inspector footer, separated from everyday actions.
export function AgentFooterActions({ agent }) {
  return agent.scope ? <StopControls scope={agent.scope} nameOf={agent.nameOf} /> : null
}
