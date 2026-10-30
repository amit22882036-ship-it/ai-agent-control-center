// The Work Map's layout: a pure function of the same visible rows the List shows (`visibleRows`),
// so both views always contain the same Tasks, in the same order, with the same open branches.
//
// Each Task is one node. Edges join a Task to its parent Task (parent_task_id) and nothing else.
// A family whose top has no loaded parent gets no edge above it. A lead sits centred over its
// helpers; when every shown helper is a leaf, they stack as a short list under the lead, joined by
// the same thread the List uses, so wide teams stay readable. Families are grouped in bands that
// follow the List's sections and wrap into rows instead of growing into one endless line.
//
// Deterministic: the same rows always produce the same positions. Inside a band, families run
// oldest to newest, so a new family is added at the end instead of pushing every other one aside.

export const MAP = {
  NODE_W: 168, ROOT_H: 132, NODE_H: 122, CHIPS: 26, ROW_W: 216, ROW_H: 54, ROW_GAP: 8, ROW_INDENT: 26,
  GAP_X: 24, GAP_Y: 60, FAMILY_GAP: 76, FAMILY_ROW_GAP: 70, BAND_GAP: 54, BAND_HEAD: 40, LABEL: 30, PAD: 48,
  MAX_ROW: 2600, RADIUS: 10,
}

// What an edge says about the Task below it. Colour is never the only signal: the node carries
// its status in words, and these are just quiet reinforcements.
export function edgeTone(derived, id) {
  const status = derived.status[id]
  const agent = derived.agents[id]?.current || derived.agents[id]?.last
  if (!status) return 'plain'
  if (agent?.removed || status.key === 'removing') return 'gone'
  if (status.tone === 'alert') return 'fail'
  if (status.tone === 'ask') return 'ask'
  if (status.tone === 'work') return 'work'
  return 'plain'
}

function orderedFamilies(section) {
  // Oldest family first inside a band (the List shows newest first): new work appears at the end.
  return [...section.families].reverse()
}

