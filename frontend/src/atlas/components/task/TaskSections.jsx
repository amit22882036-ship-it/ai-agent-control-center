import { Fragment, useMemo, useRef, useState } from 'react'
import Icon from '../Icon'
import { Avatar, StatusBead } from '../identity'
import { Button, Notice, Skeleton } from '../primitives'
import { Segmented } from '../controls'
import { Disclosure } from '../agent/fields'
import TaskTree from '../work/TaskTree'
import TaskMap from '../work/TaskMap'
import { agentLabel, descendantCount, inBranch, nowView, timeView, visibleRows, whoView } from '../../model/workList.mjs'
import { mapLayout } from '../../model/mapLayout.mjs'
import { readMapDirection, saveMapDirection } from '../../layout.mjs'
import { when } from '../../model/taskPage.mjs'

const TEAM_ROWS = 150
const clip = (text, limit) => (text && text.length > limit ? text.slice(0, limit - 1).trimEnd() + '…' : text)

function Section({ id, title, aside, children, className }) {
  return <section className={`a-card a-tsec${className ? ` ${className}` : ''}`} aria-labelledby={id}>
    <div className="a-card-head"><h2 className="a-card-title" id={id}>{title}</h2><span className="a-sp" />{aside}</div>
    <div className="a-card-body">{children}</div>
  </section>
}

// What was asked. A root task's request is the person's; a helper's comes from its lead's
// delegation (the agent's words) unless the person added the helper themselves.
// A long text (a request, the lead's explanation of its plan, a helper's summary) starts folded to
// its first lines; the whole text is always in the page (copying and screen readers get all of
// it) and opens with one button. Nothing is cut or rewritten.
const LONG_TEXT = { chars: 700, lines: 12 }
function FoldedText({ id, text, noun, as: Tag = 'p', className = 'a-prose' }) {
  const [open, setOpen] = useState(false)
  const lines = text.split('\n').length
  const long = text.length > LONG_TEXT.chars || lines > LONG_TEXT.lines
  return <>
    <Tag id={id} className={`${className} a-bidi${long && !open ? ' a-request-folded' : ''}`} dir="auto">{text}</Tag>
    {long && <button type="button" className="a-link a-request-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen((value) => !value)}>
      {open ? 'Show less' : `Show the whole ${noun} (${lines} line${lines === 1 ? '' : 's'}, ${text.length.toLocaleString('en-US')} characters)`}</button>}
  </>
}
const RequestText = ({ text }) => <FoldedText id="t-request-text" text={text} noun="request" />

export function RequestCard({ task, detail }) {
  const text = detail.data?.description
  const byAgent = Boolean(task?.delegation)
  return <Section id="t-request" title="Request" aside={<span className="a-fine">{task?.parentId ? (byAgent ? 'Written by its lead (an agent)' : 'Written by you, as a helper task') : 'Written by you'}</span>}>
    {detail.loading && !detail.data ? <Skeleton lines={2} label="Loading the request" />
      : text ? <RequestText text={text} />
        : <p className="a-fine">{detail.error ? 'The request couldn’t be read right now.' : 'No request text was recorded.'}</p>}
  </Section>
}

