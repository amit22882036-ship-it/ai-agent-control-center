# Delegations, Agent requests and Child materialization (Stage 2H.1–2H.5)

Delegations record why a Parent Task requested a unit of work and which Worker assignment originated it. They are provider-independent durable metadata, separate from Task hierarchy, dependencies, work intents and resource claims. Stage 2H.2 adds an opt-in structured request protocol and durable Parent handoff. Stage 2H.3 adds separately activated Child Task/workspace materialization and bounded execution. Stage 2H.4 adds separately activated Hybrid Parent continuation, incremental result integration/delivery and explicit acknowledgement. No new frontend controls are introduced.

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

The transactional **v19 → v20** migration adds default-off `agents.child_materialization_enabled` and the `delegation_materializations` progress journal. Each journal row belongs to one Delegation and records phase, preparation attempts, proposed launch Agent UUID, last error and update time. The launch UUID deliberately has no Agent foreign key because it is persisted before `Popen`; it is progress evidence, not a second assignment registry. Historical Agents remain disabled and no Children or workspaces are fabricated. Migration failure rolls back both schema and version.

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

Acceptance itself does not create a Child. A separately enabled materialization pass can now create and start eligible Children after the acceptance commit. Without Hybrid activation the Parent remains incomplete, blocked and dormant; no result delivery or automatic Parent resume occurs.

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

`Limits` centralizes conservative defaults: 16 unfinished direct Children per Parent (including manually created Children), four concurrent delegated Children across Projects, configurable Task-ancestry depth (four edges by default), three external preparation attempts and retry delays of 2 and 10 seconds. Backend callers can supply a validated `Limits` value for a controlled pass. Existing excess requests remain durable.

Absent declared coordination evidence, another running/waiting Worker or uncertain launch in the same Project defers the Child. Parallel admission requires active, authoritative `single_owner` work intents for both Tasks, with no overlapping declared scopes. This also considers unrelated root Tasks. No scopes are inferred from natural-language instructions, and declarations must accurately describe the work. Existing resource fairness, deadlocks, dependencies, external preflight and runtime ownership gates still apply; machine-global resource claims can conflict across Projects. Workspace isolation alone is not admission evidence.

Normal Child completion can wake staged siblings from the same live Parent run. Capacity/lifecycle deferral does not consume an external preparation attempt; each scheduled retry chain is bounded. Without Hybrid activation, Child completion does not deliver results, integrate files or resume the Parent.

## Partial failure and controlled recovery

The journal progresses through `attached`, `workspace_ready`, `launching`, `started`, or `recovery_required`. Workspace metadata remains authoritative even when a crash leaves the journal at `attached`. `started` is recorded after the existing start function returns successfully. Read-only Delegation endpoints expose the journal as `materialization` (or null); existing Task/Agent reads and SSE expose the created work without a new dashboard screen.

- Before attachment commit: Child and link both roll back.
- After attachment or workspace commit: retry validates and reuses the same identities.
- Once launch admission commits: an error or interrupted acknowledgement is conservatively uncertain; no automatic second paid attempt is allowed, even if the failure may have occurred before `Popen`.
- Restart marks interrupted launches and previously running materialized Children `recovery_required`. It never reattaches PIDs or launches Workers. The existing Parent receipt and assignment remain durable.
- `agent_manager.materialize_delegations(parent_task_id)` is the backend-only controlled retry entry point for an activated Parent. `retry=True` resets only pre-launch preparation counters after authorization. It cannot reset `launching`, `started` or `recovery_required`, replace an existing Worker, or recreate a Child. Ambiguous process ownership requires operator inspection; no automated override is implemented.
- Successful Child Stop retains workspace/history and follows ordinary Task semantics. Explicit replacement after an established, safely stopped launch follows the existing Task API. Materialization retries never replace it. Pause/Cancel continue through existing lifecycle controls and are rechecked at launch boundaries. No new recursive cancellation policy is introduced.

All UI invalidations follow durable commits; failure to publish does not undo accepted work. There is no startup scheduler. The separately activated Hybrid extension below controls Parent continuation and result delivery. The Stage 2H.5 section below describes recursive safety/recovery; Stage 2I decomposition/replanning remains future work. Synthetic subprocess tests and temporary Git repositories verify orchestration; authenticated paid Codex E2E remains separate verification.

## Hybrid Parent execution (Stage 2H.4)

Backend callers additionally opt in with `hybrid_delegation_enabled=True`, requiring both existing activation flags and a persistent Codex Parent. Flags remain off by default; Mock remains the ordinary start default. HTTP start models and dashboard controls do not enable Hybrid execution. `agent_manager.advance_delegations(parent_task_id)` runs one bounded backend pass. Live handoff/completion events schedule passes; polling and backend startup do not. Explicit Stop revokes continuation. Task Pause/Resume may release a safely completed pause, never an uncertain launch; cancellation remains authoritative.

