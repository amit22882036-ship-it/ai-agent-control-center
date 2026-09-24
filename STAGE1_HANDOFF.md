# Stage 1 technical handoff

This document describes the completed Stage 1 implementation of AI Agent Control Center. Read it together with [WORKING_AGREEMENT.md](WORKING_AGREEMENT.md) before starting Stage 2. Implementation was checked against the repository at `1878ca0` (`Harden Stage 1 agent control robustness`). Future changes must be checked against the actual working tree; this is a checkpoint, not permission to overwrite newer work.

## 1. Project purpose

AI Agent Control Center is a local control tower for AI coding agents. A user can launch multiple agents, observe output, stop or intervene, redirect active work, answer blocked agents, delegate decisions, manage parent/child relationships, stop branches, recover history after backend restarts, and receive notifications and live dashboard updates.

Stage 1 is a functioning local technical MVP: process control, Codex session continuity, durable history, a usable dashboard, and tested recovery behavior. The broader vision is a professional, approachable product that understands tasks, coordinates useful workers, and supports multiple providers and workspaces. Task orchestration and semantic decomposition are future work, not present capabilities.

## 2. Current architecture and startup

```text
Browser / React + Vite dashboard
  -> FastAPI REST API + SSE invalidation stream
  -> Agent Manager
     -> Mock / Codex subprocesses
     -> SQLite metadata and output history
```

The backend uses Python, FastAPI, Uvicorn and standard-library process/thread/SQLite facilities. The frontend uses React 19 and Vite 8. SSE is the primary live transport; two-second polling is the fallback. REST remains authoritative. Windows is the supported Codex launching and process-management target. Run one backend process/worker; in-memory registries, locks, notification leases and the SSE broker are process-local.

Repository root: `C:\Users\User\Desktop\AI agent control center\ai-agent-control-center`.

In a dedicated **backend PowerShell window**:

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\backend"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

In a separate **frontend PowerShell window**:

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\frontend"
npm run dev -- --port 5173 --strictPort
```

Dashboard: <http://localhost:5173>. Backend: <http://127.0.0.1:8000>. Health: <http://127.0.0.1:8000/health>. API documentation: <http://127.0.0.1:8000/docs>.

These commands assume the existing `.venv` and frontend dependencies are installed; see README for initial setup. CORS permits only `http://localhost:5173` and `http://127.0.0.1:5173`, with GET/POST and Content-Type. The strict Vite port avoids silently moving to a disallowed origin.

## 3. Three distinct identities

| Identity | Meaning | Lifetime |
| --- | --- | --- |
| `agent_id` | Control Center-generated string UUID; public logical identity and registry key | Stable through resumes and restarts |
| `process.pid` | Real operating-system process identifier | Temporary; changes on replacement and can be reused by the OS |
| `session_id` | Codex conversation/session UUID, detected from initial CLI metadata | Preserved when resuming the same Codex context |

Never conflate them. Two launches reporting the same PID still have different logical agents. Process-tree termination uses the current subprocess's real PID, never `agent_id`. Recovery restores logical records without attaching any old OS process.

## 4. Completed capabilities

- **Mock:** runs `backend/mock_agent.py` using `sys.executable`. Prints six simulated work stages with two-second gaps and immediate flushing. It stores the task as metadata but does not execute it; it ignores sandbox selection.
- **Codex / Start:** starts the explicit npm CLI with a task delivered through stdin and captures merged stdout/stderr. Default type is Mock; default Codex sandbox is read-only.
- **Stop:** stops the current subprocess; on Windows Codex requires whole-tree termination. Stopping a waiting agent changes its logical status to stopped. A stopped record remains stopped; an already finished record remains finished.
- **Redirect:** available only for a running Codex agent with a detected session. Validates the command, terminates the old process tree, drains its output, then resumes the existing session with a correcting instruction. UUID, original task, sandbox, session and history are retained. A readable `--- Redirect ---` marker precedes resumed output; no new card is created.
- **Waiting / Reply:** a genuine response marker records the blocking question. After process exit and output drain, the agent becomes waiting. Reply resumes the same session with the answer and a `--- User Reply ---` history marker.
- **Decision delegation:** one-time Decide, example-based Similar, and persistent Always Decide use the same session-resume machinery. See section 9 for scope and loop protection.
- **Notifications:** native browser notifications while the dashboard is receiving updates; Windows backend notifications can continue after the dashboard closes, while the backend remains running and notification preference is enabled. A presence lease suppresses duplicate browser/native delivery.
- **Hierarchy / Stop Branch:** child agents have persistent parent links. Stop Branch traverses the selected subtree children-first, attempts every stop and reports partial failures. It does not affect siblings outside that subtree.
- **Persistence / recovery:** SQLite retains metadata and complete output; waiting agents remain resumable, and crash-interrupted running records become stopped without automatic relaunch.
- **Section-aware parsing:** only Codex response sections can supply waiting or handled markers. Prompt echoes remain visible but are not treated as control responses.
- **Live updates:** one frontend EventSource receives invalidations; REST fetches fresh snapshots. Polling takes over during stream failure, with reconnect and retry behavior.
- **Navigation:** task/ID/type/status search, status counts and filters, hierarchical cards, collapse/expand, retained selection and contextual ancestors.
- **Scalable output:** bounded recent cache and bounded browser window, incremental sequence-based reads, Load older output and Return to latest output.

