import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import Icon from '../components/Icon'
import { formatHash, navSection } from '../router.mjs'
import { projectHue, visibleProjects } from '../model/projects.mjs'
import { StatusBead } from '../components/identity'

const PIN_KEY = 'control-center.lumen.rail-pinned'
// The sidebar stays open unless the person folded it away (\).
const WIDE = '(min-width: 1200px)'
const readPinned = () => { try { return globalThis.localStorage?.getItem(PIN_KEY) !== '0' } catch { return true } }
const SECTIONS = [
  { name: 'work', title: 'Work', icon: 'work' },
  { name: 'projects', title: 'Projects', icon: 'folder' },
  { name: 'connections', title: 'Connections', icon: 'plug' },
  { name: 'settings', title: 'Settings', icon: 'sliders' },
]
// Smart lists: the Work page scoped to one state, with live counts of loaded tasks.
const LISTS = [
  { filter: 'needs', title: 'Needs you', bead: 'ask', key: 'needs' },
  { filter: 'working', title: 'Working', bead: 'work', key: 'working' },
  { filter: 'waiting', title: 'Waiting', bead: 'helpers', key: 'waiting' },
  { filter: 'held', title: 'On hold', bead: 'paused', key: 'held' },
  { filter: 'done', title: 'Done', bead: 'done', key: 'doneFamilies' },
]
// Colour labels, as tags: every task whose agent carries the label, in one click.
const TAGS = ['red', 'orange', 'yellow', 'green', 'cyan', 'blue', 'violet', 'pink']
const tagName = (value) => value[0].toUpperCase() + value.slice(1)

