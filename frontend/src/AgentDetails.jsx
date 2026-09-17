import { useEffect, useState } from 'react'

function AgentDetails({ agentId, onClose, onStopped }) {
  const [agent, setAgent] = useState(null)
  const [error, setError] = useState('')
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  const [instruction, setInstruction] = useState('')
  const [redirecting, setRedirecting] = useState(false)
  const [redirectError, setRedirectError] = useState('')

  async function handleRedirect(event) {
    event.preventDefault()
    if (!instruction.trim() || redirecting || stopping) return
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
    if (stopping || redirecting) return
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
    if (stopping || redirecting) return
    const controller = new AbortController()
    let fetching = false

    async function fetchDetails() {
      if (fetching) return
      fetching = true
      try {
        const response = await fetch(`http://127.0.0.1:8000/agents/${agentId}`, {
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
      } finally {
        fetching = false
      }
    }

    fetchDetails()
    const interval = setInterval(fetchDetails, 2000)
    return () => {
      clearInterval(interval)
      controller.abort()
    }
  }, [agentId, stopping, redirecting])

  return (
    <section className="agent-details" aria-labelledby="details-heading">
      <div className="details-header">
        <h2 id="details-heading">Agent {agentId} details</h2>
        <div className="details-actions">
          {agent?.status === 'running' && (
            <button className="stop-button" type="button" onClick={handleStop} disabled={stopping || redirecting}>
              {stopping ? 'Stopping...' : 'Stop'}
            </button>
          )}
          <button className="close-button" type="button" onClick={onClose}>Close</button>
        </div>
      </div>
      {stopError && <p className="message error" role="alert">{stopError}</p>}
      {redirectError && <p className="message error" role="alert">{redirectError}</p>}
      {error && <p className="message error" role="alert">{error}</p>}
      {!agent && !error && <p role="status">Loading agent details...</p>}
      {agent && (
        <>
          {error && <p>Showing last received details.</p>}
          <dl>
            <dt>Type</dt>
            <dd>{agent.agent_type === 'codex' ? 'Codex' : 'Mock'}</dd>
            {agent.agent_type === 'codex' && (
              <>
                <dt>Sandbox</dt>
                <dd>{agent.sandbox === 'workspace-write' ? 'Workspace write' : 'Read only'}</dd>
              </>
            )}
            <dt>Task</dt>
            <dd className="task">{agent.task}</dd>
            <dt>Status</dt>
            <dd><span className={`status status-${agent.status}`}>{agent.status}</span></dd>
          </dl>
          {agent.agent_type === 'codex' && agent.status === 'running' && (
            agent.session_id ? (
              <form className="redirect-form" onSubmit={handleRedirect}>
                <label htmlFor="redirect-instruction">Redirect agent</label>
                <textarea
                  id="redirect-instruction"
                  value={instruction}
                  onChange={(event) => setInstruction(event.target.value)}
                  placeholder="Describe the correction for this agent"
                  rows={3}
                  required
                  disabled={redirecting || stopping}
                />
                <button className="start-button" type="submit"
                  disabled={redirecting || stopping || !instruction.trim()}>
                  {redirecting ? 'Sending...' : 'Send'}
                </button>
              </form>
            ) : <p className="agent-type-note">Redirect will be available once the Codex session starts.</p>
          )}
          <h3 id="output-heading">Output</h3>
          <pre className="agent-output" aria-labelledby="output-heading" tabIndex={0}>
            {agent.output.length > 0 ? agent.output.join('\n') : 'No output yet'}
          </pre>
        </>
      )}
    </section>
  )
}

export default AgentDetails
