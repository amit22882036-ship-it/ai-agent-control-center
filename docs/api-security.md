# Local API security

The backend is a local, single-user service without authentication. These guards protect it from **other websites open in the same browser** (cross-site request forgery and DNS rebinding). They do not authenticate other local processes: any program running as the same Windows user can still fetch the token and call the API.

All guards are pure ASGI middleware in `backend/app/security.py`. The order, outermost first, is Host validation → CORS → request guard. They reject a request **before** any route handler runs, and they stream responses unchanged, including Server-Sent Events (SSE).

## Request rules

| Check | Applies to | Rejection |
| --- | --- | --- |
| `Host` is exactly one header naming `127.0.0.1`, `localhost` or `[::1]` (any port) | Every HTTP and WebSocket request, including `/docs`, `/events` and preflight `OPTIONS` | `421 invalid_host` |
| If `Sec-Fetch-Site` is present and is neither `same-origin` nor `none`, an allowed `Origin` is required | `GET` and `HEAD` | `403 cross_site_forbidden` |
| A present `Origin` must be allowed | `GET /security/csrf-token` | `403 cross_site_forbidden` |
| A present `Origin` must be allowed (`null` never is) | `POST`, `PUT`, `PATCH`, `DELETE` | `403 origin_rejected` |
| Exactly one `X-Control-Center-CSRF` header equal to the application token | Every `POST`, `PUT`, `PATCH` and `DELETE`, **with or without a body** | `403 csrf_missing` (header absent) or `403 csrf_invalid` (wrong, empty or repeated) |
| Body is at most 1 MiB | Same | `413 body_too_large` |
| A non-empty body, or any `Content-Type`, must be `application/json` (optional `charset=utf-8`) | Same | `415 unsupported_media_type` |

How the body limit is enforced: an oversized `Content-Length` is rejected before the body is read. Chunked or under-declared bodies are counted while being buffered and are then replayed unchanged to the application. An invalid `Content-Length` gets `400`.

Checks run in table order, so a token problem is reported before a media-type problem. Errors use `{"detail": "...", "code": "..."}`. Rejections are logged with method, path and code only, never header values.

`Sec-Fetch-Site: cross-site` alone does not reject a state-changing request. The dashboard at `http://localhost:5173` is cross-site to `http://127.0.0.1:8000`; the CSRF token is the control. A missing `Origin` is never treated as trusted, and no path is exempt, including requests from `/docs`.

`GET` requests carry no token, because `EventSource` cannot send custom headers. They are protected by the `Host` check (against DNS rebinding), the Fetch-Metadata/`Origin` rule above, and CORS (cross-origin pages cannot read responses).

## Allowed origins

By default:

- `http://127.0.0.1:5173` and `http://localhost:5173`: the Vite development server
- `http://127.0.0.1:8000` and `http://localhost:8000`: the backend's own `/docs` page

`CONTROL_CENTER_ALLOWED_ORIGINS` replaces this list with a comma-separated list of exact origins. It is used for both the guard and CORS. For example, `vite preview` on port 4173 is not allowed unless you add it explicitly. CORS allows `GET`, `POST`, `PUT`, `PATCH` and `DELETE`, and the `Content-Type` and `X-Control-Center-CSRF` request headers.

## CSRF token

- Each backend process creates a random token (`secrets.token_urlsafe(32)`) at startup. It is held only in memory. It is never written to SQLite, logs, URLs or browser storage. Restarting the backend invalidates it.
- `GET /security/csrf-token` returns `{"csrf_token": "..."}` with `Cache-Control: no-store`.
- The dashboard's `frontend/src/apiClient.mjs` (`apiFetch`) fetches the token once per backend origin and adds the header to unsafe methods only.
- **Retry rule.** `apiFetch` refreshes the token and retries at most once, only after an explicit HTTP `403` whose JSON `code` is `csrf_missing` or `csrf_invalid`. The backend sends those codes before executing anything. It never retries after a network error, timeout, abort, `5xx`, any other `403`, any other status, or an unreadable body, because the action might already have run.

### Scripts and `/docs`

Fetch the token first, then send it as a header:

```powershell
$token = (Invoke-RestMethod http://127.0.0.1:8000/security/csrf-token).csrf_token
Invoke-RestMethod -Method Post http://127.0.0.1:8000/agents/<agent-id>/stop -Headers @{ 'X-Control-Center-CSRF' = $token }
```

In `/docs`, choose **Authorize**, paste the token value, then use **Try it out**. The OpenAPI schema marks every state-changing operation with the `csrfToken` API-key header.

## Read-only GET requests

