# Agent removal

Removing an Agent permanently retires its operational identity. It does not delete history and it does not complete, cancel or delete the Agent's Task. Stage 2D.1 built the durable foundation; Stage 2D.2 adds hierarchy-aware removal (a parent whose Children continue, or a whole branch), explicit responsibility transfer and the dashboard experience.

[Project overview and setup](../README.md) | [Task lifecycle](task-lifecycle.md) | [Process supervision](process-supervision.md) | [Local API security](api-security.md)

## Stop, Remove and Hide

| Action | Effect | Reversible |
| --- | --- | --- |
| Stop | Ends the current execution. The Task normally returns to `pending` and can get a worker again. | Yes |
| Remove | Stops any execution, then retires the Agent identity for good. The Task, output, workspace, assignments, dependencies and integrations are preserved. Work continues only through a new Agent. | No |
| Hide | A view concern: the workspace can show or hide removed Agents. Nothing is changed. | Yes |

A removed Agent keeps its row, output, name history and its place in the historical hierarchy. It can still be renamed or recolored, and Stop on it is harmless. It can never start, resume, receive a Reply, Redirect, Decide, Similar or Always continuation, parent a new Child, delegate, review or acknowledge results, acquire resources or own Hybrid orchestration again. A new Agent's admission transaction refuses a removed or retiring Parent, so a Child start racing a removal cannot slip in.

## Modes (Stage 2D.2)

| Mode | Effect |
| --- | --- |
| `single` ("Remove this agent") | Only the selected Agent is removed. Its descendants keep their identities, lineage and execution authority and continue their own Tasks. Nothing is reparented and no replacement parent is created. |
| `retire_branch` ("Remove entire branch") | The Agent and every descendant not removed yet, in one durable operation with one member row per Agent (parents first). |
| `continue_children` | Superseded by `single`; unsupported (`422 unsupported_mode`). |

**What Children may still do after their parent is removed.** Their own work: running, waiting for and answering the user, stopping, finishing their own Task, creating their own Children. **What waits:** anything that needs the removed Agent's authority. A delegated Child's result is recorded and kept but is not delivered, reviewed or acknowledged; a requested Delegation that has no Child yet is not materialized; the removed Agent's Task is not completed by its Children finishing (required Delegations stay unresolved). Previews mark each active descendant as `continues` or `continues_result_waits`.

**Coordinated delegated work.** The removed Agent's coordination is never moved to another Agent. Two conditions are distinguished from durable state, and neither is transferable:

| Reason | Condition | Why it cannot be transferred |
| --- | --- | --- |
| `hybrid_orchestration_bound` | A Hybrid continuation that is not completed. It takes precedence: its Delegations are managed by that continuation. | **Hybrid exception.** The continuation is bound to the removed Agent's session, receipt and generation. Removal disables it (`delegation_continuations.phase = 'stopped'`, with an explanatory `last_error`); previews report `orchestration_disabled` and `task_waits_hybrid_orchestration_bound`. |
| `delegation_handoff_unsettled` | Otherwise, open Delegations, or an accepted handoff receipt with a Delegation not acknowledged (including closed historical ones). | Ordinary (non-Hybrid) Delegations have no result review or acknowledgement protocol. This limitation predates removal: such a Task stays blocked by `delegation_requested` even without removal. Previews report `task_waits_delegation_handoff_unsettled`. A future orchestration milestone may add an explicit review mechanism. |

In both cases the Children still continue, their work and the Task are preserved, and the Task can still be canceled.

For `delegation_handoff_unsettled`, `GET /tasks/{task_id}/responsibility` also lists completed delegated Children in `integration_candidates`, with whether the existing explicit `POST /tasks/{child_task_id}/integrate` could currently run and why not. The listed conditions mirror its durable preconditions: both Task workspaces exist, no delegated launch or process ownership is unresolved, no Agent of either Task is running and no other integration is in progress. The integration re-checks them and the Git state itself. Integrating is not a Delegation acknowledgement: the Delegation stays `materialized`, `delegation_requested` stays active and the Task is not completed. Hybrid Tasks get no such guidance.

## Lifecycle

Removal is a durable operation in `agent_removals`, with one row per affected Agent in `agent_removal_members`. Agent execution status (`agents.status`) stays a runtime status; retirement is recorded separately in `agents.removed_at` and `agents.removal_id`.

| Product state | Stored as | Execution gate |
| --- | --- | --- |
| Removal Pending ("Removal in progress") | member phase `admitted` or `stopping` | Start, resume, child creation and continuation are refused |
| Removed | member `removed`, `agents.removed_at` set | Permanent |
| Needs Attention ("Removal blocked") | member and operation `recovery_required`, with `last_error` | Same as Pending; the assignment and resource claims are retained |

