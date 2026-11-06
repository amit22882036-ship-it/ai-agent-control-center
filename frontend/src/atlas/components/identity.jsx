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

// State marks are small blocks of the model, in the material each state is built of (the same
// materials the Site draws): a block seen from above and the front, its three faces lit from the
// left. Shape still differs where colour alone could be missed (wireframe, hatching, footprint).
const TOP = 'M8 1.8 14 5.1 8 8.4 2 5.1z'
const LEFT = 'M2 5.1 8 8.4V14.6L2 11.3z'
const RIGHT = 'M14 5.1 8 8.4V14.6L14 11.3z'
const block = (name, extra = null, stroke = 'var(--a-mat-edge)') => <>
  <path d={LEFT} fill={`var(--a-mat-${name}-l)`} />
  <path d={RIGHT} fill={`var(--a-mat-${name}-r)`} />
  <path d={TOP} fill={`var(--a-mat-${name}-top)`} />
  <path d="M8 1.8 14 5.1V11.3L8 14.6 2 11.3V5.1zM8 8.4V14.6M2 5.1 8 8.4 14 5.1" fill="none" stroke={stroke} strokeWidth=".8" strokeLinejoin="round" />
  {extra}
</>
const SHAPES = {
  // An agent working: lit from within.
  work: block('lit', null, 'var(--a-mat-lit-r)'),
  // Needs you: the one vermilion piece.
  ask: block('needs', <path d="M8 4.3 10.4 5.6 8 6.9 5.6 5.6z" fill="var(--a-mat-needs-top)" opacity=".9" />, 'var(--a-mat-needs-r)'),
  // Waiting: glass.
  helpers: block('glass', null, 'var(--a-mat-glass-edge)'),
  // On hold: hatched card; paused shows its two bars, blocked a bar.
  paused: block('held', <path d="M10.2 8.3v3.4M12.3 7.2v3.4" stroke="var(--a-mat-hatch)" strokeWidth="1.3" strokeLinecap="round" />),
  blocked: block('held', <path d="M9.6 10.6 12.8 8.8" stroke="var(--a-mat-hatch)" strokeWidth="1.4" strokeLinecap="round" />),
  // Not started: the wireframe of what is planned.
  pending: <path d="M8 1.8 14 5.1V11.3L8 14.6 2 11.3V5.1zM8 8.4V14.6M2 5.1 8 8.4 14 5.1" fill="none" stroke="var(--a-mat-wire)" strokeWidth="1" strokeLinejoin="round" strokeDasharray="1.6 1.2" />,
  // Failed: a charred block with its alert face.
  failed: <><path d={LEFT} fill="#3A3F47" /><path d={RIGHT} fill="var(--a-fail)" /><path d={TOP} fill="#4A505A" />
    <path d="M10.9 8.6v2.1M10.9 12.1v.1" stroke="#fff" strokeWidth="1.2" strokeLinecap="round" /></>,
  // Done: built, solid white card.
  done: block('built', null, 'rgba(40, 44, 52, .5)'),
  // Reviewing: lit, with its check on the lid.
  review: block('lit', <path d="M5.8 5.2 7.4 6.1 10.4 4.3" fill="none" stroke="var(--a-work)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />, 'var(--a-mat-lit-r)'),
  // Canceled: demolished, only its footprint is left.
  stopped: <><path d="M8 6.2 14 9.5 8 12.8 2 9.5z" fill="none" stroke="var(--a-text-2)" strokeWidth="1.1" strokeDasharray="1.8 1.3" strokeLinejoin="round" />
    <path d="M5.6 8.1l4.8 2.8M10.4 8.1l-4.8 2.8" stroke="var(--a-text-2)" strokeWidth="1.1" strokeLinecap="round" /></>,
  // The agent was removed: a ghost of the block.
  removed: <path d="M8 1.8 14 5.1V11.3L8 14.6 2 11.3V5.1zM8 8.4V14.6M2 5.1 8 8.4 14 5.1" fill="none" stroke="var(--a-text-4)" strokeWidth="1" strokeLinejoin="round" />,
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
