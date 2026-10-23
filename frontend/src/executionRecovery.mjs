// Stage 2D.1b P3: how unresolved executions are explained and how a human
// attestation is requested. Pure: no fetches and no state. The backend decides
// eligibility and re-checks it freshly before settling anything; nothing here
// can grant it, and a human attestation is never shown as a verification.

export const SETTLEMENTS = {
  ended_verified: { label: 'Ended: verified', basis: 'technical',
    detail: 'Its Job Object confirmed that every process ended.' },
  ended_kernel_guaranteed: { label: 'Ended: kernel guarantee', basis: 'technical',
    detail: 'Its Job Object is gone and its main process ended, so Windows ended every process in it.' },
  ended_by_reboot: { label: 'Ended: Windows restarted', basis: 'technical',
    detail: 'Windows restarted after this run was launched. No process survives a restart.' },
  launch_aborted: { label: 'Launch aborted', basis: 'technical',
    detail: 'Its process was ended before any of its work ran.' },
  attested_ended: { label: 'Released on your attestation', basis: 'human',
    detail: 'You accepted responsibility that its processes ended. This was not technically verified.' },
  uncertain: { label: 'Process end unconfirmed', basis: 'none',
    detail: 'Nothing has established that every process of this run ended, so its Task, assignment and resources stay held.' },
}

export function settlement(state) {
  const value = SETTLEMENTS[state] || { label: 'Active', basis: 'none', detail: 'This run is still active.' }
  // Only kernel or reboot evidence is a verification; an attestation never is.
  return { ...value, verified: value.basis === 'technical' }
}

const UNCERTAINTY = {
  unsupervised: ['Ran without process-tree supervision',
    'This run had no Job Object. Its main process exited, but nothing proves that the processes it started also ended.'],
  unsupervised_restart: ['Backend restarted during an unsupervised run',
    'The backend stopped while this run was active. It had no Job Object, so nothing proves its processes ended.'],
  termination_unconfirmed: ['Termination not confirmed',
    'The system asked Windows to end this run\'s Job Object but could not confirm that every process in it ended.'],
  launch_abort_unverified: ['Aborted launch not confirmed',
    'A launch was cancelled, but the system could not confirm that its process ended.'],
  launching_unresolved: ['Launch interrupted',
    'A launch was in progress when the backend stopped. It is not known whether a process started.'],
  legacy: ['Started before supervision existed',
    'This run started before process supervision was recorded (a legacy, unsupervised run). Nothing recorded can prove it ended.'],
  containment_unverified: ['Job evidence missing',
    'The record of this run\'s Job Object is missing or incomplete, so the Job cannot be trusted to prove anything.'],
  namespace_mismatch: ['Job in another Windows session',
    'Its Job Object was created in another Windows session or by another user and cannot be inspected from here.'],
  controller_state_unknown: ['Launching backend may still run',
    'The backend instance that launched this run may still be running, or its state could not be established.'],
  controller_unknown: ['Launching backend unknown',
    'There is no usable record of the backend instance that launched this run.'],
  root_alive_without_job: ['Main process still running',
    'Its Job Object is gone but its main process is still running.'],
  root_alive_outside_job: ['Main process still running',
    'Its main process is still running outside any Job Object.'],
  root_state_unknown: ['Main process could not be checked',
    'Windows did not allow checking whether its main process is still running.'],
  resolution_failed: ['Check failed temporarily',
    'The last automatic check failed. Refreshing the diagnostics runs it again.'],
  guard_without_evidence: ['End not recorded',
    'Its end could not be recorded and no launch evidence was saved.'],
}

export function explanations(execution) {
  if (!execution || execution.state !== 'uncertain') return []
  const [title, text] = UNCERTAINTY[execution.uncertainty]
    || ['Process end unconfirmed', 'The system could not establish that every process of this run ended.']
  const result = [{ code: execution.uncertainty || 'unknown', title, text }]
  if (execution.controller && !execution.controller.current) {
    result.push({ code: 'fast_startup', title: 'Shut down is not a restart',
      text: 'Only a full Windows restart counts as evidence. With Fast Startup, Shut down hibernates Windows ' +
        'instead of restarting it. Choosing Restart settles every unresolved run automatically when the backend starts again.' })
  }
  const failure = execution.recovery?.failure
  if (failure && execution.uncertainty !== 'resolution_failed') {
    result.push({ code: 'temporary_failure', title: 'Last check failed',
      text: 'The last automatic check failed temporarily. Refresh the diagnostics to try again.' })
  }
  return result
}

const CLASSIFICATIONS = {
  verified_owned: ['A process of this run is still running', 'blocking'],
  ownership_uncertain: ['A process that may belong to this run is running', 'blocking'],
  no_relevant_candidate: ['No related process seen (not proof)', 'clear'],
  unavailable: ['Diagnostics could not complete', 'blocking'],
}

export function diagnosticSummary(diagnostic, now) {
  if (!diagnostic) return { label: 'Not checked yet', tone: 'neutral', age: '', partial: false }
  const [label, tone] = CLASSIFICATIONS[diagnostic.classification] || ['Unknown result', 'blocking']
  const observed = Date.parse(diagnostic.observed_at)
  return {
    label, tone,
    age: Number.isFinite(observed) && Number.isFinite(now) ? formatAge(now - observed) : '',
    partial: diagnostic.status !== 'complete',
    status: diagnostic.status === 'failed' ? 'The check failed; nothing can be concluded from it.'
      : diagnostic.status === 'incomplete' ? 'Part of the evidence could not be inspected.' : 'Every source was inspected.',
  }
}

