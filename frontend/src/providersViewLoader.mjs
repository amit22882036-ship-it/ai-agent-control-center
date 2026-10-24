// Loads the AI Providers view: one read at a time, never discarding the read in flight (a
// refused connection can take seconds on Windows), and backing off while the backend is
// unreachable. Only an explicit user Retry (`manual`) skips the backoff; every other refresh
// (live events, polling fallback, operation polling, reloads after an action) respects it.
import { retryDelay } from './onboardingPresentation.mjs'

export function createViewLoader({ fetchView, onData, onError, now = () => Date.now(), timers = globalThis, delay = retryDelay }) {
  const state = { running: false, queued: false, queuedManual: false, failures: 0, notBefore: 0, timer: null,
    active: true, sequence: 0, applied: 0 }

  function clearTimer() {
    if (state.timer !== null) timers.clearTimeout(state.timer)
    state.timer = null
  }

  function run() {
    clearTimer()
    state.running = true
    const sequence = ++state.sequence
    let read
    try {
      read = Promise.resolve(fetchView())  // Started now, so the in-flight read is the one counted.
    } catch (error) {
      read = Promise.reject(error)
    }
    read.then((data) => {
      if (!state.active || sequence < state.applied) return  // Never let an older read overwrite a newer one.
      state.applied = sequence
      state.failures = 0
      state.notBefore = 0
      onData(data)
    }, (error) => {
      if (!state.active || sequence < state.applied) return
      state.applied = sequence
      state.failures += 1
      state.notBefore = now() + delay(state.failures)
      onError(error)
    }).finally(() => {
      state.running = false
      if (state.queued && state.active) {
        const manual = state.queuedManual
        state.queued = false
        state.queuedManual = false
        request({ manual })
      }
    })
  }

  function request({ manual = false } = {}) {
    if (!state.active) return
    if (state.running) {
      // Coalesced into one follow-up read; it is manual only if a Retry asked for it.
      state.queued = true
      state.queuedManual = state.queuedManual || manual
      return
    }
    const wait = manual ? 0 : state.notBefore - now()
    if (wait > 0) {
      if (state.timer === null) state.timer = timers.setTimeout(() => { state.timer = null; request() }, wait)
      return
    }
    run()
  }

  return {
    request,
    retry: () => request({ manual: true }),
    activate() { state.active = true },
    dispose() { state.active = false; state.queued = false; state.queuedManual = false; clearTimer() },
    state,
  }
}
