// Stage 2G presentation for the AI Providers view, provider permissions and update
// notices. Pure functions: the backend discovers, validates, authorizes and runs
// everything; this module only words what it reports and shapes requests.
import { initialChoice } from './modelPresentation.mjs'
import { providerLabel, stateLabel, stateOf, stateTone } from './providerPresentation.mjs'

export const REAL_PROVIDERS = ['codex', 'claude', 'gemini']
export const ACTION_LABELS = { install: 'Install', update: 'Update', login: 'Sign in', logout: 'Sign out' }
export const CHANNELS = { codex: ['latest'], claude: ['latest', 'stable'], gemini: ['latest', 'preview', 'nightly'] }
const OPERATION_LABELS = {
  running: 'Running', succeeded: 'Succeeded', failed: 'Failed', canceled: 'Canceled',
  interrupted: 'Interrupted', needs_attention: 'Needs attention', uncertain: 'End not verified',
}
const UPDATE_LABELS = {
  update_available: 'Update available', manual_required: 'Update available (manual)', up_to_date: 'Up to date',
  newer_than_channel: 'Newer than its channel', not_installed: 'Not installed', unsupported_channel: 'Channel not published',
  unknown: 'Not checked yet', check_failed: 'Check failed',
}
const DISCOVERY_LABELS = { installed: 'Installed', not_installed: 'Not installed', unsupported_location: 'Installed in an unsupported location' }
const METHOD_LABELS = { npm: 'npm (global)', native: 'Native installer', unknown: 'Unrecognized method' }

export function entryFor(view, provider) { return (view?.providers || []).find((item) => item.provider === provider) || null }

// The one next step that moves a provider toward Ready, in the order a new user meets them.
export function nextStep(entry, update = null) {
  if (!entry || entry.provider === 'mock') return null
  const discovery = entry.discovery || {}
  const state = stateOf(entry.status)
  const settings = entry.status?.settings || {}
  if (entry.operations?.some((operation) => operation.status === 'running')) return { kind: 'wait', text: 'A setup operation is running.' }
  if (entry.operations?.some((operation) => operation.status === 'uncertain')) {
    return { kind: 'resolve', text: 'Confirm that the last setup operation ended. Until then, this provider starts no new work or setup.' }
  }
  if (discovery.state === 'unsupported_location') return { kind: 'install', text: 'Reinstall where the Control Center can run it, or follow the manual steps.' }
  if (discovery.state === 'not_installed' || state === 'not_installed') return { kind: 'install', text: `Install ${providerLabel(entry.provider)}.` }
  if (state === 'unsupported_version' || state === 'invalid_installation') return { kind: 'update', text: 'Update to a supported version.' }
  if (state === 'unchecked') return { kind: 'check', text: 'Check the installation and sign-in.' }
  if (state === 'not_authenticated') return { kind: 'login', text: 'Sign in with your subscription.' }
  if (!settings.enabled || state === 'disabled') return { kind: 'enable', text: 'Enable it and confirm its billing route.' }
  if (!entry.allowed) return { kind: 'allow', text: 'Allow it in Permissions to use it for agents.' }
  if (update && ['update_available', 'manual_required'].includes(update.status)) return { kind: 'update', text: `Version ${update.latest_version} is available.` }
  if (state === 'ready') return { kind: 'ready', text: 'Ready for new agents.' }
  return { kind: 'check', text: entry.status?.state?.reason || 'Check again.' }
}

