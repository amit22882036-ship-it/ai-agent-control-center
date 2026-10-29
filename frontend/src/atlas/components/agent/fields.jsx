import { useId } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { Dialog } from '../overlays'

const cx = (...names) => names.filter(Boolean).join(' ')

// A labelled checkbox; the label is the full statement the person accepts.
export function Check({ checked, onChange, disabled, children, required, tone }) {
  const id = useId()
  return <label className={cx('a-check', tone && `a-check--${tone}`)} htmlFor={id}>
    <input id={id} type="checkbox" checked={checked === true} disabled={disabled} required={required}
      onChange={(event) => onChange(event.target.checked)} />
    <span>{children}</span>
  </label>
}

// A native select with option groups (the model catalog's Available / Unverified / Unavailable).
export function GroupedSelect({ label, hint, value, onChange, disabled, groups, before = [], after = [], hintTone }) {
  const id = useId()
  const option = (item) => <option key={item.value} value={item.value} disabled={item.disabled}>{item.label}</option>
  return <div className="a-field">
    <label className="a-field-label" htmlFor={id}>{label}</label>
    <span className="a-select">
      <select id={id} value={value} disabled={disabled} aria-describedby={hint ? `${id}-hint` : undefined}
        onChange={(event) => onChange(event.target.value)}>
        {before.map(option)}
        {groups.filter((group) => group.items.length).map((group) => <optgroup key={group.label} label={group.label}>{group.items.map(option)}</optgroup>)}
        {after.map(option)}
      </select>
      <Icon name="down" size={14} />
    </span>
    {hint && <p className={cx('a-field-hint', hintTone && `a-hint--${hintTone}`)} id={`${id}-hint`}>{hint}</p>}
  </div>
}

// A plain select without groups.
export function PlainSelect({ label, value, onChange, disabled, options, hint }) {
  return <GroupedSelect label={label} value={value} onChange={onChange} disabled={disabled} hint={hint} groups={[]} before={options} />
}

// A section that opens on request (progressive disclosure for advanced or rarely used controls).
export function Disclosure({ summary, children, open, onToggle, className }) {
  return <details className={cx('a-disclosure', className)} open={open}
    onToggle={onToggle ? (event) => onToggle(event.currentTarget.open) : undefined}>
    <summary><Icon name="right" size={13} stroke={2.2} /><span>{summary}</span></summary>
    <div className="a-disclosure-body">{children}</div>
  </details>
}

// The outcome of the last action, announced to screen readers. Errors are alerts.
export function ActionResult({ result, keys }) {
  if (!result || (keys && !keys.includes(result.key))) return null
  const error = result.ok === false
  return <div className={cx('a-result', error ? 'a-result--error' : result.tone === 'warn' ? 'a-result--warn' : 'a-result--ok')}
    role={error ? 'alert' : 'status'}>
    <Icon name={error ? 'warn' : result.tone === 'warn' ? 'info' : 'check'} size={15} />
    <div>{result.message}{result.details?.length > 0 && <ul>{result.details.map((line) => <li key={line}>{line}</li>)}</ul>}</div>
  </div>
}

// A confirmation for an action that stops work or cannot be undone. The confirm button carries the
// action's own name; Cancel and Escape do nothing.
export function ConfirmDialog({ open, title, children, confirmLabel, onConfirm, onCancel, busy, danger = true }) {
  return <Dialog open={open} onClose={onCancel} title={title}
    actions={<>
      <Button onClick={onCancel} disabled={busy}>Cancel</Button>
      <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} disabled={busy} aria-busy={busy || undefined}>{confirmLabel}</Button>
    </>}>
    {children}
  </Dialog>
}
