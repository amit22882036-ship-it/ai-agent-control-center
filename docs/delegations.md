# Delegations and Agent request protocol (Stage 2H.1–2H.2)

Delegations record why a Parent Task requested a unit of work and which Worker assignment originated it. They are provider-independent durable metadata, separate from Task hierarchy, dependencies, work intents and resource claims. Stage 2H.2 adds an opt-in structured request protocol and durable Parent handoff. It adds no automatic child creation, execution, workspace provisioning, result delivery, integration or frontend controls.

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

The current schema is **v19**. The transactional **v18 → v19** migration adds nullable `agents.execution_generation`, default-off `agents.delegation_protocol_enabled`, and nullable `tasks.orchestration_handoff`. Existing records retain their identities and behavior; no historical handoffs are fabricated. Execution generations distinguish resumed subprocesses within the same Agent/assignment. They augment the existing Agent row rather than introducing another ownership registry. Handoff JSON stores the reason, original Agent/assignment/generation, session, turn/message identity, validated requests and accepted Delegation IDs. Task reads expose this metadata as an object or null.

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

Linking existing work is only metadata, so it remains possible after origin replacement; it does not bypass gates to start that Child. No permissions are stored or expanded. Actual future Child execution must continue using normal workspace, sandbox, resource, work-intent and Task lifecycle checks.

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

No child is created and no Parent is automatically resumed. This is a temporary handoff policy for 2H.2, not a permanent requirement that a Parent wait for all children. Future parallel execution must retain existing dependency, workspace, resource and work-intent gates.

## Failures and recovery

- Before commit: no partial batch or handoff survives. Rejected/failed structured executions stop without completing the Parent Task or synthesizing a user question.
- After commit: Delegations and handoff survive lost acknowledgement, output persistence failure or SSE failure. Publications occur only after persistence. Output remains eligible for the existing retry mechanism.
- Restart: the stored blocker/receipt and dormant assignment survive. Old uncommitted running executions recover through the existing stopped-worker path. No process is reattached and no worker starts automatically.
- Worker replacement: newer generation/assignment ownership wins. Failed Always prompt delivery restores the prior waiting generation and output history only while the failed replacement still owns the assignment.

## Controlled activation and scope

Backend code can explicitly call `agent_manager.start_agent(..., agent_type="codex", delegation_protocol_enabled=True)` for a persistent Task. This keyword is **not** exposed in an HTTP request model or dashboard control. The caller supplies the task/instruction explaining the desired envelope; default Agents are not prompted to delegate. Mock agents cannot opt in. The setting persists on the Agent and carries through existing Codex resume paths.

Stage 2H.3 can enable this internal capability when it implements safe Child materialization. Until then, accepted requests remain inspection-only and orchestration handoffs have no release/resume workflow. Stage 2H.4 adds result delivery/Parent acknowledgement; 2H.5 adds recursive safety/recovery; Stage 2I adds autonomous decomposition/replanning. There is no scheduler, permission expansion, implicit dependency creation, delegation cancellation cascade or automatic integration here.