// The navigation rail. Collapsed it shows icons with marks; it widens to show names and a line of
// live context on pointer intent, on keyboard focus, or when pinned (\). Nothing depends on hover
// alone: every item is a link with a stable name, and the context is its description. Widening
// overlays the page, so the content never jumps; a pinned rail takes its own room instead.
export default function Rail({ route, markPush, directory, projectFacts, needs, toolsText, toolsName, currentProject, counts = null, tags = null }) {
  const [pinned, setPinned] = useState(readPinned)
  const [open, setOpen] = useState(false)
  const timer = useRef(0)
  const root = useRef(null)
  const puck = useRef(null)
  const section = navSection(route)
  // A wide window keeps the sidebar docked; a narrow one folds it to icons so the work keeps its room.
  const [wide, setWide] = useState(() => globalThis.matchMedia?.(WIDE).matches ?? true)
  useEffect(() => {
    const query = globalThis.matchMedia?.(WIDE)
    if (!query) return undefined
    const change = () => setWide(query.matches)
    query.addEventListener?.('change', change)
    return () => query.removeEventListener?.('change', change)
  }, [])
  const docked = pinned && wide
  const expanded = open || docked
  const projects = visibleProjects(directory)
  const hidden = directory.projects.length - projects.length

  const togglePin = useCallback(() => setPinned((value) => {
    try { globalThis.localStorage?.setItem(PIN_KEY, value ? '0' : '1') } catch { /* The rail still works for this visit. */ }
    return !value
  }), [])
  useEffect(() => {
    document.documentElement.classList.toggle('a-rail-pinned', docked)
    return () => document.documentElement.classList.remove('a-rail-pinned')
  }, [docked])
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
  return <aside ref={root} className={`a-rail${expanded ? ' is-open' : ''}${docked ? ' is-pinned' : ''}`}
    onPointerEnter={(event) => { if (event.pointerType !== 'touch') intent(true, 110) }}
    onPointerLeave={() => intent(false, 220)}
    onFocus={() => { clearTimeout(timer.current); setOpen(true) }}
    onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) intent(false, 0) }}
    onKeyDown={(event) => { if (event.key === 'Escape' && open && !docked) { event.stopPropagation(); setOpen(false) } }}>
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
    {counts && <>
      <div className="a-rail-sep" aria-hidden="true" />
      <nav className="a-rail-projects a-rail-lists" aria-label="Smart lists">
        <span className="a-rail-head" aria-hidden="true"><span className="a-rail-text">Lists</span></span>
        {LISTS.map((item, index) => {
          const n = counts[item.key] ?? 0
          const current = section === 'work' && route.params?.filter === item.filter
          return <a key={item.filter} className={`a-rail-link a-rail-list${n ? '' : ' is-zero'}${item.filter === 'needs' && n ? ' is-need' : ''}`}
            href={formatHash('work', { filter: item.filter })} onClick={markPush} aria-current={current ? 'true' : undefined}
            style={{ '--i': index + 4 }} title={item.title}>
            <span className="a-rail-icon" aria-hidden="true"><StatusBead state={item.bead} size={17} decorative /></span>
            <span className="a-rail-text"><b>{item.title}</b></span>
            <span className="a-rail-count a-num"><span className="a-sr">, </span>{n}</span>
          </a>
        })}
      </nav>
    </>}
    <div className="a-rail-sep" aria-hidden="true" />
    <nav className="a-rail-projects" aria-label="Your projects">
      <span className="a-rail-head" aria-hidden="true"><span className="a-rail-text">Your projects</span></span>
      {projects.map((project, index) => {
        const facts = projectFacts(project.id)
        const state = facts.needsYou ? 'need' : facts.working ? 'live' : facts.open ? 'open' : 'quiet'
        const description = facts.needsYou ? `${facts.needsYou} need you` : facts.working ? `${facts.working} working` : facts.open ? `${facts.open} open` : 'Nothing open'
        return <a key={project.id} className={`a-rail-link a-rail-project is-${state}`} href={formatHash('work', { project: project.id })}
          onClick={markPush} aria-current={currentProject === project.id ? 'true' : undefined}
          style={{ '--i': index + 4 }} title={project.name}>
          <span className="a-rail-tile" aria-hidden="true" style={{ '--hue': projectHue(project.id) }}>{[...project.name.trim()][0]?.toUpperCase() || '·'}</span>
          <span className="a-rail-text"><b className="a-bidi" dir="auto">{project.name}</b><small><span className="a-sr">, </span>{description}</small></span>
        </a>
      })}
      {hidden > 0 && <a className="a-rail-link a-rail-muted" href={formatHash('projects')} onClick={markPush}
        style={{ '--i': projects.length + 4 }}>
        <span className="a-rail-icon" aria-hidden="true"><Icon name="eyeoff" size={17} /></span>
        <span className="a-rail-text"><b>Hidden ({hidden})</b><small><span className="a-sr">, </span>Put away, nothing lost</small></span>
      </a>}
    </nav>
    {tags && TAGS.some((value) => tags[value]) && <>
      <div className="a-rail-sep" aria-hidden="true" />
      <nav className="a-rail-projects a-rail-tags" aria-label="Colour labels">
        <span className="a-rail-head" aria-hidden="true"><span className="a-rail-text">Colour labels</span></span>
        {TAGS.filter((value) => tags[value]).map((value, index) => {
          const current = section === 'work' && route.params?.colors === value
          return <a key={value} className="a-rail-link a-rail-tag" data-color={value} href={formatHash('work', { colors: value })} onClick={markPush}
            aria-current={current ? 'true' : undefined} style={{ '--i': index + 10 }} title={`${tagName(value)} label`}>
            <span className="a-rail-icon" aria-hidden="true"><i className="a-rail-swatch" /></span>
            <span className="a-rail-text"><b>{tagName(value)}</b><span className="a-sr"> label</span></span>
            <span className="a-rail-count a-num"><span className="a-sr">, </span>{tags[value]}<span className="a-sr"> tasks</span></span>
          </a>
        })}
      </nav>
    </>}
    <span className="a-sp" />
    <div className="a-rail-foot">
      <button type="button" className="a-rail-link a-rail-muted" onClick={togglePin} aria-pressed={pinned}
        title="Keep the sidebar open · \" aria-keyshortcuts="\">
        <span className="a-rail-icon" aria-hidden="true"><Icon name="pin" size={17} /></span>
        <span className="a-rail-text"><b>Keep the sidebar open</b></span>
      </button>
    </div>
  </aside>
}
