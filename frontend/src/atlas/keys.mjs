// Single-letter page shortcuts. One rule for every page, so a key never fires while the person
// types or while a menu, dialog, panel or listbox is open: a field (input, textarea, select,
// contenteditable or a textbox role), an IME composition, a held-down repeat and any Ctrl, Alt or
// Meta combination (browser and system shortcuts) all leave the key alone.
const FIELDS = 'input, textarea, select, [contenteditable=""], [contenteditable="true"], [role="textbox"], [role="combobox"], [role="spinbutton"]'
const OVERLAYS = 'dialog[open], [role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]'

export function overlayOpen(root = globalThis.document) {
  return Boolean(root?.querySelector(OVERLAYS))
}

// `exclude` adds selectors whose keys belong to that region (the Work details panel).
export function pageShortcut(event, { exclude = '' } = {}) {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.isComposing || event.keyCode === 229 || event.repeat) return false
  const target = typeof Element !== 'undefined' && event.target instanceof Element ? event.target : null
  if (target && (target.isContentEditable || target.closest(`${FIELDS}, ${OVERLAYS}${exclude ? `, ${exclude}` : ''}`))) return false
  return !overlayOpen()
}
