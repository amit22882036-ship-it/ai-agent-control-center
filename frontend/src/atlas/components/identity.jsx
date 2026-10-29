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

const SHAPES = {
  work: <><circle className="a-bead-pulse" cx="8" cy="8" r="7" fill="none" stroke="var(--a-work)" strokeWidth="1.4" /><circle cx="8" cy="8" r="4" fill="var(--a-work)" /></>,
  ask: <><circle cx="8" cy="8" r="7" fill="var(--a-ask-fill)" /><path d="M6.2 9.2V5.8a.9.9 0 0 1 1.8 0V8.4M8 8V5.2a.9.9 0 0 1 1.8 0v3.4M9.8 8.2V6.4a.85.85 0 0 1 1.7 0v3.1a3 3 0 0 1-3 3h-.4a3 3 0 0 1-2.7-1.7l-.6-1.4a.8.8 0 0 1 1.4-.7" fill="none" stroke="#2a1b00" strokeWidth="1" strokeLinecap="round" strokeLinejoin="round" /></>,
  helpers: <><circle cx="8" cy="8" r="6.2" fill="none" stroke="var(--a-text-3)" strokeWidth="1.4" strokeDasharray="2.4 2.4" /><circle cx="8" cy="8" r="1.8" fill="var(--a-text-3)" /></>,
  paused: <><circle cx="8" cy="8" r="6.4" fill="none" stroke="var(--a-ask-fill)" strokeWidth="1.6" /><path d="M6.3 5.6v4.8M9.7 5.6v4.8" stroke="var(--a-ask-fill)" strokeWidth="1.7" strokeLinecap="round" /></>,
  blocked: <><path d="M8 1.6l6.4 6.4L8 14.4 1.6 8z" fill="none" stroke="var(--a-block)" strokeWidth="1.5" strokeLinejoin="round" /><path d="M5.6 8h4.8" stroke="var(--a-block)" strokeWidth="1.6" strokeLinecap="round" /></>,
  pending: <circle cx="8" cy="8" r="6.2" fill="none" stroke="var(--a-text-3)" strokeWidth="1.4" />,
  failed: <><path d="M8 1.8l6.6 11.8H1.4z" fill="var(--a-fail)" stroke="var(--a-fail)" strokeLinejoin="round" /><path d="M8 6.2v3.5" stroke="#fff" strokeWidth="1.6" strokeLinecap="round" /><circle cx="8" cy="11.8" r=".9" fill="#fff" /></>,
  done: <><circle cx="8" cy="8" r="6.4" fill="none" stroke="var(--a-text-3)" strokeWidth="1.3" /><path d="M5.2 8.3l1.9 1.9 3.7-3.9" fill="none" stroke="var(--a-text-2)" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></>,
  review: <><circle cx="8" cy="8" r="7" fill="var(--a-work)" /><path d="M5 8.3l2 2 4-4.1" fill="none" stroke="#fff" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" /></>,
  stopped: <rect x="3.5" y="3.5" width="9" height="9" rx="2.2" fill="var(--a-text-3)" />,
  removed: <><circle cx="8" cy="8" r="6.4" fill="none" stroke="var(--a-text-4)" strokeWidth="1.3" /><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4" stroke="var(--a-text-3)" strokeWidth="1.4" strokeLinecap="round" /></>,
}

// A status bead. When it sits next to visible status text, pass `decorative`; otherwise it is an
// image with the state's name, so colour and shape are never the only information.
export function StatusBead({ state, size = 16, decorative = false, label }) {
  const name = label || STATES[state] || 'Unknown state'
  return <svg className="a-bead" width={size} height={size} viewBox="0 0 16 16" role={decorative ? undefined : 'img'}
    aria-label={decorative ? undefined : name} aria-hidden={decorative || undefined} focusable="false">
    {decorative ? null : <title>{name}</title>}
    {SHAPES[state] || SHAPES.pending}
  </svg>
}
