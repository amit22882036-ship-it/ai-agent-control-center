# Planning decisions and coordinated promotion

Planning records what an executing Agent proposes and why. It is descriptive metadata tied to a durable Task, separate from executable [Delegation requests](delegations.md). An accepted plan creates no Tasks, Agents, dependency edges, work intents, resource claims, integrations or paid continuations. Existing Child work is unaffected by revisions.

## Activation and runtime boundary

Backend callers may explicitly pass `planning_enabled=True` to `agent_manager.start_agent` for a persistent Codex Agent. It defaults to false, is not exposed by HTTP start models or the dashboard, and ordinary Root defaults are unchanged. Newly materialized Codex Children inherit planning only when the Parent explicitly has planning, delegation protocol, Child materialization, Hybrid and recursive delegation enabled together. The same bounded inheritance applies to deeper authorized descendants. Historical Agents and existing Child Workers are not retroactively upgraded. Replacement Agents must explicitly opt in on the same Task with their actual permissions validated. Planning alone does not enable any Stage 2H delegation permission or change the sandbox. Mock and older executions keep their existing behavior.

The executing Agent receives bounded optional planning instructions through the existing stdin prompt, together with its current accepted revision and content when present. It may choose independent work, propose independent deliverables, or emit no plan. Planning does not require another Agent or model invocation. The prompt asks it to consider dependencies, parallel benefit, responsibility boundaries and coordination overhead rather than arbitrary task-size heuristics.

The Codex adapter accepts a planning envelope only as a complete `item.completed` / `agent_message` text. It buffers up to 16 decisions and commits them together only after both streams close, a successful process exit and validated `turn.completed`. A later final assistant response must independently indicate ordinary work or an already-authorized Stage 2H control action. Planning messages never enter the Delegation handoff path. Stderr, tool/reasoning output, code fences and incomplete streams cannot author plans. Existing raw/readable output retention is unchanged.

**Provider limitation:** the [official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) documents JSONL events and completed assistant messages, but does not promise multiple assistant messages per invocation. This integration records a plan only when the provider actually emits a separate intermediate assistant message followed by a valid final response. It does not assume such messages always exist or force a planning-only turn. An ordinary single-message completion works without a plan. A plan-only final response is rejected as unverified execution, using the existing stopped/pending failure path; it does not complete the Task, hand off to orchestration, or automatically resume. No plan from that invocation is accepted. The recorded Stage 2I.1 real Codex E2E captured an independent decision followed by ordinary completion. It did not verify propose-to-execution promotion or guarantee an intermediate event on every run.

## Contract

The strict provider-neutral protocol is `control-center.planning.v1`:

```json
{
  "protocol": "control-center.planning.v1",
  "decision_key": "initial-review",
  "base_revision": 0,
  "decision": "propose",
  "rationale": "Independent parser analysis can precede a focused validation review.",
  "items": [
    {
      "proposal_id": "parser-review",
      "deliverable": "Document parser edge cases and evidence",
      "responsibility": "Read parser code only; do not modify runtime behavior",
      "depends_on": [],
      "parallel": true
    }
  ],
  "references": []
}
```

`decision` is `independent` (empty items) or `propose` (1–16 items). Each envelope has exactly the fields shown. Each item has exactly the five fields shown. `parallel` is a suggestion, not admission. Responsibility text is not an authoritative scope reservation. Proposal IDs are model-authored stable local keys, not Task/Agent UUIDs.

Limits: 64 KiB UTF-8 per envelope; 16 decisions per invocation; 2000 characters each for rationale, deliverable and responsibility; 64 ASCII characters per key, beginning with a letter/digit and using letters, digits, dot, underscore or hyphen. Text rejects surrogate code points, format controls and control characters except newline/tab. Fields and types are strict, including booleans versus integers. Duplicate JSON fields, duplicate proposal IDs, repeated dependencies, unknown dependency references, cycles, unsupported versions and malformed/oversized payloads are rejected.

