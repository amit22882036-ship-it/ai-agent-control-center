# AI Agent Control Center

A local dashboard for starting, stopping, and monitoring multiple agents. The React + Vite frontend connects to a FastAPI backend running with Uvicorn, which manages agent subprocesses and captures their output.

## Features

- **Mock agents:** simulate a short workflow with progress messages. They do not execute the supplied task or change files.
- **Codex agents:** run tasks through the installed Codex CLI inside a durable, isolated Task Workspace.
- **Codex sandbox modes:** `read-only` is the default; `workspace-write` allows Codex to modify files in its Task Workspace. Choose the mode before starting a Codex agent.
- **Start and stop:** enter a task and select an agent type to start a run. Select an agent to view its details and stop it while it is running.
- **Status monitoring and live output:** SSE change notifications refresh agent cards and selected-agent details from the REST API. If the stream is unavailable, the dashboard falls back to refreshing every two seconds and reconnects automatically. Status and captured output remain available through the existing REST endpoints.

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

Workspace isolation, freshness, integration conflicts, canonical/index protection and session-context checks remain separate and unchanged. There is no frontend dependency editor, autonomous replanning, or resource orchestration in this milestone.

Agent records, hierarchy, output, sessions, and autonomy settings are stored in local SQLite at `data/control_center.sqlite3`. Set `CONTROL_CENTER_DB_PATH` to use a different path. Use one backend worker for this local process manager. The database and sidecar files are ignored by Git.

Output is appended incrementally to SQLite with stable per-agent sequence numbers; the existing normalized schema is retained. Only the latest 1,000 entries per agent are cached on startup. The dashboard loads a 300-entry tail, then requests incremental changes. **Load older output** retrieves earlier history on demand. The browser retains at most 2,000 entries; loading beyond that window switches to browsing older output until **Return to latest output** is selected. Complete history remains in SQLite. Failed database writes are logged and retained in a retry backlog (which can grow during an outage); subsequent activity, reads, or shutdown retry them. Uncommitted data cannot survive a crash during a storage outage.

On normal shutdown, running agents are stopped; waiting agents stay waiting. After a crash, records last saved as running become stopped with a restart marker. No old PID is reattached or killed. Recovered waiting Codex agents can Reply or delegate through their saved session. Loading never starts agents or replays historical notifications. Autonomy settings survive, but temporary attempt flags reset. `finished` means the process exited; it does not distinguish success from failure.

## Project identity (Stage 2D)

A **Project** identifies a logical codebase. Its **Canonical Workspace** is its canonical absolute root directory. Ownership is `Agent -> TaskAssignment -> Task -> Project`; Agents do not persist a second Project identity. Project identity does not protect shared databases, ports, caches, or external services.

`POST /projects` accepts `name` and `root_path`; `GET /projects` and `GET /projects/{project_id}` return Project metadata. New registration requires an existing directory. Canonical paths resolve relative components and filesystem aliases, with Windows case normalization for comparison. Duplicate and overlapping roots are rejected. The comparison key is internal. There are no Project mutation/deletion endpoints or Project UI.

`POST /tasks` accepts an optional `project_id`; otherwise it uses the repository's canonical context. Existing `POST /agents/start` requests remain unchanged. Both providers resolve the default canonical context before provisioning isolated execution. Child Tasks inherit their parent's Project.

Schema v7 adds Projects and nullable `tasks.project_id`. The v6 schema contains no persisted execution-directory evidence, so migration leaves historical Tasks unresolved without guessing from text, hierarchy, or today's cwd. Such Tasks cannot start or resume a worker or child. All new Tasks receive a Project. Project ensure and Task creation are transactional; failed worker launches leave a valid pending Task without an active assignment, following the existing lifecycle compensation rules.

## Durable Task Workspaces (Stage 2E.1)

Schema v8 adds one `task_workspaces` record per Task, allocated lazily, without fabricating historical filesystem state during migration. The workspace belongs to the Task, never to an Agent. A replacement worker, Reply, Redirect, Decide, Similar, Always, or recovered waiting session reuses that persisted path. Missing, corrupt, wrong-repository, or unresolved workspaces fail safely; execution never falls back to the canonical directory.

