import { useEffect, useRef, useState } from 'react'
import Icon from '../Icon'

// Inline renaming: the name becomes a field in place, Enter saves, Escape restores. The caller's
// `onSave(value)` returns `{ ok, message }`; while it runs the field is read only, and a refusal
// keeps the typed text with the backend's reason under it.
export default function RenameField({ value, label, maxLength = 100, onSave, onDone, className = '' }) {
  const [text, setText] = useState(value)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const input = useRef(null)
  useEffect(() => { input.current?.focus(); input.current?.select() }, [])

  async function save() {
    const next = text.replace(/\s+/g, ' ').trim()
    if (busy) return
    if (!next || next === value) { onDone(false); return }
    setBusy(true)
    setError('')
    const result = await onSave(next)
    setBusy(false)
    if (result?.ok) onDone(true)
    else setError(result?.message || 'The name could not be saved.')
  }
  return <span className={`a-irename ${className}`}>
    <input ref={input} name="rename" className="a-irename-input" value={text} maxLength={maxLength} dir="auto" aria-label={label}
      readOnly={busy} aria-invalid={error ? true : undefined} aria-describedby={error ? 'a-irename-error' : undefined}
      onChange={(event) => { setText(event.target.value); setError('') }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') { event.preventDefault(); save() }
        if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); onDone(false) }
      }}
      onBlur={() => { if (!error) save() }} />
    <button type="button" className="a-irename-ok" aria-label="Save name" title="Save · Enter" onMouseDown={(event) => event.preventDefault()}
      onClick={save} disabled={busy}><Icon name="check" size={14} stroke={2.2} /></button>
    {error && <span id="a-irename-error" className="a-irename-error" role="alert">{error}</span>}
  </span>
}
