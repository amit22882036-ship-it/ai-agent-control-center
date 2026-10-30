// What the Connections page says about each AI tool, from GET /providers/onboarding only (the
// same read model as Classic's AI Providers view). Installation, sign-in, enablement, permission
// for tasks and readiness are separate facts and are never merged into one "connected". Usage is
// what the provider reported, never an estimate. Pure: the backend decides and validates.
import { REAL_PROVIDERS, availableActions, nextStep, signInRow, summaryRows, updateText, operationText,
  operationTone } from '../../onboardingPresentation.mjs'
import { providerLabel, stateLabel, stateOf, usageLines, usageSummary, windowRows } from '../../providerPresentation.mjs'
import { when } from './taskPage.mjs'

export { REAL_PROVIDERS }
export const CONNECTION_SECTIONS = ['setup', 'billing', 'usage', 'models', 'advanced']

// Recorded times inside backend or shared wording (ISO 8601) in the same short form as elsewhere.
export function humanTimes(text) {
  return String(text ?? '').replace(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g, (stamp) => when(stamp) || stamp)
}

const BLOCKING_STATES = ['not_installed', 'invalid_installation', 'unsupported_version', 'not_authenticated', 'incompatible', 'billing_blocked']

export function entryOf(view, provider) {
  return (view?.providers || []).find((item) => item.provider === provider) || null
}

const runningOf = (entry) => entry?.operations?.find((operation) => operation.status === 'running') || null
const uncertainOf = (entry) => entry?.operations?.find((operation) => operation.status === 'uncertain') || null

// One condition per tool, from a closed set. "Ready" needs every fact; a tool the person has not
// turned on or allowed is "not in use", never an error.
export function conditionOf(entry) {
  if (!entry) return null
  const state = stateOf(entry.status)
  const settings = entry.status?.settings || {}
  const wanted = entry.allowed === true || settings.enabled === true
  const reason = entry.status?.state?.reason || null
  const make = (key, label, tone, detail = null) => ({ key, label, tone, detail })
  if (runningOf(entry)) return make('setup', 'Setup running', 'work', operationText(runningOf(entry)))
  if (uncertainOf(entry)) return make('attention', 'Requires attention', 'alert', 'Confirm that the last setup operation ended.')
  if (state === 'usage_limited') return make('limited', 'Usage-limited', 'ask', reason)
  if (state === 'temporarily_unavailable') return make('unavailable', 'Temporarily unavailable', 'ask', reason)
  if (state === 'ready') return entry.allowed ? make('ready', 'Ready for tasks', 'ok')
    : make('off', 'Not allowed for tasks', 'muted', 'Ready, but not in your allowed tools.')
  if (state === 'disabled') return make('off', 'Turned off', 'muted', null)
  if (state === 'unchecked') return make('unchecked', 'Not checked yet', 'muted', 'Check it to read its installation and sign-in.')
  if (BLOCKING_STATES.includes(state)) return wanted ? make('attention', 'Requires attention', 'alert', reason || `${stateLabel(state)}.`)
    : make('off', 'Not set up', 'muted', stateLabel(state))
  return make('attention', 'Requires attention', 'alert', reason || stateLabel(state))
}

