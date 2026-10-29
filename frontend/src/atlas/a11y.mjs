// Roving focus for composite widgets (radio groups, tabs, menus): one item is in the Tab order and
// the arrow keys move between the others. Returns the next index, or null when the key is not ours.
export function rovingTarget(index, key, count, { isDisabled = () => false, orientation = 'horizontal', dir = 'ltr' } = {}) {
  if (count <= 0) return null
  const forward = orientation === 'vertical' ? ['ArrowDown'] : [dir === 'rtl' ? 'ArrowLeft' : 'ArrowRight']
  const backward = orientation === 'vertical' ? ['ArrowUp'] : [dir === 'rtl' ? 'ArrowRight' : 'ArrowLeft']
  let step
  let start
  if (forward.includes(key)) { step = 1; start = index }
  else if (backward.includes(key)) { step = -1; start = index }
  else if (key === 'Home') { step = 1; start = -1 }
  else if (key === 'End') { step = -1; start = count }
  else return null
  for (let moved = 1; moved <= count; moved++) {
    const candidate = (((start + step * moved) % count) + count) % count
    if (!isDisabled(candidate)) return candidate
  }
  return null
}

// The props of the panel that a `Tabs` tab controls (ids pair the tab and its panel).
export function tabPanelProps(idBase, id) {
  return { role: 'tabpanel', id: `${idBase}-panel-${id}`, 'aria-labelledby': `${idBase}-tab-${id}`, tabIndex: 0 }
}

// Initials for an agent avatar: the first letters of the first two words, or the first two
// letters of a single word. Works for any script, including Hebrew.
export function initials(name) {
  const words = String(name || '').trim().split(/\s+/).filter(Boolean)
  if (!words.length) return '?'
  const letters = words.length > 1 ? [...words[0]][0] + [...words[1]][0] : [...words[0]].slice(0, 2).join('')
  return letters.toUpperCase()
}