// `view` is visibleRows(...). Returns node boxes, edge paths grouped by tone, band headings and
// family zones, plus the shown parent/child relationships the keyboard follows. `chips(id, row)`
// says whether a node shows a row of small notes under it (open/closed, not loaded, planned), so
// its threads start below them.
// `bands: false` puts every family in one sequence without section headings (small previews).
export function mapLayout(view, derived, { maxRow = MAP.MAX_ROW, chips = (id, row) => row.hasChildren, bands: banded = true } = {}) {
  const children = new Map()
  const rows = new Map()
  const familyOf = new Map()
  const parent = {}
  for (const section of view.sections) {
    for (const family of section.families) {
      for (const row of family.rows) {
        rows.set(row.id, row)
        familyOf.set(row.id, family.topId)
        if (row.parentId) {
          parent[row.id] = row.parentId
          if (!children.has(row.parentId)) children.set(row.parentId, [])
          children.get(row.parentId).push(row.id)
        }
      }
    }
  }
  const kids = (id) => children.get(id) || []
  const heightOf = (id) => (rows.get(id)?.depth === 0 ? MAP.ROOT_H : MAP.NODE_H) + (chips(id, rows.get(id)) ? MAP.CHIPS : 0)
  // Only true leaves stack: a helper whose own branch is merely closed keeps its place as a node,
  // so closing a branch never rearranges its siblings.
  const stacked = (id) => {
    const list = kids(id)
    return list.length >= 2 && list.every((child) => kids(child).length === 0 && !rows.get(child)?.hasChildren)
  }

  // Subtree widths, bottom-up without recursion (deep chains are fine).
  const width = new Map()
  const measureFrom = (top) => {
    const order = []
    const stack = [top]
    while (stack.length) {
      const id = stack.pop()
      order.push(id)
      for (const child of kids(id)) stack.push(child)
    }
    for (let i = order.length - 1; i >= 0; i -= 1) {
      const id = order[i]
      const list = kids(id)
      if (!list.length) width.set(id, MAP.NODE_W)
      else if (stacked(id)) width.set(id, Math.max(MAP.NODE_W, MAP.ROW_INDENT + MAP.ROW_W))
      else width.set(id, Math.max(MAP.NODE_W, list.reduce((sum, child) => sum + width.get(child), 0) + MAP.GAP_X * (list.length - 1)))
    }
    return width.get(top)
  }

  const nodes = {}
  // Places a family whose subtree box starts at (x, y); returns its bottom edge.
  const place = (top, x0, y0) => {
    let bottom = y0
    const stack = [[top, x0, y0]]
    while (stack.length) {
      const [id, x, y] = stack.pop()
      const w = width.get(id)
      const row = rows.get(id)
      const h = heightOf(id)
      nodes[id] = { id, kind: 'node', x: x + w / 2 - MAP.NODE_W / 2, y, w: MAP.NODE_W, h, depth: row.depth, parentId: parent[id] || null, topId: familyOf.get(id) }
      bottom = Math.max(bottom, y + h)
      const list = kids(id)
      if (!list.length) continue
      if (stacked(id)) {
        const left = x + (w - (MAP.ROW_INDENT + MAP.ROW_W)) / 2
        const top2 = y + h + MAP.GAP_Y * 0.62
        nodes[id].spine = left + 9
        list.forEach((child, index) => {
          const childRow = rows.get(child)
          const cy = top2 + index * (MAP.ROW_H + MAP.ROW_GAP)
          nodes[child] = { id: child, kind: 'row', x: left + MAP.ROW_INDENT, y: cy, w: MAP.ROW_W, h: MAP.ROW_H, depth: childRow.depth,
            parentId: id, topId: familyOf.get(child) }
          bottom = Math.max(bottom, cy + MAP.ROW_H)
        })
        continue
      }
      const total = list.reduce((sum, child) => sum + width.get(child), 0) + MAP.GAP_X * (list.length - 1)
      let cx = x + (w - total) / 2
      for (const child of list) {
        stack.push([child, cx, y + h + MAP.GAP_Y])
        cx += width.get(child) + MAP.GAP_X
      }
    }
    return bottom
  }

  const bands = []
  const zones = []
  const tops = []
  let y = MAP.PAD
  let maxX = MAP.PAD
  const groups = banded ? view.sections : [{ key: 'all', label: '', families: view.sections.flatMap((section) => section.families).reverse() }]
  for (const section of groups) {
    const families = orderedFamilies(section)
    if (banded) bands.push({ key: section.key, label: section.label, count: section.families.length, x: MAP.PAD, y })
    let rowTop = y + (banded ? MAP.BAND_HEAD : 0) + MAP.LABEL
    let x = MAP.PAD
    let rowBottom = rowTop
    for (const family of families) {
      const w = measureFrom(family.topId)
      if (x > MAP.PAD && x + w > MAP.PAD + maxRow) {
        x = MAP.PAD
        rowTop = rowBottom + MAP.FAMILY_ROW_GAP + MAP.LABEL
        rowBottom = rowTop
      }
      const bottom = place(family.topId, x, rowTop)
      zones.push({ topId: family.topId, x: x - 16, y: rowTop - MAP.LABEL, w: w + 32, h: bottom - rowTop + MAP.LABEL + 16, section: section.key })
      tops.push(family.topId)
      rowBottom = Math.max(rowBottom, bottom)
      x += w + MAP.FAMILY_GAP
      maxX = Math.max(maxX, x - MAP.FAMILY_GAP)
    }
    y = rowBottom + MAP.BAND_GAP
  }

  // Edges: only between a shown Task and its shown parent Task.
  const paths = { plain: [], work: [], ask: [], gone: [], fail: [] }
  const r = MAP.RADIUS
  for (const id of Object.keys(nodes)) {
    const node = nodes[id]
    const from = node.parentId ? nodes[node.parentId] : null
    if (!from) continue
    const tone = edgeTone(derived, id)
    if (node.kind === 'row') {
      const sx = from.spine
      const my = node.y + node.h / 2
      paths[tone].push(`M${sx},${my - r}Q${sx},${my} ${sx + r},${my}H${node.x}`)
      continue
    }
    const px = from.x + from.w / 2
    const py = from.y + from.h - 2
    const cx = node.x + node.w / 2
    const cy = node.y + 2
    const my = py + (cy - py) / 2
    const sg = cx > px ? 1 : -1
    paths[tone].push(Math.abs(cx - px) < 1 ? `M${px},${py}V${cy}`
      : `M${px},${py}V${my - r}Q${px},${my} ${px + sg * r},${my}H${cx - sg * r}Q${cx},${my} ${cx},${my + r}V${cy}`)
  }
  // The shared spine of a stacked group of helpers.
  for (const id of Object.keys(nodes)) {
    const node = nodes[id]
    if (node.spine === undefined) continue
    const list = kids(id)
    const last = nodes[list[list.length - 1]]
    const px = node.x + node.w / 2
    const py = node.y + node.h - 2
    const sx = node.spine
    const jy = py + MAP.GAP_Y * 0.3
    paths.plain.unshift(`M${px},${py}V${jy - r}Q${px},${jy} ${px - r},${jy}H${sx + r}Q${sx},${jy} ${sx},${jy + r}V${last.y + last.h / 2 - r}`)
  }
  const edges = Object.fromEntries(Object.entries(paths).map(([tone, list]) => [tone, list.join('')]))
  const width2 = Math.max(maxX, ...Object.values(nodes).map((node) => node.x + node.w)) + MAP.PAD
  const height = Math.max(y - MAP.BAND_GAP, ...Object.values(nodes).map((node) => node.y + node.h)) + MAP.PAD
  return { nodes, edges, edgeCount: Object.values(paths).reduce((sum, list) => sum + list.length, 0), bands, zones, tops,
    parent, children: Object.fromEntries(children), order: [...rows.keys()], width: view.sections.length ? width2 : 0, height: view.sections.length ? height : 0 }
}

