import { useEffect, useLayoutEffect, useRef } from 'react'
import './styles/tokens.css'
import './styles/base.css'
import './styles/components.css'
import './styles/shell.css'
import './styles/pages.css'
import TopBar from './shell/TopBar'
import WorkPage from './pages/WorkPage'
import PreviewPage from './pages/PreviewPage'
import SettingsPage from './pages/SettingsPage'
import GalleryPage from './pages/GalleryPage'
import NotFoundPage from './pages/NotFoundPage'
import { useAppearance, useAtlasRoute, useLiveStatus } from './hooks'
import { documentTitle, routeKey } from './router.mjs'
import { exitAtlas } from './uiMode.mjs'

// The Atlas preview shell (milestone M1). It is loaded only after the person opts in, and it
// owns the single event connection while it is on screen (the classic interface is unmounted).
export default function AtlasApp() {
  const { route, navigate, markPush, navigationRef } = useAtlasRoute()
  const appearance = useAppearance()
  const live = useLiveStatus()
  const main = useRef(null)
  const scrolls = useRef(new Map())
  const currentKey = useRef(null)
  const key = routeKey(route)

  useEffect(() => {
    const previous = document.title
    return () => { document.title = previous }
  }, [])

  // A new navigation starts at the top; Back/Forward restores where the page was. Focus moves to
  // the new page's heading, except on the first load.
  useLayoutEffect(() => {
    currentKey.current = key
    document.title = documentTitle(route)
    const element = main.current
    if (!element) return
    const kind = navigationRef.current
    element.scrollTop = kind === 'traversal' ? (scrolls.current.get(key) || 0) : 0
    if (kind !== 'initial') element.querySelector('[data-page-heading]')?.focus({ preventScroll: true })
    navigationRef.current = 'traversal'
  }, [key, route, navigationRef])

  let page
  if (route.name === 'work') page = <WorkPage live={live} />
  else if (route.name === 'projects' || route.name === 'connections' || route.name === 'new') page = <PreviewPage key={route.name} kind={route.name} />
  else if (route.name === 'settings') page = <SettingsPage appearance={appearance} live={live} navigate={navigate} />
  else if (route.name === 'gallery') page = <GalleryPage />
  else page = <NotFoundPage requested={route.requested} navigate={navigate} />

  return <div className="atlas">
    <div className="a-app">
      <button type="button" className="a-skip" onClick={() => main.current?.focus()}>Skip to content</button>
      <TopBar route={route} live={live} appearance={appearance} markPush={markPush} onClassic={() => exitAtlas()} />
      <main ref={main} className="a-main" tabIndex={-1}
        onScroll={() => { if (currentKey.current) scrolls.current.set(currentKey.current, main.current.scrollTop) }}>
        {page}
      </main>
    </div>
  </div>
}
