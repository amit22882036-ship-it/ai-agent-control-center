// How far (in px) to move a popover surface horizontally so it stays inside the viewport with
// `margin` on each side. Positive moves it right. A surface wider than the viewport keeps its
// left edge visible (its width is already capped in CSS).
export function viewportShift(rect, viewportWidth, margin = 16) {
  if (!rect || !viewportWidth) return 0
  if (rect.left < margin) return Math.round(margin - rect.left)
  if (rect.right > viewportWidth - margin) return Math.max(Math.round(viewportWidth - margin - rect.right), Math.round(margin - rect.left))
  return 0
}