// The facts that make a tool ready, each on its own line: yes, no or not known.
export function readinessFacts(entry) {
  if (!entry) return []
  const discovery = entry.discovery || {}
  const readiness = entry.status?.state || {}
  const settings = entry.status?.settings || {}
  const state = stateOf(entry.status)
  const version = readiness.version || discovery.version
  const installed = discovery.state === 'installed' ? { mark: 'yes', value: `Installed${version ? ` · ${version}` : ''}` }
    : discovery.state === 'unsupported_location' ? { mark: 'no', value: 'Installed where the Control Center can’t run it' }
      : discovery.state === 'not_installed' ? { mark: 'no', value: 'Not installed' } : { mark: 'unknown', value: 'Not known' }
  const auth = signInRow(entry.provider, readiness)
  return [
    { key: 'installed', label: 'Installed', ...installed },
    { key: 'signed-in', label: 'Signed in', mark: readiness.authenticated === true ? 'yes' : readiness.authenticated === false ? 'no' : 'unknown', value: auth.value },
    { key: 'enabled', label: 'Turned on', mark: settings.enabled ? 'yes' : 'no', value: settings.enabled ? 'On for new work' : 'Off' },
    { key: 'allowed', label: 'Allowed for tasks', mark: entry.allowed ? 'yes' : 'no', value: entry.allowed ? 'In your allowed tools' : 'Not in your allowed tools' },
    { key: 'ready', label: 'Ready', mark: state === 'ready' ? 'yes' : state === 'unchecked' ? 'unknown' : 'no', value: stateLabel(state) },
  ]
}

// The tools that need the person: a confirmation, a limit, an outage or a blocker on a tool they
// turned on or allowed. No count is invented: the list is exactly what the backend reported.
export function attentionOf(view) {
  if (!view) return null
  const providers = REAL_PROVIDERS.map((provider) => ({ provider, condition: conditionOf(entryOf(view, provider)) }))
    .filter((item) => ['attention', 'limited', 'unavailable'].includes(item.condition?.key))
    .map((item) => ({ provider: item.provider, label: providerLabel(item.provider), condition: item.condition }))
  const defaultProblem = view.policy?.default_problem || null
  return { providers, defaultProblem, any: providers.length > 0 || Boolean(defaultProblem) }
}

export function attentionText(attention) {
  if (!attention?.any) return null
  const parts = attention.providers.map((item) => `${item.label}: ${item.condition.label.toLowerCase()}`)
  if (attention.defaultProblem) parts.push('the default for new tasks can’t be used')
  return `Needs attention: ${parts.join('; ')}.`
}

// Usage, honestly: provider-reported meters only, with when they were observed. Remaining usage,
// costs and reset times that the provider did not report are "Not available".
export function usageView(status) {
  const summary = usageSummary(status)
  const windows = status?.usage?.windows || []
  const rows = windowRows(status).map((row, index) => ({ ...row, observedAt: when(windows[index]?.observed_at),
    resetText: windows[index]?.reset_at ? `Provider-reported reset: ${when(windows[index].reset_at) || windows[index].reset_at}` : 'Reset time not reported',
    scope: windows[index]?.model_scope || null }))
  const blocks = (status?.usage?.blocks || []).map((block) => ({ ...block, resetText: block.reset_at ? `Provider-reported reset: ${when(block.reset_at) || block.reset_at}` : 'Reset time not reported' }))
  const lines = usageLines(status).filter((line) => !line.startsWith('Remaining usage is not reported'))
    .filter((line) => !blocks.some((block) => line.startsWith(block.reason)))
  const metered = rows.find((row) => row.usedPercent !== null)
  const source = status?.usage?.source
  const headline = blocks.length || rows.some((row) => row.exhausted) ? 'Limit reached'
    : metered ? `${metered.label}: ${metered.used}`
      : source === 'exact_when_reported' ? 'No reading yet'
        : source === 'exhaustion_signal' ? 'Only limit-reached signals' : 'Not available'
  return {
    overall: summary.overall, tone: summary.tone, source: summary.source, explanation: summary.explanation, headline,
    rows, blocks, lines, remaining: 'Not available', refreshable: summary.refreshable === true, clearable: blocks.length > 0,
    lastObserved: when(summary.lastObserved), stale: rows.some((row) => row.stale),
  }
}

