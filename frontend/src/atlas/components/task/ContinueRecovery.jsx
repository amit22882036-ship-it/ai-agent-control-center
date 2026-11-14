import { useState } from 'react'
import { Button } from '../primitives'
import { agentActions, refusalText } from '../../api/agentClient.mjs'

// A lead whose continuation stopped short of guessing ("Recovery required"): its reason, and Check and
// continue, which returns the continuation to waiting (refused unless its agent has really ended) and runs
// one orchestration pass. Asked once first, in words, because it continues work the person has not seen.
export default function ContinueRecovery({ taskId, reason }) {
  const [step, setStep] = useState('idle')
  const [message, setMessage] = useState(null)
  async function run() {
    setStep('busy')
    setMessage(null)
    const rearm = await agentActions.rearmContinuation(taskId)
    if (!rearm.ok) { setStep('idle'); setMessage({ tone: 'alert', text: refusalText(rearm, 'It could not be continued.') }); return }
    const wake = await agentActions.wakeOrchestration(taskId)
    setStep('done')
    setMessage(wake.ok ? { tone: 'ok', text: 'Continuing. The lead gets its helpers’ results or finishes; watch it in Now and Output.' }
      : { tone: 'alert', text: refusalText(wake, 'It is ready to continue, but the next step could not be started. Try again.') })
  }
  return <div className="a-recover">
    {reason && <p className="a-fine">Reason: <span className="a-bidi" dir="auto">{reason}</span></p>}
    {step === 'idle' && <Button size="sm" variant="primary" icon="refresh" onClick={() => setStep('confirm')}>Check and continue…</Button>}
    {(step === 'confirm' || step === 'busy') && <div className="a-recover-confirm" role="group" aria-label="Check and continue">
      <p className="a-fine">This continues the lead only if its agent has really stopped (the Control Center checks and refuses
        otherwise). The lead then receives its helpers’ results, or finishes if nothing is left. Nothing is applied to your folder.</p>
      <div className="a-act-row">
        <Button size="sm" variant="primary" disabled={step === 'busy'} onClick={run}>{step === 'busy' ? 'Continuing…' : 'Continue'}</Button>
        <Button size="sm" variant="ghost" disabled={step === 'busy'} onClick={() => setStep('idle')}>Cancel</Button>
      </div>
    </div>}
    {message && <p className={`a-fine${message.tone === 'alert' ? ' a-tone-alert' : ''}`} role={message.tone === 'alert' ? 'alert' : 'status'}>{message.text}</p>}
  </div>
}
