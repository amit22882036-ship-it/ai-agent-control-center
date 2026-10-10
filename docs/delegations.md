# Delegations, Agent requests and Child materialization (Stage 2H.1–2H.3)

Delegations record why a Parent Task requested a unit of work and which Worker assignment originated it. They are provider-independent durable metadata, separate from Task hierarchy, dependencies, work intents and resource claims. Stage 2H.2 adds an opt-in structured request protocol and durable Parent handoff. Stage 2H.3 adds separately activated Child Task/workspace materialization and bounded execution. Result delivery, Parent continuation, integration and new frontend controls remain outside this milestone.

## Schema and recovery

Transactional schema **v17 → v18** adds only `delegations`:

| Column | Meaning |
| --- | --- |
| `delegation_id` | Application-generated string UUID, primary key |
| `project_id` | Required Project foreign key |
| `parent_task_id` | Required requesting Task foreign key |
| `requested_by_agent_id` | Required originating Agent foreign key |
| `requested_by_assignment_id` | Required originating TaskAssignment foreign key |
| `request_key` | Required case-sensitive opaque key, 1–128 characters |
| `instruction` | Required original instruction, 1–32768 characters |
| `status` | `requested`, `materialized`, `result_ready`, `acknowledged`, or `canceled` |
| `child_task_id` | Nullable unique Task foreign key |
| `created_at`, `updated_at` | Required UTC timestamps |
| `materialized_at` | Nullable UTC timestamp, present exactly when a child is linked |
| `closed_at` | Nullable UTC timestamp, present only for canceled/acknowledged records |

`(parent_task_id, request_key)` is unique for all history, including canceled records. Child identity remains unique after cancellation. Checks enforce lifecycle/link/timestamp shape and reject a self-child. Unique indexes support parent/idempotency and child lookup; additional indexes cover Project/status, status and originating assignment. Cross-record provenance and hierarchy checks run in the transaction's domain layer.

The v18 migration creates no records for historical Tasks and does not modify existing tables. Migrations roll back on failure and reject unsupported future versions. Reopening or recovering does not advance Delegations, infer results, create missing children, or reinterpret old provenance as current ownership.

The **v18 → v19** transaction adds nullable `agents.execution_generation`, default-off `agents.delegation_protocol_enabled`, and nullable `tasks.orchestration_handoff`. Existing records retain their identities and behavior; no historical handoffs are fabricated. Execution generations distinguish resumed subprocesses within the same Agent/assignment. They augment the existing Agent row rather than introducing another ownership registry. Handoff JSON stores the reason, original Agent/assignment/generation, session, turn/message identity, validated requests and accepted Delegation IDs. Task reads expose this metadata as an object or null.

The current schema is **v20**. Its transactional **v19 → v20** migration adds default-off `agents.child_materialization_enabled` and the `delegation_materializations` progress journal. Each journal row belongs to one Delegation and records phase, preparation attempts, proposed launch Agent UUID, last error and update time. The launch UUID deliberately has no Agent foreign key because it is persisted before `Popen`; it is progress evidence, not a second assignment registry. Historical Agents remain disabled and no Children or workspaces are fabricated. Migration failure rolls back both schema and version.

## Internal service primitives

The dedicated `app/delegations.py` domain module is exposed through these `AgentStore` methods:

- `create_delegation(parent_task_id, *, project_id, requested_by_agent_id, requested_by_assignment_id, request_key, instruction)`
- `attach_delegation_child(delegation_id, child_task_id)`
- `transition_delegation(delegation_id, status)`
- `list_delegations(parent_task_id)`
- `get_delegation(delegation_id)`

These foundation mutations use `BEGIN IMMEDIATE` and return only after commit. They do not reconcile unrelated Task/resource state, publish UI events, invoke providers or touch the filesystem. They are internal metadata primitives, not execution authorization or a scheduler. The protocol acceptance service described below adds execution authorization and atomic handoff. An assignment identifies a Worker relationship, not an individual resumed subprocess.

Creation validates a resolved matching Project, an assignment belonging to the Parent Task, and the matching Agent. The assignment must be unended and its Agent running or waiting. New requests also pass the existing Task control/blocker gate. No second ownership registry is introduced.