## 5. Architectural invariants

1. Logical UUIDs are stable and must never revert to PID identities.
2. Task, reply and redirect text goes through stdin, never interpolated into a command string. `shell=False` remains mandatory.
3. Resolve Codex explicitly from `%APPDATA%\npm\codex.cmd`, not a desktop-bundled executable or an arbitrary PATH result. `cmd.exe /d /s /v:off /c` invokes the quoted launcher with fixed/validated options. Session IDs are validated as UUIDs; sandbox is a two-value Literal.
4. Codex cwd is this repository root. Initial command arguments are `exec --sandbox <mode> --color never --skip-git-repo-check -`. Resume adds `resume <session_id> -` after the exec options. Prompts are wrapped with the internal waiting protocol, written to the pipe, and stdin is closed.
5. Sandbox is `read-only` or `workspace-write`. Resume retains the original sandbox, session, task, logical agent and history. Delegation does not expand sandbox permissions.
6. Project decision history: `codex queue --thread` was tested and rejected for Redirect because it did not alter an active run. The implemented mechanism is terminate + `codex exec ... resume`, not queue.
7. Windows Stop uses `taskkill /PID <real PID> /T`, then `/F` fallback. Failure to confirm tree termination is an error; killing only the cmd wrapper must not be reported as a successful stop.
8. Exact waiting syntax is `CONTROL_CENTER_WAITING: <question>`, with a non-empty question. `CONTROL_CENTER_SIMILAR_HANDLED` and `CONTROL_CENTER_ALWAYS_HANDLED` are standalone markers for the corresponding automatic attempt.
9. Each output reader maintains its own CLI section state: `user`, `codex`, or other metadata/control. Only the `codex` section can update waiting/handled control state. Initial `session id: <UUID>` metadata is accepted only before content sections and only while the session is unknown. Echoed user/model text cannot overwrite it. This is a CLI-format-dependent parser, not a structured provider protocol.
10. Output text is preserved, including ANSI sequences; only line endings are removed for list entries. ANSI-stripped text is used separately for parsing. Control-center history markers are additional explicit entries.
11. Only the currently associated process may append output or update control state. Old readers and finalizers cannot overwrite a replacement. Finalization waits for process exit and stdout closure so buffered final markers are parsed first.
12. State-changing operations use a reentrant manager lock; output/persistence use a separate data lock. Readers must not acquire the manager lock while Stop/Resume can join them holding that lock.
13. Relevant persistence completes before SSE invalidation. Failed persistence retains a retry backlog and does not publish that save as committed. SSE is invalidation, not output delivery or durable replay.
14. SQLite is the durable source of complete output. Ordinary backend recent cache and frontend rendered output are bounded; outage retry backlog is an explicit exception.
15. Startup does not replay historical SSE changes or notifications, reattach running-at-crash PIDs, or relaunch historical work. Genuine waiting sessions stay resumable.

## 6. Persistence and recovery

Default database: repository-root `data/control_center.sqlite3`. `CONTROL_CENTER_DB_PATH` overrides it. Runtime data and SQLite sidecars are Git-ignored; never stage them.

