// The Work sheet: the List drawn as one flat sequence of fixed-height rows, so only the rows on
// screen exist in the page however many Tasks are loaded. Pure functions over `visibleRows(...)`
// (the same rows the Map lays out), the derived workspace and the clock.
import { taskCategory } from './workList.mjs'

// Row heights in CSS pixels at the default text size; the sheet scales them with the root font.
export const ROW = { section: 52, day: 36, family: 64, member: 50, more: 40, reveal: 56, end: 72 }

const DAY = 86400000
const parse = (value) => {
  const time = Date.parse(value || '')
  return Number.isFinite(time) ? time : null
}

// When a family last changed, from reported times only (its Tasks' updates and agent hand-overs).
export function familyTime(store, family) {
  let latest = null
  for (const id of family.members) {
    const task = store.tasks[id]
    const assignment = store.assignments[id]
    for (const value of [task?.updatedAt, task?.createdAt, assignment?.current?.assignedAt, assignment?.lastEnded?.endedAt]) {
      const time = parse(value)
      if (time !== null && (latest === null || time > latest)) latest = time
    }
  }
  return latest
}

// A day heading for finished work: Today, Yesterday, a weekday within the week, then a date.
export function dayLabel(time, now) {
  if (time === null) return { key: 'unknown', label: 'Date not recorded' }
  const start = new Date(now)
  start.setHours(0, 0, 0, 0)
  const midnight = start.getTime()
  if (time >= midnight) return { key: 'today', label: 'Today' }
  if (time >= midnight - DAY) return { key: 'yesterday', label: 'Yesterday' }
  const date = new Date(time)
  if (time >= midnight - 6 * DAY) return { key: `d${date.toDateString()}`, label: date.toLocaleDateString('en-GB', { weekday: 'long' }) }
  const sameYear = date.getFullYear() === new Date(now).getFullYear()
  return { key: `d${date.toDateString()}`, label: date.toLocaleDateString('en-GB', { day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }) }) }
}

// A family's team in words and counts: how many of its Tasks are in each state. Lead included.
export function teamCounts(derived, family) {
  const out = { size: family.members.length, needs: 0, working: 0, waiting: 0, held: 0, pending: 0, done: 0, canceled: 0 }
  for (const id of family.members) out[taskCategory(derived, id)] += 1
  return out
}

// The agents a family's row shows: current agents first (those working, then the others), at most
// `limit`, so a big team reads as faces without listing everyone.
export function teamFaces(derived, family, limit = 4) {
  const faces = []
  for (const id of family.members) {
    const agent = derived.agents[id]?.current
    if (agent && !agent.unknown) faces.push({ id, agent, working: agent.status === 'running' })
  }
  faces.sort((a, b) => Number(b.working) - Number(a.working))
  return { faces: faces.slice(0, limit), more: Math.max(0, faces.length - limit) }
}