// Bounding box of some nodes (or of everything).
export function boxOf(layout, ids = null) {
  const list = (ids || Object.keys(layout.nodes)).map((id) => layout.nodes[id]).filter(Boolean)
  if (!list.length) return null
  return {
    minX: Math.min(...list.map((node) => node.x)), minY: Math.min(...list.map((node) => node.y)),
    maxX: Math.max(...list.map((node) => node.x + node.w)), maxY: Math.max(...list.map((node) => node.y + node.h)),
  }
}

// Every shown Task in the branch under `id`, including it.
export function branchIds(layout, id) {
  const out = []
  const stack = [id]
  while (stack.length) {
    const next = stack.pop()
    if (!layout.nodes[next]) continue
    out.push(next)
    for (const child of layout.children[next] || []) stack.push(child)
  }
  return out
}

// Keyboard movement over the Map. Arrows follow what is drawn: up to the lead, down to the first
// helper, left and right between Tasks at the same level (between families at the top). In a
// stacked group, up and down move along the list and left returns to the lead. Space opens or
// closes a branch, Enter selects. Returns what to do; never mutates.
export function mapKey(layout, id, key, { collapsed = () => false } = {}) {
  const node = layout.nodes[id]
  if (!node) return layout.tops.length ? { focus: layout.tops[0] } : null
  const parentId = layout.parent[id] || null
  const siblings = parentId ? layout.children[parentId] || [] : layout.tops
  const index = siblings.indexOf(id)
  const firstChild = (layout.children[id] || [])[0]
  if (key === 'Enter') return { select: id }
  if (key === ' ') return firstChild ? { collapse: id } : collapsed(id) ? { expand: id } : null
  if (key === 'Home') return { focus: layout.tops[0] }
  if (key === 'End') return { focus: layout.tops[layout.tops.length - 1] }
  if (node.kind === 'row') {
    if (key === 'ArrowUp') return { focus: index > 0 ? siblings[index - 1] : parentId }
    if (key === 'ArrowDown') return index < siblings.length - 1 ? { focus: siblings[index + 1] } : collapsed(id) ? { expand: id, descend: true } : null
    if (key === 'ArrowLeft') return { focus: parentId }
    if (key === 'ArrowRight') return collapsed(id) ? { expand: id, descend: true } : null
    return null
  }
  if (key === 'ArrowUp') return parentId ? { focus: parentId } : null
  if (key === 'ArrowDown') return firstChild ? { focus: firstChild } : collapsed(id) ? { expand: id, descend: true } : null
  if (key === 'ArrowLeft') return index > 0 ? { focus: siblings[index - 1] } : null
  if (key === 'ArrowRight') return index >= 0 && index < siblings.length - 1 ? { focus: siblings[index + 1] } : null
  return null
}
