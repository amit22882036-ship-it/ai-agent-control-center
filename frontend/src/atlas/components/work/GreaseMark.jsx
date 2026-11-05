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
    <path pathLength="100" d="M30.5 5.2C22 2.1 9.6 3.6 5.1 10.8 1.2 17.1 4.6 27.4 14.9 30.9c9.4 3.2 22.7.3 25.5-8.3 2.5-7.7-4.2-15.8-15-17.7-4.3-.7-8.6-.3-11.8 1" />
  </svg>
}