// How it is going: the state in words, what holds it, the plan and its rounds, recorded activity.
export function ProgressCard({ progress, activity, taskId, store, onOpenTask }) {
  const own = activity.data?.context?.task_id === taskId
  const events = own ? activity.data.events || [] : []
  const { plan } = progress
  return <Section id="t-progress" title="How it’s going">
    {progress.status && <p className={`a-tstate a-tone-${progress.status.tone}`}>{progress.status.label}{progress.status.detail ? <span className="a-muted"> · {progress.status.detail}</span> : null}</p>}
    {progress.facts.map((fact) => <p key={fact.text} className={`a-fine${fact.tone === 'alert' ? ' a-tone-alert' : ''}`}>{fact.text}</p>)}
    {progress.continuation && <p className="a-fine">{progress.continuation.text}</p>}
    {(progress.blockers.length > 0 || progress.replanning.length > 0) && <div className="a-tblock">
      <h3>What holds it</h3>
      <ul className="a-tlist">
        {progress.blockers.map((item) => <li key={`b-${item.code}-${item.source}`}><Icon name="clock" size={14} />{item.text}
          {item.source && store.tasks[item.source] && <> · <button type="button" className="a-link a-bidi" dir="auto" onClick={() => onOpenTask(item.source)}>{clip(store.tasks[item.source].title, 40)}</button></>}</li>)}
        {progress.replanning.map((item) => <li key={`r-${item.code}-${item.source}`}><Icon name="info" size={14} />Its plan may need to change: {item.text.toLowerCase()}</li>)}
      </ul>
    </div>}
    {plan && <div className="a-tblock">
      <h3>Plan{progress.planRevision ? <span className="a-h3-aside">revision {progress.planRevision}</span> : null}</h3>
      <p>{plan.text}</p>
      {plan.rationale && <blockquote className="a-quote a-bidi" dir="auto"><span className="a-quote-by">In the lead’s words (not checked by the Control Center)</span>
        <FoldedText id="t-plan-rationale" text={plan.rationale} noun="explanation" as="span" className="a-quote-text" /></blockquote>}
      {plan.items.length > 0 && <ul className="a-tlist">{plan.items.map((item) => <li key={`${item.id}-${item.revision}`}>
        <Icon name="layers" size={14} /><span>Planned part <span className="a-mono">{item.id}</span>: {item.text}{item.admission ? ` (admitted as: ${item.admission.toLowerCase()})` : ''}</span>
        {item.childTaskId && store.tasks[item.childTaskId] && <> · <button type="button" className="a-link" onClick={() => onOpenTask(item.childTaskId)}>Open its task</button></>}
      </li>)}</ul>}
      {plan.rejected && <p className="a-fine a-tone-alert">The lead’s attempt to start helpers from this plan was refused.</p>}
      {plan.superseded && <p className="a-fine">{plan.superseded}</p>}
      {plan.lastRejection && <p className="a-fine">{plan.lastRejection}</p>}
    </div>}
    {(progress.rounds > 0 || progress.planned > 0 || progress.issues.length > 0) && <dl className="a-kv a-kv--compact">
      {progress.rounds > 0 && <><dt>Rounds with helpers</dt><dd className="a-num">{progress.rounds}</dd></>}
      {progress.planned > 0 && <><dt>Planned, not started</dt><dd className="a-num">{progress.planned}</dd></>}
      {progress.issues.map((issue) => <Fragment key={issue.id}><dt>Helper start</dt><dd>{issue.text}</dd></Fragment>)}
    </dl>}
    <div className="a-tblock">
      <h3>Recorded activity</h3>
      {activity.loading && !activity.data ? <Skeleton lines={3} label="Loading activity" />
        : activity.error ? <p className="a-fine">Activity is unavailable right now.</p>
          : !own ? <p className="a-fine">{activity.data ? 'Its agent has since moved to other work, so its activity here isn’t available.' : 'No agent has worked on this task yet.'}</p>
            : events.length === 0 ? <p className="a-fine">Nothing recorded yet.</p>
              : <ol className="a-tl">{[...events].reverse().map((event) => <li key={event.id} className="a-tl-i">
                <span className="a-tl-d" aria-hidden="true" />
                <span className="a-tl-t a-bidi" dir="auto">{event.label}</span>
                <span className="a-tl-time a-num">{when(event.at) || 'Time not recorded'}</span>
              </li>)}</ol>}
      <p className="a-fine">Recorded by the Control Center for the agent working on this task.</p>
    </div>
  </Section>
}

function Paths({ label, paths, more }) {
  if (!paths.length) return null
  return <Disclosure summary={`${label} (${paths.length + more})`}>
    <ul className="a-paths">{paths.slice(0, 50).map((path) => <li key={path} className="a-mono">{path}</li>)}</ul>
    {more > 0 && <p className="a-fine">and {more} more not listed by the backend.</p>}
  </Disclosure>
}

function IntegrationRow({ entry, empty }) {
  if (!entry) return <p className="a-fine">{empty}</p>
  return <div className={`a-integ${entry.attention ? ' is-attention' : ''}`}>
    <p><b>{entry.text}</b>{entry.appliedAt ? <span className="a-muted"> · {when(entry.appliedAt)}</span> : null}</p>
    {entry.reason && <p className="a-fine">{entry.reason}</p>}
    <Paths label="Changed files" paths={entry.changed} more={entry.changedMore} />
    <Paths label="Conflicting files" paths={entry.conflicts} more={entry.conflictsMore} />
  </div>
}

