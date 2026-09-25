import { useId, useState } from 'react'
import Popover from './Popover'
import { displayColor, displayColors } from './agentIdentity.mjs'

export default function AgentColorPicker({ agent, onSaved }) {
  const id = useId()
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const color = displayColor(agent.display_color)
  async function choose(value) {
    if (saving || value === color) return
    setSaving(true)
    setError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${encodeURIComponent(agent.agent_id)}/color`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_color: value }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(result.detail || 'Unable to save color.')
      onSaved(result.display_color)
    } catch (err) { setError(err.message) } finally { setSaving(false) }
  }
  return <Popover className="agent-color-picker" label={`Agent color: ${color}`}
    trigger={<><span className="color-swatch" data-display-color={color} />Color</>}>
    <fieldset className="choice-group" disabled={saving}><legend>Agent display color</legend>
      {displayColors.map((value) => <label className="choice color-choice" htmlFor={`${id}-${value}`} key={value}>
        <input id={`${id}-${value}`} type="radio" name={`color-${agent.agent_id}`} value={value}
          checked={color === value} onChange={() => choose(value)} />
        <span className="radio-indicator" aria-hidden="true" />
        <span className="color-swatch" data-display-color={value} />
        <span className="color-choice-label">{value[0].toUpperCase() + value.slice(1)}</span>
      </label>)}
    </fieldset>
    {saving && <p role="status" className="agent-type-note">Saving color...</p>}
    {error && <p role="alert" className="error">{error}</p>}
  </Popover>
}