Keys reject outer whitespace and control characters; they are not lowercased or otherwise normalized. Instructions reject blank text, NUL and excessive length, and are retained exactly. Retrying the same Parent/key/instruction returns the unchanged record. A different instruction under the same key conflicts. A current replacement assignment may retry, but original Agent/assignment provenance, UUID, timestamps, status and child link remain unchanged. An ended/stale assignment is rejected even for retries. A retry never revives a canceled record; different intended work needs a new key. The same key under another Parent is independent.

## Linking and lifecycle

Creation enters `requested`. Only `attach_delegation_child` advances it to `materialized`. Parent and Child must exist in the Delegation's Project, be distinct, and the Child's direct `parent_task_id` must equal the Delegation Parent. Each Delegation has at most one Child and each Child belongs to at most one Delegation. Repeating the same materialized link is an unchanged success; replacing it or attaching to a canceled record fails. Each child may independently originate further Delegations, preserving arbitrary-depth hierarchy.

`transition_delegation(..., 'canceled')` intentionally abandons a requested or materialized record and sets `closed_at`; repeating cancellation is idempotent. It does **not** cancel the Child Task or stop any Worker. `result_ready` and `acknowledged` are reserved schema states; no service transition into them exists in 2H.1. Other transitions, backward movement, and direct materialization without a validated Child are rejected. Task/Agent completion, Stop, Pause, Resume, Cancel and restart do not mirror their state into this separate protocol lifecycle.

Linking existing work is only metadata, so it remains possible after origin replacement; it does not authorize starting that Child. The link itself grants no permissions. Materialized Child execution uses the workspace, sandbox, resource, work-intent and Task lifecycle checks described below.

## Read-only inspection

- `GET /tasks/{task_id}/delegations` returns `{ "delegations": [...] }` in creation order, including closed history.
- `GET /delegations/{delegation_id}` returns one record.

Unknown Task/Delegation IDs return 404. Reads do not provision, recover, reconcile, publish, or change timestamps. There are no public mutation endpoints or normal-UI delegation controls in this milestone.

## Versioned request contract

The **entire final completed Agent message** must be one JSON object:

```json
{
  "protocol": "control-center.delegation.v1",
  "action": "request_delegations",
  "requests": [
    {"request_key": "api-tests", "instruction": "Implement authentication API tests"},
    {"request_key": "documentation", "instruction": "Document the authentication API"}
  ]
}
```

`app/delegation_protocol.py` validates this independently of any provider. Exact version/action and structural fields are required. Limits are 256 KiB UTF-8 per envelope, 1–16 requests, and the existing 128-character key / 32768-character instruction limits. Unknown fields, duplicate JSON fields, all duplicate batch keys (even identical ones), malformed JSON, invalid constants, blank instructions and mixed valid/invalid batches are rejected. Prose and Markdown examples are not requests. Object-shaped malformed requests fail closed rather than completing the Task.

Agents cannot supply identity, sandbox or permissions. The controller derives Agent, active TaskAssignment, Parent Task, Project and execution generation from its own state. Payload text is never executed as a command.

## Codex adapter and trust boundary

Opted-in Codex executions use the existing Windows npm launcher with `exec --sandbox <existing-mode> --color never --skip-git-repo-check --json`, optionally followed by `resume <session-id>`, then `-`. All prompt text remains on stdin; `shell=False`, Task workspace selection and process-tree termination are unchanged. Default Codex executions retain the existing human-output parser.

