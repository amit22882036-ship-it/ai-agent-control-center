import { useMemo } from 'react'
import { projectPreview } from '../../model/projectPreview.mjs'

// The data for a project card's hierarchy picture: bounded, from the loaded Tasks only (it never
// reads more to decorate a card), laid out by the Work Map's own layout.
export function useProjectPreview(derived, projectId) {
  return useMemo(() => projectPreview(derived, projectId), [derived, projectId])
}
