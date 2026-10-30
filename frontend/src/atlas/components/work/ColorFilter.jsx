import { useEffect, useRef, useState } from 'react'
import Icon from '../Icon'
import { COLOR_VALUES } from '../../model/workList.mjs'

const name = (value) => (value === 'none' ? 'No colour' : value[0].toUpperCase() + value.slice(1))

// Filter rows by the colour label of the agent each row shows (several colours at once). "No colour"
// covers tasks without an agent and agents that were removed, which show no colour label.
export default function ColorFilter({ selected, onChange }) {
  const [open, setOpen] = useState(false)
  const root = useRef(null)
  const trigger = useRef(null)
  // Opening moves focus into the panel; Escape closes it and returns focus to its button.
  useEffect(() => {
    if (!open) return undefined
    root.current?.querySelector('.a-colorpop button, .a-colorpop input')?.focus()
    const outside = (event) => { if (!root.current?.contains(event.target)) setOpen(false) }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])
  const chosen = COLOR_VALUES.filter((value) => selected.has(value))
  const label = chosen.length === 0 ? 'Colour · Any' : chosen.length === 1 ? `Colour · ${name(chosen[0])}` : `Colours · ${chosen.length}`
  const toggle = (value) => {
    const next = new Set(selected)
    if (next.has(value)) next.delete(value)
    else next.add(value)
    onChange(next)
  }
  return <div className="a-menu-wrap a-colorfilter" ref={root} onKeyDown={(event) => { if (event.key === 'Escape' && open) { event.stopPropagation(); setOpen(false); trigger.current?.focus() } }}>
    <button ref={trigger} type="button" className="a-btn a-btn--secondary a-btn--sm" aria-haspopup="dialog" aria-expanded={open}
      title={chosen.length ? `Agent colours: ${chosen.map(name).join(', ')}` : 'Agent colours: any'} onClick={() => setOpen((value) => !value)}>
      {chosen.length > 0 && <span className="a-colorfilter-dots" aria-hidden="true">{chosen.slice(0, 3).map((value) => <i key={value} data-color={value} />)}</span>}
      {label}<Icon name="down" size={13} stroke={2.2} />
    </button>
    {open && <div className="a-menu a-menu--start a-colorpop" role="dialog" aria-label="Filter by agent colour">
      <button type="button" className="a-menu-item" aria-pressed={chosen.length === 0} onClick={() => onChange(new Set())}>
        <span>Any colour</span>{chosen.length === 0 && <Icon name="check" size={14} />}</button>
      <div role="separator" className="a-menu-sep" />
      {COLOR_VALUES.map((value) => <label key={value} className="a-check a-colorpick">
        <input type="checkbox" checked={selected.has(value)} onChange={() => toggle(value)} />
        <span className="a-swatch-dot a-swatch-dot--sm" data-color={value} aria-hidden="true" /><span>{name(value)}</span>
      </label>)}
      <p className="a-fine a-colorpop-note">The colour of the agent each task shows. Removed agents and tasks without an agent have no colour.</p>
    </div>}
  </div>
}
