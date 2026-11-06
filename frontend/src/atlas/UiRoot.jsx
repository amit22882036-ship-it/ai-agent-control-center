import { lazy, Suspense } from 'react'

// Lumen is code-split so its styles and fonts load with it.
const AtlasApp = lazy(() => import('./AtlasApp.jsx'))

export default function UiRoot() {
  return <Suspense fallback={<p className="message" role="status">Loading…</p>}><AtlasApp /></Suspense>
}
