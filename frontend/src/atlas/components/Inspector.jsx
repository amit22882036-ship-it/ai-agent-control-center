import { useRef } from 'react'
import { IconButton } from './primitives'
import { INSPECTOR, dragInspectorWidth, keyboardInspectorWidth } from '../layout.mjs'

// The docked detail panel used across Atlas (agent details in Work, team members on Task pages).
// It can be resized with the pointer or the keyboard and closed with Escape.
export default function Inspector({ label, width, onResize, onClose, header, tabs, footer, children }) {
  const drag = useRef(null)
  function finish(event) {
    drag.current = null
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }
  return <aside className="a-inspector" style={{ '--a-inspector-w': `${width}px` }} aria-label={label}
    onKeyDown={(event) => { if (event.key === 'Escape' && onClose) { event.stopPropagation(); onClose() } }}>
    {onResize && <div className="a-inspector-resize" role="separator" tabIndex={0} aria-orientation="vertical"
      aria-label={`Resize ${label}`} aria-valuemin={INSPECTOR.min} aria-valuemax={INSPECTOR.max} aria-valuenow={width}
      title="Drag or use the arrow keys to resize. Double-click or press Home to reset."
      onDoubleClick={() => onResize(INSPECTOR.default)}
      onKeyDown={(event) => {
        const next = keyboardInspectorWidth(width, event.key)
        if (next !== null) { event.preventDefault(); onResize(next) }
      }}
      onPointerDown={(event) => {
        if (event.button !== 0) return
        event.preventDefault()
        event.currentTarget.focus()
        drag.current = { x: event.clientX, width }
        event.currentTarget.setPointerCapture?.(event.pointerId)
      }}
      onPointerMove={(event) => { if (drag.current) onResize(dragInspectorWidth(drag.current.width, drag.current.x, event.clientX)) }}
      onPointerUp={finish} onPointerCancel={finish} />}
    <div className="a-inspector-head">
      <div className="a-inspector-headmain">{header}</div>
      {onClose && <IconButton icon="close" label={`Close ${label}`} size="sm" onClick={onClose} />}
    </div>
    {tabs}
    <div className="a-inspector-body">{children}</div>
    {footer && <div className="a-inspector-foot">{footer}</div>}
  </aside>
}
