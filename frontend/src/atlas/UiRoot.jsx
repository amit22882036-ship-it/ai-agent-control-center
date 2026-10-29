import { lazy, Suspense, useSyncExternalStore } from 'react'
import { UI_EVENT, resolveUi } from './uiMode.mjs'

// Atlas is code-split: its code, styles and fonts load only when someone opens the preview.
const AtlasApp = lazy(() => import('./AtlasApp.jsx'))

function subscribe(callback) {
  window.addEventListener('hashchange', callback)
  window.addEventListener('popstate', callback)
  window.addEventListener(UI_EVENT, callback)
  return () => {
    window.removeEventListener('hashchange', callback)
    window.removeEventListener('popstate', callback)
    window.removeEventListener(UI_EVENT, callback)
  }
}

// Exactly one interface is mounted at a time, so only one live-update connection exists.
export default function UiRoot({ classic: Classic }) {
  const ui = useSyncExternalStore(subscribe, () => resolveUi(window.location))
  if (ui === 'atlas') {
    return <Suspense fallback={<p className="message" role="status">Loading the Atlas preview…</p>}><AtlasApp /></Suspense>
  }
  return <Classic />
}