Compatibility was checked with npm Codex CLI **0.154.0** using `exec --help`, `exec resume --help`, and the combined JSONL/resume flags. The adapter follows the [documented exec JSONL contract](https://developers.openai.com/codex/noninteractive/). Verification uses synthetic events, not paid AI sessions; an authenticated live run remains a separate validation step.

`app/codex_events.py` consumes stdout only. Stderr is independently drained into output history and never parsed for control. It requires `thread.started` with a valid matching session UUID, `turn.started`, a completed `agent_message`, and successful `turn.completed`. Exec supplies no wire turn ID in this contract, so one invocation's execution UUID and local turn ordinal identify the turn. The final completed Agent message is designated; completed item IDs deduplicate replays. Conflicting duplicates, unexpected structures, failed turns, interrupted streams and session mismatches invalidate control acceptance. Limits of 1 MiB per event and 4096 completed items bound parser evidence. Unknown future event/item types fail closed and require adapter review.

As an additional conservative gate, the process must exit successfully and both output pipes must drain before acceptance. Tool, command, file, reasoning and stderr text cannot request delegation or spoof Waiting/Similar/Always markers. Successful final assistant messages still support those existing control markers. Session detection remains available during execution for Redirect. Structured events become readable assistant/tool output through the existing sequenced output cache, SQLite history, pagination and SSE; stderr text is retained. Cross-pipe ordering reflects arrival order rather than a provider-supplied global sequence.

## Authorization, atomicity and handoff

Readers collect evidence without taking the manager's lifecycle lock; Stop/Redirect may join them while holding that lock. The process watcher/finalizer performs acceptance under the existing lifecycle lock after draining. It verifies current subprocess identity and persisted generation/assignment authority. Stop/Redirect revoke the old execution. Resume creates a fresh generation before sending the prompt. Stale readers and finalizers cannot persist through newer ownership.

`AgentStore.accept_delegation_message` revalidates generation, active assignment, matching Project/Task/Agent/session and existing control/resource gates inside **one `BEGIN IMMEDIATE` transaction**. It validates the whole batch, creates or reuses Delegations, records the handoff receipt, marks the runtime Agent stopped, and reconciles the existing Task blocker machinery. Any conflict or failure rolls back the whole batch. Idempotent logical retries preserve original provenance, timestamps and canceled states. A repeated receipt is read-only; it never resumes work or revives a canceled record.

The Parent Task becomes **blocked**, with an orchestration blocker whose reason is `delegation_requested`. Its existing assignment stays unended and its Codex session remains available as metadata. The Agent is **stopped**, has no `waiting_question`, and generates no user-input or finished notification. Existing Reply/Decide controls therefore do not appear. This dormant orchestration continuation cannot be manually resumed through user-waiting controls. Pause/Resume preserves the blocker; Cancel may end the assignment without deleting Delegations or the receipt. Stop remains safe and does not launch or discard accepted requests.

Acceptance itself does not create a Child. A separately enabled materialization pass can now create and start eligible Children after the acceptance commit. The Parent remains incomplete, blocked and dormant; no result delivery or automatic Parent resume occurs.

## Failures and recovery

- Before commit: no partial batch or handoff survives. Rejected/failed structured executions stop without completing the Parent Task or synthesizing a user question.
- After commit: Delegations and handoff survive lost acknowledgement, output persistence failure or SSE failure. Publications occur only after persistence. Output remains eligible for the existing retry mechanism.
- Restart: the stored blocker/receipt and dormant assignment survive. Old uncommitted running executions recover through the existing stopped-worker path. No process is reattached and no worker starts automatically.
- Worker replacement: newer generation/assignment ownership wins. Failed Always prompt delivery restores the prior waiting generation and output history only while the failed replacement still owns the assignment.

## Controlled activation and scope

Backend code can explicitly call `agent_manager.start_agent(..., agent_type="codex", delegation_protocol_enabled=True)` for a persistent Task. This keyword is **not** exposed in an HTTP request model or dashboard control. The caller supplies the task/instruction explaining the desired envelope; default Agents are not prompted to delegate. Mock agents cannot opt in. The setting persists on the Agent and carries through existing Codex resume paths.

To enable Child materialization for that Parent, backend code must additionally pass `child_materialization_enabled=True`. Both flags default off; the HTTP start models and UI expose neither. The flag is committed before delivering the initial prompt. The accepted handoff schedules a bounded background pass after commit, outside the output reader and lifecycle lock. Ordinary Agents remain unchanged, and normal creation still defaults to Mock. The generic materialization service inherits either Mock or Codex, but the only real request adapter currently implemented is Codex; Mock does not emit delegation requests.

## Child creation and workspace preparation

`app/delegation_materialization.py` owns the provider-independent transaction checks and progression. The manager supplies the existing start machinery. One `BEGIN IMMEDIATE` transaction verifies the accepted receipt, current generation/assignment/session, activation, Project, control state and blockers, then inserts one UUID Child Task and attaches it through `delegations.attach_child`. The journal entry commits with the link. Retries reuse that Child; concurrent transactions cannot create two.

Only the matching orchestration blocker is exempted for the dormant Parent. Ordinary child creation still rejects blocked Parents. Unrelated hard blockers, cancellation, pause, stale ownership and invalid Project links prevent materialization. Claims suspended by orchestration alone are not execution permission; actual conflicts still block. Externally attached Children are not silently adopted by this service.

The entire validated Delegation instruction, including whitespace and up to 32768 characters, becomes the Child description. The existing internal Task storage supports this; the public Task API retains its 20000-character limit. Only the display title is shortened by the existing title helper.

The existing start path provisions a Task-owned isolated workspace from the Parent Task workspace, reuses durable valid workspaces, checks freshness and holds existing Task/upstream integration locks. Provisioning failure preserves the Child and Delegation and records an error. Existing workspace compensation applies only to a newly created, unrecorded provisioning attempt. Startup failure never deletes a durable workspace. A crash leaving an unrecorded filesystem path still requires inspection rather than silent adoption.

## Execution admission and coordination

The Child inherits the receipt's current Parent provider and exact Codex sandbox. No permissions come from the request envelope. Provider availability is checked by the existing command builder. Children do not inherit autonomous delegation activation. stdin, `shell=False`, process-tree Stop, session parsing, output, TaskAssignment persistence and SSE use the existing launch path.

After workspace preparation, admission rechecks Parent/Child control, pending/unassigned state, assignment history, resources, work intents and workspace provenance. It commits a `launching` journal with a new Agent UUID **before** starting a process. A final short SQLite transaction serializes the last control check with `Popen`; Task/assignment persistence then revalidates ownership before sending the Codex prompt. Generic starts cannot bypass a pending or uncertain materialization. TaskAssignments remain the source of Worker ownership.

`Limits` centralizes conservative defaults: 16 direct Children per Parent (including manually created Children), four concurrent delegated Children across Projects, one hierarchy level, three external preparation attempts and retry delays of 2 and 10 seconds. Backend callers can supply a validated `Limits` value for a controlled pass. Existing excess requests remain durable.

Absent declared coordination evidence, another running/waiting Worker or uncertain launch in the same Project defers the Child. Parallel admission requires active, authoritative `single_owner` work intents for both Tasks, with no overlapping declared scopes. This also considers unrelated root Tasks. No scopes are inferred from natural-language instructions, and declarations must accurately describe the work. Existing resource fairness, deadlocks, dependencies, external preflight and runtime ownership gates still apply; machine-global resource claims can conflict across Projects. Workspace isolation alone is not admission evidence.

Normal Child completion can wake staged siblings from the same live Parent run. Capacity/lifecycle deferral does not consume an external preparation attempt; each scheduled retry chain is bounded. No Child completion delivers results, integrates files or completes/resumes the Parent.

## Partial failure and controlled recovery

The journal progresses through `attached`, `workspace_ready`, `launching`, `started`, or `recovery_required`. Workspace metadata remains authoritative even when a crash leaves the journal at `attached`. `started` is recorded after the existing start function returns successfully. Read-only Delegation endpoints expose the journal as `materialization` (or null); existing Task/Agent reads and SSE expose the created work without a new dashboard screen.

- Before attachment commit: Child and link both roll back.
- After attachment or workspace commit: retry validates and reuses the same identities.
- Once launch admission commits: an error or interrupted acknowledgement is conservatively uncertain; no automatic second paid attempt is allowed, even if the failure may have occurred before `Popen`.
- Restart marks interrupted launches and previously running materialized Children `recovery_required`. It never reattaches PIDs or launches Workers. The existing Parent receipt and assignment remain durable.
- `agent_manager.materialize_delegations(parent_task_id)` is the backend-only controlled retry entry point for an activated Parent. `retry=True` resets only pre-launch preparation counters after authorization. It cannot reset `launching`, `started` or `recovery_required`, replace an existing Worker, or recreate a Child. Ambiguous process ownership requires operator inspection; no automated override is implemented.
- Successful Child Stop retains workspace/history and follows ordinary Task semantics. Explicit replacement after an established, safely stopped launch follows the existing Task API. Materialization retries never replace it. Pause/Cancel continue through existing lifecycle controls and are rechecked at launch boundaries. No new recursive cancellation policy is introduced.

All UI invalidations follow durable commits; failure to publish does not undo accepted work. There is no startup scheduler, automatic result delivery, or Parent handoff release workflow. Stage 2H.4 adds result delivery/acknowledgement, 2H.5 recursive safety/recovery, and Stage 2I decomposition/replanning. Synthetic subprocess tests and temporary Git repositories verify orchestration; authenticated paid Codex E2E remains separate verification.
