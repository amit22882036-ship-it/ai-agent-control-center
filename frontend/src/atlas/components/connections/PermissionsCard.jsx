import { useState } from 'react'
import Icon from '../Icon'
import { Button, Card } from '../primitives'
import { ActionResult, Check, PlainSelect } from '../agent/fields'
import { ModelChoice } from '../agent/models'
import useModelCatalog from '../../../useModelCatalog'
import { agentActions } from '../../api/agentClient.mjs'
import { chosenConfig, selectionReady } from '../../../modelPresentation.mjs'
import { providerLabel } from '../../../providerPresentation.mjs'
import { REAL_PROVIDERS, allowedText, policyDraft, policyPayload } from '../../../onboardingPresentation.mjs'
import { policyChanges } from '../../model/connections.mjs'
import { outcome } from './outcome.mjs'

// Which tools any agent may use (a hard limit for new tasks, helpers, automatic choice and every
// later run) and the default for new tasks. Classic's rules and payload: the loaded revision is
// sent, a newer saved policy is a conflict (the draft is kept), and every widening and narrowing
// is listed before saving: nothing is allowed silently.
export function PermissionsCard({ policy, runner, blocked, onReload }) {
  const [edits, setEdits] = useState(null)
  const [conflict, setConflict] = useState(false)
  const draft = edits || policyDraft(policy)
  const dirty = edits !== null
  const real = Boolean(draft.defaultProvider) && draft.defaultProvider !== 'mock'
  const models = useModelCatalog(real ? draft.defaultProvider : null)
  const busy = Boolean(runner.busy)
  const off = busy || Boolean(blocked)
  if (!policy) return <Card className="a-cx-sec" id="cx-sec-permissions" titleId="cx-sec-permissions-h" title="Allowed tools and defaults">
    <p className="a-fine a-tone-alert">Your allowed tools couldn’t be read, so none can be changed here. No real tool is allowed until they can be read.</p></Card>
  const change = (patch) => setEdits({ ...draft, ...patch })
  const config = real ? chosenConfig(models.catalog, draft.pick) : {}
  const changes = dirty ? policyChanges(policy, draft, config) : []
  const ready = !real || selectionReady(models.catalog, draft.pick)
  function save(event) {
    event.preventDefault()
    if (off || !dirty || !ready) return
    runner.run('policy', async () => {
      let payload
      try { payload = policyPayload(draft, config) } catch (error) { return { ok: false, message: error.message, changed: false } }
      const result = await agentActions.savePolicy(payload)
      if (result.ok) { setEdits(null); setConflict(false) } else setConflict(result.status === 409 && /changed since/i.test(JSON.stringify(result.data || '')))
      const words = outcome(result, { done: 'Allowed tools and defaults saved. They apply to work started from now on.', refused: 'They could not be saved.',
        unknown: 'They may or may not have been saved. They are read again; your choices are kept here until you discard them.' })
      return result.status === 409 ? { ...words, message: `${words.message} Your choices are kept; discard them to load the newer version.` } : words
    })
  }
  return <Card className="a-cx-sec" id="cx-sec-permissions" titleId="cx-sec-permissions-h" title="Allowed tools and defaults">
    <form onSubmit={save} className="a-cx-perm">
      <p className="a-fine">Which tools any agent may use. This is a hard limit for new tasks, helpers, automatic choice and every later run. It is separate
        from installing, signing in and turning a tool on. An empty choice allows none (Practice still runs).</p>
      <fieldset className="a-cx-allow"><legend>Allowed tools</legend>
        {REAL_PROVIDERS.map((provider) => <Check key={provider} checked={draft.allowed.includes(provider)} disabled={off}
          onChange={(checked) => change({ allowed: checked ? [...draft.allowed, provider] : draft.allowed.filter((item) => item !== provider) })}>
          {providerLabel(provider)}</Check>)}
      </fieldset>
      <p className="a-fine" role="status">{allowedText(draft.allowed)}</p>
      <PlainSelect label="Default tool for new tasks" value={draft.defaultProvider} disabled={off}
        onChange={(value) => change({ defaultProvider: value, pick: policyDraft(null).pick })}
        options={[{ value: '', label: 'No default (choose each time)' }, { value: 'mock', label: 'Practice (Mock)' },
          ...REAL_PROVIDERS.map((provider) => ({ value: provider, label: `${providerLabel(provider)}${draft.allowed.includes(provider) ? '' : ' (not allowed)'}`,
            disabled: !draft.allowed.includes(provider) }))]} />
      {real && <ModelChoice provider={draft.defaultProvider} models={models} value={draft.pick} onChange={(pick) => change({ pick })} disabled={off} label="Default model" />}
      {policy.default_problem && !dirty && <p className="a-fine a-tone-alert">The saved default can’t be used: {policy.default_problem} It is shown, never replaced.</p>}
      {changes.length > 0 && <div className="a-cx-changes" role="status"><b>If you save:</b>
        <ul>{changes.map((item) => <li key={item.text} className={`is-${item.kind}`}>{item.text}</li>)}</ul></div>}
      <div className="a-act-row">
        <Button type="submit" variant="primary" disabled={off || !dirty || !ready}>{runner.busy === 'policy' ? 'Saving…' : 'Save allowed tools and defaults'}</Button>
        {dirty && <Button disabled={busy} onClick={() => { if (conflict) onReload(); setEdits(null); setConflict(false) }}>
          {conflict ? 'Discard and load the newer version' : 'Discard changes'}</Button>}
      </div>
      <p className="a-fine"><Icon name="info" size={13} /> A task can narrow this for itself and its helpers (in Work, the agent’s Task policies); it can never widen it.</p>
    </form>
    <ActionResult result={runner.result} keys={['policy']} />
  </Card>
}
