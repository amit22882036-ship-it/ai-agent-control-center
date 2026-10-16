# Process supervision (Windows)

On Windows, every agent execution runs under its own Job Object. This covers each start and each continuation: Reply, Redirect, Decide, Similar, Always and Parent continuation. Mock workers are supervised the same way. The implementation is in `backend/app/process_supervision.py`; it uses the standard library's `ctypes` and adds no dependencies.

## How an execution starts

1. The worker is created with `CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP`.
2. A dedicated anonymous Job Object is created with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Breakaway is not allowed: neither `BREAKAWAY_OK` nor `SILENT_BREAKAWAY_OK` is set.
3. The process is assigned to the Job, and `IsProcessInJob` verifies the assignment.
4. Only then are its threads resumed (Toolhelp thread snapshot and `ResumeThread`). The prompt is written to stdin afterwards, as before.

No worker code runs outside its Job, and descendants inherit it. Breakaway attempts are refused (`ERROR_ACCESS_DENIED`). A descendant that breaks away from an inner Job, such as one created by a launcher, still remains in the execution's Job.

**Fail-closed.** If Job creation, assignment, verification or resume fails, the still-suspended process is terminated and its handles are closed. The launch is then refused with the existing unavailable response. Nothing runs unsupervised, and there is no opt-out switch.

The output of each supervised execution includes `--- Process supervision: active (job object) ---`.

## How an execution ends

- **Stop, Stop Branch, Redirect, Pause/Cancel and shutdown:** these use `TerminateJobObject` and confirm `ActiveProcesses == 0`. This includes detached or orphaned descendants that `taskkill /T` cannot see. If termination cannot be confirmed, the existing behavior applies: "stop was not confirmed", and the execution gate is retained.
- **Normal completion:** when the main process exits, the watcher closes the Job, which ends any remaining descendants. A descendant that inherited the output pipe can therefore no longer keep the agent `running`.
- **Backend crash or kill:** Windows closes the Job handle, and every supervised tree is terminated.
- **Ctrl+C in the backend console:** workers run in their own process group, so they do not receive Ctrl+C. Ctrl+C cannot end a worker early and trigger an Always/Similar continuation. Shutdown stops workers without automatic continuation.

`taskkill /T` remains only for processes without a Job. Linux and macOS are unchanged.

## Status

`GET /system/process-supervision` returns one of:

- `{"status": "supervised"}`
- `{"status": "unavailable", "reason": "…"}`
- `{"status": "not_applicable"}` (non-Windows)

The status comes from a harmless self-test that runs once at backend startup: a supervised interpreter that exits immediately. The GET endpoint never probes. The startup result does not replace per-execution verification: every launch is still verified individually.

## Codex sandbox compatibility

One local check was run with no model execution: `codex sandbox cmd /c echo sandbox-ok` inside a supervised Job. It exited `0`, printed `sandbox-ok` and left no process behind. This shows that the CLI's Windows restricted-token sandbox launcher works under a no-breakaway Job. It does not prove that a full `codex exec` session works.

**Prerequisites for the first controlled real-Codex E2E:**

- `GET /system/process-supervision` reports `supervised`.
- The run's output shows the supervision marker.
- Sandboxed tool execution inside the Job is confirmed during that run.

## Restart recovery

There is no schema migration. At startup, each Agent still recorded as `running` is checked for supervision evidence for its last execution: the supervision marker at its exact expected position.

- A first launch writes it as output line 0.
- A resumed execution writes `[resume marker, text, supervision marker]`.

The marker is written only after `IsProcessInJob` succeeded, and it is persisted in the same transaction as the `running` status.

**With evidence:** the process tree ended with the backend, so the Agent is marked stopped, as before.

**Without evidence:** the process may still be running.

- The Agent is marked stopped with an explicit output note.
- Its Task gets the hard blocker `process_ownership_unknown`. That blocks new workers and continuations (`require_active`) and integration.
- `POST /tasks/{task_id}/process-ownership/confirm-stopped` with `{"confirm_ended": true}` clears the blocker. It requires no live handle and a clean process scan (see [explicit recovery actions](delegations.md#explicit-recovery-actions)).

**Spoofing caveat:** worker output is controlled by the worker. A worker that deliberately prints a resume marker, a line of text and the supervision marker could fake evidence for a later restart. The position rule narrows this, but only a persisted per-execution column (a migration) would remove it.