`AgentStore` uses SQLite schema version 1. `agents` stores a creation-order key, unique logical ID, parent, task, type, sandbox, status, session, waiting question, Similar enabled/examples, and Always enabled/configured settings. `output` stores `(agent_id, sequence, line)` with a composite primary key. Metadata and pending output are written together transactionally. Sequence numbers are per-agent, start at zero, and continue across resumes/recovery; they do not reset when a process is replaced. Identical sequence retries are idempotent; conflicting text is an error. Unknown/incompatible schemas are not silently overwritten.

| Limit | Value |
| --- | ---: |
| Backend recent-output cache per agent | 1,000 entries |
| Default output page / frontend batch | 300 entries |
| Maximum API page | 1,000 entries |
| Frontend output window / rendered entries | 2,000 entries |

Startup loads metadata plus only each agent's recent tail and next sequence, rather than all history. Complete successfully persisted history remains in SQLite.

- **Waiting at restart:** remains waiting with question, session and settings; Reply/delegation can resume it.
- **Running at crash/restart:** becomes stopped and appends `--- Backend Restart ---` plus an explanation that safe reattachment is impossible. No historical PID is attached or killed.
- **Finished/stopped:** retained as historical records.
- **Normal shutdown:** stops live work, drains output and retries persistence; genuine waits remain waiting. Shutdown failures are logged/reported rather than silently treated as successful.

Enabled autonomy settings and approved Similar examples survive. Temporary automatic-attempt/handled flags do not survive recovery. Loading records itself does not execute decisions.

Storage errors retain dirty metadata and uncommitted output for retry on subsequent activity, reads or shutdown. This exceptional backlog can exceed the recent cache limit. **A total backend crash during an ongoing SQLite/storage outage loses any still-uncommitted in-memory backlog.** Persistence is not a guarantee that every acknowledged in-memory change has reached disk during an outage.

## 7. Output history API and scrolling

`GET /agents/{id}/output` returns:

```json
{
  "agent_id": "stable-uuid",
  "items": [{"seq": 42, "text": "Output line"}],
  "has_older": true,
  "has_newer": false
}
```

- No cursor: most recent tail, default `limit=300`.
- `after=N`: entries strictly after N for incremental live reads.
- `before=N`: entries strictly before N for Load older.
- Every page is returned in deterministic ascending sequence order, even when selected from the tail.
- `limit` is 1 through 1000; cursors are nonnegative. Supplying both after and before is rejected with 422. Unknown agent: 404. Unavailable storage: sanitized 503.

`GET /agents/{id}` keeps legacy full `output` compatibility, which can be expensive. Normal frontend metadata requests use `include_output=false`; output is fetched independently through the paginated endpoint.

The frontend serializes page requests, computes cursors when each request runs, deduplicates by sequence and sorts ascending. Live reads trim oldest visible entries at the 2,000-entry limit. Load older prepends earlier entries and preserves a visible-line scroll anchor. If loading older exceeds the window, the older window is retained in browsing mode; live reads advance the cursor without displacing that window. Return to latest reloads the tail and resumes following. Live auto-scroll occurs only near the bottom (less than 40 pixels away); explicit Return to latest scrolls down. Selection changes dispose/abort old loaders.

## 8. Lifecycle/state model

| State | Meaning |
| --- | --- |
| `running` | Current run is active or its final buffered output has not finished draining |
| `waiting` | Run ended with a genuine non-empty blocking question requiring user input; no continuously running interactive process is required |
| `finished` | Process exited and output drained without an unresolved waiting question; not explicitly stopped |
| `stopped` | Explicitly stopped, or recovered from an interrupted running record |

Typical paths: Start → running → finished; running → waiting → Reply/Decide → running; running → Redirect → running under the same UUID; running/waiting → Stop → stopped. Automatic policies may resolve a waiting question before it becomes a visible waiting episode.

**Finished is a lifecycle result, not independently verified task success.** The implementation does not distinguish zero and nonzero process exit codes in this status, so “exited normally” must not be read as “exit code zero” or “correct work.”

Failure semantics matter: Redirect has already interrupted the old run when a later resume fails, so it may leave a stopped agent. A failed spawn before replacement leaves an existing wait intact. Failed prompt delivery attempts to stop replacement work. Always-resume staging restores the prior cache/state only when cleanup and drain are safe; otherwise the replacement stays tracked. Never advertise waiting/stopped while an unconfirmed live replacement is lost. Do not generalize this into a promise that every resume failure fully rolls back.

