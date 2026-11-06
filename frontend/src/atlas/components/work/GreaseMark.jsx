import { useEffect, useState } from 'react'

// The signature mark: what needs the person is circled, the way an editor marks a frame on a
// contact sheet with a grease pencil. It is drawn once, the first time a Task shows as needing the
// person in this window, and stays still afterwards. Decorative: the words beside it carry the
// meaning, and reduced motion shows it already drawn.
const drawn = new Set()

export default function GreaseMark({ id, className = '' }) {
  const [fresh] = useState(() => Boolean(id) && !drawn.has(id))
  useEffect(() => { if (id) drawn.add(id) }, [id])
  return <svg className={`a-mark${fresh ? ' is-fresh' : ''}${className ? ` ${className}` : ''}`} viewBox="0 0 44 36" aria-hidden="true" focusable="false">
    {/* An orienteering control: a true circle in course purple, drawn once when it first appears. */}
    <path pathLength="100" d="M22 3.5a14.5 14.5 0 1 1 0 29a14.5 14.5 0 1 1 0-29" />
  </svg>
}