Viewing an agent does not finalize a process that its watcher thread owns. The watcher alone performs finalization, automatic Always/Similar Decision continuation, and delegation scheduling.

If a read observes an exited process that has no live watcher, it settles only the visible status. It never runs automatic continuation, structured-execution handoff or delegation scheduling. If an automatic decision would apply, the read leaves the episode untouched for its owner.

### Watcher failure

A watcher thread that ends without finalizing its exited process, for example because `process.wait()` raised, leaves no owner.

- **Detection.** A sweep every 30 seconds (started by the backend lifespan only) detects such executions. It only marks them:
  - logs the failure once,
  - appends `--- Process watcher lost; explicit recovery required ---` to the output,
  - exposes `watcher_lost` in `GET /agents/{id}`,
  - moves the Agent Task's own continuation to `recovery_required` with that `last_error`.

  The sweep never finalizes, never runs a structured handoff, never launches and never wakes anything. An execution whose end could not be verified (or not yet recorded) is not a lost watcher: it is held by the [ownership policy](process-supervision.md#execution-journal-schema-v28), shows `--- Process tree end not verified: its Task and resources stay held. See Process ownership in the inspector. ---` and is never marked by the sweep.
- **Reads.** A read of a regular (non-structured) execution may still settle its visible status, as before, without automatic continuation or scheduling.
- **Recovery.** `POST /agents/{agent_id}/recovery/handoff` (CSRF-protected) is the explicit recovery. It runs the execution's finalization once: for a structured execution that is the durable handoff, guarded by `context.processed` and a per-execution attempt flag. It never performs automatic Always/Similar continuation, and every wake it would raise is suppressed. If it fails, the Agent is stopped with a visible reason and the action is not repeated.
- **Parent.** The Parent continues only through `POST /tasks/{task_id}/orchestration/wake`.

### Responsibility routes

`GET /tasks/{task_id}/responsibility` is read-only. `POST /tasks/{task_id}/responsibility/transfer` is CSRF-protected like every unsafe route and rejects unknown fields; see [Responsibility transfer](agent-removal.md#responsibility-transfer-stage-2d2).

### Execution recovery routes

`GET /executions/unresolved?agent_id=…|task_id=…` and `GET /executions/{execution_id}` are read-only. `POST /executions/{execution_id}/diagnostics` (refresh) and `POST /executions/{execution_id}/attestation` are CSRF-protected like every unsafe route. The attestation body rejects unknown fields, so a client cannot send an eligibility or verification claim. Refusals are `409` with `{"code": "attestation_refused", "reasons": [...]}`. Malformed requests are `409` with `attestation_invalid`, and database failures are `503`. See [Diagnostics and human attestation](process-supervision.md#diagnostics-and-human-attestation-schema-v29).

### Known read-side writes

None of these starts an agent or provider work:

- `GET /agents`, `GET /agents/{id}` and `GET /agents/{id}/output` retry persistence of already-captured output if an earlier write failed. An unwatched or watcher-failed exit is settled to its waiting, finished or stopped status.
- `GET /tasks/{id}/workspace` runs Git (`rev-parse`, `worktree list`, `ls-files`, and object hashing into a temporary index) against the Project root and Task workspace. `git hash-object -w` and `write-tree` add content-addressed objects to those repositories' object stores. Refs, indexes and working files are not changed. The dashboard does not call this route.

## Verification

- `backend/test_api_security.py`:
  - the reviewed inventory of every unsafe route and every `GET` route
  - rejection without execution for each unsafe route, including the exact codes
  - cross-site Fetch-Metadata accepted with a token on unsafe requests
  - the `GET`/`HEAD` Fetch-Metadata rule
  - `Host`, CORS preflight, token endpoint, logs and OpenAPI
  - the 1 MiB limit by header and by streamed bytes
  - `Content-Type` rules
  - the installed FastAPI behavior: an unguarded instance returns `422` for model bodies sent without JSON, but runs body-less routes for any request
  - SSE through the guards
- `backend/test_read_side_effects.py`:
  - every `GET` route and manager read path, against a real temporary SQLite database (database bytes unchanged, no agent process launched)
  - the guarantee that the watcher-owned Always/Similar continuation still happens exactly once
  - real reader/watcher threads driving fake processes: exactly one continuation, no launch or duplicate under concurrent reads, and the failed-watcher report and settlement
- `frontend/tests/apiClient.test.mjs`: header placement, the single shared token request, and the retry and no-replay rules.

Backend dependencies are pinned in `backend/requirements.txt` to the versions verified here. Re-run these tests when upgrading FastAPI, Starlette or Pydantic.