// Billing: the route the last check verified, and the person's own recorded statement (which is
// never proof of an account setting). The confirmation form and its payload are Classic's,
// unchanged: Claude Code and Gemini CLI are turned on together with the confirmation; Codex's
// acknowledgment is recorded on its own.
export function billingView(entry) {
  const provider = entry?.provider
  const status = entry?.status || {}
  const settings = status.settings || {}
  const readiness = status.state || {}
  const kind = status.acknowledgment_kind || 'confirmation'
  const confirmToEnable = provider === 'claude' || provider === 'gemini'
  const form = confirmToEnable && !settings.enabled
    ? { action: 'enable', payload: { enabled: true, billing_confirmed: true }, button: 'Confirm and turn on' }
    : provider === 'codex' && !settings.billing_confirmed
      ? { action: 'acknowledge', payload: { billing_confirmed: true }, button: 'Record acknowledgment' } : null
  return {
    route: readiness.billing_route ? readiness.billing_route.replaceAll('_', ' ') : null,
    authRoute: readiness.auth_route ? readiness.auth_route.replaceAll('_', ' ') : null,
    kind, form, text: status.confirmation_text || 'I confirm the billing route of this provider.',
    recorded: settings.billing_confirmed ? { at: when(settings.billing_confirmed_at) || settings.billing_confirmed_at || null } : null,
    guidance: status.billing_guidance || null,
    canEnable: provider === 'codex' && !settings.enabled, canDisable: settings.enabled === true,
  }
}

// Setup: the operation running now, one whose end is not verified, the latest result and the
// actions the person may review (the backend validates each one again).
export function setupView(entry, update) {
  const running = runningOf(entry)
  const uncertain = uncertainOf(entry)
  const latest = entry?.operations?.[0] || null
  const rows = summaryRows(entry, update)
  return {
    running, uncertain, latest: latest && latest !== running ? { ...latest, text: humanTimes(operationText(latest)), tone: operationTone(latest),
      at: when(latest.ended_at || latest.started_at) } : null,
    actions: running || uncertain ? [] : availableActions(entry),
    install: rows.find((row) => row.key === 'install') || null,
    update: update ? { text: humanTimes(updateText(update)), available: ['update_available', 'manual_required'].includes(update.status) } : null,
    activeWork: entry?.active_work || [], elsewhere: entry?.discovery?.elsewhere || [],
    step: nextStep(entry, update),
  }
}

// The explicit summary shown before saving permissions: exactly what widens and what narrows.
export function policyChanges(policy, draft, config = {}) {
  if (!policy || !draft) return []
  const before = new Set(policy.allowed || [])
  const after = new Set(draft.allowed || [])
  const changes = []
  for (const provider of REAL_PROVIDERS) {
    if (!before.has(provider) && after.has(provider)) changes.push({ kind: 'widen', text: `Allows ${providerLabel(provider)} for every new task, helper and automatic choice.` })
    if (before.has(provider) && !after.has(provider)) changes.push({ kind: 'narrow', text: `Stops allowing ${providerLabel(provider)} for new work. Agents already running are not stopped.` })
  }
  if ((policy.default_provider || '') !== (draft.defaultProvider || '')) {
    changes.push({ kind: 'default', text: draft.defaultProvider ? `Default tool for new tasks: ${draft.defaultProvider === 'mock' ? 'Practice (Mock)' : providerLabel(draft.defaultProvider)}.` : 'No default tool: choose one for each task.' })
  } else if (draft.defaultProvider && draft.defaultProvider !== 'mock'
    && ((policy.default_model || null) !== (config.model || null) || (policy.default_effort || null) !== (config.effort || null))) {
    changes.push({ kind: 'default', text: `Default model for new tasks: ${config.model || 'provider default'}${config.effort ? `, ${config.effort} effort` : ''}.` })
  }
  if (changes.some((change) => change.kind === 'default')) changes.push({ kind: 'note', text: 'Defaults apply to tasks started from now on; running agents keep their own settings.' })
  return changes
}

// The tool route parameter, if it names a real tool.
export function routeProvider(params) {
  return REAL_PROVIDERS.includes(params?.provider) ? params.provider : null
}
