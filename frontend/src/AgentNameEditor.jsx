import { useState } from 'react'
import Icon from './Icon'
export default function AgentNameEditor({ agent, disabled, onSaved }) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  async function save(event) {
    event.preventDefault()
    if (saving || !name.trim()) return
    setSaving(true); setError('')
    try {
      const response = await fetch(`http://127.0.0.1:8000/agents/${agent.agent_id}/rename`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: name.trim() }),
      })
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Unable to save name. Use 1–80 characters.')
      onSaved(result.display_name); setEditing(false)
    } catch (err) { setError(err.message) } finally { setSaving(false) }
  }
  return <div className="name-editor">{editing ? <form onSubmit={save}>
    <label htmlFor="display-name">Agent name</label>
    <input id="display-name" autoFocus value={name} maxLength={80} disabled={saving} onChange={(event) => setName(event.target.value)} />
    <div className="details-actions"><button disabled={saving || disabled || !name.trim()} className="start-button">{saving ? 'Saving...' : 'Save name'}</button>
    <button type="button" disabled={saving} onClick={() => setEditing(false)}>Cancel</button></div>
  </form> : <button className="rename-button" disabled={disabled} onClick={() => { setName(agent.display_name); setEditing(true) }}><Icon name="edit" size={13} />Rename agent</button>}
  {error && <p role="alert" className="error">{error}</p>}</div>
}