// The result in five separate statements, never merged into one "done".
export function ResultCard({ result }) {
  const row = (key, title, children) => <div className="a-rrow" key={key}><span className="a-rrow-k">{title}</span><div className="a-rrow-v">{children}</div></div>
  return <Section id="t-result" title="Result">
    {row('reported', 'Reported by the agent', <>
      <p>{result.reported.text}</p>
      {result.reported.summary && <blockquote className="a-quote a-bidi" dir="auto"><span className="a-quote-by">The helper’s summary (its own words)</span>
        <FoldedText id="t-helper-summary" text={result.reported.summary} noun="summary" as="span" className="a-quote-text" /></blockquote>}
    </>)}
    {row('recorded', 'Recorded by the Control Center', <><p>{result.recorded.text}</p>{result.recorded.result && <p className="a-fine">{result.recorded.result}</p>}</>)}
    {row('verified', 'Independently verified', <p className="a-fine">{result.verified.text}</p>)}
    {result.isChild
      ? row('integrated', 'Combined into its lead’s copy', <IntegrationRow entry={result.integrated} empty="Not combined into its lead’s copy yet." />)
      : row('applied', 'Applied to the project', <>
        <IntegrationRow entry={result.applied} empty="Not applied to the project." />
        <p className="a-fine a-apply-note"><Icon name="info" size={13} />Applying to the project isn’t available in Atlas yet. The backend applies a task’s
          changes in one step and can’t show them for review beforehand, and a confirmation alone is not a review of what would change in your files.</p>
      </>)}
    {result.history.length > 1 && <Disclosure summary={`Earlier attempts (${result.history.length - 1})`}>
      <ul className="a-tlist">{result.history.slice(0, -1).reverse().map((entry) => <li key={entry.id}>{entry.kind === 'project' ? 'Project' : 'Lead’s copy'}: {entry.text}{entry.createdAt ? ` · ${when(entry.createdAt)}` : ''}</li>)}</ul>
    </Disclosure>}
  </Section>
}

// Who is responsible now, and everyone who held the task before.
export function AgentCard({ derived, taskId, history, assignments, onOpenWork }) {
  const who = whoView(derived, taskId)
  return <Section id="t-agent" title="Responsible agent">
    <div className="a-tagent">
      {who.agent ? <Avatar name={who.name} color={who.agent.color} tool={who.agent.tool} size={40} removed={who.removed} decorative />
        : <span className="a-avatar-empty a-avatar-empty--lg" aria-hidden="true" />}
      <div className="a-tagent-main">
        <b className="a-bidi" dir="auto">{who.agent ? who.name : 'Nobody is responsible right now'}</b>
        <small>{who.agent ? (who.role === 'current' ? who.detail : `Last agent${who.removed ? ' · removed' : ''} · not working on it now`) : 'No agent holds this task.'}</small>
      </div>
    </div>
    <Button size="sm" icon="open" onClick={onOpenWork}>Agent controls in Work</Button>
    <h3 className="a-tsub">Everyone who held it</h3>
    {assignments.loading && !assignments.data ? <Skeleton lines={2} label="Loading assignments" />
      : assignments.error ? <p className="a-fine">Assignment history is unavailable right now.</p>
        : history.length === 0 ? <p className="a-fine">No agent has worked on this task yet.</p>
          : <ol className="a-hist">{history.map((entry) => <li key={entry.id} className={entry.current ? 'is-current' : 'is-ended'}>
            <Avatar name={entry.agent ? agentLabel(entry.agent) : 'Agent'} color={entry.agent?.color} tool={entry.agent?.tool} size={22} removed={entry.removed} decorative />
            <span className="a-hist-main"><b className="a-bidi" dir="auto">{entry.agent ? agentLabel(entry.agent) : entry.name}</b>
              <small>{entry.text}{entry.endedAt ? ` · ${when(entry.endedAt)}` : entry.assignedAt ? ` · since ${when(entry.assignedAt)}` : ''}</small></span>
            {entry.current && <span className="a-chip a-chip--work">Current</span>}
          </li>)}</ol>}
  </Section>
}

