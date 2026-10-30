import { boxOf } from '../../model/mapLayout.mjs'

// A small picture of a project's task hierarchy (see useProjectPreview), drawn by the same layout as
// the Work Map. Agent colours mark the nodes; a removed agent is hollow and dashed, a task without an
// agent is hollow. "+N" beside a Task counts its helpers that are not drawn (no line joins it: it
// stands for Tasks, not for a Task).
export default function MapPreview({ preview, derived }) {
  const { layout, more } = preview
  const box = boxOf(layout)
  if (!box) return null
  // Frame what is drawn (the dots), not the full node boxes, so the picture sits centred.
  const nodes = Object.values(layout.nodes)
  const top = Math.min(...nodes.map((node) => (node.kind === 'row' ? node.y + node.h / 2 - 15 : node.y + 34 - (node.depth === 0 ? 46 : 38))))
  const bottom = Math.max(...nodes.map((node) => (node.kind === 'row' ? node.y + node.h / 2 + 15 : node.y + 34 + (node.depth === 0 ? 46 : 38))))
  const pad = 40
  const extra = Object.keys(more).length ? 220 : 0 // room for a "+N" beside the rightmost Task
  const viewBox = `${box.minX - pad} ${top - pad} ${box.maxX - box.minX + 2 * pad + extra} ${bottom - top + 2 * pad}`
  return <svg className="a-pv-svg" viewBox={viewBox} preserveAspectRatio="xMidYMid meet" aria-hidden="true" focusable="false">
    {layout.order.map((id) => {
      // Simple elbows between the dots (parent_task_id only); the full threads are the Map's.
      const node = layout.nodes[id]
      const parent = node.parentId ? layout.nodes[node.parentId] : null
      if (!parent) return null
      const at = (item) => (item.kind === 'row' ? [item.x + 15, item.y + item.h / 2] : [item.x + item.w / 2, item.y + 34])
      const [px, py] = at(parent)
      const [cx, cy] = at(node)
      const d = node.kind === 'row' ? `M${px},${py}V${cy}H${cx}` : `M${px},${py}V${(py + cy) / 2}H${cx}V${cy}`
      return <path key={`e-${id}`} className="a-pv-e" d={d} vectorEffect="non-scaling-stroke" />
    })}
    {layout.order.map((id) => {
      const node = layout.nodes[id]
      const agent = derived.agents[id]?.current || derived.agents[id]?.last
      const look = !agent ? 'none' : agent.removed ? 'removed' : 'agent'
      const color = agent && !agent.removed ? agent.color || 'neutral' : undefined
      return node.kind === 'row'
        ? <rect key={id} className={`a-pv-n a-pv-n--${look}`} data-color={color} x={node.x} y={node.y + node.h / 2 - 15} width={node.w * 0.82} height={30} rx={15} />
        : <circle key={id} className={`a-pv-n a-pv-n--${look}`} data-color={color} cx={node.x + node.w / 2} cy={node.y + 34} r={node.depth === 0 ? 46 : 38} />
    })}
    {Object.entries(more).map(([id, count]) => {
      const node = layout.nodes[id]
      if (!node) return null
      const x = node.kind === 'row' ? node.x + node.w * 0.82 + 18 : node.x + node.w / 2 + (node.depth === 0 ? 46 : 38) + 14
      const y = node.kind === 'row' ? node.y + node.h / 2 : node.y + 34
      return <g key={`more-${id}`} className="a-pv-more">
        <rect x={x} y={y - 34} width={String(count).length * 34 + 68} height={68} rx={34} vectorEffect="non-scaling-stroke" />
        <text x={x + 32} y={y + 18}>+{count}</text>
      </g>
    })}
  </svg>
}
