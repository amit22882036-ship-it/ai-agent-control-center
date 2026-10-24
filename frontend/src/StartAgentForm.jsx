import { useCallback, useEffect, useId, useRef, useState } from 'react'
import Icon from './Icon'
import ProjectSelection from './ProjectSelection'
import ProviderPanel from './ProviderPanel'
import ModelPicker from './ModelPicker'
import useModelCatalog from './useModelCatalog'
import { chosenConfig, initialChoice, selectionReady } from './modelPresentation.mjs'
import { AUTO_SUMMARY, providerChoices } from './allocationPresentation.mjs'
import { initialAutonomousChoice, autonomyEligible, creationPayload } from './autonomousCreation.mjs'
import { apiFetch } from './apiClient.mjs'
import { PROVIDER_IDS, capabilityReason, providerLabel, selectable, stateLabel, stateOf, stateTone, statusFor } from './providerPresentation.mjs'
import { defaultChoice, defaultText, permissionFor } from './onboardingPresentation.mjs'

// Explicit local checks (installation, version, sign-in, billing route); never a model request.
async function loadProviders() {
  try {
    const response = await apiFetch('http://127.0.0.1:8000/providers/check', { method: 'POST' })
    const data = await response.json().catch(() => ({}))
    return response.ok && Array.isArray(data.providers) ? data.providers : null
  } catch {
    return null
  }
}

// Stage 2G: the global provider permission and the defaults for new root agents.
async function loadPolicy() {
  try {
    const response = await apiFetch('http://127.0.0.1:8000/provider-policy')
    const data = await response.json().catch(() => ({}))
    return response.ok && Array.isArray(data.allowed) ? data : null
  } catch {
    return null
  }
}

