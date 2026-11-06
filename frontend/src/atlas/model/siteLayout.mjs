// The Site: the workspace laid out as an architectural massing model. Pure functions.
//
// Every project is a district on the base board; every task family is a building on a lot; its
// floors are its tasks, stacked by construction state so a tower reads like a building going up:
// built work at the base, then held, then glass (waiting), then lit floors (an agent working),
// then the wireframe of what is planned but not started, and on top the one vermilion piece when
// something in the family needs the person. Floors are counts of recorded task states, never
// estimates. Canceled tasks are not floors; a family that was canceled entirely is a demolished lot.
import { taskCategory } from './workList.mjs'

// Bottom to top. Each name is also the material the renderer paints.
export const BANDS = ['done', 'held', 'waiting', 'working', 'pending', 'needs']
export const MATERIAL = { done: 'built', held: 'held', waiting: 'glass', working: 'lit', pending: 'wire', needs: 'needs' }

const LOT = 1
const GAP = 0.62
const STREET = 2.6

// How tall a family stands: grows with its team, quickly at first and then slowly, so one huge
// family never hides the rest. Units are lot widths.
export function buildingHeight(total, cap = 4.2) {
  if (total <= 0) return 0.18
  return Math.min(cap, 0.55 + 0.95 * Math.log2(1 + total))
}

// A family's floors: how many of its tasks are in each band, bottom to top.
export function familyFloors(derived, family) {
  const counts = { done: 0, held: 0, waiting: 0, working: 0, pending: 0, needs: 0, canceled: 0 }
  for (const id of family.members) counts[taskCategory(derived, id)] += 1
  const total = family.members.length - counts.canceled
  return { counts, total }
}

// Heights of the bands inside a building of `height`: proportional to their counts, never thinner
// than a sliver so a single task in a state is always visible.
export function bandStack(counts, total, height) {
  const present = BANDS.filter((band) => counts[band] > 0)
  if (!present.length) return []
  const min = Math.min(0.16, height / present.length)
  const free = height - min * present.length
  const out = []
  let z = 0
  for (const band of present) {
    const h = min + (total ? (counts[band] / total) * Math.max(0, free) : 0)
    out.push({ band, material: MATERIAL[band], count: counts[band], z0: z, z1: z + h })
    z += h
  }
  // Normalise rounding so the top lands exactly on `height`.
  out[out.length - 1].z1 = height
  return out
}

function familyRecord(derived, family, section) {
  const { counts, total } = familyFloors(derived, family)
  const history = section === 'done'
  const canceled = total === 0 && counts.canceled > 0
  const height = canceled ? 0.06 : history ? Math.min(1.3, 0.35 + 0.3 * Math.log2(1 + total)) : buildingHeight(total)
  return {
    id: family.topId, projectId: family.projectId ?? null, section, history, canceled, counts, total, height,
    bands: canceled ? [] : bandStack(counts, total, height),
    attention: counts.needs > 0, working: counts.working > 0,
  }
}

// Families of the current view, grouped into districts in the registered project order (unknown
// projects last, by name). `families` are visibleRows() families (any section); counts always use
// the whole family.
export function districtsOf(derived, families, projects) {
  const order = new Map(projects.map((project, index) => [project.id, index]))
  const byProject = new Map()
  for (const family of families) {
    const key = family.projectId ?? '__none'
    if (!byProject.has(key)) byProject.set(key, [])
    byProject.get(key).push(family)
  }
  const name = (id) => projects.find((project) => project.id === id)?.name || (id === '__none' ? 'No project' : 'Unregistered project')
  return [...byProject.entries()]
    .sort(([a], [b]) => (order.get(a) ?? 1e6) - (order.get(b) ?? 1e6) || name(a).localeCompare(name(b)))
    .map(([id, list]) => ({ id, name: name(id), families: list }))
}

// Within a district: what needs the person stands at the front, then open work, then history at
// the back. Inside each group the families keep the List's order.
const RANK = { needs: 0, active: 1, done: 2 }
function ordered(records) {
  return records.map((record, index) => ({ record, index }))
    .sort((a, b) => RANK[a.record.section] - RANK[b.record.section] || a.index - b.index).map(({ record }) => record)
}

