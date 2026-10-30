import { useState } from 'react'
import Inspector from '../Inspector'
import { Avatar, StatusBead } from '../identity'
import { Button, EmptyState, IconButton, Notice, Skeleton } from '../primitives'
import { Tabs } from '../controls'
import Icon from '../Icon'
import { tabPanelProps } from '../../a11y.mjs'
import { API_BASE } from '../../../apiClient.mjs'
import { useLiveResource } from '../../hooks'
import { TOOLS } from '../../vocabulary.mjs'
import { helpersView, plannedView, resultView } from '../../model/taskStatus.mjs'
import { agentLabel, nowView, timeView, whoView } from '../../model/workList.mjs'
import { pathTo } from '../../model/tasks.mjs'
import { exitAtlas } from '../../uiMode.mjs'
import { classicHandoffAgent } from '../../model/agentTarget.mjs'
import { AgentFooterActions, AgentNowPanels, AgentSettingsPanels, AgentStopResult } from '../agent/AgentPanels'
import { useAgentScope } from '../agent/useAgentScope'
import { OutputHistory } from '../agent/OutputHistory'

const TABS = [{ id: 'now', label: 'Now' }, { id: 'activity', label: 'Activity' }, { id: 'output', label: 'Output' }, { id: 'settings', label: 'Settings' }]
const AGENT_STATUS = { running: 'Working', waiting: 'Waiting', finished: 'Finished', stopped: 'Stopped' }
const ENDED_REASON = {
  completed: 'finished its work', stopped: 'was stopped', reassigned: 'was replaced', paused: 'was paused', blocked: 'was blocked',
  resource_conflict: 'stopped for a resource conflict', resource_deadlock: 'stopped for a resource deadlock',
  external_resource_unavailable: 'stopped: a resource was unavailable', removed: 'was removed', canceled: 'was canceled',
}
const DISPOSITION = { pending: 'waiting to start', ready: 'ready to start', blocked: 'held', conflict: 'in conflict', unauthorized: 'not allowed',
  reuse_available: 'can reuse earlier work', invalid: 'invalid', direct: 'requested directly' }