Set `CONTROL_CENTER_WORKSPACE_ROOT` to an external storage directory. The default is `%LOCALAPPDATA%/AI Agent Control Center/task-workspaces` on Windows (or `~/.local/share/AI Agent Control Center/task-workspaces` without LOCALAPPDATA). Paths are generated as `<root>/<project-id>/<task-id>` and must not overlap any canonical Project. Do not move or manually replace these directories.

Provisioning requires a Git working-tree root with a usable HEAD. A temporary alternate index captures current source bytes, including staged/unstaged tracked changes and non-ignored untracked files. It creates an internal snapshot commit and a detached linked worktree, without changing the user's branch, HEAD, index, or canonical files. No branch is created. Children snapshot their parent's current Task Workspace, recursively. Independent roots and siblings have separate files. New work on an existing legacy root Task without a workspace snapshots today's canonical source (`legacy_project_snapshot`), not historical state. Child starts use the current parent workspace, provisioning a legacy parent first if necessary.

Ignored files and common environment/secret/build/cache paths are excluded, including tracked `.env*`, private-key files, `node_modules`, `.venv`, and `dist`. This is filename-based filtering, not a secret scanner; linked worktrees still share the repository's Git object database and history. Symbolic links, submodules, and non-Git/unborn repositories are currently unsupported. Source snapshots do not provision runtime dependencies or copy environments. Snapshots read files sequentially; there is no coordinated freeze of a concurrently editing parent.

`GET /tasks/{task_id}/workspace` returns metadata, `null` when not yet provisioned, or 404 for an unknown Task. It never provisions a workspace. There are no mutation/reset/delete/sync APIs or Workspace UI. Stop, completion, waiting, shutdown, and worker replacement retain workspace files. Provisioning and Agent execution alone do not integrate files back into canonical or parent files.

Provisioning uses per-Task synchronization, exclusive filesystem reservation, and database uniqueness. If persistence fails, only a newly created, unrecorded worktree is compensated. An uncertain commit preserves a durably recorded workspace; an unavailable database leaves files untouched. A crash leaving an unrecorded path requires manual recovery instead of silent adoption or deletion. Spawn failure retains valid workspace state. No automatic retention cleanup or resource coordination is implemented.

## Revision and staleness safety (Stage 2E.2)

Workspace freshness uses Git content-tree identities, not HEAD or snapshot-commit timestamps. Roots compare against current canonical source; descendants compare only against their direct parent's current Task Workspace. Current source bytes include staged/unstaged tracked files and relevant untracked additions/removals, using the same exclusions as provisioning. Local Task edits alone are not staleness.

`GET /tasks/{task_id}/workspace` now derives `freshness` (`fresh`, `stale`, or `reconciliation_required`), `base_source_snapshot`, `upstream_snapshot`, `local_snapshot`, `local_dirty`, and a bounded `changed_upstream_files` list plus remaining count. `base_snapshot` remains the durable internal commit. GET does not provision or update workspace files.

Before worker start or same-session continuation, clean stale workspaces refresh in place. Dirty stale workspaces reject continuation with HTTP 409 and retain their files, session, Task status, and assignment history. Redirect checks for divergence before stopping its current process, then rechecks after stopping. If a new race/error occurs after termination, the stopped process is reported truthfully. There is no automatic reconciliation, integration, merge, or rebase.

Refresh uses a per-Task lock, expected base/local/upstream revalidation, an exclusive Task index reservation, and Git's two-tree overwrite checks. An isolated temporary Git directory disables content filters and conversions while preserving captured bytes; no canonical checkout/index/ref is changed. Filesystem and SQLite are not a single transaction: failed persistence after filesystem refresh leaves the old baseline and conservatively requires inspection/reconciliation, never silently starts work. External editors do not participate in application locks; optimistic checks detect observed races, not future writes after a check. No watcher is installed.

Schema v9 adds `agent_source_context`, recording the source baseline acknowledged by each session. Migration from v8 backfills available assignment/workspace baselines without touching Git. After refresh, resumed sessions receive an internal stdin instruction to re-read affected files (up to 50 escaped relative path labels, 240 characters each, plus remaining count). New sessions do not need this warning. Context acknowledgment advances only after prompt delivery succeeds; failed acknowledgment safely repeats the warning on the next continuation. Missing historical context prompts a conservative re-read. The existing symlink/submodule restrictions and workspace retention remain in effect.