## 9. Autonomy semantics

**Decide for me** delegates only the current waiting decision. It does not grant permanent authority over future unrelated questions.

**Decide similar automatically** resolves the current question and, after successful resume, stores it as an approved example and enables a per-agent policy. Future questions trigger a conservative, session-based similarity check, not a local keyword classifier. Uncertain/dissimilar questions return to waiting. An attempted set prevents a declined question being retried by repeated GETs or polling; unresolved reworded questions are protected too. Explicit successful approval of that question removes only its normalized entry from attempted. Ordinary Reply, one-time Decide and automatic outcomes do not clear this protection. Disabling Similar clears its policy/examples.

**Always Decide** delegates future reasonably resolvable decisions for that agent. It takes precedence over Similar while enabled; Similar examples remain saved. Always instructions explicitly prohibit inventing unavailable facts/secrets, expanding task or sandbox, bypassing required approval, or authorizing unrelated external actions. Missing required factual information still returns Waiting. Handled markers and automatic-attempt flags prevent unresolved episodes from looping. Disabling Always is communicated to later resumes so old session instructions do not silently reactivate it.

Policies are agent-scoped, not global and not arbitrary permission expansion. Enable operations commit policy changes after successful resume. Persistence retains durable settings/examples, not temporary retry flags.

## 10. Hierarchy semantics today

`parent_id` links logical agents; details expose direct `child_ids`. The child-start endpoint requires an existing parent and uses the normal start request. Children are explicitly user-created workers with their own tasks, types, sandboxes, sessions and lifecycle. Relationships survive persistence. Normal Stop affects only one agent; Stop Branch includes the selected root and all descendants, with serialized discovery/stops and per-agent results/failures. Traversal is iterative and cycle-safe. Frontend tree construction also tolerates malformed cycles without losing all their records.

**Stage 1 stores and controls structural relationships. It does not validate that a child's task is a meaningful subtask of its parent's task.** There is no automatic task decomposition, result aggregation, task reassignment or agent-created child orchestration. Those semantics belong to Stage 2.

## 11. Notifications

Browser permission is requested only through an explicit control. Preference is remembered in localStorage where available. A UUID-to-status tracker uses the first successful snapshot as a silent baseline; later transitions into waiting/finished notify once, including newly discovered terminal/waiting agents. Repeated snapshots and React rerenders do not replay notifications. Running/stopped do not notify. Body includes up to 180 task characters. Browser notification clicks try to focus the window and select the agent. Delivery failures do not break refresh.

The frontend syncs enabled preference to `/notifications/preferences` and sends `/notifications/heartbeat` every two seconds while browser delivery is considered healthy and the last successful snapshot is less than four seconds old. Backend presence lease is six seconds; native delivery waits eight seconds before checking it. A fresh lease suppresses the pending native notification. Without it, a fixed hidden PowerShell/Windows Forms NotifyIcon helper displays the notification; content is JSON on stdin. Stop/resume cancels obsolete pending delivery.

Native preference and lease are in memory, not SQLite: the dashboard resynchronizes after backend restart. Closing the dashboard does not disable the preference in a still-running backend. This is best-effort browser/native duplicate suppression, not durable exactly-once delivery across multiple tabs, browser throttling or OS policy. Historical transitions are never replayed on load. No service worker or web push exists.

## 12. SSE and fallback

```text
backend mutation -> SQLite save -> agent-change SSE invalidation
                 -> frontend REST refresh -> updated cards/details/output
```

`GET /events` emits revision plus agent ID (or null for coalesced global invalidation), not log contents. Revision is process-local, not a persistent replay cursor. Subscriber queues are bounded to one pending event; cross-thread publication is coalesced safely. Fifteen-second comment keepalives keep the stream active. Disconnect cleanup removes subscribers; startup sends no historical event replay.

One frontend EventSource controls invalidation. Errors/unsupported EventSource enable two-second polling; opening/reopening the stream stops fallback and immediately refreshes snapshots. Roughly 75 ms coalescing and single-in-flight refresh queues prevent bursts of duplicate REST work. A failed REST refresh explicitly retries after two seconds even if SSE stays open and quiet. Effects dispose timers, close streams, abort fetches and reject stale loader results.

## 13. Frontend map

All paths below are repository-relative.

