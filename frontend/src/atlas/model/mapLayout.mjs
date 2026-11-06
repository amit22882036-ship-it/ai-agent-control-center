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
  NODE_W: 272, ROOT_H: 122, NODE_H: 122, CHIPS: 0, ROW_W: 252, ROW_H: 60, ROW_GAP: 10, ROW_INDENT: 30,
  GAP_X: 28, GAP_Y: 56, GAP_X_ACROSS: 52, GAP_Y_ACROSS: 16, FAMILY_GAP: 100, FAMILY_ROW_GAP: 40, BAND_GAP: 64, BAND_HEAD: 52, LABEL: 12, PAD: 56,
  MAX_ROW: 2600, RADIUS: 14, SHELF_W: 300, SHELF_H: 100, SHELF_GAP: 16,
}
// From this many families in a band, closed families sit on a shelf (see the band loop).
const SHELF_FROM = 6

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
  // What needs the person keeps the List's order, the order the course numbers it in, so the course
  // reads forwards on the Map too. Other bands put the oldest family first (the List shows newest
  // first): new work appears at the end instead of pushing every other family aside.
  return section.key === 'needs' ? [...section.families] : [...section.families].reverse()
}

// `view` is visibleRows(...). Returns node boxes, edge paths grouped by tone, band headings and
// family zones, plus the shown parent/child relationships the keyboard follows. `chips(id, row)`
// says whether a node shows a row of small notes under it (open/closed, not loaded, planned), so
// its threads start below them.
// `bands: false` puts every family in one sequence without section headings (small previews).
// `direction: 'right'` draws each family left to right (lead on the left, helpers to its right) and
// stacks families under each other; the default draws top to bottom and wraps families into rows.
export function mapLayout(view, derived, { maxRow = MAP.MAX_ROW, chips = (id, row) => row.hasChildren, bands: banded = true, direction = 'down' } = {}) {
  const across = direction === 'right'
  // Left to right, rows wrap sooner: each family already grows sideways.
  const acrossRow = Math.min(maxRow, 1900)
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
  // "Breadth" is the axis along which siblings spread (x top to bottom, y left to right); "depth"
  // runs from a lead to its helpers.
  const listLength = (n) => n * (MAP.ROW_H + MAP.ROW_GAP) - MAP.ROW_GAP
  const ownBreadth = (id) => (across ? heightOf(id) : MAP.NODE_W)
  const depthGap = across ? MAP.GAP_X_ACROSS : MAP.GAP_Y
  const siblingGap = across ? MAP.GAP_Y_ACROSS : MAP.GAP_X

  // Subtree breadths, bottom-up without recursion (deep chains are fine).
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
      if (!list.length) width.set(id, ownBreadth(id))
      else if (stacked(id)) width.set(id, across ? Math.max(ownBreadth(id), listLength(list.length)) : Math.max(MAP.NODE_W, MAP.ROW_INDENT + MAP.ROW_W))
      else width.set(id, Math.max(ownBreadth(id), list.reduce((sum, child) => sum + width.get(child), 0) + siblingGap * (list.length - 1)))
    }
    return width.get(top)
  }

  const nodes = {}
  // Places a family whose subtree box starts at breadth b0 and depth d0; returns its far corner.
  const place = (top, b0, d0) => {
    let maxX = 0
    let maxY = 0
    const grow = (node) => { maxX = Math.max(maxX, node.x + node.w); maxY = Math.max(maxY, node.y + node.h) }
    const stack = [[top, b0, d0]]
    while (stack.length) {
      const [id, b, d] = stack.pop()
      const w = width.get(id)
      const row = rows.get(id)
      const h = heightOf(id)
      const x = across ? d : b + w / 2 - MAP.NODE_W / 2
      const y = across ? b + w / 2 - h / 2 : d
      nodes[id] = { id, kind: 'node', x, y, w: MAP.NODE_W, h, depth: row.depth, parentId: parent[id] || null, topId: familyOf.get(id) }
      grow(nodes[id])
      const list = kids(id)
      if (!list.length) continue
      if (stacked(id)) {
        let spine, left, top2
        if (across) {
          spine = x + MAP.NODE_W + MAP.ROW_INDENT
          left = spine + MAP.ROW_INDENT
          top2 = b + (w - listLength(list.length)) / 2
        } else {
          const start = b + (w - (MAP.ROW_INDENT + MAP.ROW_W)) / 2
          spine = start + 9
          left = start + MAP.ROW_INDENT
          top2 = y + h + MAP.GAP_Y * 0.62
        }
        nodes[id].spine = spine
        list.forEach((child, index) => {
          nodes[child] = { id: child, kind: 'row', x: left, y: top2 + index * (MAP.ROW_H + MAP.ROW_GAP), w: MAP.ROW_W, h: MAP.ROW_H,
            depth: rows.get(child).depth, parentId: id, topId: familyOf.get(child) }
          grow(nodes[child])
        })
        continue
      }
      const total = list.reduce((sum, child) => sum + width.get(child), 0) + siblingGap * (list.length - 1)
      let cb = b + (w - total) / 2
      const next = (across ? x + MAP.NODE_W : y + h) + depthGap
      for (const child of list) {
        stack.push([child, cb, next])
        cb += width.get(child) + siblingGap
      }
    }
    return { maxX, maxY }
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
    // At scale, a closed family (one shown node) is a capsule on the band's shelf instead of a
    // full tile: open branches stand out as trees, and the rest read as a tidy list of families.
    const shelved = !across && section.key !== 'needs' && families.length > SHELF_FROM ? families.filter((family) => family.rows.length === 1) : []
    const shelf = new Set(shelved.map((family) => family.topId))
    families.filter((family) => !shelf.has(family.topId)).forEach((family, index) => {
      const w = measureFrom(family.topId)
      if (across) {
        // Families sit side by side, each growing to the right, and wrap into rows, so a busy
        // workspace reads as a field of small trees instead of one endless column.
        let far = place(family.topId, rowTop, x)
        if (x > MAP.PAD && far.maxX > MAP.PAD + acrossRow) {
          x = MAP.PAD
          rowTop = rowBottom + MAP.FAMILY_ROW_GAP + MAP.LABEL
          rowBottom = rowTop
          far = place(family.topId, rowTop, x)
        }
        zones.push({ topId: family.topId, x: x - 16, y: rowTop - MAP.LABEL, w: far.maxX - x + 32, h: far.maxY - rowTop + MAP.LABEL + 16, section: section.key })
        tops.push(family.topId)
        rowBottom = Math.max(rowBottom, rowTop + w, far.maxY)
        x = far.maxX + MAP.FAMILY_GAP
        maxX = Math.max(maxX, far.maxX)
        return
      }
      if (x > MAP.PAD && x + w > MAP.PAD + maxRow) {
        x = MAP.PAD
        rowTop = rowBottom + MAP.FAMILY_ROW_GAP + MAP.LABEL
        rowBottom = rowTop
      }
      const far = place(family.topId, x, rowTop)
      zones.push({ topId: family.topId, x: x - 16, y: rowTop - MAP.LABEL, w: w + 32, h: far.maxY - rowTop + MAP.LABEL + 16, section: section.key })
      tops.push(family.topId)
      rowBottom = Math.max(rowBottom, far.maxY)
      x += w + MAP.FAMILY_GAP
      maxX = Math.max(maxX, x - MAP.FAMILY_GAP)
    })
    if (shelved.length) {
      const columns = Math.max(1, Math.floor((maxRow + MAP.SHELF_GAP) / (MAP.SHELF_W + MAP.SHELF_GAP)))
      const start = shelved.length === families.length ? rowTop : rowBottom + MAP.FAMILY_ROW_GAP
      shelved.forEach((family, index) => {
        const sx = MAP.PAD + (index % columns) * (MAP.SHELF_W + MAP.SHELF_GAP)
        const sy = start + Math.floor(index / columns) * (MAP.SHELF_H + MAP.SHELF_GAP)
        const id = family.topId
        nodes[id] = { id, kind: 'row', shelf: true, x: sx, y: sy, w: MAP.SHELF_W, h: MAP.SHELF_H, depth: 0, parentId: null, topId: id }
        tops.push(id)
        rowBottom = Math.max(rowBottom, sy + MAP.SHELF_H)
        maxX = Math.max(maxX, sx + MAP.SHELF_W)
      })
    }
    y = rowBottom + MAP.BAND_GAP
  }

  // Edges: only between a shown Task and its shown parent Task. Each is also listed on its own, keyed
  // by the Task below it, with the point where it meets that Task.
  const paths = { plain: [], work: [], ask: [], gone: [], fail: [] }
  const edgeList = []
  const add = (id, from, tone, d, end) => { paths[tone].push(d); edgeList.push({ id, from, tone, d, end }) }
  const r = MAP.RADIUS
  for (const id of Object.keys(nodes)) {
    const node = nodes[id]
    const from = node.parentId ? nodes[node.parentId] : null
    if (!from) continue
    const tone = edgeTone(derived, id)
    const my = node.y + node.h / 2
    if (node.kind === 'row') {
      const sx = from.spine
      // Left to right, the list can sit above the lead's middle as well as below it.
      const above = across && my < from.y + from.h / 2
      add(id, node.parentId, tone, `M${sx},${above ? my + r : my - r}Q${sx},${my} ${sx + r},${my}H${node.x}`, [node.x, my])
      continue
    }
    if (across) {
      const px = from.x + from.w - 2
      const py = from.y + from.h / 2
      const cx = node.x + 2
      const mx = px + (cx - px) / 2
      const sg = my > py ? 1 : -1
      add(id, node.parentId, tone, Math.abs(my - py) < 1 ? `M${px},${py}H${cx}`
        : `M${px},${py}H${mx - r}Q${mx},${py} ${mx},${py + sg * r}V${my - sg * r}Q${mx},${my} ${mx + r},${my}H${cx}`, [cx, my])
      continue
    }
    const px = from.x + from.w / 2
    const py = from.y + from.h - 2
    const cx = node.x + node.w / 2
    const cy = node.y + 2
    const midY = py + (cy - py) / 2
    const sg = cx > px ? 1 : -1
    add(id, node.parentId, tone, Math.abs(cx - px) < 1 ? `M${px},${py}V${cy}`
      : `M${px},${py}V${midY - r}Q${px},${midY} ${px + sg * r},${midY}H${cx - sg * r}Q${cx},${midY} ${cx},${midY + r}V${cy}`, [cx, cy])
  }
  // The shared spine of a stacked group of helpers.
  for (const id of Object.keys(nodes)) {
    const node = nodes[id]
    if (node.spine === undefined) continue
    const list = kids(id)
    const first = nodes[list[0]]
    const last = nodes[list[list.length - 1]]
    const sx = node.spine
    if (across) {
      const px = node.x + node.w - 2
      const py = node.y + node.h / 2
      const top2 = Math.min(py, first.y + first.h / 2 + r)
      const bottom2 = Math.max(py, last.y + last.h / 2 - r)
      paths.plain.unshift(`M${px},${py}H${sx}M${sx},${top2}V${bottom2}`)
      edgeList.unshift({ id: `spine:${id}`, from: id, tone: 'plain', d: `M${px},${py}H${sx}M${sx},${top2}V${bottom2}`, end: null })
      continue
    }
    const px = node.x + node.w / 2
    const py = node.y + node.h - 2
    const jy = py + MAP.GAP_Y * 0.3
    const spine = `M${px},${py}V${jy - r}Q${px},${jy} ${px - r},${jy}H${sx + r}Q${sx},${jy} ${sx},${jy + r}V${last.y + last.h / 2 - r}`
    paths.plain.unshift(spine)
    edgeList.unshift({ id: `spine:${id}`, from: id, tone: 'plain', d: spine, end: null })
  }
  const edges = Object.fromEntries(Object.entries(paths).map(([tone, list]) => [tone, list.join('')]))
  const width2 = Math.max(maxX, ...Object.values(nodes).map((node) => node.x + node.w)) + MAP.PAD
  const height = Math.max(y - MAP.BAND_GAP, ...Object.values(nodes).map((node) => node.y + node.h)) + MAP.PAD
  return { nodes, edges, edgeList, edgeCount: Object.values(paths).reduce((sum, list) => sum + list.length, 0), bands, zones, tops,
    direction: across ? 'right' : 'down',
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
  // Left to right, the arrows turn with the drawing: left to the lead, right to the first helper.
  if (layout.direction === 'right') key = { ArrowLeft: 'ArrowUp', ArrowRight: 'ArrowDown', ArrowUp: 'ArrowLeft', ArrowDown: 'ArrowRight' }[key] || key
  if (key === 'ArrowUp') return parentId ? { focus: parentId } : null
  if (key === 'ArrowDown') return firstChild ? { focus: firstChild } : collapsed(id) ? { expand: id, descend: true } : null
  if (key === 'ArrowLeft') return index > 0 ? { focus: siblings[index - 1] } : null
  if (key === 'ArrowRight') return index >= 0 && index < siblings.length - 1 ? { focus: siblings[index + 1] } : null
  return null
}
