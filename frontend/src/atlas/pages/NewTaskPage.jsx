import { useEffect, useMemo, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { Button } from '../components/primitives'
import { ProjectPicker } from '../components/create/projects'
import { LeadSettings, ToolCards } from '../components/create/LeadConfig'
import { HelpersSection, StartCard } from '../components/create/NewTaskParts'
import { useCreationContext } from '../components/create/useCreationContext'
import useModelCatalog from '../../useModelCatalog'
import { API_BASE, apiFetch } from '../../apiClient.mjs'
import { useLiveResource } from '../hooks'
import { agentActions, OFFLINE_MESSAGE, refusalText } from '../api/agentClient.mjs'
import { projectDirectory } from '../model/projects.mjs'
import { TEMPLATES, addTemplate, draftView, emptyDraft, newTaskPayload } from '../model/newTask.mjs'
import { initialChoice } from '../../modelPresentation.mjs'
import { defaultChoice, defaultText, submissionProblem } from '../../onboardingPresentation.mjs'
import { overlayOpen } from '../keys.mjs'

// The task an agent was started for (GET /agents/{id} reports it), to open it in Work.
async function taskOf(agentId) {
  try {
    const response = await apiFetch(`${API_BASE}/agents/${encodeURIComponent(agentId)}?include_output=false`, { cache: 'no-store' })
    const data = await response.json().catch(() => ({}))
    return response.ok && Array.isArray(data.task_ids) && typeof data.task_ids[0] === 'string' ? data.task_ids[0] : null
  } catch { return null }
}

// New task (approved Atlas V2 layout): configuration on the left, a sticky "Before you start" card
// with the summary and Start on the right. Creation uses POST /agents/start with the classic payload.
// An unfinished New task survives leaving the page (Back to Work, N, a Connections link) for the
// rest of this page session: the description and every choice the person made (project, tool,
// model and effort, access, helpers and allocation), but not a saved default applied for them. It
// is kept in memory only (never on disk), survives a failed or uncertain start, and is forgotten
// once the task starts or with Start over. A project named in the address wins.
let kept = null

export default function NewTaskPage({ live, navigate, route }) {
  const [restored] = useState(() => {
    if (kept === false) kept = null
    return Boolean(kept)
  })
  const routeProject = route?.params?.project || null
  const [draft, setDraft] = useState(() => (restored ? { ...kept.draft, projectId: routeProject || kept.draft.projectId }
    : { ...emptyDraft(), projectId: routeProject }))
  // Whether the person changed anything themself; a saved default applied for them doesn't count.
  const [edited, setEdited] = useState(restored)
  const [starting, setStarting] = useState(false)
  const [outcome, setOutcome] = useState(null)
  const [projectsVersion, setProjectsVersion] = useState(0)
  const [appliedDefault, setAppliedDefault] = useState(() => (restored ? kept.appliedDefault : null))
  const [touched, setTouched] = useState(() => (restored ? kept.touched : false)) // a tool the person picked is never replaced by a default
  const [showRestored, setShowRestored] = useState(restored)
  useEffect(() => { if (kept !== false) kept = edited ? { draft, touched, appliedDefault } : null }, [draft, touched, appliedDefault, edited])
  const startingRef = useRef(false)
  // After a start whose outcome is unknown, another start needs an explicit "start it anyway".
  const [uncertain, setUncertain] = useState(false)
  const context = useCreationContext(live.version)
  const projectsResource = useLiveResource(`${API_BASE}/projects`, `${live.version}-${projectsVersion}`)
  const directory = useMemo(() => projectDirectory(projectsResource), [projectsResource])
  const real = draft.provider !== 'mock'
  const models = useModelCatalog(real ? draft.provider : null)
  const view = draftView(draft, { policy: context.policy, statuses: context.statuses, catalog: models.catalog, projects: projectsResource.data?.projects || [] })
  const update = (patch) => { setEdited(true); setDraft((current) => ({ ...current, ...patch })) }
  const startOver = () => {
    setDraft({ ...emptyDraft(), projectId: routeProject }); setTouched(false); setAppliedDefault(null); setEdited(false); setShowRestored(false)
  }

  // The saved default applies once, as saved: an unusable default is selected and explained, never swapped.
  const policy = context.policy
  if (policy && !touched && appliedDefault === null) {
    const choice = defaultChoice(policy)
    setAppliedDefault(choice || false)
    if (choice) setDraft((current) => ({ ...current, provider: choice.provider, picker: initialChoice(choice.model, choice.effort) }))
  }

  const blocked = view.problem || (uncertain ? 'The last start may have gone through. Look in Work first, or choose Start it anyway.' : null)

  async function start(anyway = false) {
    if (startingRef.current || view.problem || (uncertain && !anyway)) return
    startingRef.current = true
    setUncertain(false)
    setStarting(true)
    setOutcome(null)
    try {
      const autoChosen = view.auto ? draft.autoProviders : []
      if (view.real || autoChosen.length) {
        // Fresh permissions first: a choice made under an older policy is not sent.
        const problem = submissionProblem(await context.refreshPolicy(), draft.provider, autoChosen)
        if (problem) { setOutcome({ tone: 'error', text: problem }); return }
      }
      const result = await agentActions.startRoot(newTaskPayload(draft, view))
      if (result.offline) {
        setUncertain(true)
        setOutcome({ tone: 'warn', text: `${OFFLINE_MESSAGE} The task may or may not have started; look in Work before starting it again.`, work: true, anyway: true })
        return
      }
      if (!result.ok) {
        setOutcome({ tone: 'error', text: refusalText(result, 'Unable to start agent. Check the backend and try again.') })
        context.refreshProviders()
        return
      }
      const agentId = result.data?.agent_id
      const taskId = agentId ? await taskOf(agentId) : null
      // Started: the draft is done. `false` stops this page from keeping it again as it leaves.
      kept = false
      if (taskId) { navigate('work', { task: taskId }); return }
      kept = null
      setOutcome({ tone: 'ok', text: 'The task started, but it could not be opened automatically. It appears in Work.', work: true })
      setDraft(emptyDraft())
      setEdited(false)
      setTouched(false)
    } finally {
      startingRef.current = false
      setStarting(false)
    }
  }

  // Ctrl+Enter (Cmd+Enter) starts the task from the New Task page itself: never while a dialog,
  // popover or menu is open (registering a project, choosing one), never from the app's header,
  // and never twice (start() keeps its own guard).
  const pageRef = useRef(null)
  useEffect(() => {
    const onKey = (event) => {
      if (!(event.ctrlKey || event.metaKey) || event.key !== 'Enter' || event.repeat || event.isComposing || event.defaultPrevented) return
      if (shortcutBlocked(event.target, pageRef.current)) return
      event.preventDefault()
      start()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  const outcomeView = outcome && <div className={`a-result a-result--${outcome.tone === 'error' ? 'error' : outcome.tone === 'warn' ? 'warn' : 'ok'}`}
    role={outcome.tone === 'error' ? 'alert' : 'status'}><Icon name={outcome.tone === 'error' ? 'warn' : 'info'} size={15} />
    <div>{outcome.text}{outcome.work && <> <button type="button" className="a-link" onClick={() => navigate('work')}>Open Work</button></>}
      {outcome.anyway && uncertain && <> · <button type="button" className="a-link" onClick={() => start(true)}>Start it anyway</button></>}</div></div>

  return <div className="a-page a-page--wide" ref={pageRef}>
    <div className="a-crumbs"><button type="button" className="a-link" onClick={() => navigate('work')}><Icon name="left" size={13} stroke={2.4} />Back to Work</button></div>
    <h1 className="a-title" tabIndex={-1} data-page-heading>New task</h1>
    <p className="a-subtitle">Describe what you want done. The agent works in a private copy of your project and checks in when something is unclear.</p>
    <div className="a-nt2">
      <div className="a-nt-main">
        <div className="a-write">
          <label className="a-sr" htmlFor="nt-text">What should be done?</label>
          {showRestored && <p className="a-fine a-nt-restored" role="status">Your unfinished task is as you left it.{' '}
            <button type="button" className="a-link" disabled={starting} onClick={startOver}>Start over</button></p>}
          <textarea id="nt-text" dir="auto" value={draft.text} disabled={starting} maxLength={100000}
            placeholder="For example: Fix the flaky timeout in test_exporter.py and explain the cause." onChange={(event) => update({ text: event.target.value })} />
          <div className="a-starts"><span>Start from:</span>
            {TEMPLATES.map((template) => <button key={template.id} type="button" disabled={starting}
              onClick={() => update({ text: addTemplate(draft.text, template) })}>{template.label}</button>)}</div>
        </div>
        <ProjectPicker directory={directory} value={draft.projectId} disabled={starting} onChange={(projectId) => update({ projectId })}
          onRefresh={() => setProjectsVersion((value) => value + 1)}
          onRegistered={(project) => { setProjectsVersion((value) => value + 1); if (project?.project_id) update({ projectId: project.project_id }) }} />
        <section className="a-nt-sec" aria-labelledby="nt-lead">
          <div className="a-nt-sh"><h2 id="nt-lead">Lead agent</h2><span>Responsible for the whole task</span></div>
          <ToolCards value={draft.provider} policy={context.policy} statuses={context.statuses} disabled={starting}
            onChange={(provider) => { setTouched(true); update({ provider, picker: initialChoice() }) }} />
          {appliedDefault && draft.provider === appliedDefault.provider && <p className={`a-fine${appliedDefault.problem ? ' a-tone-alert' : ''}`}>{defaultText(appliedDefault)}</p>}
          {context.policy === null && <p className="a-fine a-tone-alert">Your provider permissions couldn’t be loaded, so no real provider can be chosen yet.{' '}
            <Button size="sm" variant="ghost" onClick={() => context.refreshPolicy()}>Try again</Button></p>}
          {context.statusError && <p className="a-fine">{context.statusError}</p>}
          <LeadSettings provider={draft.provider} models={models} picker={draft.picker} onPicker={(picker) => update({ picker })}
            sandbox={draft.sandbox} onSandbox={(sandbox) => update({ sandbox, fullConfirmed: false })} disabled={starting}
            fullConfirmed={draft.fullConfirmed} onFullConfirmed={(fullConfirmed) => update({ fullConfirmed })}
            helpers={view.helpers} helperAccess={draft.helperAccess} onHelperAccess={(helperAccess) => update({ helperAccess })}
            external={draft.external === true} onExternal={(external) => update({ external })} />
        </section>
        <HelpersSection draft={draft} view={view} update={update} policy={context.policy} statuses={context.statuses} disabled={starting} />
      </div>
      <StartCard draft={draft} view={{ ...view, problem: blocked }} starting={starting} onStart={() => start()} onCancel={() => navigate('work')} outcome={outcomeView} />
    </div>
  </div>
}

// An overlay anywhere on screen, or focus outside the page (the header, a toast), blocks the shortcut.
function shortcutBlocked(target, page) {
  if (!page) return true
  if (overlayOpen()) return true
  const element = target instanceof Element ? target : null
  if (!element || element === document.body || element === document.documentElement) return false
  return !page.contains(element)
}
