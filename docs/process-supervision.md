# Process supervision (Windows)

On Windows, every agent execution runs under its own Job Object. This covers each start and each continuation: Reply, Redirect, Decide, Similar, Always and Parent continuation. Mock workers are supervised the same way. The implementation is in `backend/app/process_supervision.py`; it uses the standard library's `ctypes` and adds no dependencies.

## How an execution starts

0. **Reservation.** A short transaction of its own commits the execution's journal row in state `launching`, with the Job name chosen in advance, before any process exists. It refuses the launch if this backend already has an open reservation, an [unconfirmed end](#execution-journal-schema-v28) or an emergency-guard entry for the same Task or Agent. If the reservation cannot be committed, no process is created.
1. A dedicated Job Object is created with an unpredictable name, `Local\ControlCenter-` followed by 128 random bits. If the name already exists, the handle Windows returns for that other object is closed unused and the launch fails: the backend never configures, uses or terminates a Job it did not create.
2. The Job is configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Breakaway is not allowed: neither `BREAKAWAY_OK` nor `SILENT_BREAKAWAY_OK` is set. The limits are then read back with `QueryInformationJobObject`, and the launch fails unless Windows reports exactly that configuration.
3. The worker is created with `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP`, and its creation time is read with `GetProcessTimes`. The pair of PID and creation time identifies the process; a PID alone can be reused.
4. The process is assigned to the Job. `IsProcessInJob` verifies the assignment, and the Job's accounting must show exactly one process.
5. The launch admission transaction (the last control check before `Popen`) revalidates authorization, including removal, Task control and blockers, and that its reservation is still open and the only one. It then consumes the reservation: the row becomes `contained` with this evidence, committed in the same transaction.
6. Only after that commit are its threads resumed (Toolhelp thread snapshot and `ResumeThread`), and the row becomes `running`. A thread that was not suspended is refused. The prompt is written to stdin afterwards, as before.

No worker code runs outside its Job or before its containment evidence is committed, and descendants inherit the Job. Breakaway attempts are refused (`ERROR_ACCESS_DENIED`). A descendant that breaks away from an inner Job, such as one created by a launcher, still remains in the execution's Job.

**Fail-closed.** If Job creation, a name collision, the limit read-back, process creation, assignment, verification, the controller identity, the journal write, the admission commit or resume fails, the still-suspended process is terminated and its handles are closed. A failure before the process exists creates no process at all. The launch is then refused with the existing unavailable response. Nothing runs unsupervised, and there is no opt-out switch.

The backend run that writes these rows is its **controller instance**. Each startup records one in `controller_instances`: its PID and creation time, Windows session id, user SID, and boot evidence (the registry boot counter and the boot time derived from `GetTickCount64`, each stored only when Windows reports it). Supervised launches are refused when the PID, creation time, session or SID cannot be read. A clean shutdown sets `stopped_at`; a crash leaves it empty.

The output of each supervised execution includes `--- Process supervision: active (job object) ---`.

## How an execution ends

- **Stop, Stop Branch, Redirect, Pause/Cancel and shutdown:** these use `TerminateJobObject` and confirm `ActiveProcesses == 0`. This includes detached or orphaned descendants that `taskkill /T` cannot see. If termination cannot be confirmed, the existing behavior applies: "stop was not confirmed", and the execution gate is retained.
- **Normal completion:** when the main process exits, the watcher terminates the Job and confirms `ActiveProcesses == 0` before closing it, which ends any remaining descendants. A descendant that inherited the output pipe can therefore no longer keep the agent `running`. If the confirmation fails, the end is recorded as unconfirmed and the Job handle is kept open, because closing a named Job proves nothing while another handle to it may exist. The Agent stays `running`: its Task is not completed, its assignment and resource ownership stay held and no automatic continuation runs. Reads and explicit lost-watcher recovery cannot settle it. Only a Stop, Pause/Cancel or Remove that then confirms `ActiveProcesses == 0` settles it, and it settles as stopped, never as completed.
- **Backend crash or kill:** Windows closes the Job handle, and every supervised tree is terminated.
- **Ctrl+C in the backend console:** workers run in their own process group, so they do not receive Ctrl+C. Ctrl+C cannot end a worker early and trigger an Always/Similar continuation. Shutdown stops workers without automatic continuation.

