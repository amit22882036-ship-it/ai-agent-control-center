import { useRef, useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { TextArea } from '../controls'
import { Dialog } from '../overlays'
import { LeadSettings, ToolCards } from './LeadConfig'
import { useCreationContext } from './useCreationContext'
import useModelCatalog from '../../../useModelCatalog'
import { agentActions, OFFLINE_MESSAGE, refusalText } from '../../api/agentClient.mjs'
import { creationPayload } from '../../../autonomousCreation.mjs'
import { chosenConfig, initialChoice, selectionReady } from '../../../modelPresentation.mjs'
import { submissionProblem } from '../../../onboardingPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { toolState } from '../../model/newTask.mjs'

const COPY = {
  helper: {
    title: (name) => `Add a helper to ${name}`,
    description: 'Starts a new agent on a new task under this one, in the same project. Its private copy starts from this task’s work, and it works under this task’s responsibility. Autonomous helpers are not started from here.',
    button: 'Start helper',
  },
  again: {
    title: () => 'Start a new agent on this task',
    description: 'Starts a new agent assignment on this same task, with its workspace and history. It does not resume the earlier agent or its provider session, and it does not create another task.',
    button: 'Start new agent',
  },
}

// Starting an agent from an existing task: a manual helper (POST /agents/{parent}/children/start)
// or a new assignment on a task without a current agent (POST /tasks/{id}/start-agent). Both use
// the classic payload rules; the backend's admission checks decide.
export function AgentStartDialog({ mode, open, onClose, parentAgentId, parentName, taskId, version, onStarted }) {
  const copy = COPY[mode]
  const context = useCreationContext(version)
  const [text, setText] = useState('')
  const [provider, setProvider] = useState('mock')
  const [sandbox, setSandbox] = useState('read-only')
  const [picker, setPicker] = useState(initialChoice)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const sending = useRef(false)
  const real = provider !== 'mock'
  const models = useModelCatalog(real ? provider : null)
  const tool = toolState(provider, context.policy, context.statuses)
  const config = real ? chosenConfig(models.catalog, picker) : { model: null, effort: null, attempt: false }
  const problem = mode === 'helper' && !text.trim() ? 'Describe the helper’s part of the work.'
    : !tool.usable ? `${providerLabel(provider)} can’t start now: ${tool.note}`
      : real && !selectionReady(models.catalog, picker) ? 'Confirm the explicit attempt for the unverified model, or choose another model.' : null

  async function start(event) {
    event.preventDefault()
    if (sending.current || problem) return
    sending.current = true
    setBusy(true)
    setError(null)
    try {
      if (real) {
        const refused = submissionProblem(await context.refreshPolicy(), provider, [])
        if (refused) { setError({ tone: 'error', text: refused }); return }
      }
      const payload = creationPayload(mode === 'helper' ? text : 'unused', provider, sandbox, false, mode === 'helper' ? parentAgentId : null, undefined,
        config.model || undefined, { effort: config.effort, attempt: config.attempt })
      const result = mode === 'helper' ? await agentActions.startChild(parentAgentId, payload)
        : await agentActions.startTaskAgent(taskId, { agent_type: payload.agent_type, sandbox: payload.sandbox,
          ...(payload.model ? { model: payload.model } : {}), ...(payload.effort ? { effort: payload.effort } : {}),
          ...(payload.attempt_unverified ? { attempt_unverified: true } : {}) })
      if (result.offline) {
        setError({ tone: 'warn', text: `${OFFLINE_MESSAGE} The agent may or may not have started; check the task before trying again.` })
        return
      }
      if (!result.ok) {
        setError({ tone: 'error', text: refusalText(result, 'Unable to start agent. Check the backend and try again.') })
        context.refreshProviders()
        return
      }
      onStarted(result.data)
    } finally {
      sending.current = false
      setBusy(false)
    }
  }

  return <Dialog open={open} onClose={() => { if (!busy) onClose() }} size="lg" title={copy.title(parentName)} description={copy.description}>
    <form className="a-start-form" onSubmit={start}>
      {mode === 'helper' && <TextArea label="What should the helper do?" rows={3} value={text} disabled={busy} onChange={(event) => setText(event.target.value)} />}
      <ToolCards value={provider} policy={context.policy} statuses={context.statuses} disabled={busy} label="Tool for the new agent"
        onChange={(value) => { setProvider(value); setPicker(initialChoice()) }} />
      <LeadSettings provider={provider} models={models} picker={picker} onPicker={setPicker} sandbox={sandbox} onSandbox={setSandbox} disabled={busy} />
      {error && <div className={`a-result a-result--${error.tone}`} role={error.tone === 'error' ? 'alert' : 'status'}>
        <Icon name={error.tone === 'error' ? 'warn' : 'info'} size={15} /><div>{error.text}</div></div>}
      <div className="a-act-row">
        <Button type="submit" variant="primary" disabled={busy || Boolean(problem)} aria-describedby="start-why">{busy ? 'Starting…' : copy.button}</Button>
        <Button disabled={busy} onClick={onClose}>Cancel</Button>
        <span className="a-fine" id="start-why" role="status">{busy ? 'Checking permissions and starting…' : problem || ''}</span>
      </div>
    </form>
  </Dialog>
}
