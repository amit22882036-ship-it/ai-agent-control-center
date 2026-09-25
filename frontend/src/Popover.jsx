import { useEffect, useRef, useState } from 'react'
export default function Popover({ label, trigger, children, className = '' }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const button = useRef(null)
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
  return <div className={`popover ${className}`} ref={root}>
    <button ref={button} type="button" className="popover-trigger" aria-label={label} title={label} aria-expanded={open} onClick={() => setOpen(!open)}>{trigger}</button>
    {open && <div className="popover-surface" aria-label={label}>{children}</div>}
  </div>
}
