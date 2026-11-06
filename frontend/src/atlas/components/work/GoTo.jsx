import { useEffect, useId, useMemo, useRef, useState } from 'react'
import Icon from '../Icon'
import { StatusBead } from '../identity'
import { agentLabel } from '../../model/workList.mjs'
import { attentionKind } from '../../model/attention.mjs'

const PAGES = [
  { id: 'page:work', label: 'Work', hint: 'Every task, as a list or a map', icon: 'work', route: ['work', {}] },
  { id: 'page:new', label: 'New task', hint: 'Describe work for an agent', icon: 'plus', route: ['new', {}], key: 'N' },
  { id: 'page:projects', label: 'Projects', hint: 'Order, rename, hide or move to the Trash', icon: 'folder', route: ['projects', {}] },
  { id: 'page:connections', label: 'Connections', hint: 'AI tools, sign-in, models and limits', icon: 'plug', route: ['connections', {}] },
  { id: 'page:settings', label: 'Settings', hint: 'Appearance, notifications, keys', icon: 'sliders', route: ['settings', {}] },
  { id: 'page:map', label: 'Work map', hint: 'Who works on what, drawn as a map', icon: 'map', route: ['work', { lens: 'map' }] },
]

function score(text, q) {
  const value = text.toLowerCase()
  if (value.startsWith(q)) return 3
  if (value.includes(` ${q}`)) return 2
  if (value.includes(q)) return 1
  return 0
}