## Controlled integration (Stage 2E.3)

`POST /tasks/{task_id}/integrate` accepts no destination controls (an empty body or `{}` is sufficient). The server derives the destination from durable hierarchy: children integrate into their direct parent Task Workspace; roots integrate into their Project canonical workspace. `GET /tasks/{task_id}/integrations` lists source history and `GET /integrations/{integration_id}` inspects a result. Conflicts and obsolete candidates return HTTP 409 with structured history; unsafe prerequisites return sanitized errors. There is no integration UI or automatic Agent action.

Integration compares base **B**, source **L**, and destination **U** content trees. Candidates are prepared outside canonical/workspaces, using per-path three-way comparison and Git's textual three-way merge for files changed on both sides. Delete/modify, incompatible additions, binary conflicts, mode conflicts, and ambiguous directory/file cases are refused. Renames are represented as delete/add, without heuristic rename inference. A source equal to its base records a no-op. Source work and its original workspace baseline are never reset or deleted.

Schema v10 adds integration history, durable active-source/destination protection, snapshots, conflict/change summaries, and a pre-image/result apply journal. Migration from v9 creates no historical integrations. Internal `refs/control-center/integrations/...` retain provenance/recovery objects across Git garbage collection; they are not branches. `validation_status` remains `not_run`: a clean textual result is not semantic validation.

Per-destination/task locks coordinate integrations with worker start/resume. Source and parent destinations must have no live managed writer; integration never stops a worker. Both source and destination snapshots are rechecked immediately before apply. Replacement bytes are fully staged externally and an `applying` journal is committed before file writes. Apply changes only destination working files: it never stages, commits, stashes, changes HEAD, or switches branches. User staging remains byte-for-byte intact, including when canonical is already dirty. Ignored/excluded path collisions are refused.

Each write checks its expected pre-image and uses atomic file replacement. On failure, already-applied paths are restored only if they still match this integration's output; newer external edits are preserved. Multi-file apply is not an atomic filesystem transaction, and external editors do not participate in application locks. An ambiguous failure retains a durable `recovery_required` claim, blocking further integration/unsafe continuation until future recovery tooling handles it.

At backend startup, interrupted preparation is marked failed. Interrupted apply matching the complete before snapshot is failed safely; matching the result, source, and journal can be finalized as applied. Anything else requires recovery. Recovery does not write files. Exact already-applied source snapshots are idempotent against the same logical destination, even after reopening storage.

Parent session context uses the existing durable source-context acknowledgment plus an integration-history cursor. All child-integration paths since the last successful prompt delivery are combined into a bounded re-read instruction; failed delivery retains the warning. Integrations do not modify Task lifecycle or descriptions, resume Agents, or perform AI review/tests/deployment. Sibling staleness follows naturally from the parent's changed content. Source continuation may still require reconciliation because integration deliberately does not refresh/rebase the retained source workspace.

## Requirements

- Python 3.10 or newer.
- Node.js 22.13+ within the 22.x release line, or Node.js 24+, with npm, for the frontend tooling.
- For Codex agents, Windows with the npm-installed Codex CLI at `%APPDATA%\npm\codex.cmd`, configured and authenticated for use. The current backend supports Codex agents only through this Windows launcher. Mock agents do not require Codex.

## Start the backend

From the repository root, run in Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
cd backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

On macOS or Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt
cd backend
../.venv/bin/python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/health> to check the backend. It returns:

```json
{"status": "ok"}
```

Interactive API documentation is available at <http://127.0.0.1:8000/docs>.

## Start the frontend

Keep the backend running. In a second terminal, starting from the repository root:

```sh
cd frontend
npm install
npm run dev -- --port 5173 --strictPort
```

Open <http://localhost:5173>. The frontend calls the backend at `http://127.0.0.1:8000`; the backend allows frontend requests from `localhost:5173` and `127.0.0.1:5173`.

## Use the dashboard

1. Select **Mock** or **Codex** under **Agent Type**.
2. For Codex, choose **Read only** or **Workspace write** under **Sandbox**.
3. Enter a task and click **Start Agent**. Repeat to run multiple agents.
4. Click an agent's name to view its status and live output.
5. Click **Stop** in the details panel to stop a running agent.
