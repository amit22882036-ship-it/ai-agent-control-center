import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { viewportShift } from './popoverPlacement.mjs'
export default function Popover({ label, trigger, children, className = '' }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const button = useRef(null)
  const surface = useRef(null)
  useEffect(() => {
    if (!open) return
    function outside(event) { if (!root.current?.contains(event.target)) setOpen(false) }
    function escape(event) {
      if (event.key === 'Escape') { event.stopPropagation(); setOpen(false); button.current?.focus() }
    }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('focusin', outside)
    root.current?.addEventListener('keydown', escape)
    const element = root.current
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('focusin', outside)
      element?.removeEventListener('keydown', escape)
    }
  }, [open])
  // The surface is anchored to the trigger's right edge; on a narrow screen it is moved back
  // inside the viewport so every action stays reachable.
  useLayoutEffect(() => {
    const element = surface.current
    if (!open || !element) return undefined
    function place() {
      // `translate` composes with the opening animation's transform instead of waiting for it to end.
      element.style.translate = ''
      const shift = viewportShift(element.getBoundingClientRect(), document.documentElement.clientWidth)
      element.style.translate = shift ? `${shift}px 0` : ''
    }
    place()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])
  return <div className={`popover ${className}`} ref={root}>
    <button ref={button} type="button" className="popover-trigger" aria-label={label} title={label} aria-expanded={open} onClick={() => setOpen(!open)}>{trigger}</button>
    {open && <div className="popover-surface" aria-label={label} ref={surface}
      onClick={(event) => { if (event.target.closest?.('[data-popover-close]')) setOpen(false) }}>{children}</div>}
  </div>
}