// Progressive disclosure: one line per fact, each with a tone.
export function summaryRows(entry, update = null) {
  if (!entry) return []
  const state = stateOf(entry.status)
  const discovery = entry.discovery || {}
  const settings = entry.status?.settings || {}
  const readiness = entry.status?.state || {}
  const rows = [
    { key: 'install', label: 'Installation', tone: discovery.state === 'installed' ? 'ready' : 'blocked',
      value: `${DISCOVERY_LABELS[discovery.state] || 'Unknown'}${discovery.method ? ` · ${METHOD_LABELS[discovery.method]}` : ''}${readiness.version || discovery.version ? ` · ${readiness.version || discovery.version}` : ''}` },
    { key: 'auth', label: 'Sign-in', tone: readiness.authenticated ? 'ready' : state === 'unchecked' ? 'waiting' : 'blocked',
      value: readiness.authenticated ? `Signed in${readiness.auth_route ? ` (${readiness.auth_route.replaceAll('_', ' ')})` : ''}`
        : state === 'unchecked' ? 'Not checked' : 'Not signed in' },
    { key: 'enabled', label: 'Enabled', tone: settings.enabled ? 'ready' : 'blocked', value: settings.enabled ? 'Enabled' : 'Disabled' },
    { key: 'allowed', label: 'Permission', tone: entry.allowed ? 'ready' : 'blocked', value: entry.allowed ? 'Allowed for agents' : 'Not allowed' },
    { key: 'billing', label: 'Billing route', tone: readiness.billing_route ? 'ready' : 'waiting',
      value: readiness.billing_route ? readiness.billing_route.replaceAll('_', ' ') : 'Not verified yet' },
    { key: 'models', label: 'Models', tone: entry.catalog?.fresh ? 'ready' : 'waiting',
      value: entry.catalog?.last_success_at ? `Listed ${entry.catalog.last_success_at}${entry.catalog.fresh ? '' : ' (stale)'}${entry.catalog.account_changed ? ' · account changed' : ''}` : 'Not listed yet' },
  ]
  if (update) rows.push({ key: 'update', label: 'Updates', tone: update.status === 'update_available' || update.status === 'manual_required' ? 'waiting' : update.status === 'up_to_date' ? 'ready' : 'waiting',
    value: updateText(update) })
  return rows
}

export function readinessBadge(entry) {
  const state = stateOf(entry?.status)
  return { label: stateLabel(state), tone: stateTone(state) }
}

export function updateText(update) {
  if (!update) return 'Not checked yet'
  const label = UPDATE_LABELS[update.status] || 'Unknown'
  const versions = update.latest_version ? ` · ${update.installed_version || '?'} → ${update.latest_version} (${update.channel})` : ''
  const when = update.checked_at ? ` · checked ${update.checked_at}${update.stale ? ' (stale)' : ''}` : ''
  return `${label}${update.status === 'update_available' || update.status === 'manual_required' ? versions : ''}${when}${update.error ? ` · ${update.error}` : ''}`
}

export function operationText(operation) {
  if (!operation) return ''
  const label = OPERATION_LABELS[operation.status] || operation.status
  return `${ACTION_LABELS[operation.action] || operation.action}: ${label}${operation.result ? `. ${operation.result}` : ''}`
}
export function operationTone(operation) {
  if (!operation) return 'waiting'
  if (operation.status === 'succeeded') return 'ready'
  if (operation.status === 'running' || operation.status === 'needs_attention' || operation.status === 'interrupted') return 'waiting'
  if (operation.status === 'uncertain') return 'blocked'
  return 'blocked'
}

// Which setup actions to offer; the backend still validates every one of them.
export function availableActions(entry) {
  if (!entry || entry.provider === 'mock') return []
  const installed = entry.discovery?.state === 'installed'
  const signedIn = entry.status?.state?.authenticated === true
  if (!installed) return ['install']
  return signedIn ? ['update', 'login', 'logout'] : ['update', 'login']
}

export function planRequest(action, channel) {
  return { action, ...(channel ? { channel } : {}) }
}
// The approval names the exact reviewed plan; the backend recomputes and compares it.
export function approvalRequest(plan) {
  if (!plan?.plan_id || !plan.digest) throw new Error('This operation has no automated path to approve.')
  return { plan_id: plan.plan_id, digest: plan.digest, approved: true }
}
export function planSource(plan) {
  if (!plan?.package) return null
  return `${plan.package}@${plan.target_version} from ${plan.registry}, without package scripts`
}
export function planCommand(plan) {
  return plan?.command ? plan.command.join(' ') : null
}

