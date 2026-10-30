// The List/Map transition: each visible agent's avatar travels from where it was to where it is
// now, so both views read as the same work. DOM-only and short; it never delays the new view,
// which is already interactive underneath. Skipped with reduced motion and for very large views.

const LIMIT = 70
const DURATION = 520
const EASE = 'cubic-bezier(.3,.7,.1,1)'

function anchorOf(element) {
  for (const candidate of element.querySelectorAll('.a-avatar, .a-avatar-empty, .a-row-bead')) {
    const rect = candidate.getBoundingClientRect()
    if (rect.width > 0 && rect.height > 0) return candidate
  }
  return null
}

const onScreen = (rect) => rect.bottom > -40 && rect.top < window.innerHeight + 40 && rect.right > -40 && rect.left < window.innerWidth + 40

// Where each Task's avatar is now, by Task id (only those on screen).
export function captureAnchors(root) {
  const out = new Map()
  if (!root) return out
  for (const element of root.querySelectorAll('[data-id]')) {
    if (out.size >= LIMIT * 2) break
    const anchor = anchorOf(element)
    if (!anchor) continue
    const rect = anchor.getBoundingClientRect()
    if (onScreen(rect)) out.set(element.dataset.id, rect)
  }
  return out
}

export function prefersReducedMotion() {
  return Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
}

export function playLensTransition(root, from) {
  if (!root || !from?.size || prefersReducedMotion() || typeof Element.prototype.animate !== 'function') return
  const host = root.closest('.atlas') || document.body
  const bounds = root.getBoundingClientRect()
  const layer = document.createElement('div')
  layer.className = 'a-ghosts'
  layer.setAttribute('aria-hidden', 'true')
  layer.style.clipPath = `inset(${bounds.top}px ${window.innerWidth - bounds.right}px ${window.innerHeight - bounds.bottom}px ${bounds.left}px)`
  host.appendChild(layer)
  const animations = []
  let count = 0
  for (const element of root.querySelectorAll('[data-id]')) {
    if (count >= LIMIT) break
    const source = from.get(element.dataset.id)
    const target = anchorOf(element)
    if (!source || !target) continue
    const rect = target.getBoundingClientRect()
    if (!onScreen(rect) && !onScreen(source)) continue
    count += 1
    const ghost = target.cloneNode(true)
    ghost.removeAttribute('id')
    ghost.classList.add('a-ghost')
    Object.assign(ghost.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` })
    layer.appendChild(ghost)
    const dx = source.left + source.width / 2 - (rect.left + rect.width / 2)
    const dy = source.top + source.height / 2 - (rect.top + rect.height / 2)
    const scale = source.width / rect.width
    animations.push(ghost.animate([{ transform: `translate(${dx}px, ${dy}px) scale(${scale})`, opacity: 1 }, { opacity: 1, offset: 0.9 },
      { transform: 'none', opacity: 0 }], { duration: DURATION, easing: EASE, fill: 'forwards' }))
    animations.push(target.animate([{ opacity: 0 }, { opacity: 0, offset: 0.88 }, { opacity: 1 }], { duration: DURATION }))
  }
  // The new view's lines and text settle in while the avatars travel.
  for (const element of root.querySelectorAll('.a-edges, .a-gt, .a-gs, .a-grt, .a-c-main, .a-c-now, .a-c-time, .a-c-who .a-w')) {
    if (animations.length > LIMIT * 4) break
    animations.push(element.animate([{ opacity: 0 }, { opacity: 0, offset: 0.35 }, { opacity: 1 }], { duration: DURATION, easing: 'ease-out' }))
  }
  const done = () => layer.remove()
  Promise.all(animations.map((animation) => animation.finished)).then(done, done)
  setTimeout(done, DURATION + 300)
}
