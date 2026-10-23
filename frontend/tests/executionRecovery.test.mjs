import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import {
  SETTLEMENTS, attestationGate, attestationRequest, describeSources, diagnosticSummary, explanations, formatAge,
  newRequestKey, outcomeMessage, readError, refusalHint, settlement,
} from '../src/executionRecovery.mjs'

const OBSERVED = '2026-10-22T10:00:00.000Z'
const AT = Date.parse(OBSERVED)
const STATEMENT = 'I have independently verified that every process of this execution has ended.'
const execution = (values = {}) => ({
  execution_id: 'e1', state: 'uncertain', uncertainty: 'unsupervised', containment: 'none',
  controller: { current: true, legacy: false },
  diagnostic: { diagnostic_id: 'd1', observed_at: OBSERVED, status: 'complete', classification: 'no_relevant_candidate',
    sources: { controller: { status: 'self' }, root: { status: 'not_recorded' }, scan: { status: 'complete' } }, candidates: [] },
  attestation_offer: { eligible: true, reasons: [], statement: STATEMENT, review_window_seconds: 900, reviewed_diagnostic_id: 'd1' },
  ...values,
})

test('only kernel or reboot evidence is ever presented as verified; an attestation never is', () => {
  for (const state of ['ended_verified', 'ended_kernel_guaranteed', 'ended_by_reboot', 'launch_aborted']) {
    assert.equal(settlement(state).verified, true, state)
  }
  const attested = settlement('attested_ended')
  assert.equal(attested.verified, false)
  assert.equal(attested.basis, 'human')
  assert.match(attested.detail, /not technically verified/)
  assert.doesNotMatch(attested.label, /verif/i)
  assert.equal(settlement('uncertain').verified, false)
  assert.equal(settlement('running').verified, false)
  assert.equal(Object.values(SETTLEMENTS).filter(s => s.basis === 'human').length, 1)
})

test('each kind of uncertainty is explained, including legacy, missing Job evidence and Fast Startup', () => {
  const titles = (values) => explanations(execution(values)).map(e => e.code)
  assert.match(explanations(execution())[0].text, /no Job Object/)
  assert.match(explanations(execution({ uncertainty: 'legacy', containment: 'legacy_unknown' }))[0].text, /legacy/)
  assert.match(explanations(execution({ uncertainty: 'containment_unverified' }))[0].title, /Job evidence missing/)
  assert.match(explanations(execution({ uncertainty: 'termination_unconfirmed' }))[0].text, /could not confirm/)
  assert.deepEqual(titles({}), ['unsupervised'])
  // A run of an earlier backend: a Windows shut down with Fast Startup is not a restart.
  const earlier = explanations(execution({ controller: { current: false } }))
  assert.equal(earlier[1].code, 'fast_startup')
  assert.match(earlier[1].text, /Fast Startup/)
  assert.deepEqual(titles({ recovery: { failure: 'OSError: boom' } }), ['unsupervised', 'temporary_failure'])
  assert.equal(explanations(execution({ uncertainty: 'something_new' }))[0].title, 'Process end unconfirmed')
  assert.deepEqual(explanations(execution({ state: 'attested_ended' })), [])
})

test('diagnostics are summarized honestly: a clean scan is never proof and failures never look clean', () => {
  const clean = diagnosticSummary(execution().diagnostic, AT + 3 * 60000)
  assert.equal(clean.label, 'No related process seen (not proof)')
  assert.equal(clean.age, '3 min ago')
  assert.equal(clean.partial, false)
  const failed = diagnosticSummary({ ...execution().diagnostic, status: 'failed', classification: 'unavailable' }, AT)
  assert.equal(failed.tone, 'blocking')
  assert.match(failed.status, /nothing can be concluded/)
  assert.equal(diagnosticSummary({ ...execution().diagnostic, status: 'incomplete' }, AT).partial, true)
  assert.equal(diagnosticSummary(null, AT).label, 'Not checked yet')
  assert.equal(diagnosticSummary({ ...execution().diagnostic, classification: 'verified_owned' }, AT).tone, 'blocking')
  assert.deepEqual([formatAge(-5), formatAge(30000), formatAge(2 * 3600000), formatAge(72 * 3600000)],
    ['just now', 'just now', '2 h ago', '3 days ago'])
  const sources = describeSources({ controller: { status: 'alive' }, job: { status: 'not_inspectable' }, scan: { status: 'failed' } })
  assert.deepEqual(sources.map(s => s.key), ['controller', 'job', 'scan'])
  assert.match(sources[0].text, /may still be running/)
  assert.match(describeSources({ scan: { status: 'complete' } })[0].text, /never proof/)
})