`references` permits up to 16 distinct `{ "task_id": "<existing UUID>" }` objects. Referenced Tasks must exist in the same Project and Task tree. This intentionally narrow reference mechanism does not grant access across unrelated trees or reserve work. Delegations, claims and results remain available through their existing read models; this protocol does not add polymorphic references or a semantic similarity service.

## Revisions, authority and replay

`base_revision` is zero before the first decision and must equal the current accepted revision for a fresh decision. Every accepted decision creates an immutable next revision. The latest revision is derived transactionally from SQLite. Each revision replaces the proposed item set: retained items keep their local IDs, new IDs add proposals, omitted IDs withdraw proposals. No real Child lifecycle changes follow.

Acceptance checks Project, Task, Agent, active TaskAssignment, execution generation, session and explicit planning permission inside `BEGIN IMMEDIATE`, including existing Task/ancestor control gates. Stale generations cannot write or replay over a newer generation. Competing submissions based on the same revision cannot silently overwrite each other: one advances it and the other is rejected.

`decision_key` is unique per Task across executions. Repeating an accepted key with identical normalized JSON returns its original revision/provenance. Changed content conflicts. Provider message identity is additionally unique per Task and execution generation. Exact receipt reads can recover a lost acknowledgement after assignment completion; they do not restore ownership or reopen work. A newly authorized replacement may revise the current plan using a new decision key and current base revision. All decisions in one execution are persisted atomically; publication follows commit. A rejected submission triggers no model retry.

Backend read methods are `AgentStore.current_plan(task_id)` and `planning_history(task_id)`. The bounded backend ingress is `accept_planning_messages(origin, messages, session_id=...)`; it is an internal trusted-adapter boundary, not a public endpoint accepting arbitrary user-supplied execution identity. The provider adapter is responsible for successful execution evidence. Existing SSE invalidation is reused after acceptance; no new stream, scheduler or frontend behavior is introduced.

## Schema v24 and recovery