| Module in `frontend/src/` | Responsibility |
| --- | --- |
| `App.jsx` | List fetching, selected UUID, top-level notification control and one live connection |
| `StartAgentForm.jsx` | Task/type/sandbox start form, also used for child creation |
| `AgentList.jsx`, `AgentTree.jsx` | Counts, search/status filter, structural cards, selection and collapse controls |
| `buildAgentTree.mjs`, `agentListView.mjs` | Pure tree construction, cycle handling, iterative filtering and visibility helpers |
| `AgentDetails.jsx` | Metadata, waiting/decision controls, redirect, stop/branch, child start and action errors |
| `AgentOutput.jsx` | Output viewport, navigation and scroll anchors |
| `outputHistory.mjs` | Serialized incremental loader, sequence merge/deduplication, bounded windows |
| `agentEventStream.mjs` | EventSource/fallback lifecycle and refresh queues |
| `agentNotifications.js`, `useAgentNotifications.js`, `systemNotificationSync.js` | Transition tracking, browser permission/preference, backend sync and presence |

Filtering retains matching agents and ancestor context. Necessary descendant paths temporarily expand via an effective collapsed set without mutating saved choices; clearing filters restores those choices. Collapse controls are disabled during filtering. If search/filter/collapse hides the selected card, its details remain open and a hidden-selection indication explains why.

## 14. Backend map and API surface

| Module | Responsibility |
| --- | --- |
| `backend/app/main.py` | FastAPI models/routes/error mapping, CORS, startup recovery and shutdown |
| `backend/app/agent_manager.py` | UUID registries, lifecycle locks, CLI launch/read/watch, parsing, resume/autonomy, hierarchy and persistence coordination |
| `backend/app/persistence.py` | SQLite schema, transactional metadata/output, range queries and recovery reads |
| `backend/app/output_history.py` | Sequence assignment, 1,000-entry recent cache and retry/staging state |
| `backend/app/realtime.py` | Bounded thread-safe invalidation broker and async SSE stream |
| `backend/app/system_notifications.py` | In-memory preference/lease, delayed native notifications and safe Windows helper |
| `backend/mock_agent.py` | Flushed simulated work stages |

Public routes: `GET /health`, `GET /agents`, `POST /agents/start`, `GET /agents/{id}`, `GET /agents/{id}/output`, `GET /events`; `POST /agents/{id}/stop`, `/redirect`, `/reply`, `/decide`, `/decide-similar`, `/decide-similar/disable`, `/decide-always`, `/decide-always/disable`, `POST /agents/{parent_id}/children/start`, `/stop-branch`; notification preference/heartbeat POSTs described above.

Start body: `{ "task": "...", "agent_type": "mock" | "codex", "sandbox": "read-only" | "workspace-write" }`. Reply uses `answer`, Redirect uses `instruction`; blank replies/redirects are rejected. Missing records generally return 404, invalid lifecycle actions 409, request validation 422 and launch/resume availability failures 503. Stop Branch returns `ok`, `results` and `failures`; inspect the body for partial failure, not just the HTTP status.

## 15. Verification baseline

Final Stage 1 baseline supplied for this handoff and supported by the prior robustness verification:

| Check | Result |
| --- | --- |
| Complete backend unittest suite | 130 passing |
| Frontend Node tests | 55 passing |
| `npm run build` | Passing |
| `npm run lint` | Passing |
| `git diff --check` | Passing; local Windows LF/CRLF conversion warnings may occur |
| Final live E2E | Passed, as confirmed by Amit for this handoff |

The live E2E covered output, Waiting/Reply, hierarchy, Stop Branch, restart persistence, SSE reconnect, scalable history and search/filter/collapse. These are recorded results, not new tests run during this documentation-only task.

Tests under `backend/test_*.py` cover identity/PID reuse, command/stdin safety, process control, section markers and session trust, resume failures, decision loop protection, hierarchy, storage/recovery/output ordering, notifications and SSE. Frontend `tests/*.test.mjs` use Node's built-in runner for tree/filter behavior, output ordering/window/scroll helpers, notifications, system sync and transport/retry lifecycle; they are not a full browser UI test suite. Backend tests use mocks/local shims, not real AI requests.

