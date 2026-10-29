import { Skeleton } from '../primitives'
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

// Outcomes of actions whose controls close or move when they succeed (Stop from the footer, a
// removal dialog, a removal retry), shown above whichever tab is open.
export function AgentStopResult({ agent }) {
  return <ActionResult result={agent.runner.result} keys={['stop', 'stop-branch', 'remove', 'removal-retry']} />
}

// Stop controls for the inspector footer, separated from everyday actions.
export function AgentFooterActions({ agent }) {
  return agent.scope ? <StopControls scope={agent.scope} nameOf={agent.nameOf} /> : null
}