The transactional v23 → v24 migration adds default-off `agents.planning_enabled` and one table, `planning_revisions`. Rows retain Task/Project, originating Agent/TaskAssignment, execution generation, session/message identity, decision key, canonical structured content and an `accepted_at` timestamp (also the row's creation time). `(task_id, revision)` is the durable revision identity; no separate Worker ownership system or duplicated Delegation journal is created.

Migration preserves existing ownership and delegation history, grants no historical permissions, and launches no processes. Failure rolls back schema and version together; future schemas are rejected. Restart retains accepted revisions; uncommitted/buffered decisions are not reconstructed from log text. A crash after plan commit but before normal execution finalization may leave an accepted plan on an interrupted Task: a plan is never completion evidence. Existing recovery decides execution state without launching a Worker.

Stage 2I.1 supplies optional planning records only. The Stage 2I.2 extension below adds explicit execution admission. Stage 2I.3 will handle changes to active work and safe handoffs. Stage 2I.4 will add UI integration and future creation defaults. Neither milestone guarantees semantic duplicate detection.

## Verification limitation

The recorded Stage 2I.1 Windows full backend run executed 787 tests and ended with two errors: `test_agent_manager.AgentTests.test_tree_stop` and `test_always_decisions.AlwaysDecisionTests.test_windows_resume_stdin_cwd_session_sandbox_and_watcher`. Both passed when rerun together in isolation (2 tests, 1.094 seconds). The failures may be timing/environment-sensitive, but their cause has not been conclusively identified or fixed; that full suite cannot be described as passing. Stage 2I.1 publication was approved with this known limitation. Real paid propose-to-promotion behavior remains unverified.

## Coordinated promotion (Stage 2I.2)

Promotion is a separate explicit decision, not an automatic consequence of storing a plan. It requires a current persistent Codex execution with **all four** existing flags: `planning_enabled`, `delegation_protocol_enabled`, `child_materialization_enabled`, and `hybrid_delegation_enabled`. Recursive Children also require the existing independently granted recursive capability. Root permission defaults remain unchanged. New automatically materialized Children inherit planning only when all five capabilities, including recursive delegation, are authorized. At the final Worker activation boundary the launch capabilities are checked against fresh durable Parent authority; revocation or changed ownership prevents launch. Provider and sandbox remain exactly those of the Parent, existing depth/ancestor/assignment/generation gates remain enforced, and no workspace or resource authority is added by capability inheritance. Mock cannot inherit planning. Replacements are not automatically opted in. No additional model, provider, dashboard control, or HTTP mutation endpoint is introduced.

An authorized Agent may emit this complete **final** assistant JSON message:

```json
{
  "protocol": "control-center.promotion.v1",
  "task_id": "00000000-0000-4000-8000-000000000001",
  "revision": 1,
  "decision_key": "promote-parser",
  "items": [{
    "proposal_id": "parser-review",
    "scope": {"namespace": "component", "key": "parser/review", "from_intent_id": null},
    "resources": [],
    "depends_on_task_ids": [],
    "reuse_task_id": null
  }],
  "continuation": null
}
```

Use the supplied real Parent Task UUID; it is matched against trusted runtime origin, never taken as execution authority. Fresh decisions must reference the current accepted revision. The exact fields above are required; duplicate fields, unknown versions/IDs, invalid types, and malformed declarations reject the entire request. The contract permits 1–16 distinct selected items, 64 KiB per envelope, and the same 1–64-character decision/proposal keys as planning. Each item permits at most eight resource declarations and sixteen distinct same-Project Task prerequisites. Resource objects require exactly `resource_type`, `resource_key`, `scope`, `mode`, and `recursive`, using existing resource normalization and task-scoped lifetime. Credentials are not resource identities.

`scope` is a Project-local structured single-owner Work Intent. Its namespace/key use existing normalization. An optional `from_intent_id` must name an active authoritative containing intent on the Parent or its ancestors. Null scope is retained as ambiguous and cannot launch. Scope text and proposal prose do not prove semantic identity. `reuse_task_id` designates known same-Project work for inspection, not automatic result acceptance or workspace adoption. Parent/ancestor completion cannot be a Child prerequisite.

`continuation` is the unchanged Hybrid v2 nullable `{instruction, intent_ids}` contract. The model chooses independent work; the backend requires authoritative non-overlapping scopes and all existing execution gates before any Parent resume. Promotion consumes the successful final message through the existing durable Hybrid handoff. It retains the Parent Agent, assignment and session and never completes the Parent as a side effect. Waiting, explicit result acknowledgement, and ordinary completion remain separate existing paths.

Promotion can refer to an already-persisted plan or to planning messages earlier in the same successful invocation. In the latter case, plans, promotion provenance, Delegations, handoff and initial admission commit together. A malformed promotion rolls the batch back. A promotion followed by another assistant message is rejected. Tools, stderr, fenced JSON, failed turns and incomplete streams cannot promote. This does not depend on a guarantee of intermediate events: a previously persisted plan can be promoted by a single final assistant message. No paid invocation is added just to plan or recover a lost receipt.

### Partial execution and authority

Every selected proposal is required work. Each has one durable Delegation, including blocked, invalid, ambiguous and reuse-candidate items. Such a record is a request, not permission to run. Safe independent items may receive real Child Tasks and authoritative Work Intents in the acceptance transaction. Task-scoped resources reuse the existing claim bundles, fairness, global scope, runtime ownership and preflight mechanisms. A Child Task can exist while blocked; no Worker launches until materialization, dependency, scope, resource, capacity, sandbox and workspace checks pass.

Admission dispositions are `pending`, `ready`, `blocked`, `conflict`, `unauthorized`, `reuse_available`, or `invalid`, with reasons. Inspection also derives current Child Task state, `launching`, `recovery_required`, `completed_pending_review`, `result_ready`, `acknowledged`, and `canceled` from existing journals; `admission_disposition` retains the last admission decision. `ready` is not a permanent lease or proof that a Worker started. Workspace/launch failures remain in the existing materialization journal. Definitively unauthorized/invalid pre-launch work is not retried indefinitely; its pre-launch claims are retired when safe. Required rejected/canceled work never becomes successful completion.

Known equal or containing active scopes conflict across siblings, deep descendants, distant branches and separate roots in the same Project. Equal completed scopes surface a reuse candidate, not permission to execute duplicate work. Missing/ambiguous scope fails closed. Different scopes are not made exclusive merely because their prose sounds similar. Same scopes in different Projects are independent; genuinely shared resource claims remain machine-global. Context never includes other Projects' Task descriptions or output because a global resource is occupied. Deterministic declaration checks cannot prove that model-provided scopes accurately describe all actual work.

SQLite immediate transactions serialize admission, scope ownership and immutable provenance, preventing concurrent check/create races. Real Work Intents and Resource Claims own reservations; no parallel Worker registry or general-purpose scheduler exists. Fresh checks also run at materialization and immediately before launch. Explicitly releasing a required scope/resource cannot bypass promotion's launch gate.

### Dependencies and later rounds

Frozen proposal prerequisites are resolved to real `task_dependencies` edges as their Children become available. An unpromoted prerequisite creates a durable `promotion` blocker on the dependent Child, without creating a fake completed Task. Proposal prerequisites additionally wait for successful integration and explicit Parent acknowledgement, so later Children snapshot the reviewed Parent workspace. Explicit existing-Task prerequisites retain existing completion semantics. Cycles across frozen proposals and later revisions are rejected; real Task edges use the existing cycle validator. Canceled/failed prerequisites cannot release required work as success.

Later rounds may select previously unselected proposals. A retained proposal ID cannot create another Delegation or Child in a later revision. Its original accepted revision, responsibility, coordination declaration and provenance remain immutable. Attempting to change already promoted responsibility conflicts; this is reserved for Stage 2I.3. Updating or withdrawing a descriptive plan does not cancel real work. Resource/dependency resolution requires a fresh bounded orchestration pass, not an old snapshot. Result acknowledgement schedules the existing materialization pass; resource changes alone do not start a paid turn. Operators may invoke the existing backend pass explicitly. A waiting Parent needs authorized independent continuation to choose another round; no model turn is invented solely to select an absent prerequisite.

### Identity, inspection, migration and recovery

Schema **v24 → v25** adds `promotion_decisions` (immutable payload/receipt, accepted plan revision, Agent/assignment/generation/session/message provenance) and `promotion_items` (first proposal snapshot, coordination declaration, Delegation reference and admission disposition). Uniqueness covers `(task_id, decision_key)`, provider message identity and `(task_id, proposal_id)`. Delegation request keys are backend-derived stable UUID-based keys; model strings never become Task/Agent/assignment identities. Schema/version changes roll back together and preserve all previous records and permissions.

Exact decision replay returns the original receipt without creating work or resetting continuation. Changed payload with the same key conflicts. Current-generation authorization is checked even for replay. An authorized replacement may recover a receipt, but that does not transfer the old continuation's ownership or authorize relaunch. New responsibility on the same Task uses existing fresh-round gates.

Existing `GET /tasks` and `GET /tasks/{task_id}` include `promotions` and `promotion_decisions`; `AgentStore.list_promotions(task_id)` is available internally. Delegation reads retain their normal Child, admission-error and materialization evidence. Safe prompt preparation/resume includes at most sixteen own promotion dispositions and eight related same-Project scope declarations, with a total 16 KiB serialized context limit. Related work includes bounded Task identity/title/status and declared scope, including completed-work candidates; it never includes entire output histories or foreign-Project owners. Context is informative, never a lease; live processes receive no injected messages.

There is no committed gap between admission and Delegation provenance: they share one transaction. Interrupted workspace preparation reuses the existing Child/journal. Unknown launch outcomes remain `recovery_required` and cannot trigger another automatic launch. Restart preserves mixed dispositions and never reattaches PIDs or starts a Parent/Child. Stop, subtree cancellation, permission gates, direct-Parent result delivery, explicit acknowledgement and integration idempotency continue through Stage 2H. Required Delegations from all rounds block premature Parent completion. Reuse/ambiguous/conflicting work needs explicit inspection or later replanning; no semantic-resolution loop is added.
