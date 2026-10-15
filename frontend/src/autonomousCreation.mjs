// Store the user's choice independently of provider eligibility.
export const initialAutonomousChoice = true
export function autonomyEligible(provider, parentId = null) {
  return provider === 'codex' && parentId === null
}
export function creationPayload(task, provider, sandbox, choice, parentId = null, projectId = undefined) {
  if (parentId === null && projectId !== undefined && (typeof projectId !== 'string' || !projectId.trim())) throw new Error('Choose a valid Project before starting.')
  return { ...(parentId === null && projectId !== undefined ? { project_id: projectId } : {}), task: task.trim(), agent_type: provider, sandbox,
    autonomous: autonomyEligible(provider, parentId) && choice === true }
}