// The task's team: its helper tasks as a List or a Map, from the same model and layout as Work.
export function TeamCard({ store, derived, taskId, projectName, now, complete, onOpenTask, onShowInWork, onLoadMore, loadingMore }) {
  const [lens, setLens] = useState('list')
  const [direction, setDirection] = useState(() => readMapDirection())
  const turnMap = (next) => { setDirection(next); saveMapDirection(next) }
  const [selected, setSelected] = useState(null)
  const [overrides, setOverrides] = useState(() => new Map())
  const [focusId, setFocusId] = useState(null)
  const cameraRef = useRef(null)
  const [limit, setLimit] = useState(TEAM_ROWS)
  // A small team opens fully; a large one shows the direct helpers, and deeper branches open on request.
  const members = useMemo(() => Object.keys(store.tasks).filter((id) => inBranch(derived.forest, taskId, id)), [store, derived, taskId])
  const expanded = useMemo(() => {
    const small = members.length <= 60
    return new Set(members.filter((id) => overrides.get(id) ?? (small || id === taskId)))
  }, [members, taskId, overrides])
  const view = useMemo(() => {
    const rows = visibleRows(store, derived, { expanded, focus: taskId })
    return { ...rows, sections: rows.sections.map((section) => ({ ...section, label: 'This task and its helpers' })) }
  }, [store, derived, expanded, taskId])
  const layout = useMemo(() => (lens === 'map' ? mapLayout(view, derived, { chips: (id, row) => row.hasChildren || store.tasks[id].planned.unmaterialized > 0, direction }) : null),
    [lens, view, derived, store, direction])
  // The List draws the first rows and more on request, so a team of hundreds opens quickly.
  const listView = useMemo(() => (view.shown <= limit ? view : { ...view,
    sections: view.sections.map((section) => ({ ...section, families: section.families.map((family) => ({ ...family, rows: family.rows.slice(0, limit) })) })) }),
  [view, limit])
  const helpers = descendantCount(derived.forest, taskId)
  const reported = store.tasks[taskId]?.childCount ?? 0
  const toggle = (id, open) => setOverrides((current) => new Map(current).set(id, open ?? !expanded.has(id)))
  const pick = (id) => setSelected(id)
  const member = selected && store.tasks[selected] ? selected : null
  const states = { working: 0, waiting: 0, done: 0, other: 0 }
  for (const id of Object.keys(store.tasks)) {
    if (id === taskId || !inBranch(derived.forest, taskId, id)) continue
    const tone = derived.status[id].tone
    if (tone === 'work') states.working += 1
    else if (tone === 'ask' || tone === 'wait') states.waiting += 1
    else if (tone === 'done') states.done += 1
    else states.other += 1
  }
  const planned = store.tasks[taskId]?.planned.unmaterialized ?? 0
  return <Section id="t-team" title="Who’s on it" className="a-tteam"
    aside={helpers > 0 && <Segmented label="Show the team as" size="sm" className="a-lens" value={lens} onChange={setLens}
      options={[{ value: 'list', label: 'List', icon: 'list' }, { value: 'map', label: 'Map', icon: 'map' }]} />}>
    <p className="a-fine">{helpers === 0 ? 'It works alone: no helper tasks.' : `${helpers} helper task${helpers === 1 ? '' : 's'} below it: ${states.working} working, ${states.waiting} waiting, ${states.done} finished${states.other ? `, ${states.other} other` : ''}.`}
      {reported > (derived.forest.children[taskId] || []).length && !complete ? ' Some helpers aren’t loaded yet.' : ''}
      {planned > 0 ? ` ${planned} more planned by the lead, not started (not tasks yet).` : ''}</p>
    {helpers > 0 && <div className={`a-team-body a-team-body--${lens}`}
      style={lens === 'list' ? { height: Math.min(460, 110 + Math.min(view.shown, limit) * 50) } : undefined}>
      {lens === 'list'
        ? <TaskTree view={listView} store={store} derived={derived} projectName={projectName} now={now} selectedId={member} focusId={focusId}
          setFocusId={setFocusId} onSelect={pick} onToggle={toggle} onLoadMore={onLoadMore} loadingMore={loadingMore} empty={<p className="a-ol-empty">Nothing to show.</p>} />
        : <TaskMap layout={layout} view={view} store={store} derived={derived} projectName={projectName} now={now} selectedId={member}
          onSelect={pick} onToggle={toggle} onFocusBranch={onOpenTask} focusBranch={taskId} onDirection={turnMap} cameraRef={cameraRef}
          onLoadMore={onLoadMore} loadingMore={loadingMore} empty={<p className="a-ol-empty">Nothing to show.</p>} />}
    </div>}
    {lens === 'list' && view.shown > limit && <div className="a-act-row">
      <span className="a-fine">Showing {limit} of {view.shown} rows.</span>
      <Button size="sm" icon="down" onClick={() => setLimit((value) => value + TEAM_ROWS)}>Show {Math.min(TEAM_ROWS, view.shown - limit)} more</Button>
    </div>}
    {member && <div className="a-team-pick" role="status">
      <StatusBead state={derived.status[member].bead} decorative />
      <div className="a-team-pick-main"><b className="a-bidi" dir="auto">{store.tasks[member].title || 'Untitled task'}</b>
        <small>{derived.status[member].label}{nowView(store, derived, member).second ? ` · ${nowView(store, derived, member).second}` : ''}
          {' · '}{whoView(derived, member).agent ? `${whoView(derived, member).name}${whoView(derived, member).role === 'last' ? ' (last agent)' : ''}` : 'No agent'}
          {' · '}{timeView(store, member, now).text}</small></div>
      {member !== taskId && <Button size="sm" variant="primary" icon="open" onClick={() => onOpenTask(member)}>Open task</Button>}
      <Button size="sm" onClick={() => onShowInWork(member)}>Show in Work</Button>
    </div>}
    {lens === 'map' && helpers > 0 && <p className="a-fine">Double-click a task in the map to open it.</p>}
  </Section>
}