export function formatAge(ms) {
  if (!Number.isFinite(ms) || ms < 0) return 'just now'
  const minutes = Math.floor(ms / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.floor(minutes / 60)
  return hours < 48 ? `${hours} h ago` : `${Math.floor(hours / 24)} days ago`
}

export const CANDIDATE_LABELS = { owned: 'Belongs to this run', uncertain: 'Cannot be excluded', unrelated: 'Unrelated' }

const SOURCE_TEXT = {
  controller: { gone: 'The backend that launched it has exited.', self: 'Launched by this backend.',
    legacy: 'Recorded before supervision existed.', alive: 'The backend that launched it may still be running.',
    unknown: 'The backend that launched it could not be checked.', missing: 'No record of the backend that launched it.',
    superseded_in_process: 'An earlier instance inside this backend process launched it; it is not verified to have exited.' },
  root: { alive: 'Its main process is running.', gone: 'Its main process has ended.',
    unknown: 'Its main process could not be checked.', not_recorded: 'No main process identity was recorded.' },
  job: { found: 'Its Job Object still exists.', not_found: 'No Job Object with its name exists in this session.',
    not_inspectable: 'Its Job Object cannot be inspected from this Windows session.' },
  scan: { complete: 'A process list was read (a heuristic, never proof).', failed: 'The process list could not be read.' },
}

export function describeSources(sources) {
  return ['controller', 'root', 'job', 'scan'].filter(key => sources?.[key]).map(key => ({
    key, label: { controller: 'Launching backend', root: 'Main process', job: 'Job Object', scan: 'Process list' }[key],
    text: SOURCE_TEXT[key][sources[key].status] || `Status: ${sources[key].status}`,
  }))
}

const HINTS = {
  tracked_live: 'Use Stop: it verifies the end itself.',
  verified_owned_alive: 'A process of this run is still running. Wait for it to end, then refresh.',
  ownership_uncertain_candidate: 'Check the listed process. The system never ends a process it cannot prove belongs to this run. Refresh once it has exited.',
  job_present: 'Refresh: recovery verifies and ends a Job Object itself, so no attestation is needed.',
  controller_inconsistent: 'Close the backend that launched it, or restart Windows, then refresh.',
  diagnostics_superseded: 'Newer diagnostics arrived while it was being decided. Review them, then try again.',
  diagnostics_not_eligible: 'The latest diagnostics do not allow an attestation. Refresh them, then review again.',
  concurrent_decision: 'Another recovery action is in progress. Try again in a moment.',
  not_unresolved: 'This run is already settled.',
}
const REFRESH = 'Refresh the diagnostics, then review them again.'

export function refusalHint(code) {
  return HINTS[code] || REFRESH
}

// Advisory only: the backend re-checks every condition freshly and may still refuse.
export function attestationGate(execution, now) {
  const offer = execution?.attestation_offer
  if (!offer || execution.state !== 'uncertain') {
    return { open: false, reasons: [{ code: 'not_unresolved', message: 'This run is not unresolved.' }] }
  }
  if (!offer.eligible) return { open: false, reasons: offer.reasons || [] }
  const observed = Date.parse(execution.diagnostic?.observed_at)
  if (!offer.reviewed_diagnostic_id || !Number.isFinite(observed)) {
    return { open: false, reasons: [{ code: 'no_diagnostic', message: 'No diagnostic has been taken yet.' }] }
  }
  if (Number.isFinite(now) && now - observed > offer.review_window_seconds * 1000) {
    return { open: false, reasons: [{ code: 'review_expired', message: 'These diagnostics are too old to attest on.' }] }
  }
  return { open: true, reasons: [], reviewedDiagnosticId: offer.reviewed_diagnostic_id, statement: offer.statement }
}

// Exactly the fields the backend accepts; nothing the client sends can claim eligibility.
export function attestationRequest(gate, { acknowledged, note, requestKey }) {
  if (!gate?.open || acknowledged !== true || !requestKey) return null
  const reason = (note || '').trim()
  return { request_key: requestKey, reviewed_diagnostic_id: gate.reviewedDiagnosticId, statement: gate.statement,
    reason: reason ? reason.slice(0, 2000) : null }
}

export function newRequestKey(random = globalThis.crypto) {
  return random?.randomUUID ? random.randomUUID() : `attest-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

export function outcomeMessage(result) {
  if (result?.outcome === 'settled_by_attestation') {
    return 'Released on your attestation. It is recorded as your decision, not as a technical verification.'
  }
  if (result?.outcome === 'settled_by_evidence') return 'Settled by technical evidence; no attestation was needed.'
  return 'Updated.'
}

export function readError(status, data) {
  const detail = data?.detail
  if (status === 409 && detail?.code === 'attestation_refused') {
    return { message: detail.message || 'Attestation was refused.', reasons: detail.reasons || [] }
  }
  if (status === 404) return { message: 'This run no longer exists.', reasons: [] }
  if (status === 503) return { message: 'Recovery is temporarily unavailable. Please retry.', reasons: [] }
  const message = typeof detail === 'string' ? detail : detail?.message
  return { message: message || 'The request could not be completed.', reasons: [] }
}
