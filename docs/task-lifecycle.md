# Tasks, lifecycle, and dependencies

Tasks represent durable work; Agents are workers linked through assignment history. These lifecycle and dependency controls are backend APIs, not dedicated dashboard controls. The dashboard still exposes Agent-level Stop, Reply, Redirect, and decision delegation. Stopping a worker and canceling a Task have different semantics.

`POST /tasks` creates work, `GET /tasks` and `GET /tasks/{task_id}` inspect it, `GET /tasks/{task_id}/assignments` exposes assignment history, and `POST /tasks/{task_id}/start-agent` starts a worker for eligible pending work. Explicit child creation uses `POST /agents/{parent_id}/children/start`; the current dashboard displays existing relationships but does not mount a child-creation form. Hierarchy does not automatically infer dependencies or decompose work.

This reference preserves the Stage 2 design and migration details. Stage labels explain how the implementation evolved; later sections refine earlier behavior. The current database schema is v18; [Delegation records](delegations.md) add request provenance without replacing Task hierarchy or lifecycle.

[Project overview and setup](../README.md) | [Task lifecycle](task-lifecycle.md) | [Workspaces](workspaces.md) | [Resource coordination](resource-coordination.md)

## Work lifecycle foundation (Stage 2F.1)

Task work states are `pending`, `in_progress`, `waiting`, `blocked`, `paused`, `completed`, and `canceled`. Schema v11 transactionally migrates v10, preserving history and adding `control_intent` (`active`, `paused`, `canceled`) and nullable `resume_status`. Historical canceled work receives canceled intent; other existing work defaults to active. Task responses expose these fields. Blocked is system-managed; there is no public arbitrary block/status-setting endpoint.

Backend-only `POST /tasks/{task_id}/pause`, `/resume`, and `/cancel` accept no required body. Stage 2F.2 replaces the initial descendant guard with the subtree controls described below. Completed work rejects all three actions, canceled work rejects Pause/Resume, and repeated Pause/Cancel are safe. Resume requires a safely paused Task and never spawns a worker.

Pause/Cancel commit intent before attempting to stop a worker. Worker starts, child creation, Reply, Redirect, Decide, Similar, and Always cannot bypass non-active intent or blocked/terminal work. A running worker is stopped using existing process-tree handling, final output is drained and persisted, and its assignment ends with `paused` or `canceled`. Stop/drain/persistence failure returns an unavailable response; GET exposes the retained intent and unfinished runtime state for retry/recovery. Completion wins only if committed before control intent; otherwise the intent governs finalization.

Pause preserves a dormant waiting assignment, question, and session, including across restart. Resume restores `waiting`, `blocked`, or `pending` as appropriate and clears pause intent. Cancel ends any waiting association without deleting output, sessions, workspaces, or integration history. Generic Stop Agent and Stop Branch retain worker semantics (normally Task pending); an existing pause/cancel intent takes precedence. If generic Stop explicitly ends a paused waiting association, Resume restores pending work rather than an orphan waiting state.

These controls do not refresh/reset/integrate workspaces or change canonical files. Recovery retains paused/canceled/blocked work and finalizes interrupted control intent using the existing no-process-reattachment model. No frontend Work controls or orchestration are included yet.

## Dependencies and control impact (Stage 2F.2)

Schema v12 transactionally migrates v11. It preserves existing Tasks, assignments (including row order), Projects, workspaces, sessions, source context and integrations. It adds dependency edges, generic source-specific blockers and replanning reasons with resolved history, plus control operations and pause ownership. Existing independent pauses retain their ownership; migration fabricates no edges, blockers or replanning reasons. Newer unknown schemas are rejected.

`B depends_on A` means B cannot proceed until A is **completed**. Edges are explicit, unique, within one resolved Project, and distinct from hierarchy. Self-links and arbitrary-depth cycles are rejected inside the same `BEGIN IMMEDIATE` transaction that inserts the edge, including concurrent inverse-link requests. Terminal dependent Tasks reject new edges.

Backend APIs:

- `POST /tasks/{task_id}/dependencies` with `{"depends_on_task_id":"<uuid>"}` adds an edge and safely reconciles the dependent.
- `GET /tasks/{task_id}/dependencies` returns `{dependencies: [...]}`; `GET /tasks/{task_id}/dependents` returns reverse edges as `{dependents: [...]}`.
- `DELETE /tasks/{task_id}/dependencies/{depends_on_task_id}` idempotently removes an edge and resolves only its attributable reasons.
- `POST /tasks/{task_id}/control-impact` with `{"action":"pause"}` or `{"action":"cancel"}` previews affected subtree Tasks, same-project external dependents, running workers, untouched terminal descendants, blocking and replanning consequences. It does not mutate anything. Application recomputes the same plan transactionally.
- `GET /tasks/{task_id}/control-operations` exposes operation identity, impact, status, release state and per-Task recovery failures.

Task responses expose `active_blockers`, `replanning_reasons`, derived `replanning_required`, `block_resume_status` and a durable `stop_required` gate alongside work intent. Unsatisfied edges produce `dependency_incomplete`, `dependency_paused` or `dependency_canceled`; canceled prerequisites also produce a separate replanning reason. All active hard blockers must clear before restoration. Dependency removal does not clear unrelated resource blockers or hierarchical replanning reasons. No public generic-blocker editor or replacement-edge API is introduced; remove/add are separate explicit actions.

Pending work restores to pending; blocked waiting work retains its assignment/question/session and restores to waiting. Running work is gated durably before its worker is stopped and output drained, ends its assignment as `blocked`, and restores only to pending. An already-completed prerequisite does not disturb the worker. Completion reconciles dependents in the lifecycle transaction before publication, without polling or automatic worker creation. Pause/Cancel intent takes precedence, and Resume re-evaluates current blockers rather than restoring an obsolete blocked state. Starts, child creation and all same-session continuation actions check hard blockers and unsettled-stop authority.

Pause/Cancel now apply to the target and all unfinished descendants, preserving completed/canceled descendants. Pause ownership is recorded per operation: resuming a parent releases only that cascade, preserving independent and overlapping child pauses. An ancestor-owned child pause cannot be released through the child without its own operation. External dependents become blocked, not paused/canceled. Pause alone does not request replanning. Cancellation adds source-specific `child_canceled` awareness through nonterminal ancestors without inventing hierarchy dependencies; direct dependents additionally receive cancellation blockers/replanning reasons.

Multi-worker application is phased: commit intent/provenance/blockers, stop and drain workers outside SQL transactions, then persist completion or `recovery_required`. A failed stop retains truthful running state and execution gates; other workers still settle. The API returns an unavailable error on runtime/storage failure; inspect Tasks and operation records and retry the same action. A dependency stop failure exposes `stop_required` and can be retried by repeating the edge operation. Recovery uses durable intent and the existing no-reattachment policy; it preserves graph/history and never spawns workers. It does not claim to terminate or reattach an unknown OS process surviving an abrupt backend crash.

Workspace isolation, freshness, integration conflicts, canonical/index protection and session-context checks remain separate and unchanged. There is no frontend dependency editor or autonomous replanning.
