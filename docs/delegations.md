# Delegation foundation (Stage 2H.1)

Delegations record why a Parent Task requested a unit of work and which Worker assignment originated it. They are provider-independent durable metadata, separate from Task hierarchy, dependencies, work intents and resource claims. This milestone adds no output protocol, automatic child creation, execution, workspace provisioning, result delivery, integration or frontend controls.

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

Migration creates no records for historical Tasks and does not modify existing tables. It supports the repository's idempotent migration conventions, rolls back on failure, and rejects unsupported future versions. Reopening or recovering does not advance Delegations, infer results, create missing children, or reinterpret old provenance as current ownership. No delegation-specific recovery actions are needed.

## Internal service primitives

The dedicated `app/delegations.py` domain module is exposed through these `AgentStore` methods:

- `create_delegation(parent_task_id, *, project_id, requested_by_agent_id, requested_by_assignment_id, request_key, instruction)`
- `attach_delegation_child(delegation_id, child_task_id)`
- `transition_delegation(delegation_id, status)`
- `list_delegations(parent_task_id)`
- `get_delegation(delegation_id)`

Mutations use `BEGIN IMMEDIATE` and return only after commit. They do not reconcile unrelated Task/resource state, publish UI events, invoke providers or touch the filesystem. These are internal metadata primitives, not execution authorization or a scheduler. Future protocol callers must also enforce current subprocess identity before invoking them, as they do for other output-driven actions. An assignment identifies a Worker relationship, not an individual resumed subprocess.

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

Later milestones must add protocol parsing with stale-process checks, controlled child materialization, same-or-less permission inheritance, and durable result delivery. No automatic decomposition, scheduling, dependencies, delegation cancellation cascades or integration is inferred here.
