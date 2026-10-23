import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { agentName, agentContext, executionLabel } from './agentPresentation.mjs'
import { displayColor } from './agentIdentity.mjs'
import { topologyOf, layoutHierarchy, indexHierarchy, zoomCamera, fitCamera, focusCamera, selectionCamera, retainCamera, connectorPath, directionalTarget, nodeWidth, nodeHeight } from './hierarchyLayout.mjs'
import Icon from './Icon'
import { removalState } from './agentRemoval.mjs'

export default function AgentHierarchy({ nodes, allNodes, collapsed, onToggle, selectedAgentId, onSelect, filterKey }) {
  const labelId = useId()
  const viewport = useRef(null), buttons = useRef(new Map()), drag = useRef(null)
  const topology = topologyOf(nodes, collapsed)
  const layout = useMemo(() => layoutHierarchy(topology), [topology])
  const [camera, setCamera] = useState(() => ({ x: 24, y: 24 - (layout.nodes[0]?.y || 0), scale: 1 }))
  const [moving, setMoving] = useState(false)
  const indexed = useMemo(() => indexHierarchy(allNodes), [allNodes])
  const visible = useMemo(() => indexHierarchy(nodes), [nodes])
  const [dimensions, setDimensions] = useState({ width: 600, height: 520 })
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      if (width && height) setDimensions({ width, height })
    })
    observer.observe(viewport.current)
    return () => observer.disconnect()
  }, [])
  const size = () => dimensions
  const [previous, setPrevious] = useState({ layout, filterKey, selectedAgentId: null, dimensions: null })
  if (previous.layout !== layout || previous.filterKey !== filterKey || previous.selectedAgentId !== selectedAgentId || previous.dimensions !== dimensions) {
    const { width, height } = size()
    const selectionChanged = previous.selectedAgentId !== selectedAgentId || previous.dimensions !== dimensions
    const selected = selectionChanged && layout.nodes.find((node) => node.id === selectedAgentId)
    const match = !selected && filterKey
      ? layout.nodes.find((node) => !visible.get(node.id)?.contextOnly) : null
    setPrevious({ layout, filterKey, selectedAgentId, dimensions })
    setMoving(Boolean(selected || match))
    setCamera(selected || match ? selectionCamera(selected || match, width, height, layout.nodes)
      : retainCamera(previous.layout, layout, camera, width, height))
  }
  function navigate(id) {
    const node = layout.nodes.find((item) => item.id === id)
    if (!node) return
    const { width, height } = size()
    setMoving(true)
    setCamera(selectionCamera(node, width, height, layout.nodes))
    buttons.current.get(id)?.focus({ preventScroll: true })
  }
  function keyNavigate(event, node) {
    if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return
    event.preventDefault()
    if (event.key === 'ArrowDown' && collapsed.has(node.id) && visible.get(node.id).children.length) onToggle?.(node.id)
    else navigate(directionalTarget(layout.nodes, node.id, event.key))
  }
  function zoom(factor) { const { width, height } = size(); setMoving(true); setCamera((current) => zoomCamera(current, factor, width, height)) }
  function pan(x, y) { setMoving(false); setCamera((current) => ({ ...current, x: current.x + x, y: current.y + y })) }
  return <div className="hierarchy" style={{ '--hierarchy-height': `${Math.max(320, Math.min(640, layout.height + 70))}px` }}>
    <div className="hierarchy-toolbar" role="group" aria-label="Hierarchy navigation">
      <button aria-label="Zoom out" onClick={() => zoom(1 / 1.2)}>−</button><span>{Math.round(camera.scale * 100)}%</span><button aria-label="Zoom in" onClick={() => zoom(1.2)}>+</button>
      <button onClick={() => { const { width, height } = size(); setMoving(true); setCamera(fitCamera(layout, width, height)) }}>Fit</button>
      <button disabled={!layout.nodes.some((n) => n.id === selectedAgentId)} onClick={() => navigate(selectedAgentId)}>Focus selected</button>
      <span className="pan-controls"><button aria-label="Pan left" onClick={() => pan(100, 0)}>←</button><button aria-label="Pan up" onClick={() => pan(0, 100)}>↑</button><button aria-label="Pan down" onClick={() => pan(0, -100)}>↓</button><button aria-label="Pan right" onClick={() => pan(-100, 0)}>→</button></span>
    </div>
    <p className="agent-type-note hierarchy-help">Drag to explore · Arrow keys navigate · Enter selects an agent</p>
    <div className="hierarchy-viewport" ref={viewport} role="region" aria-label="Graphical agent hierarchy"
      onPointerDown={(event) => {
        if (event.button !== 0 || event.target.closest('button')) return
        drag.current = { x: event.clientX, y: event.clientY }
        event.currentTarget.setPointerCapture(event.pointerId)
      }} onPointerMove={(event) => {
        if (!drag.current) return
        pan(event.clientX - drag.current.x, event.clientY - drag.current.y)
        drag.current = { x: event.clientX, y: event.clientY }
      }} onPointerUp={() => { drag.current = null }} onPointerCancel={() => { drag.current = null }}>
      <div className={`hierarchy-world${moving ? ' camera-moving' : ''}`} style={{ width: layout.width, height: layout.height, transform: `translate(${camera.x}px, ${camera.y}px) scale(${camera.scale})` }}>
        <svg className="hierarchy-connectors" width={layout.width} height={layout.height} aria-hidden="true">
          {layout.edges.map(({ from, to }) => <path key={to.id} className={from.id === selectedAgentId || to.id === selectedAgentId ? 'near-focus' : ''} d={connectorPath(from, to)} />)}
        </svg>
        {layout.nodes.map((node) => {
          const entry = indexed.get(node.id), agent = entry.agent
          const removal = removalState(agent)
          return <article key={node.id} className={`hierarchy-node agent-card${selectedAgentId === node.id ? ' selected' : ''}${visible.get(node.id)?.contextOnly ? ' context-only' : ''}${removal === 'removed' ? ' agent-removed' : removal ? ' agent-removing' : ''}`} data-display-color={displayColor(agent.display_color)} data-status={agent.status} data-removal={removal || undefined} style={{ left: node.x, top: node.y, width: nodeWidth, height: nodeHeight }}>
            <button className="hierarchy-select" ref={(element) => { if (element) buttons.current.set(node.id, element); else buttons.current.delete(node.id) }} aria-pressed={selectedAgentId === node.id} aria-label={`${agentName(agent)}${removal === 'removed' ? ', removed' : ''}`} aria-describedby={`${labelId}-${node.id}`}
                onClick={() => onSelect(node.id)} onFocus={() => {
                  const { width, height } = size()
                  if (camera.scale < 0.5 || node.x * camera.scale + camera.x < 0 || (node.x + nodeWidth) * camera.scale + camera.x > width || node.y * camera.scale + camera.y < 0 || (node.y + nodeHeight) * camera.scale + camera.y > height) {
                    setMoving(true)
                    setCamera((current) => focusCamera(node, { ...current, scale: Math.max(0.75, current.scale) }, width, height))
                  }
                }} onKeyDown={(event) => keyNavigate(event, node)}>
            <span className="hierarchy-node-heading"><span className="agent-avatar" aria-hidden="true">{removal === 'removed' ? <Icon name="removed" size={18} className="removal-mark" /> : <Icon name="agent" size={18} />}</span><strong className="agent-name" title={agentName(agent)}>{agentName(agent)}</strong></span>
            <span className="hierarchy-responsibility">{agentContext(agent) || agent.task}</span>
            <span className="hierarchy-node-state"><span id={`${labelId}-${node.id}`} className={`status status-${agent.status}`}>{executionLabel(agent)}</span>
              {agent.historical_assignment && <span className="agent-type-note">Past worker</span>}
              {agent.task_status === 'canceled' && <span className="agent-type-note">Task canceled</span>}
              {entry.parentHidden && <span className="agent-type-note">Parent removed · hidden</span>}</span>
            </button>
            {entry.descendants > 0 && <button className="hierarchy-branch" disabled={!onToggle} aria-expanded={!collapsed.has(node.id)} onClick={() => onToggle?.(node.id)} aria-label={`${collapsed.has(node.id) ? 'Expand' : 'Collapse'} ${agentName(agent)} branch`}>
              <Icon name={collapsed.has(node.id) ? 'chevron' : 'down'} size={12} />{entry.descendants} descendants{entry.active > 0 ? ` · ${entry.active} working` : ''}{entry.attention > 0 ? ` · ${entry.attention} need attention` : ''}</button>}
          </article>
        })}
      </div>
    </div>
  </div>
}
