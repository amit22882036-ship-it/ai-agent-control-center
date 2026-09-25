import Icon from './Icon'
import { useEffect, useRef, useState } from 'react'
import { createRefreshQueue } from './agentEventStream.mjs'
import AgentOutput from './AgentOutput'
import AgentNameEditor from './AgentNameEditor'
import AgentColorPicker from './AgentColorPicker'
import AgentNameHistory from './AgentNameHistory'
import { agentName, statusLabels } from './agentPresentation.mjs'

function AgentDetails({ agentId, onClose, onStopped, refreshVersion, agents, onSelect }) {
  const [outputOpened, setOutputOpened] = useState(false)
  const panelRef = useRef(null)
  useEffect(() => {
    const previous = document.activeElement
    panelRef.current?.focus()
    return () => { if (previous?.isConnected) previous.focus() }
  }, [agentId])
  const refreshRef = useRef(null)
  const [detailsVersion, setDetailsVersion] = useState(0)
  const [agent, setAgent] = useState(null)
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  const [instruction, setInstruction] = useState('')
  const [redirecting, setRedirecting] = useState(false)
  const [redirectError, setRedirectError] = useState('')
  const [answer, setAnswer] = useState('')
  const [replying, setReplying] = useState(false)
  const [replyError, setReplyError] = useState('')
  const [deciding, setDeciding] = useState(false)
  const [decideError, setDecideError] = useState('')

  const [similarAction, setSimilarAction] = useState('')
  const [similarError, setSimilarError] = useState('')
  const [alwaysAction, setAlwaysAction] = useState('')
  const [alwaysError, setAlwaysError] = useState('')
  const [stoppingBranch, setStoppingBranch] = useState(false)
  const [branchError, setBranchError] = useState('')
  const busy = stoppingBranch || stopping || redirecting || replying || deciding || Boolean(similarAction) || Boolean(alwaysAction)

  async function handleStopBranch() {
    if (busy || !window.confirm('Stop this agent and all of its descendants? This does not affect its parent or siblings outside this branch.')) return
    setStoppingBranch(true)
    setBranchError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/stop-branch`, { method: 'POST' })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to stop branch. Please try again.')
      }
      if (result.ok !== true) {
        const failures = (result.failures ?? []).map((failure) => `${failure.agent_id}: ${failure.error}`).join('; ')
        setBranchError(`Branch stopping was only partially successful. ${failures}`)
      }
    } catch (err) {
      setBranchError(err.message)
    } finally {
      setStoppingBranch(false)
      setDetailsVersion((version) => version + 1)
      onStopped()
    }
  }

  async function handleSimilar(disable = false) {
    if (busy) return
    setSimilarAction(disable ? 'disabling' : 'enabling')
    setSimilarError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/decide-similar${disable ? '/disable' : ''}`, {
        method: 'POST',
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to change similar decisions. Please try again.')
      }
      setAgent((current) => ({
        ...current,
        similar_decisions_enabled: !disable,
        ...(disable ? {} : { status: result.status, waiting_question: null }),
      }))
      if (!disable) setAnswer('')
      onStopped()
    } catch (err) {
      setSimilarError(err.message)
    } finally {
      setSimilarAction('')
    }
  }

  async function handleAlways(disable = false) {
    if (busy) return
    setAlwaysAction(disable ? 'disabling' : 'enabling')
    setAlwaysError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/decide-always${disable ? '/disable' : ''}`, {
        method: 'POST',
      })
      const result = await response.json().catch(() => ({}))
      if (!response.ok) {
        throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to change Always Decide. Please try again.')
      }
      setAgent((current) => ({
        ...current,
        always_decide_enabled: !disable,
        ...(disable ? {} : { status: result.status, waiting_question: null }),
      }))
      if (!disable) setAnswer('')
      onStopped()
    } catch (err) {
      setAlwaysError(err.message)
    } finally {
      setAlwaysAction('')
    }
  }

  async function handleDecide() {
    if (busy) return
    setDeciding(true)
    setDecideError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/decide`, {
        method: 'POST',
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        const message = typeof data.detail === 'string'
          ? data.detail
          : Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(' ') : ''
        throw new Error(message || 'Unable to delegate this decision. Please try again.')
      }
      const result = await response.json()
      setAnswer('')
      setAgent((current) => ({ ...current, status: result.status, waiting_question: null }))
      onStopped()
    } catch (err) {
      setDecideError(err.message)
    } finally {
      setDeciding(false)
    }
  }

  async function handleReply(event) {
    event.preventDefault()
    if (!answer.trim() || busy) return
    setReplying(true)
    setReplyError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/reply`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ answer }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        const message = typeof data.detail === 'string'
          ? data.detail
          : Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(' ') : ''
        throw new Error(message || 'Unable to send reply. Please try again.')
      }
      const result = await response.json()
      setAnswer('')
      setAgent((current) => ({ ...current, status: result.status, waiting_question: null }))
      onStopped()
    } catch (err) {
      setReplyError(err.message)
    } finally {
      setReplying(false)
    }
  }

  async function handleRedirect(event) {
    event.preventDefault()
    if (!instruction.trim() || busy) return
    setRedirecting(true)
    setRedirectError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/redirect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ instruction }),
      })
      if (!response.ok) {
        const data = await response.json().catch(() => ({}))
        const message = typeof data.detail === 'string'
          ? data.detail
          : Array.isArray(data.detail) ? data.detail.map((item) => item.msg).join(' ') : ''
        throw new Error(message || 'Unable to redirect agent. Please try again.')
      }
      setInstruction('')
    } catch (err) {
      setRedirectError(err.message)
    } finally {
      // Restart polling immediately to fetch the replacement process's details.
      setRedirecting(false)
    }
  }

  async function handleStop() {
    if (busy) return
    setStopping(true)
    setStopError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}/stop`, {
        method: 'POST',
      })
      if (!response.ok) {
        throw new Error(response.status === 404
          ? 'Agent not found.'
          : 'Unable to stop agent. Please try again.')
      }
      const result = await response.json()
      setAgent((current) => ({ ...current, status: result.status }))
      onStopped()
    } catch (err) {
      setStopError(err.message)
    } finally {
      setStopping(false)
    }
  }

  useEffect(() => {
    if (busy) return
    const controller = new AbortController()

    async function fetchDetails() {
      try {
        const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}?include_output=false`, {
          signal: controller.signal,
        })
        if (!response.ok) {
          throw new Error(response.status === 404
            ? 'Agent not found.'
            : 'Unable to load agent details.')
        }
        const data = await response.json()
        if (!controller.signal.aborted) {
          setAgent(data)
          setError('')
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setError(`${err.message} Retrying automatically.`)
        }
        return false
      }
    }

    const queue = createRefreshQueue(fetchDetails)
    refreshRef.current = queue.request
    return () => {
      refreshRef.current = null
      queue.dispose()
      controller.abort()
    }
  }, [agentId, busy])

  useEffect(() => {
    refreshRef.current?.(true)
  }, [agentId, busy, detailsVersion, refreshVersion])

  return (
    <aside ref={panelRef} tabIndex={-1} onKeyDown={(event) => { if (event.key === 'Escape') onClose() }} className="agent-details" aria-labelledby="details-heading">
      <div className="details-header">
        <h2 id="details-heading">{agent ? agentName(agent) : 'Agent details'}</h2>
        {agent && <span className={`status status-${agent.status}`}>{statusLabels[agent.status] || agent.status}</span>}
        <div className="details-actions">
          <button className="close-button" type="button" onClick={onClose} aria-label="Close inspector" title="Close inspector"><Icon name="close" /></button>
        </div>
        {agent && <div className="identity-controls">
          <AgentNameEditor agent={agent} disabled={busy} onSaved={(name) => { setAgent((current) => ({ ...current, display_name: name })); setDetailsVersion((version) => version + 1); onStopped() }} />
          <AgentColorPicker agent={agent} onSaved={(color) => { setAgent((current) => ({ ...current, display_color: color })); onStopped() }} />
        </div>}
      </div>
      {branchError && <p className="message error" role="alert">{branchError}</p>}
      {stopError && <p className="message error" role="alert">{stopError}</p>}
      {redirectError && <p className="message error" role="alert">{redirectError}</p>}
      {replyError && <p className="message error" role="alert">{replyError}</p>}
      {decideError && <p className="message error" role="alert">{decideError}</p>}
      {alwaysError && <p className="message error" role="alert">{alwaysError}</p>}
      {similarError && <p className="message error" role="alert">{similarError}</p>}
      {error && <p className="message error" role="alert">{error}</p>}
      {!agent && !error && <p role="status">Loading agent details...</p>}
      {agent && (
        <>
          {error && <p>Showing last received details.</p>}
          <details className="detail-section overview-section" open><summary>Overview</summary>
          <details className="assignment-disclosure"><summary>Assignment</summary><p className="task">{agent.task}</p></details>
          {agent.agent_type === 'codex' && agent.status === 'waiting' && (
            <section className="waiting-panel" aria-labelledby="waiting-heading">
              <h3 id="waiting-heading">Waiting for you</h3>
              <p className="waiting-question">{agent.waiting_question}</p>
              <form className="redirect-form" onSubmit={handleReply}>
                <label htmlFor="reply-answer">Your answer</label>
                <textarea
                  id="reply-answer"
                  value={answer}
                  onChange={(event) => setAnswer(event.target.value)}
                  rows={3}
                  required
                  disabled={busy}
                />
                <button className="start-button" type="submit"
                  disabled={busy || !answer.trim()}>
                  {replying ? 'Sending...' : 'Reply'}
                </button>
                <button className="close-button" type="button" onClick={handleDecide}
                  disabled={busy} aria-describedby="decide-help">
                  {deciding ? 'Deciding...' : 'Decide for me'}
                </button>
                <details className="delegation-options"><summary>Delegate future decisions</summary>
                <button className="close-button" type="button" disabled={busy}
                  onClick={() => handleSimilar()} aria-describedby="similar-help">
                  {similarAction === 'enabling' ? 'Enabling...' : 'Decide similar automatically'}
                </button>
                <p id="similar-help" className="agent-type-note">
                  Let the agent handle this decision and materially similar decisions for this agent automatically.
                </p>
                {!agent.always_decide_enabled && (
                  <>
                    <button className="close-button" type="button" disabled={busy}
                      onClick={() => handleAlways()} aria-describedby="always-help">
                      {alwaysAction === 'enabling' ? 'Enabling...' : 'Always decide for this agent'}
                    </button>
                    <p id="always-help" className="agent-type-note">
                      Let this agent make future decisions itself when possible. It can still ask when genuinely missing information.
                    </p>
                  </>
                )}
                </details>
                <p id="decide-help" className="agent-type-note">
                  Let the agent make this decision itself and continue. This applies only this time.
                </p>
              </form>
            </section>
          )}
          </details>
          {(agent.parent_id || agent.child_ids?.length > 0) && <details className="detail-section"><summary>Relationships</summary>
            {agent.parent_id && <p>Parent <button onClick={() => onSelect(agent.parent_id)}>{agentName(agents.find((item) => item.agent_id === agent.parent_id))}</button></p>}
            {agent.child_ids?.map((id) => <button className="relationship-link" key={id} onClick={() => onSelect(id)}>{agentName(agents.find((item) => item.agent_id === id))}<span>Child agent <Icon name="arrow" size={12} /></span></button>)}
          </details>}

          <details className="detail-section"><summary>Actions</summary>
          <div className="details-actions">
          {(agent?.status === 'running' || agent?.status === 'waiting') && (
            <button className="stop-button" type="button" onClick={handleStop} disabled={busy}>
              {stopping ? 'Stopping...' : 'Stop'}
            </button>
          )}
          {agent?.child_ids?.length > 0 && (
            <button className="stop-button" type="button" onClick={handleStopBranch} disabled={busy}>
              {stoppingBranch ? 'Stopping branch...' : 'Stop branch'}
            </button>
          )}
          </div>
          {agent.agent_type === 'codex' && agent.always_decide_enabled && (
            <div className="waiting-panel">
              <p>Always decide for this agent: On</p>
              <p className="agent-type-note">Always Decide takes precedence over Similar Decisions. The agent can still ask for genuinely missing information.</p>
              <button className="close-button" type="button" disabled={busy} onClick={() => handleAlways(true)}>
                {alwaysAction === 'disabling' ? 'Turning off...' : 'Turn off'}
              </button>
            </div>
          )}
          {agent.agent_type === 'codex' && agent.similar_decisions_enabled && (
            <div className="waiting-panel">
              <p>Auto-decide similar questions: {agent.always_decide_enabled ? 'Saved (Always Decide is active)' : 'On'}</p>
              <button className="close-button" type="button" disabled={busy} onClick={() => handleSimilar(true)}>
                {similarAction === 'disabling' ? 'Turning off...' : 'Turn off'}
              </button>
            </div>
          )}
          {agent.agent_type === 'codex' && agent.status === 'running' && (
            agent.session_id ? (
              <details className="detail-section"><summary>Redirect this agent</summary><form className="redirect-form" onSubmit={handleRedirect}>
                <label htmlFor="redirect-instruction">Redirect agent</label>
                <textarea
                  id="redirect-instruction"
                  value={instruction}
                  onChange={(event) => setInstruction(event.target.value)}
                  placeholder="Describe the correction for this agent"
                  rows={3}
                  required
                  disabled={busy}
                />
                <button className="start-button" type="submit"
                  disabled={busy || !instruction.trim()}>
                  {redirecting ? 'Sending...' : 'Send'}
                </button>
              </form></details>
            ) : <p className="agent-type-note">Redirect will be available once the Codex session starts.</p>
          )}
          </details>
          <details className="detail-section" onToggle={(event) => { if (event.currentTarget.open) setOutputOpened(true) }}><summary>Output history</summary>
            {outputOpened && <AgentOutput key={agentId} agentId={agentId} refreshVersion={refreshVersion} />}
          </details>
          <AgentNameHistory agentId={agentId} refreshVersion={`${refreshVersion}-${detailsVersion}`} />
          <details className="detail-section advanced"><summary>Advanced information</summary><dl>
            <dt>Agent UUID</dt><dd>{agentId}</dd><dt>Codex session</dt><dd>{agent.session_id || 'Not available'}</dd>
            <dt>Agent type</dt><dd>{agent.agent_type}</dd><dt>Sandbox</dt><dd>{agent.sandbox || 'Not applicable'}</dd>
            <dt>Internal status</dt><dd>{agent.status}</dd>
          </dl></details>
        </>
      )}
    </aside>
  )
}

export default AgentDetails
