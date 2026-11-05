import { useState } from 'react'
import Icon from '../Icon'
import RenameField from '../projects/RenameField'
import { agentActions, refusalText } from '../../api/agentClient.mjs'

// A Task's title with an inline Rename. Only the display title changes: the agent's instructions,
// the hierarchy, assignments and history stay as they are (PATCH /tasks/{id}).
export default function TaskTitle({ taskId, title, as: Tag = 'div', className = '', headingProps = {}, onRenamed }) {
  const [editing, setEditing] = useState(false)
  const shown = title || 'Untitled task'
  async function save(next) {
    const result = await agentActions.renameTask(taskId, next)
    if (result.ok) { onRenamed?.(result.data); return { ok: true } }
    return { ok: false, message: refusalText(result, 'The title could not be saved.') }
  }
  if (editing) {
    return <RenameField value={title || ''} label="New task title" onSave={save} onDone={() => setEditing(false)} />
  }
  return <span className="a-ttitle">
    <Tag className={`${className} a-bidi`} dir="auto" {...headingProps}>{shown}</Tag>
    <button type="button" className="a-ttitle-edit" aria-label={`Rename task: ${shown}`} title="Rename this task"
      onClick={() => setEditing(true)}><Icon name="edit" size={14} /></button>
  </span>
}