`taskkill /T` remains only for processes without a Job.

**Other platforms.** Trustworthy process-tree supervision exists only on Windows for now, so on Linux and macOS every worker launch is refused before a reservation or process exists, with `Process supervision unavailable` (`process_supervision.platform_supported()` is the single place to add supervision there later; Task and Agent ownership do not change).

A confirmed end is recorded as `ended_verified` with the accounting evidence, before the Agent's stopped or finished status is saved. A closed Job handle, a root exit or a never-resumed root is never treated as evidence that no process remains. A refused launch whose end cannot be confirmed fails with `TerminationUnverified`: its launch-time resource ownership is not released, so a new launch for those resources is refused until a backend restart marks that ownership `ownership_lost` (as after a crash at that point), and it is journaled even when the admission transaction rolled back.

**Admission gate.** An unresolved execution (any `uncertain` row, of any controller) refuses every new execution of the same Task or Agent (start, Reply, Decide, Similar, Always, Redirect, Hybrid continuation, delegated launch), with or without declared resource claims. The check runs in the reservation and again in the admission transaction, before `Popen`, so a refused launch creates no process and no row. Only authoritative evidence clears it (see [Restart recovery](#restart-recovery)): a Stop that confirms `ActiveProcesses == 0`, kernel evidence about its Job, or a verified Windows restart.

**Reservation conflicts and closing.** Reservations are checked and created under SQLite's write lock (`BEGIN IMMEDIATE`), so two launches can never reserve the same Task or Agent. A reservation that a refused admission could not close (for example while the database is unavailable) keeps blocking; the next reservation of this backend closes it as `launch_aborted` only when this backend observed that no process was created. At startup an open reservation of a previous backend becomes `uncertain` (`launching_unresolved`) unless a verified Windows restart settles it; a missing Job name is never enough.

**Residual risk (documented, not closed).** Between `Popen` and the Job assignment the suspended root is outside any Job for an instant. If the backend crashes there, or a failed launch cannot confirm that root's end, a suspended process that never ran code can survive until the computer restarts and keeps the handles it inherited (including its working directory). The journal keeps its `launching` reservation with the Job name. Creating the process directly inside its Job (`PROC_THREAD_ATTRIBUTE_JOB_LIST`) would close this window and is deferred.

**Emergency guard (in memory).** Journal writes can fail. The backend therefore also keeps an in-memory ownership guard for its own launches. An entry is created inside the admission transaction before the process exists, and becomes `unverified` before any fallible journal write when a launch's or a watcher's termination cannot be confirmed (or when a never-resumed root outside a verified Job cannot be confirmed ended). Every admission checks it in its own admission transaction, before `Popen`, for the same Task or Agent. Because SQLite's write lock serializes those transactions, no launch can be admitted between discovering an unconfirmed end and blocking it. An entry carries the process identity that was observed (PID, creation time, Job name, execution id). It ends only with a verified successful resume or a verified settlement (a verified Stop, or [current-controller reconciliation](#restart-recovery)); a recovered database, a root exit, an output marker or a clean scan never ends it. The guard is not durable: it protects only while this backend runs. If the backend stops while an unconfirmed end was never journaled, the database keeps what was committed before: the `contained` row, or, if the admission rolled back, the open `launching` reservation with its Job name. The Windows Job handle closes with the backend.

## Execution journal (schema v28)

`executions` holds one row per worker process: every start and every continuation gets a new row. It does not replace `agents.status` or the [removal journal](agent-removal.md); it describes only the physical process.

| Column | Meaning |
| --- | --- |
| `agent_id`, `task_id`, `assignment_id`, `generation` | What the process worked for. The assignment of a new start is linked in the same transaction that creates it. |
| `controller_instance_id` | The backend run that launched it. |
| `containment` | `job_kill_on_close` for a verified Job; `none` without one (non-Windows, test doubles); `legacy_unknown` is reserved for history and never set by this step. |
| `job_name`, `job_config` | The Job's name and the limit flags read back from it. |
| `root_pid`, `root_created_at` | The root's identity (creation time as a FILETIME). |
| `state`, `evidence`, `uncertainty` | The lifecycle below, the observed evidence (JSON) and, when uncertain, the reason code. |

States written in this step:

| State | When |
| --- | --- |
| `launching` | Reserved before any process exists. A failed attempt closes it: `launch_aborted` when no process was created or termination was verified, otherwise `uncertain`. An abandoned one (crash, or a close that could not be written) stays open and is never treated as completed. |
| `contained` | Containment verified and committed. Treat it as possibly executed: a crash after resume but before `running` was written leaves it. |
| `running` | Resumed. |
| `ended_verified` | Stop or normal completion confirmed `ActiveProcesses == 0`. |
| `launch_aborted` | Resume never succeeded (no worker code ran) **and** the Job confirmed that no process remains. |
| `uncertain` | `termination_unconfirmed` (the Job's end could not be confirmed), `launch_abort_unverified` (a refused launch whose end could not be confirmed; `worker_code_may_have_run` in its evidence says separately whether it was ever resumed) or `unsupervised` (a process without a Job ended; nothing proves its descendants did). |

Restart recovery writes `ended_kernel_guaranteed` and `ended_by_reboot`. `attested_ended` is written only by a [human attestation](#diagnostics-and-human-attestation-schema-v29), and the database refuses it without a settled attestation record. `spawned` is reserved. Constraints keep the evidence honest: kernel-backed states require `containment = 'job_kill_on_close'`, a Job row requires its name, flags and root identity, an uncertain row requires a reason, and only ended rows have `ended_at`. Transitions are conditional, so concurrent writers (Stop and the watcher) record one end.

The v27 → v28 migration adds only these two tables. It changes no existing table and creates no row: no controller and no execution is fabricated for history. An older backend rejects v28; back up the database before upgrading.

**Ownership policy.** An execution is *unresolved* while its row is `uncertain`, for any reason. An observed root exit is not evidence that a process tree ended, so an execution without a Job stays unresolved even after its root exited. The moment a row becomes `uncertain`, the same transaction adds the hard blocker `process_ownership_unknown` to its Task, before the Agent's outcome is saved; it is lifted in the same way once every execution of the Task is settled from evidence. If that write fails, the emergency guard (below) already holds the Agent and Task, and finalization does not proceed: the Agent stays `running` with its assignment, claims and scopes until a later finalization retries the write successfully. An unrelated successful write or a read never stands in for it. Stop on such an execution is refused with "Stop was not confirmed" and saves nothing; Cancel, Pause and Remove treat it as still live, so none of them ends its assignment or releases its resources. While unresolved, its Agent and Task keep their assignment (even with the Agent stopped), worker claims and work-intent scopes; no new execution is admitted; assignments do not end through saves, Cancel, Pause or `end_assignment`; and no removal completes. The Task carries the hard blocker `process_ownership_unknown`.

## Status

`GET /system/process-supervision` returns one of:

- `{"status": "supervised"}`
- `{"status": "unavailable", "reason": "…"}`
- `{"status": "not_applicable"}` (non-Windows)

The status comes from a harmless self-test that runs once at backend startup: it checks the controller identity, then runs a supervised interpreter that exits immediately under a named Job. The GET endpoint never probes. The startup result does not replace per-execution verification: every launch is still verified individually.

## Codex sandbox compatibility

One local check was run with no model execution: `codex sandbox cmd /c echo sandbox-ok` inside a supervised Job. It exited `0`, printed `sandbox-ok` and left no process behind. This shows that the CLI's Windows restricted-token sandbox launcher works under a no-breakaway Job. It does not prove that a full `codex exec` session works.

**Prerequisites for the first controlled real-Codex E2E:**

- `GET /system/process-supervision` reports `supervised`.
- The run's output shows the supervision marker.
- Sandboxed tool execution inside the Job is confirmed during that run.

## Restart recovery

Restart recovery reads only the execution journal and fresh kernel observations. The supervision output marker is display only and is never evidence.

**Controller ownership.** At startup the backend claims the database in one `BEGIN IMMEDIATE` transaction: every earlier controller that did not stop cleanly must be proven gone by PID and creation time. One that is alive, or whose state cannot be established, refuses this startup; two controllers starting together cannot both succeed. An earlier instance in the same process is superseded. On non-Windows systems process identity cannot be verified, so after a crash (no clean stop recorded) startup is refused (fail closed); this is a known platform limitation.

**Decisions.** Each open row of a previous controller, and every unresolved row, is decided without holding a database transaction while processes are observed:

| Evidence | Outcome |
| --- | --- |
| The boot counter increased **and** the boot time moved past the controller's start (by at least a minute past the old boot time). Fast Startup keeps the old boot time, so it never counts; missing or conflicting values never count | `ended_by_reboot`, for any containment, including reservations and unsupervised or legacy work |
| A `launching` reservation without a verified restart | `uncertain` (`launching_unresolved`). A missing Job name proves nothing about it |
| Job evidence complete (name, read-back flags, root identity, verified assignment), same session and user SID as this backend, earlier controller proven gone, the Job name not found and the root (PID and creation time) gone | `ended_kernel_guaranteed` |
| The Job still exists (another handle keeps it): reopened with one handle; its limits equal the recorded ones, every member was created at or after the root and a live root is a member; then terminated through that same handle until no process remains | `ended_verified` |
| A never-resumed root outside any Job whose exact identity is gone, or is ended by verified PID and creation time | `launch_aborted` |
| Anything else: no Job (unsupervised), another session or user, a member older than the root, other limits, a live or unknown root without a Job, a probe failure | `uncertain`, with that reason |

`contained` is decided exactly like `running`: the worker may have resumed before `running` was written. The backend never terminates an object because its name matches: inspection and termination use the same handle, and nothing is terminated when identity is not established. Process scans are never consulted.

**Legacy work.** Running Agents with no journal rows, Tasks with an old `process_ownership_unknown` blocker and no rows, and removals that awaited confirmation without rows each get one `legacy_unknown` uncertainty row under a legacy controller that records today's boot evidence. No containment evidence is invented; a later verified Windows restart settles them. Rows that were legitimately settled before stay settled.

**After deciding.** Startup then marks running Agents stopped, keeps assignments with unresolved executions open, adds the `process_ownership_unknown` blocker to their Tasks and clears that blocker where every execution was settled from evidence. Removal members settle automatically when their Agent's executions are settled, even if it was executing when the backend stopped; otherwise they are `recovery_required` (`execution_unresolved`). Nothing is launched, resumed or reattached.

**Current-controller reconciliation.** The explicit recovery actions and removal retry first reconcile the executions they concern, without a restart: a tracked execution whose end was unconfirmed is only observed (its Job's accounting; termination stays with Stop); an untracked one, such as a refused launch, is decided as above with this backend as its controller; an emergency-guard entry whose journal write failed is decided from the identity it carries. A guard entry ends only after a verified settlement.

Restart recovery and reconciliation never attest anything. When evidence cannot settle an execution, the user can investigate it and, if eligible, take responsibility for it (next section).

**Residual risks.** Same-user code can open a named Job and keep it alive, or create a Job with a known name after ours was destroyed; it is outside this threat model. Inspecting a reopened Job takes a handle: if every other handle closes meanwhile, Windows' kill-on-close ends its members. A process created outside the Job by WMI, a service or the scheduler is never linked to an execution.

## Diagnostics and human attestation (schema v29)

When no Windows evidence can settle an unresolved execution, the inspector explains why, shows fresh diagnostics and, only when they allow it, lets the user accept responsibility for its end. An attestation is a recorded human decision, never technical proof: it settles as `attested_ended` with `technically_verified: false`, and the UI labels it "Released on your attestation", never "verified". `ended_verified`, `ended_kernel_guaranteed` and `ended_by_reboot` remain the only technical outcomes.

**Diagnostics (observations, never authority).** `execution_diagnostics.diagnose` inspects one execution without changing anything and classifies each candidate process:

| Class | Meaning |
| --- | --- |
| Verified owned | Its exact root (PID plus creation time) is alive, or it is a member of the execution's Job whose limits match the journal (opened with query access only). |
| Verified unrelated | Created before the execution started (with a 2-second clock margin), or running in another Windows session than its controller. |
| Ownership uncertain | Might belong to it and cannot be excluded: a descendant by parent PID, a Codex-like command line, a child of the controller PID, a member of an unverified Job, or a process whose creation time cannot be read. |
| No relevant candidate | This inspection saw nothing relevant. It is reported as "not proof". |

A PID is always paired with a creation time. Parent PIDs, command lines, output markers and a clean scan are never proof of ownership or of an end. Command lines are matched in memory only and never stored or returned, because they may carry secrets. Legacy executions have no start time, so no process can be excluded by age. Trees this backend still supervises are skipped. A failed scan makes the diagnostic incomplete; any other failure makes it failed; neither is ever clean. Diagnostics never terminate a process, never release ownership and are never an input to restart recovery.

**When diagnostics run (event-driven).** A bounded in-memory queue (256 entries) is fed when a termination cannot be verified, when an unsupervised end is recorded, when an aborted launch cannot be confirmed ended, at controller startup for every unresolved execution, and when a removal awaits an unresolved execution. One daemon thread started by the backend lifespan observes them. `POST /executions/{execution_id}/diagnostics` refreshes explicitly: it first reconciles from evidence, then observes. There is no polling, no worker interruption and no model call. A database transaction is never held while processes are observed.

**Eligibility.** The offer appears only when every condition holds. Each failed condition is a hard refusal, and no acknowledgment overrides it:

- the execution is still `uncertain` and this backend does not track it as live (`tracked_live`; use Stop);
- a diagnostic exists, completed and did not fail (`no_diagnostic`, `diagnostic_failed`, `diagnostics_incomplete`);
- no verified-owned process is alive (`verified_owned_alive`) and no uncertain candidate remains (`ownership_uncertain_candidate`);
- its Job Object no longer exists (`job_present`; recovery verifies and ends a live Job itself);
- the controller that launched it is verified to have exited (its PID now names no process with the recorded creation time), is a legacy placeholder, or is the exact running instance of this backend (`controller_inconsistent` otherwise). The exact instance knows in memory everything it still tracks, and a tracked execution is refused separately (`tracked_live`). An earlier controller instance inside the same OS process (`superseded_in_process`) is not verified to have exited and is refused, as is a controller that is alive or cannot be checked.

**Workflow.** `POST /executions/{execution_id}/attestation` takes exactly `{request_key, reviewed_diagnostic_id, statement, reason?}`. The server then:

1. replays a known `request_key` (same execution and review): a settled request returns its record and a refused one is refused again; another use of the key is rejected;
2. requires the exact acknowledgment statement and takes the per-execution lock and the Task lock without waiting. Every diagnostic of the execution (explicit refresh, event-driven worker, the attestation's own fresh check) runs under that same lock, so no observation is in flight while an attestation decides; a diagnostic requested meanwhile waits. If either lock is busy, it refuses with `concurrent_decision` (not recorded; nothing was evaluated);
3. refuses an execution that is no longer unresolved, an unknown or foreign review, a review that is no longer the latest (`stale_review`), older than 15 minutes (`review_expired`) or not eligible (`review_not_eligible`);
4. reconciles from evidence first. If that settles the execution, no attestation is recorded (`settled_by_evidence`);
5. takes fresh diagnostics (`attestation_recheck`) and refuses if they are not eligible;
6. in one `BEGIN IMMEDIATE` transaction, revalidates that the row is still `uncertain` with the same `updated_at` and that no launch reservation exists for its Agent or Task. It also revalidates the evidence: the fresh diagnostic must still be the newest one of the execution, taken by this controller as the attestation's recheck, and eligible both by its stored conclusion and by eligibility recomputed from its stored observations. Any newer diagnostic refuses it (`diagnostics_superseded`), whatever it says, so newer evidence of an owned or uncertain process, a live Job or a failed or incomplete inspection can never be overridden. It then inserts the settled record, transitions the row and releases the assignment, claims and work-intent scopes only when no other execution of the Agent or Task is unresolved. Any failure rolls back all of it.

Refusals are recorded. The client cannot send eligibility, and nothing runs at startup, during reconciliation or from agent output.

**Audit.** `execution_attestations` records each decided attempt:

- the attestation id, request key, execution, Agent, Task and the deciding controller;
- the prior state, reason and `updated_at`;
- the reviewed and fresh diagnostic ids, the actor, the statement and the optional note;
- the outcome (`settled` or `refused`) and the refusal reasons.

At most one `settled` record exists per execution. The actor is what the local API can honestly say: channel `local_api`, `authenticated: false`, origin, client host, user agent and the account running the backend, with a note that no user authentication exists. `execution_diagnostics` stores observations (sources and candidates with their provenance) separately from conclusions (eligibility). It keeps the last 20 per execution plus every one an attestation cites. Triggers on `executions` refuse `attested_ended` unless `attestation` names a settled `user_responsibility` record of the same execution. The v28 → v29 migration only adds these two tables, their indexes and the triggers; it is idempotent.

**Release conditions.** An attestation settles only its own execution. A Task or Agent with another unresolved execution keeps its blocker, assignment, claims and scopes until that one is settled too. A refused or failed attestation releases nothing.

**Residual races.** The fresh check observes processes without a database transaction (only the in-memory per-execution lock is held). A process that starts after that check and before the commit is not seen, but it could not belong to this execution: nothing is admitted while the execution is unresolved, and any reservation or journal write in between refuses the commit. The process list is a snapshot heuristic; a process created by WMI, a service or the scheduler is never linked to an execution. The operator's own verification is the residual assurance, which is why the statement says so.

**User interface.** The Agent inspector shows an inline "Process ownership" section only while the Agent has unresolved executions. Each run shows:

- its state label and a plain explanation: unsupervised run, missing Job evidence, legacy run, interrupted launch, unconfirmed termination, launching backend still alive, temporary failure, and a note for runs of an earlier backend that Fast Startup's Shut down is not a restart;
- the diagnostic summary with its age and a Refresh button;
- the observations, in a disclosure;
- the refusal reasons with next steps, or a collapsed "I verified it myself" disclosure with the statement, a required checkbox, an optional note and a confirm button.

There is no modal, results are announced politely, errors are alerts, and the section honors reduced motion and both themes. An unverified end is labeled "Process end unconfirmed" in the Agent list and is no longer marked as a lost watcher.

## Verification

`backend/test_executions.py` covers the journal constraints and migration (including rollback, idempotent re-run and no fabricated rows), controller identity, Job names (unpredictable, validated, collisions with Jobs and with other object types), limit read-back, closed handles, and real supervised launches through the store: evidence committed before resume, Stop and normal completion with a surviving descendant, a failed admission commit, a failed resume, incomplete controller identity, a name collision and a Redirect. Two tests kill a disposable helper backend with `TerminateProcess` before and after the admission commit: the suspended worker dies with its Job, never runs, and the journal keeps exactly the committed evidence. They use temporary databases and harmless local interpreters only, not Codex.

`backend/test_execution_recovery.py` covers the decision table with explicit fake probes (reboot evidence including Fast Startup and conflicting values, reservations, `contained`, namespace and identity mismatches, probe failures), controller claims, the startup ownership hold (assignment, claims, work intents, blocker, admission, Cancel, `end_assignment`), legacy classification, recovery racing an admission and automatic removal settlement. Real Windows tests kill a helper backend and decide its executions: a destroyed Job, a worker that really resumed, a Job kept alive by another process's handle (verified and terminated; the other process untouched), a tampered configuration and a foreign session (never terminated), a reused PID (the bystander untouched) and two controllers claiming one database at once. Tests that need a settled restart use an explicit fixture presenting verified reboot evidence (`execution_test_support.rebooted`).

`backend/test_execution_attestation.py` covers diagnosis with fake observations: PID reuse and creation times, parent chains, other sessions, Job evidence, controller states, and failed or incomplete scans. It also covers eligibility, and the attestation flow on temporary databases with a chosen process scan:

- settlement, audit and release only of what the execution held;
- refusals for fresh candidates, stale, expired, foreign or ineligible reviews;
- the exact statement, idempotent replay and concurrent decisions;
- a change, launch or reboot settlement after the fresh check;
- database failures during settlement and multiple unresolved executions on one Task;
- event-driven diagnostics that never settle, and the schema triggers and migration;
- the API (CSRF, extra fields, refusals and actor context).

Real Windows tests use disposable interpreters: an exact root and a reused PID, a real Job inspected with query access only, and a surviving descendant found by the real process scan. None of them ends a process. Test doubles are attested only by the fixture kind `test_fixture`, which the production schema refuses; the fixture widens the trigger of its own temporary database.
