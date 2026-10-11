# Planning decisions (Stage 2I.1)

Planning records what an executing Agent proposes and why. It is descriptive metadata tied to a durable Task, separate from executable [Delegation requests](delegations.md). An accepted plan creates no Tasks, Agents, dependency edges, work intents, resource claims, integrations or paid continuations. Existing Child work is unaffected by revisions.

## Activation and runtime boundary

Backend callers may explicitly pass `planning_enabled=True` to `agent_manager.start_agent` for a persistent Codex Agent. It defaults to false, is not exposed by HTTP start models or the dashboard, and is not automatically inherited by Children or replacement Agents. A replacement can explicitly opt in on the same Task. Planning alone does not enable any Stage 2H delegation permission or change the sandbox. Mock and older executions keep their existing behavior.

The executing Agent receives bounded optional planning instructions through the existing stdin prompt, together with its current accepted revision and content when present. It may choose independent work, propose independent deliverables, or emit no plan. Planning does not require another Agent or model invocation. The prompt asks it to consider dependencies, parallel benefit, responsibility boundaries and coordination overhead rather than arbitrary task-size heuristics.

The Codex adapter accepts a planning envelope only as a complete `item.completed` / `agent_message` text. It buffers up to 16 decisions and commits them together only after both streams close, a successful process exit and validated `turn.completed`. A later final assistant response must independently indicate ordinary work or an already-authorized Stage 2H control action. Planning messages never enter the Delegation handoff path. Stderr, tool/reasoning output, code fences and incomplete streams cannot author plans. Existing raw/readable output retention is unchanged.

**Provider limitation:** the [official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode) documents JSONL events and completed assistant messages, but does not promise multiple assistant messages per invocation. This integration records a plan only when the provider actually emits a separate intermediate assistant message followed by a valid final response. It does not assume such messages always exist or force a planning-only turn. An ordinary single-message completion works without a plan. A plan-only final response is rejected as unverified execution, using the existing stopped/pending failure path; it does not complete the Task, hand off to orchestration, or automatically resume. No plan from that invocation is accepted. Live intermediate-message emission has not been verified with a paid Codex run. Do not describe this as guaranteed live planning or end-to-end autonomous decomposition.

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

Stage 2I.2 will handle coordinated admission/allocation of real delegated work. Stage 2I.3 will handle changes to active work and safe handoffs. Stage 2I.4 will add UI integration and future creation defaults. This milestone supplies optional planning records only and makes no guarantee against semantic duplicate work.

## Verification limitation

The recorded Windows full backend run executed 787 tests and ended with two errors: `test_agent_manager.AgentTests.test_tree_stop` and `test_always_decisions.AlwaysDecisionTests.test_windows_resume_stdin_cwd_session_sandbox_and_watcher`. Both passed when rerun together in isolation (2 tests, 1.094 seconds). The failures may be timing/environment-sensitive, but their cause has not been conclusively identified or fixed; the full suite cannot be described as passing. Publication was approved with this known limitation. Paid Codex planning-event behavior remains unverified.
