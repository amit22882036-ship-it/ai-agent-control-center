import { useState } from 'react'
import { Button } from '../primitives'
import { ConfirmDialog } from './fields'
import { agentActions, refusalText } from '../../api/agentClient.mjs'
import { STOP_TEXT } from '../../model/agentCapabilities.mjs'

// Stop this agent, or this agent and the agents it started. Both stop agents, not tasks: the task
// is kept (pause and cancel are task actions), and each asks for confirmation first.
export default function StopControls({ scope, nameOf }) {
  const { caps, detail, runner } = scope
  const [confirm, setConfirm] = useState(null)
  const busy = Boolean(runner.busy)
  if (!caps.stop && !caps.stopBranch) return null
  const children = detail.child_ids || []

  async function stop(kind) {
    await runner.run(kind === 'branch' ? 'stop-branch' : 'stop', async () => {
      const stale = scope.guard()
      if (stale) return { ok: false, message: stale, changed: false }
      if (kind === 'single') {
        const result = await agentActions.stop(scope.agentId)
        return result.ok ? { ok: true, message: `${scope.agentName} was stopped.` }
          : { ok: false, message: refusalText(result, result.status === 404 ? 'Agent not found.' : 'Unable to stop agent. Please try again.') }
      }
      const result = await agentActions.stopBranch(scope.agentId)
      if (!result.ok) return { ok: false, message: refusalText(result, 'Unable to stop branch. Please try again.') }
      if (result.data?.ok !== true) {
        // Partial success: say exactly which agents did not stop, in the backend's words.
        const failures = (result.data?.failures || []).map((failure) => `${nameOf(failure.agent_id)}: ${failure.error}`)
        return { ok: false, message: 'Branch stopping was only partially successful.', details: failures }
      }
      return { ok: true, message: `${scope.agentName} and the agents it started were stopped.` }
    })
    setConfirm(null)
  }

  return <>
    {caps.stop && <Button variant="danger" size="sm" icon="stop" disabled={busy} onClick={() => setConfirm('single')}>
      {runner.busy === 'stop' ? 'Stopping…' : 'Stop agent'}</Button>}
    {caps.stopBranch && <Button variant="danger" size="sm" disabled={busy} onClick={() => setConfirm('branch')}>
      {runner.busy === 'stop-branch' ? 'Stopping branch…' : 'Stop branch'}</Button>}
    {confirm === 'single' && <ConfirmDialog open title={`Stop ${scope.agentName}?`} confirmLabel="Stop agent" busy={busy}
      onCancel={() => setConfirm(null)} onConfirm={() => stop('single')}>
      <p>{STOP_TEXT.single}</p>
      {children.length > 0 && <p className="a-fine">Agents it started keep working. Use Stop branch to stop them too.</p>}
    </ConfirmDialog>}
    {confirm === 'branch' && <ConfirmDialog open title={`Stop ${scope.agentName} and its branch?`} busy={busy}
      confirmLabel="Stop branch" onCancel={() => setConfirm(null)} onConfirm={() => stop('branch')}>
      <p>{STOP_TEXT.branch}</p>
      <p className="a-fine">It started {children.length} agent{children.length === 1 ? '' : 's'} directly; agents they started are included.</p>
    </ConfirmDialog>}
  </>
}