// The flat row sequence. `finishedOpen` says whether the Finished section lists its families or
// only offers to; finished families are grouped by the day they last changed, newest first.
// `keep` (the selected Task's id) keeps that Task's family listed under a folded History, so the
// row the person is looking at never vanishes when its work finishes or is opened from elsewhere.
export function sheetRows(view, store, derived, { now = Date.now(), finishedOpen = true, complete = true, keep = null } = {}) {
  const rows = []
  const pushFamily = (family, sectionKey) => {
    for (const row of family.rows) {
      rows.push({ kind: row.depth === 0 ? 'family' : 'member', key: `task:${row.id}`, id: row.id, row, family, section: sectionKey })
      if (row.hiddenChildren > 0) {
        rows.push({ kind: 'more', key: `more:${row.id}`, id: row.id, depth: row.depth + 1, hidden: row.hiddenChildren, family })
      }
    }
    if (family.summary.notLoaded > 0 && !complete) {
      rows.push({ kind: 'more', key: `notloaded:${family.topId}`, id: family.topId, depth: 1, notLoaded: family.summary.notLoaded, family })
    }
  }
  for (const section of view.sections) {
    const finished = section.key === 'done'
    const families = finished
      ? section.families.map((family) => ({ family, time: familyTime(store, family) })).sort((a, b) => (b.time ?? -1) - (a.time ?? -1))
      : section.families.map((family) => ({ family, time: null }))
    const open = !finished || finishedOpen || view.searching || view.focus
    // A family kept in place for the selection is listed here but counted where it now belongs.
    rows.push({ kind: 'section', key: `section:${section.key}`, section: section.key, label: section.label,
      count: section.families.reduce((n, family) => n + (family.kept ? 0 : 1), 0), collapsible: finished && !view.searching && !view.focus, open })
    if (!open) {
      const kept = keep ? section.families.find((family) => family.rows.some((row) => row.id === keep)) : null
      if (kept) pushFamily(kept, section.key)
      const rest = section.families.length - (kept ? 1 : 0)
      if (rest > 0) rows.push({ kind: 'reveal', key: `reveal:${section.key}`, section: section.key, count: rest, more: Boolean(kept) })
      continue
    }
    let lastDay = null
    for (const { family, time } of families) {
      if (finished) {
        const day = dayLabel(time, now)
        if (day.key !== lastDay) {
          rows.push({ kind: 'day', key: `day:${day.key}`, label: day.label })
          lastDay = day.key
        }
      }
      pushFamily(family, section.key)
    }
  }
  // Rows between two section headings are drawn as one sheet: mark where each sheet starts and ends.
  let run = []
  const close = () => {
    if (run.length) { run[0].first = true; run[run.length - 1].last = true }
    run = []
  }
  for (const row of rows) {
    if (row.kind === 'section' || row.kind === 'reveal') close()
    else run.push(row)
  }
  close()
  return rows
}

// Where each row starts, and the total height, for heights scaled by `scale`.
export function sheetOffsets(rows, scale = 1) {
  const tops = new Array(rows.length)
  let y = 0
  for (let i = 0; i < rows.length; i += 1) {
    tops[i] = y
    y += Math.round(ROW[rows[i].kind] * scale)
  }
  return { tops, total: y + Math.round(ROW.end * scale) }
}

// The index of the first row whose box reaches `y` (binary search over the tops).
export function rowAt(tops, y) {
  let low = 0
  let high = tops.length - 1
  if (high < 0) return 0
  while (low < high) {
    const mid = (low + high + 1) >> 1
    if (tops[mid] <= y) low = mid
    else high = mid - 1
  }
  return low
}

// The rows to draw for a scroll position: those on screen plus a margin above and below.
export function sheetWindow(tops, total, scrollTop, height, overscan = 480) {
  if (!tops.length) return { start: 0, end: 0 }
  const start = rowAt(tops, Math.max(0, scrollTop - overscan))
  const end = rowAt(tops, Math.min(total, scrollTop + height + overscan)) + 1
  return { start, end: Math.min(tops.length, end) }
}

// Rows a keyboard can stand on: Tasks (family and member rows).
export const focusable = (row) => row.kind === 'family' || row.kind === 'member'

// "Changed since you looked": a Task whose record changed after the later of the moment the person
// last cleared the marks and the moment they last opened that Task. Reported times only.
export function changedSince(store, seen) {
  const since = parse(seen?.since)
  if (since === null) return () => false
  const opened = seen?.opened || {}
  return (id) => {
    const task = store.tasks[id]
    const updated = parse(task?.updatedAt)
    if (updated === null) return false
    const looked = parse(opened[id])
    return updated > Math.max(since, looked ?? -Infinity)
  }
}

// Recently opened Tasks, newest first, without repeats (at most `limit`).
export function rememberRecent(list, id, limit = 12) {
  return [id, ...(Array.isArray(list) ? list.filter((item) => item !== id) : [])].slice(0, limit)
}
