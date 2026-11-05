import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { formatHash, navSection } from '../router.mjs'
import { visibleProjects } from '../model/projects.mjs'

const PIN_KEY = 'control-center.lumen.rail-pinned'
const readPinned = () => { try { return globalThis.localStorage?.getItem(PIN_KEY) === '1' } catch { return false } }
const SECTIONS = [
  { name: 'work', title: 'Work', icon: 'work' },
  { name: 'projects', title: 'Projects', icon: 'folder' },
  { name: 'connections', title: 'Connections', icon: 'plug' },
  { name: 'settings', title: 'Settings', icon: 'sliders' },
]

// The navigation rail. Collapsed it shows icons with marks; it widens to show names and a line of
// live context on pointer intent, on keyboard focus, or when pinned (\). Nothing depends on hover
// alone: every item is a link with a stable name, and the context is its description. Widening
// overlays the page, so the content never jumps; a pinned rail takes its own room instead.
export default function Rail({ route, markPush, directory, projectFacts, needs, toolsText, toolsName, currentProject, onClassic }) {
  const [pinned, setPinned] = useState(readPinned)
  const [open, setOpen] = useState(false)
  const timer = useRef(0)
  const root = useRef(null)
  const puck = useRef(null)
  const section = navSection(route)
  const expanded = open || pinned
  const projects = visibleProjects(directory)
  const hidden = directory.projects.length - projects.length

  const togglePin = useCallback(() => setPinned((value) => {
    try { globalThis.localStorage?.setItem(PIN_KEY, value ? '0' : '1') } catch { /* The rail still works for this visit. */ }
    return !value
  }), [])
  useEffect(() => {
    document.documentElement.classList.toggle('a-rail-pinned', pinned)
    return () => document.documentElement.classList.remove('a-rail-pinned')
  }, [pinned])
  // "\" pins and unpins from anywhere (not while typing).
  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== '\\' || event.ctrlKey || event.metaKey || event.altKey) return
      const target = event.target
      if (target instanceof Element && target.closest('input, textarea, select, [contenteditable="true"]')) return
      event.preventDefault()
      togglePin()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [togglePin])
  // The current-page puck slides between items instead of jumping.
  useLayoutEffect(() => {
    const current = root.current?.querySelector('.a-rail-link[aria-current="page"]')
    const mark = puck.current
    if (!mark) return
    if (!current) { mark.style.opacity = '0'; return }
    mark.style.opacity = '1'
    mark.style.transform = `translateY(${current.offsetTop}px)`
    mark.style.height = `${current.offsetHeight}px`
  })

  const intent = (value, delay) => {
    clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(value), delay)
  }
  const sub = {
    work: needs > 0 ? `${needs} need you` : 'Nothing needs you',
    projects: `${projects.length} shown${hidden ? `, ${hidden} hidden` : ''}`,
    connections: toolsText || 'All tools ready',
    settings: 'Appearance, notifications, keys',
  }
  return <aside ref={root} className={`a-rail${expanded ? ' is-open' : ''}${pinned ? ' is-pinned' : ''}`}
    onPointerEnter={(event) => { if (event.pointerType !== 'touch') intent(true, 110) }}
    onPointerLeave={() => intent(false, 220)}
    onFocus={() => { clearTimeout(timer.current); setOpen(true) }}
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) intent(false, 0) }}
    onKeyDown={(event) => { if (event.key === 'Escape' && open && !pinned) { event.stopPropagation(); setOpen(false) } }}>
    <span ref={puck} className="a-rail-puck" aria-hidden="true" />
    <nav className="a-rail-nav" aria-label="Main">
      {SECTIONS.map((item, index) => {
        const tone = (item.name === 'work' && needs > 0) || (item.name === 'connections' && toolsText) ? ' has-mark' : ''
        return <a key={item.name} className={`a-rail-link${tone}`} href={formatHash(item.name)} onClick={markPush}
          aria-current={section === item.name ? 'page' : undefined} data-section={item.name}
          aria-label={item.name === 'connections' && toolsName ? `${item.title}. ${toolsName}` : item.title} aria-describedby={`a-rail-sub-${item.name}`}
          style={{ '--i': index }} title={`${item.title} · ${index + 1}`}>
          <span className="a-rail-icon"><Icon name={item.icon} size={19} />{tone && <i className="a-rail-dot" aria-hidden="true" />}</span>
          <span className="a-rail-text"><b>{item.title}</b><small id={`a-rail-sub-${item.name}`}>{sub[item.name]}</small></span>
        </a>
      })}
    </nav>
    <div className="a-rail-sep" aria-hidden="true" />
    <nav className="a-rail-projects" aria-label="Your projects">
      <span className="a-rail-head" aria-hidden="true"><span className="a-rail-text">Your projects</span></span>
      {projects.map((project, index) => {
        const facts = projectFacts(project.id)
        const state = facts.needsYou ? 'need' : facts.working ? 'live' : facts.open ? 'open' : 'quiet'
        const description = facts.needsYou ? `${facts.needsYou} need you` : facts.working ? `${facts.working} working` : facts.open ? `${facts.open} open` : 'Nothing open'
        return <a key={project.id} className={`a-rail-link a-rail-project is-${state}`} href={formatHash('work', { project: project.id })}
          onClick={markPush} aria-current={currentProject === project.id ? 'true' : undefined}
          aria-label={`${project.name}, ${description}`} style={{ '--i': index + 4 }} title={project.name}>
          <span className="a-rail-tile" aria-hidden="true">{[...project.name.trim()][0]?.toUpperCase() || '·'}</span>
          <span className="a-rail-text" aria-hidden="true"><b className="a-bidi" dir="auto">{project.name}</b><small>{description}</small></span>
        </a>
      })}
      {hidden > 0 && <a className="a-rail-link a-rail-muted" href={formatHash('projects')} onClick={markPush}
        aria-label={`${hidden} hidden ${hidden === 1 ? 'project' : 'projects'}`} style={{ '--i': projects.length + 4 }}>
        <span className="a-rail-icon"><Icon name="eyeoff" size={17} /></span>
        <span className="a-rail-text" aria-hidden="true"><b>Hidden ({hidden})</b><small>Put away, nothing lost</small></span>
      </a>}
    </nav>
    <span className="a-sp" />
    <div className="a-rail-foot">
      <button type="button" className="a-rail-link a-rail-muted" onClick={onClassic} aria-label="Open the classic interface"
        title="The classic interface stays available as a fallback">
        <span className="a-rail-icon"><Icon name="swap" size={17} /></span>
        <span className="a-rail-text" aria-hidden="true"><b>Classic interface</b><small>Fallback view</small></span>
      </button>
      <button type="button" className="a-rail-link a-rail-muted" onClick={togglePin} aria-pressed={pinned}
        aria-label="Keep the sidebar open" title="Keep open · \">
        <span className="a-rail-icon"><Icon name="pin" size={17} /></span>
        <span className="a-rail-text" aria-hidden="true"><b>{pinned ? 'Unpin sidebar' : 'Keep open'}</b><small>Shortcut: \</small></span>
      </button>
    </div>
  </aside>
}