test('the attestation is offered only for an eligible, fresh, unresolved review', () => {
  const open = attestationGate(execution(), AT + 60000)
  assert.equal(open.open, true)
  assert.equal(open.reviewedDiagnosticId, 'd1')
  const reasons = (values, now = AT) => attestationGate(execution(values), now).reasons.map(r => r.code)
  assert.deepEqual(reasons({ attestation_offer: { ...execution().attestation_offer, eligible: false,
    reasons: [{ code: 'ownership_uncertain_candidate', message: 'x' }] } }), ['ownership_uncertain_candidate'])
  assert.deepEqual(reasons({ state: 'attested_ended' }), ['not_unresolved'])
  assert.deepEqual(reasons({ diagnostic: null }), ['no_diagnostic'])
  assert.deepEqual(reasons({}, AT + 901000), ['review_expired'])
  assert.equal(attestationGate(execution({ attestation_offer: undefined }), AT).open, false)
})

test('the request carries exactly the reviewed evidence and an intentional acknowledgment, never a claim', () => {
  const gate = attestationGate(execution(), AT)
  assert.equal(attestationRequest(gate, { acknowledged: false, requestKey: 'k' }), null)
  assert.equal(attestationRequest(gate, { acknowledged: 'yes', requestKey: 'k' }), null)
  assert.equal(attestationRequest(gate, { acknowledged: true, requestKey: '' }), null)
  assert.equal(attestationRequest({ ...gate, open: false }, { acknowledged: true, requestKey: 'k' }), null)
  const body = attestationRequest(gate, { acknowledged: true, note: '  checked Task Manager  ', requestKey: 'k' })
  assert.deepEqual(body, { request_key: 'k', reviewed_diagnostic_id: 'd1', statement: STATEMENT, reason: 'checked Task Manager' })
  assert.equal(attestationRequest(gate, { acknowledged: true, note: 'x'.repeat(3000), requestKey: 'k' }).reason.length, 2000)
  assert.equal(attestationRequest(gate, { acknowledged: true, note: '   ', requestKey: 'k' }).reason, null)
  assert.equal(newRequestKey({ randomUUID: () => 'uuid-1' }), 'uuid-1')
  assert.match(newRequestKey(null), /^attest-/)
})

test('refusals, outcomes and errors are explained without implying anything was released', () => {
  const refused = readError(409, { detail: { code: 'attestation_refused', message: 'Refused',
    reasons: [{ code: 'controller_inconsistent', message: 'm' }] } })
  assert.deepEqual(refused.reasons.map(r => r.code), ['controller_inconsistent'])
  assert.equal(readError(409, { detail: { code: 'attestation_invalid', message: 'exact' } }).message, 'exact')
  assert.match(readError(503, {}).message, /temporarily unavailable/)
  assert.match(readError(404, {}).message, /no longer exists/)
  assert.match(readError(500, null).message, /could not be completed/)
  assert.match(refusalHint('tracked_live'), /Stop/)
  assert.match(refusalHint('ownership_uncertain_candidate'), /never ends a process/)
  assert.match(refusalHint('stale_review'), /Refresh/)
  assert.match(outcomeMessage({ outcome: 'settled_by_attestation' }), /not as a technical verification/)
  assert.match(outcomeMessage({ outcome: 'settled_by_evidence' }), /technical evidence/)
})

test('the panel is inline, accessible and has no client-side bypass', () => {
  const source = readFileSync(new URL('../src/ProcessOwnership.jsx', import.meta.url), 'utf8')
  const details = readFileSync(new URL('../src/AgentDetails.jsx', import.meta.url), 'utf8')
  const css = readFileSync(new URL('../src/App.css', import.meta.url), 'utf8')
  assert.match(details, /<ProcessOwnership /)
  assert.doesNotMatch(source, /role="dialog"|aria-modal|window\.confirm|className="modal/)
  assert.match(source, /aria-live="polite"/)
  assert.match(source, /role="alert"/)
  assert.match(source, /type="checkbox"[^>]*required/)
  assert.match(source, /disabled=\{busy \|\| !acknowledged\}/)
  assert.match(source, /<details className="attestation-disclosure">/)
  // The only bodies sent are built by attestationRequest; no eligibility or verification claim exists client-side.
  assert.doesNotMatch(source, /eligible\s*:/)
  assert.doesNotMatch(source, /technically_verified|confirm_ended/)
  assert.match(css, /prefers-reduced-motion:reduce\) \{ \.recovery-section/)
})