`delegation_continuation.py` validates the provider-independent v2 contract. `delegation_orchestration.py` owns transactional continuation, result and acknowledgement state. `delegation_rounds.py` retains acceptance history and admission slots. `delegation_delivery.py` coordinates existing workspace/integration services through manager callbacks. TaskAssignments and execution generations still own Workers; these journals do not introduce another ownership registry.

### Protocol and responsibility evidence

The strict `control-center.delegation.v1` schema is unchanged. A Hybrid-enabled Parent can use v1 to delegate and wait, or v2 to declare optional independent work:

```json
{
  "protocol": "control-center.delegation.v2",
  "action": "request_delegations",
  "requests": [{"request_key": "tests", "instruction": "Review the assigned tests"}],
  "continuation": {
    "instruction": "Review independent documentation",
    "intent_ids": ["00000000-0000-4000-8000-000000000001"]
  }
}
```

`continuation` may be null. Otherwise it requires a non-blank instruction of at most 4096 characters and 1–16 distinct canonical UUIDs naming existing Work Intents. Existing envelope/request bounds still apply. Unknown fields, duplicate keys, malformed JSON and unrecognized actions are rejected. Only the successful final Codex JSONL assistant message may carry the contract; tools, stderr and embedded examples do not authorize control actions. Prompts stay on stdin and sandbox permissions remain unchanged.

No responsibility scopes are inferred from instructions. Independent execution requires accepted Children to be attached and their scopes verifiable. Parent and Child scopes must be active authoritative `single_owner` Work Intents with no overlap; unrelated same-Project Workers and uncertain launches are also checked. Existing dependency, resource fairness/deadlock, machine-global resource, runtime ownership and workspace freshness gates apply. Missing or conflicting evidence leaves the Parent waiting with an inspectable reason.

### Multiple rounds, provenance and retries

A Parent Task supports multiple explicit Delegation rounds with no lifetime round or historical-child cutoff. Every accepted round has a stable UUID and immutable original Agent/assignment/generation/message provenance, exact receipt and admission time. Existing request keys may appear in a fresh batch without creating duplicate Delegations or Children. Reusing a key with different instructions remains a conflict. Whole batches commit atomically under immediate transactions and uniqueness constraints.

The single `delegation_continuations` row per Task is the current orchestration cursor, not historical batch storage. Its batch pointer advances only after acceptance; previous round receipts and result/delivery evidence remain intact. `orchestration_handoff` remains the compatible latest-receipt view. Task reads expose ordered `delegation_rounds` and the current `delegation_continuation`.

An authorized independent or finalizing Parent may request another round while older Children are active. A prepared, delivered or uncertain result delivery must be resolved first; new work cannot bypass acknowledgement. Pending Children use their historical receipt and the current authorized Parent assignment/control state. Newer rounds do not replace existing Child Workers or workspaces. Stop, Pause, Cancel, stale generations and unrelated blockers still prevent admission.

Exact generation/message replay returns the original receipt without writes. A current replacement execution repeating the same logical batch reuses its receipt and yields for inspection, without resetting the consumed plan or creating a paid continuation loop. A fresh round must contain at least one fresh key; changing only the plan or regrouping old keys cannot turn old work into new work. No v1/v2 protocol fields were silently added.

### Child results and safe integration

Successful results require successful execution evidence plus authoritative completed Task/assignment state. Codex Children, including explicit replacements, use JSONL completion validation. That validation alone does not grant recursive Delegation permission; the separate Stage 2H.5 capability is required. Agent status, assignment end, Task status, output references and result proof commit together. Failed, stopped, canceled or unverified results cannot be accepted as success. Worker replacements append result records, preserving prior attempts.

Each result records Child Task, assignment, Agent/generation, outcome, output sequence bounds, a bounded untrusted excerpt, source snapshot, integration reference and review outcome. Complete output stays in its existing history. Parent context contains at most eight results and 32 KiB of reference data per delivery, with excerpts capped at 2000 characters. Unicode size may reduce a delivery further. Child output is data, never permission or a privileged control instruction.

Delivery waits for a quiet Parent synchronization point; it does not interrupt active execution or alter files underneath it. Changed work uses existing direct-Parent integration locks, three-way comparison, snapshots, pre-image journal, rollback and recovery. A read-only Parent cannot authorize automatic file-changing integration. No-change results use a validated `noop` journal without filesystem changes. Parent-to-canonical integration remains explicit and outside this workflow.

