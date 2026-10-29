import { useId, useRef } from 'react'
import Icon from './Icon'
import { rovingTarget } from '../a11y.mjs'

const cx = (...names) => names.filter(Boolean).join(' ')

function focusIndex(container, selector, index) {
  container?.querySelectorAll(selector)[index]?.focus()
}

// A single choice among a few options (radio semantics): one Tab stop, arrow keys move and select.
export function Segmented({ label, labelledBy, value, options, onChange, size = 'md', className }) {
  const root = useRef(null)
  const selected = Math.max(0, options.findIndex((option) => option.value === value))
  function onKeyDown(event, index) {
    const dir = root.current ? getComputedStyle(root.current).direction : 'ltr'
    const next = rovingTarget(index, event.key, options.length, { isDisabled: (i) => options[i].disabled, dir })
    if (next === null) return
    event.preventDefault()
    onChange(options[next].value)
    focusIndex(root.current, '[role="radio"]', next)
  }
  return <div ref={root} className={cx('a-seg', size === 'sm' && 'a-seg--sm', className)} role="radiogroup"
    aria-label={labelledBy ? undefined : label} aria-labelledby={labelledBy}>
    {options.map((option, index) => <button key={option.value} type="button" role="radio" className="a-seg-item"
      aria-checked={option.value === value} tabIndex={index === selected ? 0 : -1} disabled={option.disabled}
      title={option.title} onClick={() => onChange(option.value)} onKeyDown={(event) => onKeyDown(event, index)}>
      {option.icon && <Icon name={option.icon} size={15} />}
      <span className={option.iconOnly ? 'a-sr' : undefined}>{option.label}</span>
      {option.count !== undefined && <span className="a-seg-count a-num">{option.count}</span>}
    </button>)}
  </div>
}

export function Switch({ checked, onChange, labelledBy, label, disabled, describedBy }) {
  return <button type="button" role="switch" className="a-switch" aria-checked={checked} aria-labelledby={labelledBy}
    aria-label={labelledBy ? undefined : label} aria-describedby={describedBy} disabled={disabled}
    onClick={() => onChange(!checked)} />
}

// Tabs with automatic activation; panels are rendered by the caller with `tabPanelProps`.
export function Tabs({ label, tabs, value, onChange, idBase }) {
  const root = useRef(null)
  function onKeyDown(event, index) {
    const dir = root.current ? getComputedStyle(root.current).direction : 'ltr'
    const next = rovingTarget(index, event.key, tabs.length, { dir })
    if (next === null) return
    event.preventDefault()
    onChange(tabs[next].id)
    focusIndex(root.current, '[role="tab"]', next)
  }
  return <div ref={root} className="a-tabs" role="tablist" aria-label={label}>
    {tabs.map((tab, index) => <button key={tab.id} type="button" role="tab" className="a-tab" id={`${idBase}-tab-${tab.id}`}
      aria-selected={tab.id === value} aria-controls={`${idBase}-panel-${tab.id}`} tabIndex={tab.id === value ? 0 : -1}
      onClick={() => onChange(tab.id)} onKeyDown={(event) => onKeyDown(event, index)}>{tab.label}</button>)}
  </div>
}

function Field({ label, hint, error, children, id }) {
  return <div className={cx('a-field', error && 'a-field--error')}>
    <label className="a-field-label" htmlFor={id}>{label}</label>
    {children}
    {error ? <p className="a-field-error" id={`${id}-error`}>{error}</p> : hint && <p className="a-field-hint" id={`${id}-hint`}>{hint}</p>}
  </div>
}

function described(id, hint, error) {
  return error ? `${id}-error` : hint ? `${id}-hint` : undefined
}

export function TextField({ label, hint, error, mono, ...props }) {
  const id = useId()
  return <Field label={label} hint={hint} error={error} id={id}>
    <input id={id} className={cx('a-input', mono && 'a-mono')} dir="auto" aria-invalid={error ? true : undefined}
      aria-describedby={described(id, hint, error)} {...props} />
  </Field>
}

export function TextArea({ label, hint, error, ...props }) {
  const id = useId()
  return <Field label={label} hint={hint} error={error} id={id}>
    <textarea id={id} className="a-textarea" dir="auto" aria-invalid={error ? true : undefined}
      aria-describedby={described(id, hint, error)} {...props} />
  </Field>
}

export function SelectField({ label, hint, error, options, ...props }) {
  const id = useId()
  return <Field label={label} hint={hint} error={error} id={id}>
    <span className="a-select"><select id={id} aria-describedby={described(id, hint, error)} {...props}>
      {options.map((option) => <option key={option.value} value={option.value} disabled={option.disabled}>{option.label}</option>)}
    </select><Icon name="down" size={14} /></span>
  </Field>
}