A branch operation aggregates its members: `removed` when every member is removed, `recovery_required` when any member is blocked (the others may already be removed; they are never resurrected), otherwise in progress. Each member progresses and fails independently.

Steps, each in its own short SQLite transaction (no transaction is held while a process stops):

1. **Admission.** The plan is recomputed from durable state and its token compared with the one the caller previewed. Availability, idempotency and conflicting removals are checked. The operation and member rows commit before any process is touched.
2. **Stopping.** For a running or waiting Agent, the member is journaled as `stopping`, then the existing Stop path runs (Job Object termination, confirmed end, output drained and saved).
3. **Settlement.** Only after the execution is verified settled: the open assignment ends with the distinct reason `removed`, `removed_at` is set and the member becomes `removed`.

An Agent with no live execution (finished, stopped or waiting) is admitted and settled in the same request.

`ended_reason='removed'` is applied only by removal. Ordinary Stop still ends assignments as `stopped`. If Task Cancel governs the termination first, the assignment keeps `canceled`, and the member outcome records that.

### Task outcome

- The Task is preserved. After removal it is `pending` (or `blocked`/`paused` when its own gates or intent say so) and awaits an explicit decision: start a replacement with the existing `POST /tasks/{task_id}/start-agent`, cancel it with `POST /tasks/{task_id}/cancel`, or leave it. Nothing restarts automatically.
- A replacement is always a new Agent identity with a new session. The previous assignment with reason `removed` in `GET /tasks/{task_id}/assignments` shows the lineage.
- Dependencies are not satisfied by removal: dependents of an unfinished Task stay blocked.
- Workspaces, partial changes, output and integration history are untouched. Normal freshness and integration gates apply to a replacement.

### Completion races

- A successful completion committed **before** admission is preserved: the Task stays `completed`.
- Once admission has committed, removal wins. A later exit, including a successful one, is recorded as `stopped`: the assignment ends `removed`, the Task does not complete, no Always/Similar continuation runs, and a structured execution's final message is not accepted. Its output is still saved. This is enforced in the persistence transaction (`work_control.sync`), not only in memory. Planning, delegation, promotion and replanning acceptance check the removal journal inside their own transactions: an exact replay of a receipt committed before admission is still returned read-only, but no new structured work is accepted. Hybrid ownership checks and direct `AgentStore.end_assignment` refuse an Agent under removal in the same way; only verified removal settlement ends its assignment.

## Availability

Supported: `mode` `single` or `retire_branch`, with `task_disposition: "preserve"`. Other values fail explicitly with `422` and code `unsupported_mode` or `unsupported_task_disposition`. Active descendants and coordinated delegated work no longer refuse removal (Stage 2D.2).

Removal is unavailable (`409 not_available`, with reason codes) when:

| Reason | Applies to | Meaning |
| --- | --- | --- |
| `already_removed`, `removal_in_progress` | both | The identity is retired or another removal is live. |
| `responsibility_transition_in_progress` | both (every member for a branch) | A replanning transition targets the Task. |
| `unknown_launch_ownership` | both (every member for a branch) | A delegated launch for the Task is `launching` or `recovery_required`. |
| `descendant_removal_in_progress` | branch | A descendant is already part of another live removal. Finish that removal first. |
| `launch_in_progress` | branch | A worker launch for a Child Task of a member is in flight and its Agent is not recorded yet. Retry shortly. |

The preview lists the exact branch (`members`, with role, depth, Task, assignment, status and unresolved executions) and its `plan_token` covers it. Any hierarchy change before admission (for example a new Child) makes the token stale: `409 stale_preview` returns the fresh preview and nothing is executed. Once admitted, the member set is fixed; an Agent created later is never added silently.

A replanning transition that would retire a Task whose worker is being removed stays `blocked` until a fresh decision.

## API

All `POST` routes are protected by the existing [CSRF and Origin guards](api-security.md).

- `POST /agents/{agent_id}/removal-preview` with optional `{"mode": "single" | "retire_branch", "task_disposition": "preserve"}`. Read-only. Returns the impact (Agent, Task, assignment, descendants, blockers, external dependents, integrations, delegated results, resource claims, workspace), per-option availability with reason codes, consequences and `plan_token`.
- `POST /agents/{agent_id}/remove` with `{"request_key", "plan_token", "mode", "task_disposition", "reason"}`. `request_key` (1–128 visible characters) makes the request idempotent: the same key with the same content returns the original operation without doing anything again; the same key with different content is `409 request_key_conflict`. `reason` is optional free text of at most 2000 characters. A changed impact returns `409 stale_preview` with the fresh preview; nothing is executed.
- `POST /agent-removals/{removal_id}/retry` (the optional `{"confirm_ended": true}` is accepted but settles nothing by itself). Reconciles the members' executions from kernel and reboot evidence, then continues an unfinished removal.
- `GET /agent-removals/{removal_id}` and `GET /agents/{agent_id}/removals`. Read-only.