// "Go to": one field that finds any loaded task (by title, agent, project or id), any project and
// any page, and remembers where the person was. Arrow keys choose, Enter goes, Escape closes.
export default function GoTo({ open, onClose, store, derived, projects, recent, onTask, onRoute }) {
  const ref = useRef(null)
  const input = useRef(null)
  const id = useId()
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return
    if (open && !dialog.open) { dialog.showModal(); setQuery(''); setActive(0); requestAnimationFrame(() => input.current?.focus()) }
    if (!open && dialog.open) dialog.close()
  }, [open])

  const projectName = (projectId) => projects.find((project) => project.id === projectId)?.name || 'No project'
  const taskItem = (taskId, group) => {
    const task = store.tasks[taskId]
    const status = derived.status[taskId]
    const agent = derived.agents[taskId]?.current || derived.agents[taskId]?.last
    return { id: `task:${taskId}:${group}`, taskId, group, label: task.title || 'Untitled task', bead: status.bead,
      hint: [projectName(task.projectId), agent ? agentLabel(agent) : null, status.label].filter(Boolean).join(' · ') }
  }

  const groups = useMemo(() => {
    if (!open) return []
    const q = query.trim().toLowerCase()
    if (!q) {
      const out = []
      const seen = (recent || []).filter((taskId) => store.tasks[taskId]).slice(0, 6)
      if (seen.length) out.push({ label: 'Recently opened', items: seen.map((taskId) => taskItem(taskId, 'recent')) })
      const needs = Object.keys(store.tasks).filter((taskId) => attentionKind(derived, taskId)).slice(0, 6)
      if (needs.length) out.push({ label: 'Needs you', items: needs.map((taskId) => taskItem(taskId, 'needs')) })
      out.push({ label: 'Pages', items: PAGES })
      return out
    }
    const tasks = []
    for (const taskId of Object.keys(store.tasks)) {
      const task = store.tasks[taskId]
      const agent = derived.agents[taskId]?.current || derived.agents[taskId]?.last
      // Earlier agents count too, so a task is found by any agent that ever held it, or by an id.
      const earlier = (derived.agents[taskId]?.previous || []).filter((item) => !item.unknown).map((item) => score(agentLabel(item) || '', q))
      const best = Math.max(score(task.title || '', q) * 3, score(agentLabel(agent) || '', q), ...earlier, score(projectName(task.projectId), q),
        taskId.toLowerCase() === q || agent?.id?.toLowerCase() === q ? 9 : 0)
      if (best > 0) tasks.push([best, task.createdAt || '', taskId])
    }
    tasks.sort((a, b) => b[0] - a[0] || (a[1] < b[1] ? 1 : a[1] > b[1] ? -1 : 0))
    const out = []
    if (tasks.length) out.push({ label: tasks.length > 40 ? `Tasks · first 40 of ${tasks.length}` : 'Tasks', items: tasks.slice(0, 40).map(([, , taskId]) => taskItem(taskId, 'match')) })
    const matchedProjects = projects.filter((project) => project.name.toLowerCase().includes(q)).slice(0, 6)
    if (matchedProjects.length) {
      out.push({ label: 'Projects', items: matchedProjects.map((project) => ({ id: `project:${project.id}`, label: project.name, hint: 'Show its work',
        icon: 'folder', route: ['work', { project: project.id }] })) })
    }
    const pages = PAGES.filter((page) => page.label.toLowerCase().includes(q) || page.hint.toLowerCase().includes(q))
    if (pages.length) out.push({ label: 'Pages', items: pages })
    return out
  // The store and derived workspace are read when the field changes or the palette opens.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, query, store, derived, recent, projects])
  const flat = groups.flatMap((group) => group.items)
  const current = Math.min(active, Math.max(0, flat.length - 1))

  function choose(item) {
    if (!item) return
    onClose()
    if (item.taskId) onTask(item.taskId)
    else if (item.route) onRoute(...item.route)
  }
  function onKeyDown(event) {
    if (event.key === 'ArrowDown') { event.preventDefault(); setActive((current + 1) % Math.max(1, flat.length)) }
    else if (event.key === 'ArrowUp') { event.preventDefault(); setActive((current - 1 + flat.length) % Math.max(1, flat.length)) }
    else if (event.key === 'Enter') { event.preventDefault(); choose(flat[current]) }
  }
  useEffect(() => {
    ref.current?.querySelector(`#${CSS.escape(`${id}-o${current}`)}`)?.scrollIntoView({ block: 'nearest' })
  }, [current, id])

  let index = -1
  return <dialog ref={ref} className="a-dialog a-goto" aria-label="Go to" onClose={onClose}
    onClick={(event) => { if (event.target === ref.current) onClose() }}>
    {/* Only while open: a listbox in the page would count as an open overlay and pause page keys. */}
    {open && <>
    <div className="a-goto-field">
      <Icon name="search" size={18} />
      <input ref={input} type="text" name="go-to" role="combobox" aria-expanded="true" aria-controls={`${id}-list`} aria-autocomplete="list"
        aria-activedescendant={flat.length ? `${id}-o${current}` : undefined} aria-label="Go to a task, project or page"
        placeholder="Go to a task, project or page…" value={query} dir="auto"
        onChange={(event) => { setQuery(event.target.value); setActive(0) }} onKeyDown={onKeyDown} />
      <kbd className="a-kbd">Esc</kbd>
    </div>
    <div className="a-goto-list" id={`${id}-list`} role="listbox" aria-label="Results">
      {groups.map((group) => <div key={group.label} role="group" aria-label={group.label} className="a-goto-group">
        <div className="a-goto-head" aria-hidden="true">{group.label}</div>
        {group.items.map((item) => {
          index += 1
          const mine = index
          return <div key={item.id} id={`${id}-o${mine}`} role="option" aria-selected={mine === current} className="a-goto-item"
            onPointerMove={() => setActive(mine)} onClick={() => choose(item)}>
            <span className="a-goto-icon">{item.bead ? <StatusBead state={item.bead} decorative /> : <Icon name={item.icon || 'right'} size={15} />}</span>
            <span className="a-goto-text"><b className="a-bidi" dir="auto">{item.label}</b><small className="a-bidi" dir="auto">{item.hint}</small></span>
            {item.key && <kbd className="a-kbd">{item.key}</kbd>}
            <Icon name="right" size={14} />
          </div>
        })}
      </div>)}
      {!flat.length && <p className="a-goto-empty">Nothing loaded matches “{query.trim()}”. Older tasks load from Work.</p>}
    </div>
    </>}
  </dialog>
}