Conflicts, stale snapshots and ambiguous operations preserve their evidence. Failed integration can be reported when review admission is safe, but cannot be accepted. An already-applied source snapshot is reused after lost result persistence instead of integrated again. Results from different rounds are delivered incrementally when safe; unrelated siblings need not finish first. A prepared delivery/launch journal commits before resume. Delivery is marked successful only after stdin delivery succeeds; uncertain external outcomes require inspection, not another automatic model call.

### Explicit acknowledgement and completion

The Parent reviews its integrated workspace and result references, then emits:

```json
{
  "protocol": "control-center.delegation.v2",
  "action": "acknowledge_results",
  "delivery_id": "00000000-0000-4000-8000-000000000002",
  "results": [{
    "result_id": "00000000-0000-4000-8000-000000000003",
    "decision": "accept",
    "reason": "Reviewed the contribution and its checks"
  }]
}
```

Use delivered IDs and account for exactly that delivery's results. Decisions are `accept`, `reject` or `failure`, each with a non-blank reason of at most 2000 characters. Assignment, generation, session/control state and delivery ownership are checked transactionally. Exact retries are idempotent; contradictory acknowledgements are rejected. Acceptance requires the latest successful Child assignment and an applied/noop integration. Rejection/failure preserves files and history and leaves required work unresolved; there is no automatic undo or corrective replanning. Direct status mutation cannot bypass these services.

Acknowledgement does not itself complete the Parent. Once required Delegations from **all rounds** are accepted, a final continuation asks the Parent to verify and finish its overall work. Durable Task synchronization and explicit assignment completion reject premature completion. Canceled/rejected required work is not silently counted as success. Genuine user questions retain Waiting/Reply/Decide; orchestration waiting is a stopped Agent plus blocked Task/reason and does not trigger a user-input notification.

### Schema v22 and recovery

The transactional **v20 → v21** migration adds default-off `agents.hybrid_delegation_enabled` and three tables:

- `delegation_continuations`: current batch, assignment, original/current generation, bounded plan, consumption flag, phase and launch/delivery progress.
- `delegation_results`: per-assignment result identity/provenance, outcome, output bounds, validation/integration progress and explicit review outcome.
- `delegation_deliveries`: bounded context/result IDs, assignment/generation, delivery progress and acknowledgement receipt.

The **v21 → v22** migration adds `delegation_rounds` and `delegation_round_requests`. It archives existing v19/v20/v21 handoffs without changing their exact receipts, Child relationships, results or acknowledgements. Existing v21 continuation batch UUIDs are retained; unknown historical acceptance times remain null and migrated rounds are already admitted. Schema, archive data and version rollback together on failure. Historical Agents are not enabled; migration launches no Workers, fabricates no results and provisions no workspaces.

Startup preserves round history, results and acknowledgements, collects ended-Child references without inventing successful proof, and marks interrupted Parent launches/executions as requiring recovery. Existing integration recovery compares its journal without writing files. Startup never launches a Parent or Child. Unknown launch state cannot be cleared by ordinary retries or Pause/Resume. A lost delivery acknowledgement is not assumed safe to resend. SSE invalidations follow durable commits; there are no new public mutation endpoints.

### Admission bounds and remaining scope

`delegation_rounds.Policy` defaults to four fresh rounds per rolling 60 seconds per Parent. Acceptance reserves durable slots; excess valid work remains pending until `not_before`, exposed in history. No timer resumes a Parent or starts paid work at that deadline: a bounded live-event pass or explicit backend pass must reconsider all gates. Older admitted results can still be reviewed while a newer round is throttled.

`delegation_materialization.Limits` retains 16 unfinished direct Children per Parent, four concurrent delegated Workers globally (including uncertain ownership), bounded preparation retries and, as of Stage 2H.5, configurable recursive depth. Protocol batches retain the 16-request limit. Completed/canceled historical Child Tasks no longer consume unfinished-child capacity. Capacity exhaustion retains pending work. Resource claims, freshness and authoritative non-overlapping scopes still apply.

These structural/rate safeguards do not detect semantic infinite loops, prove that another Delegation is useful, or provide unlimited resources. Work Intent declarations must accurately describe actual work. Automatic decomposition, corrective replanning and tooling to resolve unknown external ownership remain outside this milestone. The recursive extension below adds ancestor admission gates and inherited capability; intelligent planning remains outside this milestone. Authenticated paid Codex E2E remains a separate explicitly authorized check.

## Recursive delegation (Stage 2H.5)

