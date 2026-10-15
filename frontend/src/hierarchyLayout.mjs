export const nodeWidth = 304
export const nodeHeight = 188

// Layout depends only on topology and collapse choices, never output/status.
export function topologyOf(nodes, collapsed) {
  const rows = [], stack = nodes.map((node) => ({ node, parent: null, depth: 0 })).reverse()
  while (stack.length) {
    const { node, parent, depth } = stack.pop()
    rows.push([node.agent.agent_id, parent, depth])
    if (!collapsed.has(node.agent.agent_id)) {
      for (let i = node.children.length - 1; i >= 0; i--) stack.push({ node: node.children[i], parent: node.agent.agent_id, depth: depth + 1 })
    }
  }
  return JSON.stringify(rows)
}

export function layoutHierarchy(topology) {
  const nodes = JSON.parse(topology).map(([id, parent, depth]) => ({ id, parent, depth, x: 0, y: depth * (nodeHeight + 88) }))
  const positions = new Map(nodes.map((node) => [node.id, node]))
  const children = new Map()
  for (const node of nodes) {
    if (!children.has(node.parent)) children.set(node.parent, [])
    children.get(node.parent).push(node)
  }
  let leaf = 0
  for (const node of nodes) if (!children.has(node.id)) node.x = leaf++ * (nodeWidth + 48)
  for (const node of [...nodes].reverse()) {
    const branch = children.get(node.id)
    if (branch) node.x = (branch[0].x + branch.at(-1).x) / 2
  }
  const edges = nodes.filter((node) => positions.has(node.parent)).map((node) => ({ from: positions.get(node.parent), to: node }))
  return { nodes, edges, width: Math.max(nodeWidth, ...nodes.map((n) => n.x + nodeWidth)), height: Math.max(nodeHeight, ...nodes.map((n) => n.y + nodeHeight)) }
}

export function connectorPath(from, to) {
  const x1 = from.x + nodeWidth / 2, y1 = from.y + nodeHeight
  const x2 = to.x + nodeWidth / 2, y2 = to.y, middle = (y1 + y2) / 2
  return `M ${x1} ${y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${y2}`
}

export function directionalTarget(nodes, id, key) {
  const node = nodes.find((item) => item.id === id)
  if (!node) return null
  if (key === 'ArrowUp') return node.parent
  if (key === 'ArrowDown') return nodes.find((item) => item.parent === id)?.id || null
  const siblings = nodes.filter((item) => item.parent === node.parent)
  const offset = key === 'ArrowLeft' ? -1 : key === 'ArrowRight' ? 1 : 0
  return offset ? siblings[siblings.findIndex((item) => item.id === id) + offset]?.id || null : null
}

export function indexHierarchy(nodes) {
  const indexed = new Map(), order = [], stack = [...nodes].reverse()
  while (stack.length) {
    const node = stack.pop()
    indexed.set(node.agent.agent_id, { ...node, descendants: 0, active: 0, attention: 0 })
    order.push(node)
    for (let i = node.children.length - 1; i >= 0; i--) stack.push(node.children[i])
  }
  for (const node of order.reverse()) {
    const entry = indexed.get(node.agent.agent_id)
    for (const child of node.children) {
      const sub = indexed.get(child.agent.agent_id)
      entry.descendants += 1 + sub.descendants
      entry.active += Number(child.agent.status === 'running') + sub.active
      entry.attention += Number(['user', 'recovery'].includes(child.agent.waiting_context?.kind)) + sub.attention
    }
  }
  return indexed
}

export function zoomCamera(camera, factor, width, height) {
  const scale = Math.max(0.005, Math.min(1.6, camera.scale * factor))
  const ratio = scale / camera.scale
  return { scale, x: width / 2 - (width / 2 - camera.x) * ratio, y: height / 2 - (height / 2 - camera.y) * ratio }
}
export function focusCamera(node, camera, width, height) {
  return { ...camera, x: width / 2 - (node.x + nodeWidth / 2) * camera.scale, y: height / 2 - (node.y + nodeHeight / 2) * camera.scale }
}

export function selectionCamera(node, width, height, nodes = []) {
  const scale = Math.max(0.85, Math.min(1, (width - 48) / nodeWidth, (height - 48) / (nodeHeight * 2 + 88)))
  let left = node.x, right = node.x + nodeWidth, top = node.y, bottom = node.y + nodeHeight
  const parent = nodes.find((item) => item.id === node.parent)
  const child = nodes.filter((item) => item.parent === node.id).sort((a, b) => Math.abs(a.x - node.x) - Math.abs(b.x - node.x))[0]
  for (const relative of [parent, child]) {
    if (!relative) continue
    const bounds = [Math.min(left, relative.x), Math.max(right, relative.x + nodeWidth), Math.min(top, relative.y), Math.max(bottom, relative.y + nodeHeight)]
    if ((bounds[1] - bounds[0]) * scale <= width - 48 && (bounds[3] - bounds[2]) * scale <= height - 48) [left, right, top, bottom] = bounds
  }
  return { scale, x: width / 2 - (left + right) / 2 * scale, y: height / 2 - (top + bottom) / 2 * scale }
}
export function fitCamera(layout, width, height) {
  const scale = Math.max(0.005, Math.min(1, (width - 32) / layout.width, (height - 32) / layout.height))
  return { scale, x: (width - layout.width * scale) / 2, y: (height - layout.height * scale) / 2 }
}

// Keep a visible, surviving node in place when a branch changes geometry.
export function retainCamera(previous, next, camera, width, height) {
  const positions = new Map(next.nodes.map((node) => [node.id, node]))
  const anchor = previous.nodes.find((node) => positions.has(node.id)
    && node.x * camera.scale + camera.x >= 0 && node.x * camera.scale + camera.x < width
    && node.y * camera.scale + camera.y >= 0 && node.y * camera.scale + camera.y < height)
  if (!anchor) return next.nodes.length ? focusCamera(next.nodes[0], camera, width, height) : camera
  const replacement = positions.get(anchor.id)
  return { ...camera, x: camera.x + (anchor.x - replacement.x) * camera.scale, y: camera.y + (anchor.y - replacement.y) * camera.scale }
}
