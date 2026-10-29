/* eslint-disable react-refresh/only-export-components -- a test-only page entry; it is never hot-reloaded. */
// Test-only page: renders the real useActionRunner under React StrictMode with React's development
// build, so effect setup/cleanup is replayed exactly as in development. Driven by strict-runner.spec.mjs.
import { StrictMode, useCallback, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { useActionRunner } from '../../src/atlas/hooks.js'

window.__probe = { effectSetups: 0, changes: 0, calls: 0, runner: null }

function Probe() {
  const onChanged = useCallback(() => { window.__probe.changes += 1 }, [])
  const runner = useActionRunner(onChanged)
  useEffect(() => { window.__probe.effectSetups += 1 }, [])
  useEffect(() => { window.__probe.runner = runner })
  return <div>
    <span id="busy">{runner.busy || 'idle'}</span>
    <span id="result">{runner.result ? `${runner.result.key}:${runner.result.message}` : 'none'}</span>
  </div>
}

function App() {
  const [shown, setShown] = useState(true)
  const [key, setKey] = useState(0)
  useEffect(() => {
    window.__probe.unmount = () => setShown(false)
    window.__probe.remount = () => { setShown(true); setKey((value) => value + 1) }
  }, [])
  return shown ? <Probe key={key} /> : <span id="gone">unmounted</span>
}

// The action resolves when the test says so, so timing is deterministic.
window.__probe.start = (key, ok = true) => {
  let release
  const done = new Promise((resolve) => { release = resolve })
  window.__probe.release = () => release()
  return window.__probe.runner.run(key, async () => { window.__probe.calls += 1; await done; return { ok, message: `${key} finished` } })
}

createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>)