// ---------------------------------------------------------------- permissions and defaults

export function policyDraft(policy) {
  return {
    allowed: [...(policy?.allowed || [])], defaultProvider: policy?.default_provider || '',
    pick: initialChoice(policy?.default_model || null, policy?.default_effort || null), revision: policy?.revision ?? null,
  }
}
// `config` is the model picker's chosen model and effort for the default provider.
export function policyPayload(draft, config = {}) {
  if (draft.revision === null || draft.revision === undefined) throw new Error('Reload the provider permissions before saving.')
  const provider = draft.defaultProvider || null
  const real = provider && provider !== 'mock'
  return {
    allowed: REAL_PROVIDERS.filter((id) => draft.allowed.includes(id)),
    default_provider: provider,
    default_model: real ? (config.model || null) : null,
    default_effort: real ? (config.effort || null) : null,
    expected_revision: draft.revision,
  }
}
export function allowedText(allowed) {
  const list = REAL_PROVIDERS.filter((id) => (allowed || []).includes(id))
  if (list.length === 0) return 'No real provider is allowed. Only Mock agents can start.'
  if (list.length === REAL_PROVIDERS.length) return 'All providers are allowed.'
  return `Allowed: ${list.map(providerLabel).join(', ')}.`
}

// For the New Agent form: whether a provider may be chosen, and why not.
export function permissionFor(policy, provider) {
  if (provider === 'mock' || !policy) return { allowed: true, reason: null }
  if ((policy.allowed || []).includes(provider)) return { allowed: true, reason: null }
  return { allowed: false, reason: `${providerLabel(provider)} is not in your allowed providers. Allow it in AI Providers.` }
}

// The saved default for a new root agent. A forbidden or unusable default is shown
// with its reason and never replaced by another provider or model.
export function defaultChoice(policy) {
  if (!policy?.default_provider) return null
  return { provider: policy.default_provider, model: policy.default_model || null, effort: policy.default_effort || null,
    problem: policy.default_problem || null }
}
export function defaultText(choice) {
  if (!choice) return null
  const label = [providerLabel(choice.provider), choice.model, choice.effort && `${choice.effort} effort`].filter(Boolean).join(' · ')
  return choice.problem ? `Your default (${label}) cannot be used: ${choice.problem} Nothing else was chosen for you.` : `Using your default: ${label}.`
}

// ---------------------------------------------------------------- Task restrictions

export function restrictionText(view) {
  if (!view) return ''
  const effective = view.effective
  if (effective === null || effective === undefined) return 'Provider permissions are not available.'
  const unverified = (view.sources || []).find((source) => source.scope === 'unverified')
  if (unverified) return `The Task hierarchy could not be verified (${unverified.reason}), so no real provider is permitted here. Mock only.`
  const sources = (view.sources || []).filter((source) => source.scope === 'task')
  const own = view.own ? `This Task allows ${view.own.providers.map(providerLabel).join(', ') || 'no real provider'}.` : 'This Task has no restriction of its own.'
  const inherited = sources.filter((source) => source.task_id !== view.task_id)
  const parts = [own]
  if (inherited.length) parts.push(`Inherited from ${inherited.map((source) => `"${source.title || source.task_id}"`).join(', ')}.`)
  parts.push(effective.length ? `Effective: ${effective.map(providerLabel).join(', ')}.` : 'Effective: no real provider (Mock only).')
  return parts.join(' ')
}
export function restrictionPayload(selected, own) {
  return { providers: REAL_PROVIDERS.filter((id) => selected.includes(id)), expected_revision: own ? own.revision : null }
}

// ---------------------------------------------------------------- notifications

export function noticeText(notice) {
  return `${notice.label} ${notice.available_version} is available (installed ${notice.installed_version || 'unknown'}, ${notice.channel} channel).${notice.manual ? ' This installation is updated manually.' : ''}`
}