Recursive execution is a separate backend opt-in: pass `recursive_delegation_enabled=True` to `agent_manager.start_agent`, together with the existing protocol/materialization/Hybrid flags. The originating Codex Agent grants the same capability to automatically materialized Children. Ordinary Agents, HTTP start models and Mock remain disabled by default. A nested Agent cannot acquire recursive capability without an authorized direct Parent assignment. Replacement does not silently acquire new permissions.

Capability is saved with the Agent and assignment in one transaction before prompt delivery. Children inherit the exact provider and sandbox, reusing JSONL, sessions, generations and Hybrid services. Capability permits explicit requests; it does not perform automatic decomposition. Task ancestry, Delegations and TaskAssignments remain authoritative, without a second ownership tree.

### Depth and operational bounds

`CONTROL_CENTER_DELEGATION_DEPTH` is a positive integer, default **4**. It counts Task-parent edges from root depth zero, allowing five Task levels. Set it before launching the backend; normal Tasks need no configuration. Internal materialization `Limits.depth` may impose a stricter bound. Requests, attachment, admission and launch recheck ancestry; cycles and cross-Project paths fail closed.

Lowering depth retains historical Tasks, requests, workspaces and results. New prohibited requests are rejected. Previously accepted requests retain a durable `admission_error` in Delegation reads when preparation is prevented. An explicit later pass can clear that reason when policy permits. Uncertain launch admission remains inspectable; lowering policy neither deletes work nor starts a model retry loop.

Defaults retain 16 unfinished direct Children, 16 requests per batch and four concurrent delegated Workers globally. Dormant orchestration Parents do not consume running capacity; running/waiting Workers and uncertain launches do. Nested Parent resume checks capacity again. Unknown launch ownership also reserves scope and prevents filesystem integration.

The durable round journal reserves per-Parent slots (four fresh rounds per 60 seconds) and aggregate root-tree slots (eight per 60 seconds). Internal `delegation_rounds.Policy.tree_burst` controls the aggregate burst. Slots commit atomically with each accepted batch, including concurrent descendant requests. Excess work retains `not_before`; there is no timer or model retry at the deadline. There is no lifetime round limit, lifetime tree-size cap or mandatory financial/token budget. Completed historical Children do not consume unfinished capacity. These bounds do not detect semantic infinite loops.

### Direct-Parent results, control and failures

Nested workspaces derive from their immediate Parent. Grandchild results integrate into that Parent only, using quiet-workspace checks, snapshots, the existing integration journal and explicit acknowledgement. The intermediate Parent cannot finish its required contribution before its Child results are handled. Only its subsequently completed assignment contributes a result to the next Parent. Applied integration receipts, assignment identity and delivery acknowledgements prevent duplicate effects. No-change results retain `noop`; rejection/failure remains unresolved work.

Stop Agent revokes that execution's continuation, not descendant Tasks or independent Workers. Cancel Task retains the existing subtree operation. Acceptance, attachment, assignment persistence and launch check ancestor control intent inside SQLite transactions. Cancellation recomputes membership under the same write serialization: attachment winning first includes the Child; cancellation winning first rejects attachment/launch. Popen remains behind the short final transaction; a canceled assignment commit triggers compensation before stdin delivery. Uncertain termination retains gates and recovery evidence. Pause ownership and Resume semantics are unchanged.

A failed branch does not cancel siblings. Output, workspaces and result evidence remain available while the direct Parent is inactive. Structured Codex errors with explicit `rate_limit_exceeded`, `usage_limit_reached` or `insufficient_quota` codes add a durable provider-limit explanation. Free-form text does not establish a quota or reset time. No automatic provider switch, permission elevation or repeated launch occurs; explicit continuation still checks ownership and control.

### Schema, recovery and limits

Current schema **v23** transactionally adds default-off `agents.recursive_delegation_enabled` and nullable `delegations.admission_error` to v22. There are no new tables, historical capability grants, process launches or workspace operations. Failure rolls back both schema and version; newer schemas are rejected.

Existing journals apply at every level. Restart preserves requests, attached Children, rounds, acknowledgements and output without reattaching PIDs or starting Workers. Deterministic journal inspection retains/reconciles evidence; ambiguous launch/integration outcomes require inspection. A stopped record does not prove an unknown external process tree terminated, nor authorize conflicting workspace changes.

Deterministic regressions use synthetic streams/processes, temporary SQLite databases and temporary Git worktrees. They cover multi-generation execution, depth changes, inherited permissions, cancellation races, aggregate admission, direct-Parent integration/acknowledgement and recovery. This is not paid Codex E2E verification. Automatic decomposition, corrective replanning, semantic loop detection, provider-account onboarding and unknown-ownership recovery tooling remain future work. Stage 2I planning must preserve these safety gates.