Errors that refuse a removal use `{"detail": {"code": ..., "message": ...}}`. When an admitted removal cannot settle, the response is `503` with code `removal_recovery_required` and the stored operation; inspect `last_error` and retry.

Every operation records its source (`user`), authorization basis (`local_api`), optional reason, plan, timestamps and per-member outcome. The API has no user authentication; see [Local API security](api-security.md).

## Failures and recovery

| Situation | Result |
| --- | --- |
| Stop cannot be confirmed | Member `recovery_required` (`stop_not_confirmed`). Assignment, claims and gates are kept. Retry after the cause is fixed. |
| Lost process watcher | `recovery_required` (`watcher_lost`); use the existing [watcher recovery](api-security.md#watcher-failure) first. |
| Saving settlement fails | The member stays `stopping`; the process is already stopped. Retry settles it. |
| Saving admission fails | Nothing is recorded and nothing is stopped. |
| Backend restart, Agent had no live execution | Settled automatically at startup (`settled_by_restart_recovery`). |
| Backend restart while the Agent was executing, its execution settled from evidence | Settled automatically at startup (`settled_by_restart_recovery`). |
| Backend restart, its execution unresolved | `recovery_required` (`execution_unresolved`). Never resumed. Startup leaves its assignment open, so worker-lifetime claims, work-intent scopes and the Task's assignment slot stay held; ordinary saves, Cancel and later restarts cannot settle it. |

## Responsibility transfer (Stage 2D.2)

When an Agent is removed while it owns a non-terminal Task (an open assignment), the Task keeps its identity, workspace, results and history and waits for an explicit new owner. Nothing creates or promotes an Agent automatically.

- `GET /tasks/{task_id}/responsibility` (read-only) returns the current `owner`, `awaiting_transfer`, `transferable`, the `removed_owner` (Agent, assignment, removal), the refusal `reasons` and the transfer history.
- `POST /tasks/{task_id}/responsibility/transfer` (CSRF-protected) with `{"request_key", "expected_owner_assignment_id", "agent_type": "mock" | "codex", "sandbox", "reason"}` starts a new Agent for the Task through the existing start path. Unknown fields are rejected (`422`). `expected_owner_assignment_id` binds the request to the reviewed state (`409 stale_responsibility` otherwise). The same `request_key` returns the recorded transfer without starting anything again.

A transfer is refused (`409 not_transferable`, with reasons) while: the Task is completed or canceled or already has an owner; the old owner's removal has not settled; another transfer is live; any execution of the Task or the removed owner is unresolved (it settles only from kernel or reboot evidence or an eligible [P3 attestation](process-supervision.md#diagnostics-and-human-attestation-schema-v29)); its coordination is bound to the removed owner (`hybrid_orchestration_bound` or `delegation_handoff_unsettled`, above); a replanning transition or an unknown delegated launch targets it; the Task is paused, must stop or has other hard blockers; an ancestor's control prevents work; or the parent Task's owner is being removed.

Each decision is journaled in `responsibility_transfers`: `admitted` commits first, then the existing start path re-checks every execution, resource, work-intent and lifecycle gate in its own admission transaction, then the row becomes `completed` with the new Agent's assignment. A refused start leaves it `failed` and the Task still waiting. Earlier assignments, including the one that ended `removed`, are never rewritten. `POST /tasks/{task_id}/start-agent` on a Task that awaits a new owner goes through the same transfer (recorded with source `start_agent`). The new Agent's parent is the parent Task's current owner, if any; it is never placed under the removed Agent.

**Restart.** An interrupted `admitted` transfer is settled from durable assignments alone: `completed` if the new owner's assignment committed, otherwise `failed`. Startup never starts an Agent; a process a launch may have created is governed by the execution journal.

## Dashboard (Stage 2D.2)

Removed Agents stay in the historical hierarchy, marked with a red X icon, a strike-through name and a "Removed" status label (screen readers announce "removed"; color is never the only signal). Removal in progress and removal blocked have their own labels; a blocked removal also appears in attention. The hierarchy options can hide removed Agents: their remaining descendants stay visible as top-level entries marked "Parent removed · hidden", without inventing a parent. Branches in which every Agent is removed start collapsed and can be expanded again. The Hierarchy Explorer offers the same Show/Hide control.

The inspector's removal section uses progressive disclosure: choose "This agent only" or "Entire branch", review the backend preview (affected Agents, continuing Children, waiting results, unresolved executions, consequences, or why removal is unavailable), confirm deliberately, and see the backend's outcome. A stale preview is replaced and needs a new confirmation. In progress or blocked, it shows branch counts (affected, removed, pending, blocked), each blocked member's reason, Retry and Refresh, and a link to Process ownership when an execution is unresolved. A removed Agent shows what remains: its Children and the Task's responsibility, with an inline form to assign a new owner when the backend allows it. There is no modal and no override.

## Settlement evidence

A member settles only when every execution of its Agent is settled by the [execution journal](process-supervision.md#restart-recovery): kernel evidence about its Job, a verified Windows restart, or an eligible [human attestation](process-supervision.md#diagnostics-and-human-attestation-schema-v29) recorded for that execution. After an attestation, retry the removal. A confirmation flag, a clean process scan or the supervision output marker is never proof that a process ended. Settling the removal does not clear a Task's `process_ownership_unknown` blocker; `POST /tasks/{task_id}/process-ownership/confirm-stopped` still governs new work on that Task.

While any removal of a Task's worker is live, resource claims and work intents of that Task are neither suspended nor released, as for uncertain replanning stops. After confirmation the assignment ends as `removed` and claims follow normal coordination. Startup recovery is idempotent and never starts, resumes or reattaches a process.

## Schema v27

The transactional v26 → v27 migration:

- adds `agent_removals` (request key, target, mode, disposition, source, authorization basis, reason, plan JSON and token, phase, error and timestamps) and `agent_removal_members` (role, Task, assignment, execution generation, phase, outcome, failure). Partial unique indexes allow one live operation per target and one live membership per Agent.
- adds nullable `agents.removed_at` and `agents.removal_id`.
- widens `task_assignments.ended_reason` with `removed` by rebuilding the table, preserving row order, identifiers, partial unique indexes and constraints.

No historical rows are fabricated and no Agent is marked removed. Foreign-key enforcement is disabled only on the migration connection while upgrading (the rebuild needs it, as earlier rebuilds did); `PRAGMA foreign_key_check` runs after the step. Failure rolls back schema and version together, and newer schemas are rejected.

## Schema v30

The additive v29 → v30 migration creates `responsibility_transfers` (request key, Task, removed owner's Agent and assignment, removal, new Agent and assignment, provider and sandbox, source, authorization basis, optional reason, phase, failure and timestamps), with a partial unique index allowing one live transfer per Task. It changes no existing table and fabricates no rows; it is idempotent and rolls back with the version on failure. Branch removal reuses the v27 journal (`retire_branch` mode and `descendant` members already existed).

Read models add `removed`, `removal` (`state`: `removing`, `blocked` or `removed`, with operation and mode) and, on workspace nodes, `responsibility_awaiting`.

## Not implemented yet

- Transferring Hybrid coordination (by design it is disabled) or ordinary Delegation coordination (deferred: it needs a result review and acknowledgement protocol, to be considered in a future orchestration milestone).
- Selecting an existing Agent as the new owner (each Agent owns its own Task; a new owner is always a new Agent).
- Canceling the Task as part of removal (cancel it separately).
- Archive, and removal proposed or performed by Agents.

## Verification

`backend/test_smart_removal.py` (Stage 2D.2) covers parent removal with continuing Children, refused authority of a removed Agent, a Child start racing admission, exact branch selection and parent-first order, stale and frozen branches, previously removed descendants, overlapping removals (including the live-membership index), partial failure and retry, an unresolved member settled only by a P3 attestation, launches in flight, crash and restart with reboot evidence, idempotent submissions, the Hybrid exception (continuation disabled, Children continue, results kept, no completion, no transfer), explicit and refused transfers, failed starts, concurrent transfers, interrupted transfers, read models, the API and the v29 → v30 migration. A real Windows test removes a branch of supervised disposable workers and checks that every tree ended with kernel evidence.

`backend/test_agent_removal.py` uses temporary SQLite databases, temporary Git repositories and process doubles. It covers admission, idempotency, stale previews, completion and Cancel/Stop/replacement races, failed termination, restart at each boundary, repeated recovery, storage failures, preservation of Tasks, dependencies, workspaces and integrations, execution gates, the API and the v26 → v27 migration and rollback. These tests do not prove real Windows process behavior or real Codex behavior.
