import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import Icon from '../Icon'
import { Button } from '../primitives'
import { MenuButton } from '../overlays'
import RenameField from './RenameField'
import { dropProject, moveProject, projectHue } from '../../model/projects.mjs'

const reduced = () => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
// A critically damped spring (no overshoot) as a CSS easing, for reflowing plates; the dropped plate
// settles with a little give because it was moved by hand.
const SETTLE = 'linear(0, 0.157 4.6%, 0.48 11.5%, 0.73 19.2%, 0.877 27.5%, 0.953 37%, 0.986 48.5%, 1)'
const GIVE = 'linear(0, 0.21 5%, 0.65 13%, 0.94 21%, 1.04 29%, 1.05 36%, 1.02 46%, 1)'

// The person's projects as plates they can arrange: drag a handle, or focus it and use the keyboard
// (Space to pick up, arrows to move, Space to drop, Escape to cancel; Alt and an arrow moves at
// once). Dragging is never the only way: every plate's menu has Move up and Move down.
export default function ProjectPlates({ projects, order, stats, onCommit, onRename, onOpen, onMap, onNewTask, onHide, onTrash,
  announce, renderPicture, reorderable = true }) {
  const [draft, setDraft] = useState(null) // the order while a keyboard move is in progress
  const [grabbed, setGrabbed] = useState(null)
  const [drag, setDrag] = useState(null) // { id, offset }
  const [renaming, setRenaming] = useState(null)
  const list = useRef(null)
  const before = useRef(null)
  const dropped = useRef(null)
  const shown = draft ? draft.map((id) => projects.find((project) => project.id === id)).filter(Boolean) : projects

  // FLIP: a plate whose place changed glides from where it was.
  const remember = () => {
    const rects = new Map()
    for (const node of list.current?.querySelectorAll('[data-plate]') || []) rects.set(node.dataset.plate, node.getBoundingClientRect())
    before.current = rects
  }
  useLayoutEffect(() => {
    const rects = before.current
    before.current = null
    if (!rects || reduced()) return
    for (const node of list.current?.querySelectorAll('[data-plate]') || []) {
      const was = rects.get(node.dataset.plate)
      if (!was) continue
      const dy = was.top - node.getBoundingClientRect().top
      if (Math.abs(dy) < 1) continue
      const lifted = dropped.current === node.dataset.plate
      node.animate([{ transform: `translateY(${dy}px)${lifted ? ' scale(1.02)' : ''}` }, { transform: 'none' }],
        { duration: lifted ? 520 : 380, easing: lifted ? GIVE : SETTLE })
    }
    dropped.current = null
  })

  const visibleIds = projects.map((project) => project.id)
  const commit = useCallback(async (nextVisible, id) => {
    const next = dropProject(order, visibleIds, id, nextVisible.indexOf(id))
    if (next.join() === order.join()) return
    remember()
    dropped.current = id
    await onCommit(next, id)
  }, [order, visibleIds, onCommit])

  // ------------------------------------------------------------ keyboard
  function onHandleKey(event, project, index) {
    const ids = draft || visibleIds
    if (event.key === ' ' || event.key === 'Enter') {
      event.preventDefault()
      if (grabbed === project.id) {
        const position = ids.indexOf(project.id)
        setGrabbed(null)
        setDraft(null)
        announce(`${project.name} dropped at position ${position + 1} of ${ids.length}.`)
        commit(ids, project.id)
      } else {
        setGrabbed(project.id)
        setDraft(visibleIds)
        announce(`${project.name} picked up, position ${index + 1} of ${ids.length}. Use the arrow keys to move it, Space to drop it, Escape to cancel.`)
      }
      return
    }
    if (event.key === 'Escape' && grabbed === project.id) {
      event.preventDefault()
      event.stopPropagation()
      remember()
      setGrabbed(null)
      setDraft(null)
      announce('Move canceled. The order is unchanged.')
      return
    }
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      const offset = event.key === 'ArrowUp' ? -1 : 1
      if (grabbed === project.id) {
        event.preventDefault()
        remember()
        const next = moveProject(ids, project.id, offset)
        setDraft(next)
        announce(`${project.name}, position ${next.indexOf(project.id) + 1} of ${next.length}.`)
      } else if (event.altKey) {
        event.preventDefault()
        const next = moveProject(visibleIds, project.id, offset)
        commit(next, project.id)
        announce(`${project.name} moved to position ${next.indexOf(project.id) + 1} of ${next.length}.`)
      }
    }
  }

  // ------------------------------------------------------------ pointer
  function onPointerDown(event, project) {
    if (event.button !== 0) return
    event.preventDefault()
    const node = event.currentTarget.closest('[data-plate]')
    const nodes = [...list.current.querySelectorAll('[data-plate]')]
    const rects = nodes.map((item) => item.getBoundingClientRect())
    const from = nodes.indexOf(node)
    const pitch = rects.length > 1 ? rects[1].top - rects[0].top : rects[0].height + 12
    const scroller = node.closest('.a-main')
    const start = { y: event.clientY, scroll: scroller?.scrollTop || 0 }
    let to = from
    let moved = false
    let pointerY = event.clientY
    let frame = 0
    event.currentTarget.setPointerCapture(event.pointerId)

    const place = () => {
      const offset = pointerY - start.y + ((scroller?.scrollTop || 0) - start.scroll)
      const min = -from * pitch
      const max = (nodes.length - 1 - from) * pitch
      // Resistance past the ends, so the plate never looks stuck.
      const band = (over) => (over * 120) / (120 + Math.abs(over))
      const y = offset < min ? min - band(min - offset) : offset > max ? max + band(offset - max) : offset
      to = Math.max(0, Math.min(nodes.length - 1, Math.round(from + y / pitch)))
      setDrag({ id: project.id, offset: y, to, from, pitch })
    }
    const tick = () => {
      if (scroller) {
        const bounds = scroller.getBoundingClientRect()
        const edge = 64
        const speed = pointerY < bounds.top + edge ? -(bounds.top + edge - pointerY) / 5 : pointerY > bounds.bottom - edge ? (pointerY - (bounds.bottom - edge)) / 5 : 0
        if (speed) { scroller.scrollTop += speed; place() }
      }
      frame = requestAnimationFrame(tick)
    }
    const move = (moveEvent) => {
      pointerY = moveEvent.clientY
      if (!moved && Math.abs(pointerY - start.y) < 4) return
      if (!moved) { moved = true; frame = requestAnimationFrame(tick); announce(`${project.name} picked up.`) }
      place()
    }
    const up = () => {
      cancelAnimationFrame(frame)
      node.removeEventListener('pointermove', move)
      node.removeEventListener('pointerup', up)
      node.removeEventListener('pointercancel', up)
      if (!moved) { setDrag(null); return }
      const next = moveProject(visibleIds, project.id, to - from)
      remember()
      setDrag(null)
      if (to !== from) {
        commit(next, project.id)
        announce(`${project.name} moved to position ${to + 1} of ${nodes.length}.`)
      } else announce(`${project.name} dropped where it was.`)
    }
    node.addEventListener('pointermove', move)
    node.addEventListener('pointerup', up)
    node.addEventListener('pointercancel', up)
  }

  const shiftFor = (index) => {
    if (!drag) return 0
    if (index === drag.from) return null
    if (drag.from < drag.to && index > drag.from && index <= drag.to) return -drag.pitch
    if (drag.from > drag.to && index < drag.from && index >= drag.to) return drag.pitch
    return 0
  }

  return <ol className={`a-plates${drag ? ' is-sorting' : ''}`} ref={list} aria-label="Your projects, in your order"
    aria-describedby="a-plates-help">
    {shown.map((project, index) => {
      const stat = stats(project.id)
      const shift = shiftFor(index)
      const lifted = drag?.id === project.id
      const style = lifted ? { transform: `translateY(${drag.offset}px) scale(1.02)` } : shift ? { transform: `translateY(${shift}px)` } : undefined
      return <li key={project.id} data-plate={project.id}
        className={`a-plate${lifted ? ' is-lifted' : ''}${grabbed === project.id ? ' is-grabbed' : ''}`} style={style}>
        <div className="a-plate-glass">
          {reorderable ? <button type="button" className="a-plate-handle" aria-pressed={grabbed === project.id}
            aria-label={`Move ${project.name}, position ${index + 1} of ${shown.length}`}
            title="Drag to reorder, or press Space and use the arrow keys" onPointerDown={(event) => onPointerDown(event, project)}
            onKeyDown={(event) => onHandleKey(event, project, index)}>
            <Icon name="grip" size={18} stroke={3} />
          </button> : <span className="a-plate-handle" aria-hidden="true" />}
          {renderPicture(project.id)}
          <div className="a-plate-text">
            {renaming === project.id
              ? <RenameField value={project.name} label={`New name for ${project.name}`} onSave={(name) => onRename(project, name)}
                onDone={() => setRenaming(null)} />
              : <button type="button" className="a-plate-name a-bidi" dir="auto" onClick={() => onOpen(project)}
                title="Show this project's work"><span className="a-ptile" aria-hidden="true" style={{ '--hue': projectHue(project.id) }} />{project.name}</button>}
            <span className="a-plate-path a-mono" title={project.path}>{project.path}</span>
            <span className="a-plate-quiet">{stat.tasks} task{stat.tasks === 1 ? '' : 's'} in {stat.families} famil{stat.families === 1 ? 'y' : 'ies'}</span>
          </div>
          <div className="a-plate-stats a-pstats">
            <span title="Tasks that are not finished"><b className="a-num">{stat.open}</b>open tasks</span>
            <span title="Agents running right now on a task that is working"><b className="a-num">{stat.working}</b>agents working</span>
            <span title="Tasks waiting for you" className={stat.needsYou ? 'a-plate-need' : undefined}><b className="a-num">{stat.needsYou}</b>need you</span>
          </div>
          <div className="a-plate-actions">
            <Button size="sm" variant="ghost" icon="list" onClick={() => onOpen(project)}>Show its work</Button>
            <Button size="sm" variant="ghost" icon="map" onClick={() => onMap(project)}>Open map</Button>
            <MenuButton label={`More for ${project.name}`} items={[
              { label: 'New task here', icon: 'plus', onSelect: () => onNewTask(project) },
              { label: 'Rename', icon: 'edit', onSelect: () => setRenaming(project.id) },
              { separator: true },
              { label: 'Move up', icon: 'left', disabled: !reorderable || index === 0, onSelect: () => commit(moveProject(visibleIds, project.id, -1), project.id) },
              { label: 'Move down', icon: 'right', disabled: !reorderable || index === shown.length - 1, onSelect: () => commit(moveProject(visibleIds, project.id, 1), project.id) },
              { separator: true },
              { label: 'Hide', icon: 'eyeoff', onSelect: () => onHide(project) },
              { label: 'Move to Trash…', icon: 'trash', danger: true, onSelect: () => onTrash(project) },
            ]} />
          </div>
        </div>
      </li>
    })}
  </ol>
}