const encode = encodeURIComponent
const clock = (value) => {
  const time = Date.parse(value || '')
  return Number.isFinite(time) ? new Date(time).toLocaleString('en-GB', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : null
}
const clip = (text, limit) => (text && text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text)

// Classic opens with this task's current agent selected when there is one (never an ended or
// removed agent); otherwise it opens without a selection.
function ClassicHint({ handoff, children }) {
  return <p className="a-fine a-insp-classic">{children} <button type="button" className="a-link"
    onClick={() => exitAtlas(undefined, { agentId: handoff })}>Open Classic view</button></p>
}

function StateNotes({ store, derived, id, activity, handoff, controls }) {
  const status = derived.status[id]
  const { current, last } = derived.agents[id]
  const agent = current || last
  const notes = []
  if (status.key === 'needs_input' && !controls?.reply) {
    const question = activity?.context?.task_id === id ? activity.context.waiting_question : null
    notes.push(<div key="q" className="a-qcard">
      <div className="a-qh"><Icon name="hand" size={15} stroke={2} />Asked you</div>
      {question ? <p className="a-qt a-bidi" dir="auto">{clip(question, 1200)}</p> : <p className="a-fine">Loading the question…</p>}
      <ClassicHint handoff={handoff}>Replying arrives in Atlas in the next milestone.</ClassicHint>
    </div>)
  }
  if (status.key === 'provider_wait' && !controls?.providerWait) notes.push(<Notice key="p" tone="warn" title={status.label}>
    The provider reported this limit; nothing resumes by itself. A reported reset time is not a guarantee.</Notice>)
  if (status.key === 'blocked') notes.push(<Notice key="b" tone="info" title={status.label}>
    {store.tasks[id].blockers.length ? `Reported reasons: ${store.tasks[id].blockers.map((code) => code.replace(/_/g, ' ')).join(', ')}.` : 'No specific reason was reported.'}</Notice>)
  if (status.key === 'needs_owner') notes.push(<Notice key="o" tone="warn" title={status.label}>
    {status.detail || 'Its agent was removed. The task, its history and its work are kept until you choose a new owner.'}</Notice>)
  if (status.key === 'recovery') notes.push(<Notice key="r" tone="error" title={status.label}>
    The Control Center stopped short of guessing what happened. Review the details below before anything continues.</Notice>)
  if (status.key === 'paused') notes.push(<Notice key="pa" tone="info" title="Paused">Nothing runs until it is resumed.</Notice>)
  if (agent?.removed) notes.push(<Notice key="rm" tone="info" title="This agent was removed">
    Its assignment, output and history are kept. It is shown as history, not as working.</Notice>)
  if (!current && last && !agent?.removed && !['completed', 'integrated', 'canceled'].includes(status.key)) {
    notes.push(<Notice key="l" tone="info" title="No agent is working on this task">Shown below is its last agent.</Notice>)
  }
  return notes.length ? <div className="a-blk a-insp-notes">{notes}</div> : null
}

// The lead above, this task and up to five helpers below, from real Task relationships only: a
// small picture of where it sits, with each Task's status shape. Choosing one selects it; the
// whole family opens in the Map.
function Family({ store, derived, id, onSelect, onShowInMap, lens }) {
  const { forest } = derived
  const parent = store.tasks[id].parentId && store.tasks[store.tasks[id].parentId] ? store.tasks[id].parentId : null
  const all = forest.children[id] || []
  const children = all.slice(0, 5)
  const more = all.length - children.length
  const above = pathTo(forest, id).length
  const siblings = parent ? forest.children[parent] || [] : null
  const face = (taskId, size) => {
    const who = whoView(derived, taskId)
    const status = derived.status[taskId]
    return <span className="a-fam-face">
      {who.agent ? <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={size} removed={who.removed} decorative />
        : <span className="a-avatar-empty" style={{ width: size, height: size }} aria-hidden="true" />}
      <span className="a-fam-bead"><StatusBead state={status.bead} size={12} decorative /></span>
    </span>
  }
  const node = (taskId, size, role) => <button key={taskId} type="button" className="a-fam-node" onClick={() => onSelect(taskId)}
    title={store.tasks[taskId].title || 'Untitled task'}>
    {face(taskId, size)}<span className="a-sr">{role}: </span><span className="a-fam-label a-bidi" dir="auto">{store.tasks[taskId].title || 'Untitled task'}</span>
    <span className="a-sr">, {derived.status[taskId].label}</span>
  </button>
  const where = [above === 0 ? 'Top of its family' : `Level ${above + 1}${above > 1 ? ` · ${above - 1} more level${above - 1 === 1 ? '' : 's'} above its lead` : ''}`,
    siblings && siblings.length > 1 ? `helper ${siblings.indexOf(id) + 1} of ${siblings.length} under its lead` : null,
    all.length ? `${all.length} helper task${all.length === 1 ? '' : 's'} below` : null].filter(Boolean).join(' · ')
  return <>
    <div className="a-fam" role="group" aria-label="Lead and helpers">
      {parent && <>{node(parent, 26, 'Its lead')}<span className="a-fam-line" aria-hidden="true" /></>}
      <div className="a-fam-self" aria-current="true" title="This task">{face(id, 36)}</div>
      {children.length > 0 && <>
        <span className="a-fam-line" aria-hidden="true" />
        <div className={`a-fam-kids${children.length > 1 ? ' a-fam-kids--many' : ''}`}>{children.map((child) => node(child, 26, 'Helper'))}</div>
        {more > 0 && <span className="a-fine">and {more} more</span>}
      </>}
    </div>
    <div className="a-fam-foot"><span className="a-fine">{where}</span>
      <Button size="sm" variant="ghost" icon="map" onClick={() => onShowInMap(id)}>{lens === 'map' ? 'Centre in map' : 'Show in map'}</Button></div>
  </>
}

function NowTab({ store, derived, id, projectName, onSelect, task, activity, now, handoff, agent, onShowInMap, lens, onOpenTask }) {
  const data = store.tasks[id]
  const parent = data.parentId ? store.tasks[data.parentId] : null
  const children = derived.forest.children[id] || []
  const helpers = helpersView(data)
  const planned = plannedView(data)
  const result = resultView(data)
  const into = derived.integration[id]
  const description = task?.data?.task_id === id ? task.data.description : null
  return <>
    <AgentNowPanels agent={agent} store={store} taskId={id} now={now} />
    <StateNotes store={store} derived={derived} id={id} activity={activity} handoff={handoff} controls={agent.scope?.caps} />
    <section className="a-blk" aria-labelledby={`resp-${id}`}>
      <h3 id={`resp-${id}`}>Responsible for</h3>
      <div className="a-resp">
        <div className="a-rt a-bidi" dir="auto">{data.title || 'Untitled task'}</div>
        {description && <p className="a-rd a-bidi" dir="auto">{clip(description, 600)}</p>}
        <div className="a-rm"><Icon name="folder" size={13} /><span className="a-bidi" dir="auto">{projectName(data.projectId)}</span>
          {parent && <> · delegated by <button type="button" className="a-link a-bidi" dir="auto" onClick={() => onSelect(parent.id)}>{clip(parent.title, 48)}</button></>}
        </div>
      </div>
    </section>
    {data.parentId && !parent && <Notice tone="info" title="Its lead isn’t shown">
      {data.ancestry === 'missing_parent' ? 'The lead task it was delegated from no longer exists.' : 'Its lead task isn’t loaded in this view.'}</Notice>}
    {(parent || children.length > 0) && <section className="a-blk" aria-labelledby={`fam-${id}`}>
      <h3 id={`fam-${id}`}>Family{children.length > 0 && <span className="a-h3-aside">{children.length} helper{children.length === 1 ? '' : 's'}</span>}</h3>
      <Family store={store} derived={derived} id={id} onSelect={onSelect} onShowInMap={onShowInMap} lens={lens} />
    </section>}
    {(children.length > 0 || helpers.proposed > 0 || helpers.active > 0) && <section className="a-blk" aria-labelledby={`kids-${id}`}>
      <h3 id={`kids-${id}`}>Delegated work<span className="a-h3-aside">{children.length} helper task{children.length === 1 ? '' : 's'}</span></h3>
      {children.length > 0 && <div className="a-kids">{children.map((child) => {
        const status = derived.status[child]
        const who = whoView(derived, child)
        return <div key={child} className="a-kid-row">
          <button type="button" className="a-kid" onClick={() => onSelect(child)}>
            <StatusBead state={status.bead} decorative />
            <span className="a-kid-main"><span className="a-kt a-ell a-bidi" dir="auto">{store.tasks[child].title || 'Untitled task'}</span>
              <small>{who.agent ? `${who.name} · ` : 'No agent · '}{status.label}</small></span>
            <span className="a-c-time a-num">{timeView(store, child, now).text}</span>
          </button>
          <IconButton icon="open" size="sm" label={`Open task: ${store.tasks[child].title || 'Untitled task'}`} onClick={() => onOpenTask(child)} />
        </div>
      })}</div>}
      <dl className="a-kv a-kv--compact">
        <dt>Still working</dt><dd className="a-num">{data.delegations.materialized}</dd>
        <dt>Results ready</dt><dd className="a-num">{helpers.resultsReady}</dd>
        <dt>Reviewed by the lead</dt><dd className="a-num">{helpers.closed}</dd>
        {helpers.canceled > 0 && <><dt>Canceled</dt><dd className="a-num">{helpers.canceled}</dd></>}
        {helpers.rounds > 0 && <><dt>Planning rounds</dt><dd className="a-num">{helpers.rounds}</dd></>}
      </dl>
    </section>}
    {planned && <section className="a-blk" aria-labelledby={`plan-${id}`}>
      <h3 id={`plan-${id}`}>Planned, not started</h3>
      <p className="a-fine">{planned.count} part{planned.count === 1 ? '' : 's'} of this plan {planned.count === 1 ? 'is' : 'are'} not a task yet: {Object.entries(planned.byDisposition)
        .map(([key, count]) => `${count} ${DISPOSITION[key] || key.replace(/_/g, ' ')}`).join(', ')}.</p>
    </section>}
    {(result || into) && <section className="a-blk" aria-labelledby={`res-${id}`}>
      <h3 id={`res-${id}`}>Result</h3>
      <dl className="a-kv">
        {result && <><dt>Reported</dt><dd>{result.label}</dd></>}
        {into && <><dt>{into.scope === 'project' ? 'Applied to project' : 'Combined into lead'}</dt>
          <dd className={into.attention ? 'a-tone-alert' : undefined}>{into.label}{into.appliedAt ? ` · ${clock(into.appliedAt)}` : ''}</dd></>}
        {into && <><dt>Checked</dt><dd>Not independently verified</dd></>}
      </dl>
    </section>}
  </>
}

function ActivityTab({ store, derived, id, activity, assignments }) {
  const own = activity.data?.context?.task_id === id
  const events = own ? activity.data.events || [] : []
  const history = assignments.data?.assignments || []
  return <>
    <section className="a-blk" aria-labelledby={`hist-${id}`}>
      <h3 id={`hist-${id}`}>Agents on this task</h3>
      {assignments.loading && !assignments.data ? <Skeleton lines={2} label="Loading assignments" />
        : assignments.error ? <p className="a-fine">Assignment history is unavailable right now.</p>
          : history.length === 0 ? <p className="a-fine">No agent has worked on this task yet.</p>
            : <ol className="a-hist">{[...history].reverse().map((entry) => {
              const agent = store.agents[entry.agent_id]
              return <li key={entry.assignment_id} className={entry.ended_at ? 'is-ended' : 'is-current'}>
                <Avatar name={agentLabel(agent) || 'Agent'} color={agent?.color} tool={agent?.tool} size={22} removed={Boolean(agent?.removed)} decorative />
                <span className="a-hist-main"><b className="a-bidi" dir="auto">{agent ? agentLabel(agent) : 'Earlier agent'}</b>
                  <small>{entry.ended_at ? `${ENDED_REASON[entry.ended_reason] || 'ended'} · ${clock(entry.ended_at)}` : `working since ${clock(entry.assigned_at)}`}</small></span>
                {!entry.ended_at && <span className="a-chip a-chip--work">Current</span>}
              </li>
            })}</ol>}
    </section>
    <section className="a-blk" aria-labelledby={`act-${id}`}>
      <h3 id={`act-${id}`}>Recorded activity</h3>
      {!derived.agents[id].current && !derived.agents[id].last ? <p className="a-fine">Nothing recorded yet.</p>
        : activity.loading && !activity.data ? <Skeleton lines={3} label="Loading activity" />
          : activity.error ? <p className="a-fine">Activity is unavailable right now.</p>
            : !own ? <p className="a-fine">This agent has since moved to other work, so its activity here isn’t available.</p>
              : events.length === 0 ? <p className="a-fine">Nothing recorded yet.</p>
                : <ol className="a-tl">{events.map((event) => <li key={event.id} className="a-tl-i">
                  <span className="a-tl-d" aria-hidden="true" />
                  <span className="a-tl-t a-bidi" dir="auto">{event.label}</span>
                  <span className="a-tl-time a-num">{clock(event.at) || 'Time not recorded'}</span>
                </li>)}</ol>}
      <p className="a-fine">Recorded by the Control Center (most recent 30).</p>
    </section>
  </>
}

function DetailsTab({ store, derived, id, projectName }) {
  const task = store.tasks[id]
  const who = whoView(derived, id)
  const agent = who.agent
  return <section className="a-blk" aria-labelledby={`det-${id}`}>
    <h3 id={`det-${id}`}>{agent ? (who.role === 'current' ? 'Agent' : 'Last agent') : 'Task'}</h3>
    <dl className="a-kv">
      {agent && <>
        <dt>Tool</dt><dd>{TOOLS[agent.tool]?.label || 'Not reported'}</dd>
        <dt>Model</dt><dd>{agent.model || 'Not reported'}</dd>
        <dt>Effort</dt><dd>{agent.effort || 'Not reported'}</dd>
        <dt>Agent status</dt><dd>{AGENT_STATUS[agent.status] || 'Not reported'}{agent.removed ? ' · removed' : ''}</dd>
        <dt>Helpers</dt><dd>{agent.autonomous ? 'May bring in helpers' : 'Works without helpers'}</dd>
      </>}
      <dt>Project</dt><dd className="a-bidi" dir="auto">{projectName(task.projectId)}</dd>
      <dt>Task status</dt><dd>{derived.status[id].label}{derived.status[id].certainty === 'unknown' ? ' (not fully reported)' : ''}</dd>
      {task.planRevision !== null && <><dt>Plan revision</dt><dd className="a-num">{task.planRevision}</dd></>}
      <dt>Created</dt><dd>{clock(task.createdAt) || 'Not recorded'}</dd>
      <dt>Task id</dt><dd className="a-mono">{task.id}</dd>
      {agent && <><dt>Agent id</dt><dd className="a-mono">{agent.id}</dd></>}
    </dl>
  </section>
}

// Read-only details for the selected Task and its current or last Agent. Actions arrive in M4;
// the footer keeps a way to Classic view for what Atlas does not offer yet.
export default function TaskInspector({ store, derived, taskId, selection, projectName, version, now, width, onResize, onClose, onSelect, onLoadMore,
  getStore, onRefresh, lens = 'list', onShowInMap, onFocusBranch, onOpenTask }) {
  const [tab, setTab] = useState('now')
  const agentScope = useAgentScope({ store, taskId, version, getStore, onRefresh })
  const present = selection === 'present'
  const who = present ? whoView(derived, taskId) : null
  const agent = who?.agent && !who.agent.unknown ? who.agent : null
  const task = useLiveResource(present ? `${API_BASE}/tasks/${encode(taskId)}` : null, version)
  const assignments = useLiveResource(present && tab === 'activity' ? `${API_BASE}/tasks/${encode(taskId)}/assignments` : null, version)
  const activity = useLiveResource(present && agent && (tab === 'activity' || derived.status[taskId].key === 'needs_input')
    ? `${API_BASE}/agents/${encode(agent.id)}/activity` : null, version)

  if (!present) {
    return <Inspector label="Task details" width={width} onResize={onResize} onClose={onClose}
      header={<h2 className="a-insp-title">{selection === 'not_loaded' ? 'Task not loaded yet' : 'Task not found'}</h2>}>
      {selection === 'not_loaded'
        ? <EmptyState icon="layers" title="This task isn’t in the loaded pages yet"
          actions={<Button size="sm" icon="down" onClick={onLoadMore}>Load older tasks</Button>}>Older tasks load a page at a time.</EmptyState>
        : <EmptyState icon="info" title="This task no longer exists">It may have been removed from the Control Center. Nothing else was selected in its place.</EmptyState>}
    </Inspector>
  }

  const status = derived.status[taskId]
  const data = store.tasks[taskId]
  const handoff = classicHandoffAgent(store, taskId)
  const nowLine = nowView(store, derived, taskId)
  const time = timeView(store, taskId, now)
  const header = <div className="a-ih">
    <div className="a-ih-row">
      {who.agent ? <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={44} removed={who.removed} decorative />
        : <span className="a-avatar-empty a-avatar-empty--lg" aria-hidden="true" />}
      <div className="a-ih-main">
        <h2 className="a-insp-title a-bidi" dir="auto">{who.agent ? who.name : 'No agent'}</h2>
        <div className="a-role">{data.parentId ? 'Helper' : 'Lead'}{who.agent ? ` · ${who.role === 'current' ? who.detail : 'last agent' + (who.removed ? ' · removed' : '')}` : ' · nobody is assigned'}</div>
      </div>
      <div className="a-ih-acts">
        <IconButton icon="open" size="sm" label="Open the task page (O)" onClick={() => onOpenTask(taskId)} />
        <IconButton icon="map" size="sm" label={lens === 'map' ? 'Centre this task in the map' : 'Show this task in the map'} onClick={() => onShowInMap(taskId)} />
        <IconButton icon="focus" size="sm" label="Focus this branch (F)" onClick={() => onFocusBranch(taskId)} />
      </div>
    </div>
    <div className={`a-ih-state a-tone-${status.tone}`}><StatusBead state={status.bead} decorative />{status.label}
      <span className="a-muted"> · {nowLine.second || time.text}</span></div>
  </div>
  const idBase = `insp-${taskId}`
  return <Inspector label="Task details" width={width} onResize={onResize} onClose={onClose} header={header}
    tabs={<Tabs label="Task details" tabs={TABS} value={tab} onChange={setTab} idBase={idBase} />}
    footer={<><AgentFooterActions agent={agentScope} /><span className="a-sp" />
      <Button size="sm" variant="ghost" icon="open" onClick={() => onOpenTask(taskId)}>Open task</Button>
      <Button size="sm" variant="ghost" icon="swap" onClick={() => exitAtlas(undefined, { agentId: handoff })}
        title={handoff ? 'Classic view opens with this agent selected.' : 'Classic view opens without a selection because this task has no current agent.'}>
        Open in Classic</Button></>}>
    <div {...tabPanelProps(idBase, tab)} className="a-insp-panel">
      <AgentStopResult agent={agentScope} />
      {tab === 'now' && <NowTab store={store} derived={derived} id={taskId} projectName={projectName} onSelect={onSelect} task={task} activity={activity.data} now={now}
        handoff={handoff} agent={agentScope} onShowInMap={onShowInMap} lens={lens} onOpenTask={onOpenTask} />}
      {tab === 'activity' && <ActivityTab store={store} derived={derived} id={taskId} activity={activity} assignments={assignments} />}
      {tab === 'output' && (agentScope.agentId ? <OutputHistory key={agentScope.agentId} agentId={agentScope.agentId} version={version}
        historical={agentScope.target.kind !== 'current'} /> : <EmptyState icon="layers" title="No output yet">No agent has worked on this task.</EmptyState>)}
      {tab === 'settings' && <><DetailsTab store={store} derived={derived} id={taskId} projectName={projectName} />
        <AgentSettingsPanels agent={agentScope} taskId={taskId} /></>}
    </div>
  </Inspector>
}