export default function StartAgentForm({
  parentId = null,
  onStarted,
  disabled = false,
  onStartingChange,
  onCancel,
  sectionId,
  onOpenProviders,
  visible = true,
}) {
  const id = useId()
  const [task, setTask] = useState('')
  const [project, setProject] = useState(null)
  const [agentType, setAgentType] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [modelChoice, setModelChoice] = useState(initialChoice)
  const [autonomous, setAutonomous] = useState(initialAutonomousChoice)
  const [childAllocation, setChildAllocation] = useState('explicit')
  const [autoProviders, setAutoProviders] = useState([])
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const [statuses, setStatuses] = useState(null)
  const [statusError, setStatusError] = useState('')
  const [policy, setPolicy] = useState(null)
  const [appliedDefault, setAppliedDefault] = useState(null)
  const touched = useRef(false)  // A provider the user picked is never replaced by a default.
  const defaultApplied = useRef(false)
  const unavailable = disabled || starting
  const child = parentId !== null
  const status = statusFor(statuses, agentType)
  const real = agentType !== 'mock'
  const permission = permissionFor(policy, agentType)
  const usable = selectable(status, agentType) && permission.allowed
  const [catalogVersion, setCatalogVersion] = useState(0)
  const models = useModelCatalog(real ? agentType : null, catalogVersion)
  const config = chosenConfig(models.catalog, modelChoice)
  const modelReady = !real || selectionReady(models.catalog, modelChoice)

  const applyProviders = useCallback((result) => {
    if (result) {
      setStatuses(result)
      setStatusError('')
    } else {
      setStatusError('Provider status is unavailable. Mock agents can still start.')
    }
  }, [])
  const refreshProviders = useCallback(() => loadProviders().then((result) => {
    applyProviders(result)
    setCatalogVersion((version) => version + 1)
  }), [applyProviders])
  useEffect(() => {
    let active = true
    loadProviders().then((result) => { if (active) applyProviders(result) })
    return () => { active = false }
  }, [applyProviders])
  // Re-read permissions (and readiness) when the form is shown again, e.g. after setup.
  const wasVisible = useRef(visible)
  useEffect(() => {
    let active = true
    if (visible) {
      loadPolicy().then((result) => {
        if (!active) return
        setPolicy(result)
        // The saved default applies once to a new root agent, as saved: an unusable
        // default is selected and explained, never swapped for another provider or model.
        if (child || !result || touched.current || defaultApplied.current) return
        defaultApplied.current = true
        const choice = defaultChoice(result)
        setAppliedDefault(choice)
        if (choice) {
          setAgentType(choice.provider)
          setModelChoice(initialChoice(choice.model, choice.effort))
        }
      })
      if (!wasVisible.current) refreshProviders()
    }
    wasVisible.current = visible
    return () => { active = false }
  }, [visible, refreshProviders, child])

  async function handleStart(event) {
    event.preventDefault()
    if (!task.trim() || unavailable || (!child && !project) || !usable || !modelReady) return
    setStarting(true)
    onStartingChange?.(true)
    setError('')
    try {
      const path = child ? `/agents/${encodeURIComponent(parentId)}/children/start` : '/agents/start'
      const response = await apiFetch(`http://127.0.0.1:8000${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(creationPayload(task, agentType, sandbox, autonomous, parentId, child ? undefined : project.project_id,
          config.model || undefined, { effort: config.effort, attempt: config.attempt,
            autoProviders: childAllocation === 'auto' ? autoProviders : null })),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to start agent. Check the backend and try again.')
      }
      setTask('')
      if (child) {
        setAgentType('mock')
        setSandbox('read-only')
        setModelChoice(initialChoice())
      }
      onStarted()
    } catch (err) {
      setError(err.message || 'Unable to start agent. Please try again.')
      refreshProviders()
    } finally {
      setStarting(false)
      onStartingChange?.(false)
    }
  }

  const autonomyReason = capabilityReason(status, 'autonomous')
  return (
    <section id={sectionId} className={`start-agent ${child ? 'start-agent-child' : 'start-agent-root'}`} aria-labelledby={`${id}-heading`}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !unavailable && onCancel) {
          event.stopPropagation()
          onCancel()
        }
      }}>
      <div className="start-agent-heading">
        <span className="start-agent-icon"><Icon name="agent" size={18} /></span>
        <div><h2 id={`${id}-heading`}>{child ? 'Start child agent' : 'New agent'}</h2>
          <p>{child ? 'Add a focused agent to this branch.' : 'Describe the assignment, then choose how it should run.'}</p></div>
        {!child && <button type="button" className="close-button composer-close" onClick={onCancel} disabled={unavailable} aria-label="Close new agent composer"><Icon name="close" /></button>}
      </div>
      <form onSubmit={handleStart}>
        {!child && <ProjectSelection disabled={unavailable} onChange={setProject} />}
        <div className="task-field">
          <label htmlFor={`${id}-task`}>Task</label>
          <textarea id={`${id}-task`} value={task} onChange={(event) => setTask(event.target.value)}
            placeholder="What should this agent work on?" rows={3} required disabled={unavailable} autoFocus={!child} />
        </div>
        <fieldset className="start-choice-field">
          <legend>Provider</legend>
          <div className="segmented-control provider-choice">
            {PROVIDER_IDS.map((value) => {
              const item = statusFor(statuses, value)
              const state = value === 'mock' ? 'ready' : stateOf(item)
              return <label key={value}>
                <input type="radio" name={`${id}-type`} value={value} checked={agentType === value}
                  onChange={() => { touched.current = true; setAgentType(value); setModelChoice(initialChoice()) }}
                  disabled={unavailable || !permissionFor(policy, value).allowed}
                  aria-describedby={`${id}-provider-state`} />
                <span title={permissionFor(policy, value).reason || undefined}>{providerLabel(value)}{value !== 'mock' && <small className={`provider-dot provider-${permissionFor(policy, value).allowed ? stateTone(state) : 'blocked'}`}
                  aria-label={permissionFor(policy, value).allowed ? stateLabel(state) : 'Not allowed'} title={permissionFor(policy, value).allowed ? stateLabel(state) : 'Not allowed'} />}</span>
              </label>
            })}
          </div>
          <p id={`${id}-provider-state`} className="agent-type-note" role="status">
            {real && !permission.allowed ? permission.reason
              : real ? `${providerLabel(agentType)}: ${stateLabel(stateOf(status))}.${!usable && status?.state?.reason ? ` ${status.state.reason}` : ''}${!usable && status?.state?.guidance ? ` ${status.state.guidance}` : ''}`
                : 'Mock only simulates work and uses no provider.'}
          </p>
          {appliedDefault && agentType === appliedDefault.provider && <p className={`agent-type-note${appliedDefault.problem ? ' provider-reason' : ''}`}>{defaultText(appliedDefault)}</p>}
          {real && !usable && onOpenProviders && <button type="button" className="close-button provider-setup-link" onClick={onOpenProviders}>
            Set up in AI Providers<Icon name="arrow" size={14} /></button>}
          {statusError && <p className="agent-type-note">{statusError}</p>}
        </fieldset>
        {real && (
          <fieldset className="start-choice-field sandbox-field">
            <legend>Access</legend>
            <div className="segmented-control">
              {[['read-only', 'Read only'], ['workspace-write', 'Workspace write']].map(([value, label]) => <label key={value}>
                <input type="radio" name={`${id}-sandbox`} value={value} checked={sandbox === value}
                  onChange={() => setSandbox(value)} disabled={unavailable}
                  aria-describedby={value === 'workspace-write' ? `${id}-warning` : undefined} />
                <span>{label}</span>
              </label>)}
            </div>
            {sandbox === 'workspace-write' && (
              <p id={`${id}-warning`} className="agent-type-note" role="status">Can modify files in its task workspace.</p>
            )}
          </fieldset>
        )}
        {real && (
          <div className="task-field model-field">
            <ModelPicker provider={agentType} catalog={models.catalog} catalogError={models.error} refreshing={models.refreshing}
              onRefresh={models.refresh} value={modelChoice} onChange={setModelChoice} disabled={unavailable} label="Model (this agent's default)" />
          </div>
        )}
        {!child && <div className="autonomous-option">
          <label htmlFor={`${id}-autonomous`}><span><strong>Autonomous mode</strong><span className="agent-type-note">Plan work, create sub-agents and adapt the plan.</span></span>
            <input id={`${id}-autonomous`} type="checkbox" role="switch" checked={autonomyEligible(agentType) && autonomous}
              disabled={unavailable || !autonomyEligible(agentType)} onChange={(event) => setAutonomous(event.target.checked)} aria-describedby={`${id}-autonomous-help`} />
          </label>
          <p id={`${id}-autonomous-help`} className="agent-type-note">{!autonomyEligible(agentType)
            ? (agentType === 'mock' ? 'Available for new Codex and Claude Code root agents. Mock only simulates work.'
              : autonomyReason || 'Not available for this provider.')
            : autonomous ? 'May create additional agents and use more provider usage. Set at creation only.' : 'Runs without autonomous planning or sub-agent creation.'}</p>
          {autonomyEligible(agentType) && autonomous && <fieldset className="start-choice-field allocation-choice">
            <legend>Child agents</legend>
            <div className="segmented-control">
              {[['explicit', 'Explicit provider/model'], ['auto', 'Auto — best suitable']].map(([value, label]) => <label key={value}>
                <input type="radio" name={`${id}-allocation`} value={value} checked={childAllocation === value} disabled={unavailable}
                  onChange={() => { setChildAllocation(value); if (value === 'auto' && autoProviders.length === 0) setAutoProviders([agentType]) }} />
                <span>{label}</span></label>)}
            </div>
            <p className="agent-type-note">{childAllocation === 'auto' ? `${AUTO_SUMMARY} Model allowlists, preferences and suitability profiles can be set in the agent inspector after creation.`
              : "Children use the provider the plan requests, or this agent's; an unavailable one is never replaced."}</p>
            {childAllocation === 'auto' && <div className="allocation-providers" role="group" aria-label="Providers Auto may use">
              {providerChoices(statuses).map((choice) => <label key={choice.id} className="attestation-check">
                <input type="checkbox" checked={autoProviders.includes(choice.id)} disabled={unavailable || !permissionFor(policy, choice.id).allowed}
                  onChange={(event) => setAutoProviders(event.target.checked ? [...autoProviders, choice.id]
                    : autoProviders.filter((item) => item !== choice.id))} />
                <span>{choice.label}{!permissionFor(policy, choice.id).allowed ? ' (not allowed)' : choice.ready ? '' : ` (${choice.state.replace('_', ' ')})`}</span></label>)}
            </div>}
          </fieldset>}
        </div>}
        <ProviderPanel statuses={statuses} onChanged={refreshProviders} disabled={unavailable} />
        <div className="start-agent-footer">
          <p>{real ? `${providerLabel(agentType)} · ${sandbox === 'workspace-write' ? 'Workspace write' : 'Read only'}${config.model ? ` · ${config.model}` : ''}${config.effort ? ` · ${config.effort} effort` : ''}` : 'Mock agent · Simulated work'}</p>
          <div className="start-agent-actions">
            {!child && <button className="close-button" type="button" onClick={onCancel} disabled={unavailable}>Cancel</button>}
            <button className="start-button" type="submit" disabled={unavailable || !task.trim() || (!child && !project) || !usable || !modelReady
              || (autonomous && autonomyEligible(agentType) && childAllocation === 'auto' && autoProviders.length === 0)}>
              {starting ? 'Starting...' : child ? 'Start child agent' : 'Start agent'}<Icon name="arrow" size={15} />
            </button>
          </div>
        </div>
      </form>
      {error && <p className="message error" role="alert">{error}</p>}
    </section>
  )
}