// Where the task sits: its lead, its project, what it depends on.
export function PlaceCard({ store, taskId, projectName, dependencies, dependents, onOpenTask }) {
  const task = store.tasks[taskId]
  const parent = task?.parentId ? store.tasks[task.parentId] : null
  const deps = dependencies.data?.dependencies || []
  const backs = dependents.data?.dependents || []
  return <Section id="t-place" title="Where it sits">
    <dl className="a-kv">
      <dt>Project</dt><dd className="a-bidi" dir="auto">{projectName(task?.projectId)}</dd>
      <dt>Lead task</dt><dd>{parent ? <button type="button" className="a-link a-bidi" dir="auto" onClick={() => onOpenTask(parent.id)}>{clip(parent.title, 60)}</button>
        : task?.parentId ? 'Not loaded' : 'None: this is a top-level task'}</dd>
      <dt>Depends on</dt><dd>{dependencies.error ? 'Unavailable' : deps.length ? deps.map((item) => item.depends_on_task_id).map((id) => store.tasks[id]
        ? <button key={id} type="button" className="a-link a-bidi a-dep" dir="auto" onClick={() => onOpenTask(id)}>{clip(store.tasks[id].title, 40)}</button>
        : <span key={id} className="a-dep">a task not loaded here</span>) : 'Nothing'}</dd>
      <dt>Needed by</dt><dd>{dependents.error ? 'Unavailable' : backs.length ? `${backs.length} task${backs.length === 1 ? '' : 's'}` : 'Nothing'}</dd>
    </dl>
  </Section>
}

// Diagnostic facts for the technically curious, closed by default.
export function AdvancedCard({ taskId, detail, orchestration, operations, task }) {
  const d = detail.data || {}
  const o = orchestration.data || {}
  const ops = operations.data?.operations || []
  return <Section id="t-adv" title="Advanced">
    <Disclosure summary="Technical details">
      <dl className="a-kv">
        <dt>Task id</dt><dd className="a-mono">{taskId}</dd>
        <dt>Lifecycle</dt><dd>{d.status || task?.lifecycle || 'Not reported'}{d.resume_status ? ` (resumes as ${d.resume_status})` : ''}</dd>
        <dt>Control intent</dt><dd>{d.control_intent || task?.controlIntent || 'Not reported'}</dd>
        <dt>Stop required</dt><dd>{d.stop_required ? 'Yes' : 'No'}{d.stop_reason ? ` · ${d.stop_reason}` : ''}</dd>
        <dt>Resource claims</dt><dd>{d.resource_claims_pending ? 'Waiting for a claim' : 'None waiting'}</dd>
        {o.continuation && <><dt>Continuation</dt><dd>{o.continuation.phase}{o.continuation.mode ? ` · ${o.continuation.mode}` : ''}</dd></>}
        {o.decision && <><dt>Decision</dt><dd>{o.decision.state}{o.decision.decision_key ? ` · key ${o.decision.decision_key}` : ''}{o.decision.source ? ` · from ${o.decision.source}` : ''}</dd></>}
        {o.wake && <><dt>Wake</dt><dd>{o.wake.state}</dd></>}
        {(o.materializations || []).length > 0 && <><dt>Helper starts</dt><dd>{o.materializations.map((item) => `${item.phase} (${item.attempts})`).join(', ')}</dd></>}
        <dt>Control operations</dt><dd>{operations.error ? 'Unavailable' : ops.length ? ops.map((item) => `${item.action} · ${item.status}${item.released ? ' · released' : ''}`).join('; ') : 'None'}</dd>
      </dl>
    </Disclosure>
    {detail.error && <Notice tone="warn" title="Some details couldn’t be read">The page shows what was read last.</Notice>}
  </Section>
}
