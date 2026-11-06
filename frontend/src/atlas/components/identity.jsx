import { STATES, colorLabel, toolMark, TOOLS } from '../vocabulary.mjs'
import { initials } from '../a11y.mjs'

// An agent's avatar: its colour label, initials and the AI tool it runs on. Removed agents stay
// visible in grey so history is never silently dropped.
export function Avatar({ name, color, tool, size = 30, removed = false, decorative = false }) {
  const description = `${name}${tool ? `, ${TOOLS[tool]?.label || tool}` : ''}${removed ? ', removed' : ''}`
  return <span className={`a-avatar${removed ? ' a-avatar--removed' : ''}`} data-color={colorLabel(color)} style={{ '--s': `${size}px` }}
    role={decorative ? undefined : 'img'} aria-label={decorative ? undefined : description} aria-hidden={decorative || undefined}>
    <span className="a-avatar-initials" aria-hidden="true">{initials(name)}</span>
    {tool && <i className="a-avatar-tool" aria-hidden="true">{toolMark(tool)}</i>}
  </span>
}

// State marks: one circle, a familiar symbol inside it and one colour per kind of state, so a
// state reads at a glance without a legend. Only the two states that ask for action are filled:
// what needs the person (orange) and what failed (red). The words beside a mark always say it too.
const R = 6.3
const ring = (color, dashed = false) => <circle cx="8" cy="8" r={R} fill="none" stroke={color} strokeWidth="1.5" strokeDasharray={dashed ? '2.2 1.9' : undefined} />
const line = (d, color, width = 1.5) => <path d={d} fill="none" stroke={color} strokeWidth={width} strokeLinecap="round" strokeLinejoin="round" />
const SHAPES = {
  // Needs you: an exclamation mark on orange.
  ask: <><circle cx="8" cy="8" r="7.2" fill="var(--a-ask-fill)" />{line('M8 4.5v4.1', '#fff', 1.8)}<circle cx="8" cy="11.25" r="1.05" fill="#fff" /></>,
  // Working: under way, half done.
  work: <>{ring('var(--a-live)')}<path d="M8 4.1a3.9 3.9 0 0 1 0 7.8z" fill="var(--a-live)" /></>,
  // Waiting for something else to happen first.
  helpers: <>{ring('var(--a-water)')}<circle cx="5.4" cy="8" r="1.05" fill="var(--a-water)" /><circle cx="8" cy="8" r="1.05" fill="var(--a-water)" /><circle cx="10.6" cy="8" r="1.05" fill="var(--a-water)" /></>,
  paused: <>{ring('var(--a-idle)')}{line('M6.5 5.7v4.6M9.5 5.7v4.6', 'var(--a-idle)')}</>,
  // Blocked: a bar across the way.
  blocked: <>{ring('var(--a-idle)')}{line('M5.3 8h5.4', 'var(--a-idle)', 1.7)}</>,
  pending: ring('var(--a-idle)', true),
  failed: <><circle cx="8" cy="8" r="7.2" fill="var(--a-fail)" />{line('M5.7 5.7l4.6 4.6M10.3 5.7l-4.6 4.6', '#fff', 1.7)}</>,
  done: <>{ring('var(--a-ok-fill)')}{line('M5.3 8.2l1.9 1.9 3.6-3.9', 'var(--a-ok-fill)', 1.6)}</>,
  review: <>{ring('var(--a-live)')}{line('M5.3 8.2l1.9 1.9 3.6-3.9', 'var(--a-live)', 1.6)}</>,
  stopped: <>{ring('var(--a-idle)')}{line('M5.8 10.2l4.4-4.4', 'var(--a-idle)')}</>,
  removed: <>{ring('var(--a-text-4)', true)}{line('M6.4 6.4l3.2 3.2M9.6 6.4l-3.2 3.2', 'var(--a-text-4)', 1.3)}</>,
}

// A status bead. When it sits next to visible status text, pass `decorative`; otherwise it is an
// image with the state's name, so colour and shape are never the only information.
// `live`: an agent is recorded running right now, so a gleam of light travels round the mark (still
// under reduced motion). Only the working mark takes it.
export function StatusBead({ state, size = 16, decorative = false, label, live = false }) {
  const name = label || STATES[state] || 'Unknown state'
  const gleam = live && state === 'work'
  return <svg className={`a-bead${gleam ? ' is-live' : ''}`} width={size} height={size} viewBox="0 0 16 16" role={decorative ? undefined : 'img'}
    aria-label={decorative ? undefined : name} aria-hidden={decorative || undefined} focusable="false">
    {decorative ? null : <title>{name}</title>}
    {SHAPES[state] || SHAPES.pending}
    {gleam && <circle className="a-bead-gleam" cx="8" cy="8" r={R} fill="none" strokeWidth="1.6" strokeLinecap="round" strokeDasharray="5 40" />}
  </svg>
}

// A family's progress: one rounded bar split by state in a fixed order (finished, canceled,
// working, waiting, needs you, on hold; the empty track is what has not started), always shown
// with its "finished/total" count by the caller. Decorative: the count and the row's words say it.
const BAR = ['done', 'canceled', 'working', 'waiting', 'needs', 'held']
export function ProgressBar({ counts }) {
  return <span className="a-prog" aria-hidden="true">
    {BAR.filter((key) => counts[key] > 0).map((key) => <i key={key} className={`a-prog-${key}`} style={{ flexGrow: counts[key] }} />)}
    {counts.pending > 0 && <i className="a-prog-pending" style={{ flexGrow: counts.pending }} />}
  </span>
}

