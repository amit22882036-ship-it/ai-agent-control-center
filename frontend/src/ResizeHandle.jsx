import { useRef } from 'react'
import { dragWidth, keyboardWidth, panelLimits } from './panelLayout.mjs'

export default function ResizeHandle({ side, width, onChange }) {
  const drag = useRef(null)
  function finish(event) {
    drag.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  return <div className={`resize-handle resize-${side}`} role="separator" tabIndex={0}
    aria-orientation="vertical" aria-label={`Resize ${side === 'left' ? 'sidebar' : 'agent details'}`}
    aria-valuemin={panelLimits[side].min} aria-valuemax={panelLimits[side].max} aria-valuenow={width}
    title="Drag or use arrow keys to resize. Double-click or press Home to reset."
    onDoubleClick={() => onChange(panelLimits[side].default)}
    onKeyDown={(event) => {
      const next = keyboardWidth(side, width, event.key)
      if (next !== null) { event.preventDefault(); onChange(next) }
    }}
    onPointerDown={(event) => {
      if (event.button !== 0) return
      event.preventDefault()
      event.currentTarget.focus()
      drag.current = { x: event.clientX, width }
      event.currentTarget.setPointerCapture(event.pointerId)
    }}
    onPointerMove={(event) => {
      if (drag.current) onChange(dragWidth(side, drag.current.width, drag.current.x, event.clientX))
    }}
    onPointerUp={finish} onPointerCancel={finish} onLostPointerCapture={() => { drag.current = null }} />
}
