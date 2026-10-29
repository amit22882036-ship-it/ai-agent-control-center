import { useEffect, useId, useRef, useState } from 'react'
import Icon from './Icon'
import { IconButton } from './primitives'
import { rovingTarget } from '../a11y.mjs'

// A menu of actions. Arrow keys move, Enter or Space chooses, Escape closes and returns focus.
export function MenuButton({ label, text, icon = 'more', items, align = 'end' }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const trigger = useRef(null)
  const menu = useRef(null)
  const enabled = items.filter((item) => !item.separator)

  useEffect(() => {
    if (!open) return undefined
    menu.current?.querySelector('[role="menuitem"]:not(:disabled)')?.focus()
    function outside(event) { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  function close(returnFocus = true) {
    setOpen(false)
    if (returnFocus) trigger.current?.focus()
  }
  function onMenuKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
    if (event.key === 'Tab') { close(false); return }
    const nodes = [...menu.current.querySelectorAll('[role="menuitem"]')]
    const index = nodes.indexOf(document.activeElement)
    const next = rovingTarget(index, event.key, nodes.length, { orientation: 'vertical', isDisabled: (i) => nodes[i].disabled })
    if (next !== null) { event.preventDefault(); nodes[next].focus() }
  }
  return <div className="a-menu-wrap" ref={root}>
    <button ref={trigger} type="button" className={text ? 'a-btn a-btn--secondary a-btn--sm' : 'a-iconbtn'}
      aria-haspopup="menu" aria-expanded={open} aria-label={text ? undefined : label} title={label}
      onClick={() => setOpen((value) => !value)}
      onKeyDown={(event) => { if (event.key === 'ArrowDown' && !open) { event.preventDefault(); setOpen(true) } }}>
      <Icon name={icon} size={text ? 14 : 17} />{text}
    </button>
    {open && <div ref={menu} className={`a-menu a-menu--${align}`} role="menu" aria-label={label} onKeyDown={onMenuKey}>
      {items.map((item, index) => item.separator ? <div key={`sep-${index}`} role="separator" className="a-menu-sep" />
        : <button key={item.label} type="button" role="menuitem" tabIndex={-1} disabled={item.disabled}
          className={`a-menu-item${item.danger ? ' a-menu-item--danger' : ''}`}
          onClick={() => { close(); item.onSelect?.() }}>
          {item.icon && <Icon name={item.icon} size={15} />}<span>{item.label}</span>
          {item.hint && <small>{item.hint}</small>}
        </button>)}
      {enabled.length === 0 && <p className="a-menu-empty">No actions available</p>}
    </div>}
  </div>
}

// A modal dialog built on the native <dialog> element: the browser keeps focus inside it, makes
// the page behind it inert, closes it on Escape and returns focus to the opening control.
export function Dialog({ open, onClose, title, description, children, actions, size = 'md' }) {
  const ref = useRef(null)
  const id = useId()
  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) dialog.showModal()
    if (!open && dialog.open) dialog.close()
  }, [open])
  return <dialog ref={ref} className={`a-dialog a-dialog--${size}`} aria-labelledby={`${id}-title`}
    aria-describedby={description ? `${id}-description` : undefined} onClose={onClose}
    onClick={(event) => { if (event.target === ref.current) onClose() }}>
    <div className="a-dialog-head">
      <h2 id={`${id}-title`} className="a-dialog-title">{title}</h2>
      <IconButton icon="close" label="Close" size="sm" onClick={onClose} />
    </div>
    {description && <p id={`${id}-description`} className="a-dialog-description">{description}</p>}
    {children && <div className="a-dialog-body">{children}</div>}
    {actions && <div className="a-dialog-foot">{actions}</div>}
  </dialog>
}

// Brief confirmations. The region is always present so screen readers announce new messages.
export function ToastRegion({ toasts, onDismiss }) {
  return <div className="a-toasts" role="status" aria-live="polite">
    {toasts.map((toast) => <div key={toast.id} className="a-toast">
      <Icon name={toast.icon || 'check'} size={15} /><span>{toast.message}</span>
      <button type="button" className="a-toast-close" aria-label="Dismiss" onClick={() => onDismiss(toast.id)}><Icon name="close" size={13} /></button>
    </div>)}
  </div>
}
