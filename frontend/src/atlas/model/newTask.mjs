// The New Task draft and what it means, from the same rules as the classic form: provider
// permission and readiness (onboarding/provider presentation), the model catalog (model
// presentation), autonomy eligibility and the creation payload (autonomousCreation). Pure.
import { autonomyEligible, creationPayload } from '../../autonomousCreation.mjs'
import { chosenConfig, initialChoice, selection, selectionReady } from '../../modelPresentation.mjs'
import { permissionFor, submissionProblem } from '../../onboardingPresentation.mjs'
import { providerLabel, selectable, stateLabel, stateOf, statusFor } from '../../providerPresentation.mjs'

export const LEAD_TOOLS = ['claude', 'codex', 'gemini', 'mock']

// "Start from" adds a short structure to an empty or existing description.
export const TEMPLATES = [
  { id: 'bug', label: 'Fix a bug', text: 'What’s broken:\n\nHow to reproduce:\n\nHow to check the fix:\n' },
  { id: 'build', label: 'Build something', text: 'Goal:\n\nConstraints:\n\nHow to check it:\n' },
  { id: 'investigate', label: 'Investigate', text: 'Question:\n\nWhat to look at:\n\nWhat to report back:\n' },
]

// Atlas starts with helpers off: more agents can mean more usage, possibly on several provider
// accounts, so delegation is a conscious choice. This is Atlas's own initial choice; Classic keeps
// its default (autonomousCreation's initialAutonomousChoice) and nothing is remembered between tasks.
export const INITIAL_HELPERS = false

export function emptyDraft() {
  return { text: '', projectId: null, provider: 'mock', sandbox: 'read-only', picker: initialChoice(), autonomous: INITIAL_HELPERS,
    allocation: 'explicit', autoProviders: [] }
}

export function addTemplate(text, template) {
  return text.trim() ? `${text.replace(/\s+$/, '')}\n\n${template.text}` : template.text
}

// Whether a tool can be chosen (permission) and can start now (readiness), with the reason.
export function toolState(provider, policy, statuses) {
  if (provider === 'mock') return { allowed: true, usable: true, note: 'Simulated work. No AI tool runs.' }
  const permission = permissionFor(policy, provider)
  if (!permission.allowed) return { allowed: false, usable: false, note: permission.reason }
  if (!statuses) return { allowed: true, usable: false, note: 'Checking whether it is ready…' }
  const status = statusFor(statuses, provider)
  const state = stateOf(status)
  if (selectable(status, provider)) return { allowed: true, usable: true, note: 'Ready' }
  const reason = [status?.state?.reason, status?.state?.guidance].filter(Boolean).join(' ')
  return { allowed: true, usable: false, note: `${stateLabel(state)}.${reason ? ` ${reason}` : ''}` }
}

const join = (items) => (items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} or ${items.at(-1)}`)

// Everything the page shows and sends for a draft.
export function draftView(draft, { policy, statuses, catalog, projects = [] }) {
  const real = draft.provider !== 'mock'
  const tool = toolState(draft.provider, policy, statuses)
  const config = real ? chosenConfig(catalog, draft.picker) : { model: null, effort: null, attempt: false }
  const chosen = real ? selection(catalog, draft.picker.choice, draft.picker.typed) : null
  const modelReady = !real || selectionReady(catalog, draft.picker)
  const canLead = autonomyEligible(draft.provider)
  const helpers = canLead && draft.autonomous === true
  const auto = helpers && draft.allocation === 'auto'
  const revokedAuto = draft.autoProviders.filter((provider) => !permissionFor(policy, provider).allowed)
  const project = projects.find((item) => item.project_id === draft.projectId) || null
  let problem = null
  if (!draft.text.trim()) problem = 'Describe the task to start.'
  else if (!project) problem = 'Choose a project to start.'
  else if (!tool.usable) problem = `${providerLabel(draft.provider)} can’t start now: ${tool.note}`
  else if (!modelReady) problem = chosen?.needsAttempt ? 'Confirm the explicit attempt for the unverified model, or choose another model.' : 'Choose an available model.'
  else if (auto && draft.autoProviders.length === 0) problem = 'Check at least one tool for automatic choice.'
  else if (auto && revokedAuto.length) problem = submissionProblem(policy, 'mock', revokedAuto)
  const lead = providerLabel(draft.provider)
  const where = project ? project.name : 'the chosen project'
  const summary = !real ? [`Mock simulates one agent in ${where}. No AI tool runs and no helpers are brought in.`] : [
    `${lead} (${config.model || 'provider default model'}${config.effort ? `, ${config.effort} effort` : ''}) leads this task in a private copy of ${where} and ${draft.sandbox === 'workspace-write' ? 'may edit files there' : 'can only read files'}.`,
    !canLead ? `It works alone: only Claude Code and Codex can lead helpers.`
      : !helpers ? 'It works alone. No helper creation is authorized for this task.'
        : 'It may bring in helpers for parts of the work if it decides that helps. It is not required to.',
    ...(!helpers ? [] : auto
      ? [draft.autoProviders.length ? `The lead’s own choices still win. For a helper it leaves open, the Control Center may pick ${join(draft.autoProviders.map(providerLabel))}, only if allowed, ready and suited; otherwise that helper waits.`
        : 'Check at least one tool for automatic choice.']
      : [`The lead names each helper’s tool and model. A helper with none named uses the provider the plan requests, or ${lead}; an unavailable one is never replaced.`]),
  ]
  return { real, tool, config, chosen, modelReady, canLead, helpers, auto, revokedAuto, project, problem, summary }
}

// The request body for POST /agents/start, built by the classic payload builder.
export function newTaskPayload(draft, view) {
  return creationPayload(draft.text, draft.provider, draft.sandbox, draft.autonomous, null, view.project.project_id,
    view.config.model || undefined, { effort: view.config.effort, attempt: view.config.attempt,
      autoProviders: draft.allocation === 'auto' ? draft.autoProviders : null })
}
