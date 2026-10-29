import { useCallback, useState } from 'react'
import useModelCatalog from '../../../useModelCatalog'
import { useActionRunner, useAgentDetail } from '../../hooks'
import { actionTarget, targetStillCurrent } from '../../model/agentTarget.mjs'
import { capabilitiesFor } from '../../model/agentCapabilities.mjs'
import { agentLabel } from '../../model/workList.mjs'

export const CHANGED_TARGET = 'This task’s agent changed. Nothing was sent; review the task and try again.'

// Everything an action needs: the task's current agent (never history), that agent's own read
// model, the model catalog of its provider and one shared action runner. `getStore` returns the
// latest store, so a request is refused if the agent changed after the controls were drawn.
export function useAgentScope({ store, taskId, version, getStore, onRefresh }) {
  const target = actionTarget(store, taskId)
  const agentId = target.agentId || target.lastAgentId
  const [local, setLocal] = useState(0)
  const detailVersion = `${version}-${local}`
  const detailResource = useAgentDetail(agentId, detailVersion)
  const detail = detailResource.data?.agent_id === agentId ? detailResource.data : null
  const current = target.kind === 'current' && detail !== null
  const models = useModelCatalog(current && detail.agent_type !== 'mock' ? detail.agent_type : null)
  const onChanged = useCallback(() => { setLocal((value) => value + 1); onRefresh?.() }, [onRefresh])
  const runner = useActionRunner(onChanged)
  // Drafts belong to the task, so a reply being written survives while a replacement agent's
  // details load; each remembers the agent it was written for.
  const [drafts, setDrafts] = useState({})
  const draftStore = { get: (kind) => drafts[kind] || { text: '', for: null }, set: (kind, value) => setDrafts((current) => ({ ...current, [kind]: value })) }
  const nameOf = (id) => (store.agents[id] ? agentLabel(store.agents[id]) : id === detail?.agent_id ? detail.display_name || 'Untitled agent' : 'Agent outside the loaded list')
  const caps = current ? capabilitiesFor(detail) : null
  const guard = () => (targetStillCurrent(getStore(), taskId, target) ? null : CHANGED_TARGET)
  // Identity edits address the agent shown (current, or the last one, labelled as history).
  const guardIdentity = () => {
    const latest = actionTarget(getStore(), taskId)
    return (latest.agentId || latest.lastAgentId) === agentId ? null : CHANGED_TARGET
  }
  const scope = caps ? { taskId, agentId: target.agentId, detail, caps, models, runner, guard, drafts: draftStore,
    agentName: detail.display_name || nameOf(target.agentId) } : null
  return { target, agentId, detail, detailResource, scope, runner, nameOf, guardIdentity, version: detailVersion }
}