// The full site: districts on a grid of parcels, each parcel a grid of lots. Front rows are the
// rows with the largest y (nearest the viewer at the default angle).
export function siteLayout(derived, view, projects, { history = true } = {}) {
  const families = view.sections.flatMap((section) => section.families.map((family) => ({ family, section: section.key === 'focus' ? family.section : section.key })))
  const districts = districtsOf(derived, families.map(({ family }) => family), projects)
  const sectionOf = new Map(families.map(({ family, section }) => [family.topId, section]))
  const parcels = districts.map((district) => {
    const records = ordered(district.families.map((family) => familyRecord(derived, family, sectionOf.get(family.topId))))
      .filter((record) => history || !record.history)
    const columns = Math.max(3, Math.ceil(Math.sqrt(records.length * 1.7)))
    const rows = Math.max(1, Math.ceil(records.length / columns))
    return { ...district, records, columns, rows }
  })
  // Parcels on a grid, roughly square overall.
  const across = Math.max(1, Math.ceil(Math.sqrt(parcels.length)))
  const buildings = []
  const plots = []
  let y = 0
  for (let row = 0; row < parcels.length; row += across) {
    const line = parcels.slice(row, row + across)
    const depth = Math.max(...line.map((parcel) => parcel.rows)) * (LOT + GAP) - GAP
    let x = 0
    for (const parcel of line) {
      const width = parcel.columns * (LOT + GAP) - GAP
      plots.push({ id: parcel.id, name: parcel.name, x: x - 0.45, y: y - 0.45, w: width + 0.9, d: depth + 0.9, count: parcel.records.length })
      parcel.records.forEach((record, index) => {
        const column = index % parcel.columns
        const lotRow = Math.floor(index / parcel.columns)
        // Row 0 (what needs you, then open work) is the front: the largest y.
        buildings.push({ ...record, x: x + column * (LOT + GAP), y: y + depth - LOT - lotRow * (LOT + GAP), w: LOT, d: LOT, district: parcel.id })
      })
      x += width + STREET
    }
    y += depth + STREET
  }
  return { buildings, plots, bounds: boundsOf(plots) }
}

// The panorama strip: districts side by side along the screen's horizontal, each showing its open
// work as buildings. History is counted on the district's label, not built, so the strip stays
// readable whether there are ten finished families or ten thousand.
export function stripLayout(derived, view, projects, { azimuth = 32 } = {}) {
  const families = view.sections.flatMap((section) => section.families.map((family) => ({ family, section: section.key })))
  const sectionOf = new Map(families.map(({ family, section }) => [family.topId, section]))
  const districts = districtsOf(derived, families.map(({ family }) => family), projects)
  const a = (azimuth * Math.PI) / 180
  // u runs along the screen's horizontal, v away from the viewer; world = R(-azimuth)(u, v).
  const world = (u, v) => ({ x: u * Math.cos(a) + v * Math.sin(a), y: -u * Math.sin(a) + v * Math.cos(a) })
  const buildings = []
  const plots = []
  const archives = []
  let u = 0
  for (const district of districts) {
    const records = ordered(district.families.map((family) => familyRecord(derived, family, sectionOf.get(family.topId))))
    const open = records.filter((record) => !record.history)
    const done = records.filter((record) => record.history)
    const rows = open.length > 14 ? 2 : 1
    const columns = Math.max(2, Math.ceil(open.length / rows))
    const width = Math.max(2.4, columns * (LOT + GAP) - GAP)
    open.forEach((record, index) => {
      const column = Math.floor(index / rows)
      const row = index % rows
      const p = world(u + column * (LOT + GAP), -row * (LOT + GAP))
      buildings.push({ ...record, ...p, w: LOT, d: LOT, district: district.id, u: u + column * (LOT + GAP) })
    })
    const back = rows * (LOT + GAP)
    plots.push({ id: district.id, name: district.name, u0: u - 0.45, u1: u + width + 0.45, v0: -back + 0.2, v1: LOT + 0.45,
      count: open.length, finished: done.length, world })
    u += width + STREET
  }
  return { buildings, plots, archives, azimuth, length: Math.max(0, u - STREET) }
}

function boundsOf(plots) {
  if (!plots.length) return { x0: 0, y0: 0, x1: 1, y1: 1 }
  return { x0: Math.min(...plots.map((p) => p.x)), y0: Math.min(...plots.map((p) => p.y)),
    x1: Math.max(...plots.map((p) => p.x + p.w)), y1: Math.max(...plots.map((p) => p.y + p.d)) }
}

// Words for one building (the accessible name and the label the Site shows on hover).
export function buildingSummary(record) {
  const c = record.counts
  const parts = [c.needs && `${c.needs} need${c.needs === 1 ? 's' : ''} you`, c.working && `${c.working} working`, c.waiting && `${c.waiting} waiting`,
    c.held && `${c.held} on hold`, c.pending && `${c.pending} not started`, c.done && `${c.done} done`, c.canceled && `${c.canceled} canceled`].filter(Boolean)
  return `${record.total + c.canceled} task${record.total + c.canceled === 1 ? '' : 's'}: ${parts.join(', ')}`
}
