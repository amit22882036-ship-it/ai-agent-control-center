import { useId, useState } from 'react'
import Icon from './Icon'
import ProjectSelection from './ProjectSelection'
import { initialAutonomousChoice, autonomyEligible, creationPayload } from './autonomousCreation.mjs'
import { apiFetch } from './apiClient.mjs'

export default function StartAgentForm({
  parentId = null,
  onStarted,
  disabled = false,
  onStartingChange,
  onCancel,
  sectionId,
}) {
  const id = useId()
  const [task, setTask] = useState('')
  const [project, setProject] = useState(null)
  const [agentType, setAgentType] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [autonomous, setAutonomous] = useState(initialAutonomousChoice)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState('')
  const unavailable = disabled || starting
  const child = parentId !== null

  async function handleStart(event) {
    event.preventDefault()
    if (!task.trim() || unavailable || (!child && !project)) return
    setStarting(true)
    onStartingChange?.(true)
    setError('')
    try {
      const path = child ? `/agents/${encodeURIComponent(parentId)}/children/start` : '/agents/start'
      const response = await apiFetch(`http://127.0.0.1:8000${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(creationPayload(task, agentType, sandbox, autonomous, parentId, child ? undefined : project.project_id)),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        throw new Error(typeof data.detail === 'string' ? data.detail : 'Unable to start agent. Check the backend and try again.')
      }
      setTask('')
      if (child) {
        setAgentType('mock')
        setSandbox('read-only')
      }
      onStarted()
    } catch (err) {
      setError(err.message || 'Unable to start agent. Please try again.')
    } finally {
      setStarting(false)
      onStartingChange?.(false)
    }
  }

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
          <legend>Agent type</legend>
          <div className="segmented-control">
            {['mock', 'codex'].map((value) => <label key={value}>
              <input type="radio" name={`${id}-type`} value={value} checked={agentType === value}
                onChange={() => setAgentType(value)} disabled={unavailable} />
              <span>{value === 'mock' ? 'Mock' : 'Codex'}</span>
            </label>)}
          </div>
        </fieldset>
        {agentType === 'codex' && (
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
              <p id={`${id}-warning`} className="agent-type-note" role="status">Can modify files in this project.</p>
            )}
          </fieldset>
        )}
        {!child && <div className="autonomous-option">
          <label htmlFor={`${id}-autonomous`}><span><strong>Autonomous mode</strong><span className="agent-type-note">Plan work, create sub-agents and adapt the plan.</span></span>
            <input id={`${id}-autonomous`} type="checkbox" role="switch" checked={autonomyEligible(agentType) && autonomous}
              disabled={unavailable || !autonomyEligible(agentType)} onChange={(event) => setAutonomous(event.target.checked)} aria-describedby={`${id}-autonomous-help`} />
          </label>
          <p id={`${id}-autonomous-help`} className="agent-type-note">{!autonomyEligible(agentType)
            ? 'Available for new Codex root agents. Mock only simulates work.'
            : autonomous ? 'May create additional agents and use more provider usage. Set at creation only.' : 'Runs without autonomous planning or sub-agent creation.'}</p>
        </div>}
        <div className="start-agent-footer">
          <p>{agentType === 'codex' ? `Codex · ${sandbox === 'workspace-write' ? 'Workspace write' : 'Read only'}` : 'Mock agent · Simulated work'}</p>
          <div className="start-agent-actions">
            {!child && <button className="close-button" type="button" onClick={onCancel} disabled={unavailable}>Cancel</button>}
            <button className="start-button" type="submit" disabled={unavailable || !task.trim() || (!child && !project)}>
              {starting ? 'Starting...' : child ? 'Start child agent' : 'Start agent'}<Icon name="arrow" size={15} />
            </button>
          </div>
        </div>
      </form>
      {error && <p className="message error" role="alert">{error}</p>}
    </section>
  )
}