For a future verification checkpoint, use a **verification PowerShell window** (these commands do not start servers):

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\backend"
..\.venv\Scripts\python.exe -B -m unittest discover -s . -p "test_*.py" -v
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\frontend"
node --test tests\*.test.mjs
npm run build
npm run lint
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center"
git diff --check
git status
```

## 16. Known Stage 1 limitations

- Windows-specific real Codex launching/process-tree management; one backend process/worker.
- Textual Codex CLI section/protocol dependence, rather than a structured provider event API.
- Mock remains a visible developer-oriented type; current UI is functional and prototype-like.
- No archive/hide/delete workflow; no first-class independent Task entity.
- Codex is the only real provider; no provider onboarding/capability abstraction yet.
- No autonomous agent-created children or semantic task decomposition; hierarchy is structural.
- No multi-project/workspace management; Codex uses the fixed repository root.
- Finished does not independently verify correctness or even distinguish process exit success/failure.
- Uncommitted persistence retry backlog cannot survive a total crash during storage outage.
- Local-use architecture, not a multi-user hosted service; no authentication system.

## 17. Agreed Stage 2 direction — not implemented

Stage 2 should turn this technical MVP into a professional agent-control product. The following is product direction, not a claim that APIs or detailed implementation plans have been finalized.

### 2A — Professional UI / Design System

Approachable, polished, low-clutter UX; Apple-like principles of clarity, accessibility and progressive disclosure, not literal copying. Move UUID/process details into secondary/advanced views. A desktop/control-center layout is a possible direction, not a committed design.

### 2B — First-class Tasks

Separate Agent identity from Task identity: the Agent is the worker; the Task is the work. Replacing or deleting a worker must not automatically delete its work.

### 2C — Task-aware hierarchy

Children should exist because a parent task was decomposed into meaningful contributions. The agent tree should follow task decomposition rather than arbitrary nesting.

### 2D — Smart Agent Removal

Distinguish two user intentions: (1) the work is no longer wanted, so cancel/delete the work; (2) the worker is unwanted but unfinished work still matters, so preserve and intelligently reassign/restructure it. Consider every unfinished task represented by an entire removed subtree; work must never silently disappear.

The user should not have to manually choose the receiving worker, whether it handles two tasks, whether to create a child, or whether a sibling/replacement branch is appropriate. Those choices must be task-aware and justified, not arbitrary nesting changes.

### 2E — Task Orchestrator

Design toward **Understand → Plan → Validate → Execute**. Consider the removed agent/task, children/subtasks, completion, parent task, dependencies, priorities, existing workers and their workloads/capabilities. Combine AI reasoning with deterministic validation: child work contributes to its parent; unfinished work is preserved; unnecessary agents are avoided; unrelated major tasks do not overload one worker; dependencies survive; completed work is not needlessly restarted.

### 2F — Multi-provider architecture

Plan an Agent Provider abstraction for Codex, Claude Code, Gemini CLI and future providers. The control center should consume capabilities rather than spread Codex-specific assumptions throughout the app. Candidate capabilities include installed/available detection, start, stop, resume, reply, redirect and session handling. Exact provider APIs are not yet designed, and equal capability support must not be assumed.

### 2G — Provider setup/onboarding

Show installed/ready, not installed, configuration issues, installation guidance and re-check availability. Raw FileNotFoundError should not be the normal product UX.

### 2H — Projects / Workspaces

Add deliberate project/workspace organization and execution context selection, replacing the single fixed repository assumption when that milestone is designed.

### 2I — Autonomous task decomposition and sub-agent creation

A root agent should eventually accept a complex goal, decompose meaningful subtasks and create only workers that are actually useful, subject to task-aware validation.

### 2J — Production polish

Future concepts include human-friendly editable agent names and useful task-derived names; distinct Hide/Archive/Delete; Mock behind Developer Mode; activity feed; provider/agent health; usage/runtime/token visibility; clearer permissions UX; command palette; task handoff; and professional project/workspace organization. These remain future concepts, not Stage 1 features.

## 18. Starting Stage 2 in a fresh conversation

1. Read `STAGE1_HANDOFF.md`.
2. Read `WORKING_AGREEMENT.md`.
3. Inspect current Git status, history and repository.
4. Treat Stage 1 invariants as existing behavior to preserve.
5. Do not casually redesign stable Stage 1 mechanisms.
6. Work one Stage 2 milestone at a time.
