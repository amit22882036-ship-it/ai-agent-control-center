import { memo } from 'react'
import { bandStack, buildingHeight } from '../../model/siteLayout.mjs'

// A family's building, as the Site draws it, small enough for a row: its floors stacked by
// construction state (built at the base, then held, glass, lit, wireframe, and the vermilion piece
// that needs you on top). The same object in the List and on the Site, so the eye learns it once.
const FILL = {
  built: ['--a-mat-built-l', '--a-mat-built-r', '--a-mat-built-top'],
  held: ['--a-mat-held-l', '--a-mat-held-r', '--a-mat-held-top'],
  glass: ['--a-mat-glass-l', '--a-mat-glass-r', '--a-mat-glass-top'],
  lit: ['--a-mat-lit-l', '--a-mat-lit-r', '--a-mat-lit-top'],
  needs: ['--a-mat-needs-l', '--a-mat-needs-r', '--a-mat-needs-top'],
}
const TowerGlyph = memo(function TowerGlyph({ counts, size = 30 }) {
  const total = ['done', 'held', 'waiting', 'working', 'pending', 'needs'].reduce((sum, key) => sum + (counts[key] || 0), 0)
  const height = buildingHeight(total)
  const bands = bandStack(counts, total, height)
  const cx = 11
  const hw = 7
  const dy = 3.6
  const base = size - 1.5
  const k = (size - 2 * dy - 3) / 4.2
  const y = (z) => base - z * k
  const faces = []
  bands.forEach((band, index) => {
    const yb = y(band.z0)
    const yt = y(band.z1)
    const top = index === bands.length - 1
    const left = `M${cx - hw} ${yb - dy}L${cx} ${yb}V${yt}L${cx - hw} ${yt - dy}Z`
    const right = `M${cx} ${yb}L${cx + hw} ${yb - dy}V${yt - dy}L${cx} ${yt}Z`
    const lid = `M${cx} ${yt}L${cx + hw} ${yt - dy}L${cx} ${yt - 2 * dy}L${cx - hw} ${yt - dy}Z`
    if (band.material === 'wire') {
      faces.push(<path key={`w${index}`} d={left + right + (top ? lid : '')} fill="none" stroke="var(--a-mat-wire)" strokeWidth=".8" strokeDasharray="1.4 1.1" />)
      return
    }
    const [l, r, t] = FILL[band.material] || FILL.built
    faces.push(<path key={`l${index}`} d={left} fill={`var(${l})`} />, <path key={`r${index}`} d={right} fill={`var(${r})`} />)
    if (top) faces.push(<path key={`t${index}`} d={lid} fill={`var(${t})`} />)
    faces.push(<path key={`e${index}`} d={left + right + (top ? lid : '')} fill="none" stroke="var(--a-mat-edge)" strokeWidth=".6" strokeLinejoin="round" />)
  })
  return <svg className="a-tower" width="22" height={size} viewBox={`0 0 22 ${size}`} aria-hidden="true" focusable="false">
    <ellipse cx={cx + 2} cy={base - dy * 0.4} rx={hw + 2} ry={dy * 0.9} fill="var(--a-mat-shadow)" />
    {faces}
  </svg>
})
export default TowerGlyph
