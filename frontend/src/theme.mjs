export const themeModes = ['automatic', 'light', 'dark']
export function readTheme(storage) {
  try { const value = (storage ?? globalThis.localStorage).getItem('control-center.theme'); return themeModes.includes(value) ? value : 'automatic' } catch { return 'automatic' }
}
export function applyTheme(mode, darkSystem) { return mode === 'automatic' ? (darkSystem ? 'dark' : 'light') : mode }
export function connectTheme(mode, root, media) {
  const update = () => { root.dataset.theme = applyTheme(mode, media.matches) }
  update()
  media.addEventListener('change', update)
  return () => media.removeEventListener('change', update)
}
