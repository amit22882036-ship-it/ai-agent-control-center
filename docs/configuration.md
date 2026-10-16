# Configuration and troubleshooting

The Control Center needs no configuration for normal use. The backend reads environment variables when it starts, so set them in the PowerShell window before you launch Uvicorn. A variable set with `$env:NAME = "value"` lasts only for that window.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONTROL_CENTER_DB_PATH` | `data\control_center.sqlite3` in the repository | SQLite database with all Agents, Tasks, plans, delegations and output history |
| `CONTROL_CENTER_WORKSPACE_ROOT` | `%LOCALAPPDATA%\AI Agent Control Center\task-workspaces` | Parent folder for per-Task Git worktrees. It must not overlap a registered Project. |
| `CONTROL_CENTER_ALLOWED_ORIGINS` | `http://127.0.0.1:5173`, `http://localhost:5173`, and the backend's own origins on port 8000 | Comma-separated browser origins that may call the API. See [API security](api-security.md). |
| `CONTROL_CENTER_CODEX_MODEL` | Codex CLI default | Model passed to Codex for new executions and session resumes |
| `CONTROL_CENTER_CODEX_REASONING_EFFORT` | Codex CLI default | One of `none`, `minimal`, `low`, `medium`, `high`, `xhigh` |
| `CONTROL_CENTER_DELEGATION_DEPTH` | `4` | Maximum number of Parent-to-Child levels below a root Task |
| `CONTROL_CENTER_DELEGATION_CHILDREN_PER_PARENT` | `16` | Maximum **unfinished** direct Children of one Parent |
| `CONTROL_CENTER_DELEGATION_CONCURRENT_CHILDREN` | `4` | Maximum delegated Child workers running at the same time, across the whole backend |

Invalid values fail closed with an error that names the variable. A malformed model name or effort rejects the Codex launch. Blank model and effort values are treated as unset.

### Codex model and reasoning effort

Leave both unset unless you need them. They exist for accounts where the CLI's default model is unavailable, or for keeping experiments cheap:

```powershell
$env:CONTROL_CENTER_CODEX_MODEL = "<a model your account can use>"
$env:CONTROL_CENTER_CODEX_REASONING_EFFORT = "low"
```

The model must support the chosen effort. Model names may contain only letters, digits, `.`, `_` and `-`. The values are passed as fixed CLI options, while prompts still travel through stdin. Mock agents ignore both settings. Global and project Codex configuration files are never edited.

### Delegation limits

The three delegation limits measure different things. None of them caps the **total** number of Children over a Task's lifetime.

| Concept | Limited by | Counts |
| --- | --- | --- |
| Recursion depth | `CONTROL_CENTER_DELEGATION_DEPTH` | Task-parent edges from the root, which is at depth 0. With a depth of `1`, Children may not create Grandchildren. |
| Unfinished Children | `CONTROL_CENTER_DELEGATION_CHILDREN_PER_PARENT` | A Parent's direct Child Tasks that are not `completed` or `canceled` |
| Concurrent Children | `CONTROL_CENTER_DELEGATION_CONCURRENT_CHILDREN` | Delegated Child workers that are running or waiting, plus launches whose ownership is uncertain, across all Projects. Dormant Parents do not count. |
| Total Children | Not limited | Every Child a Parent ever had, including completed ones |

When the unfinished or concurrent limit is reached, the request is retained, not rejected. The next Child is attached or launched when capacity becomes available. A request that would exceed the depth limit is rejected. Per-Parent and per-tree round rate limits also apply, as described in [delegations](delegations.md#depth-and-operational-bounds).

For example, setting all three variables to `1` gives **one Child level, one unfinished Child at a time and one running Child at a time**. A Parent that promotes two responsibilities therefore runs them one after the other, with two Children in total. If you need exactly one Child, the run itself has to promote only one responsibility. The limits cannot enforce that.

## Where local state lives

| Data | Location | In Git? |
| --- | --- | --- |
| Database, including prompts and agent output | `data\control_center.sqlite3` and its `-wal`/`-shm` sidecars | No, ignored |
| Task worktrees | `%LOCALAPPDATA%\AI Agent Control Center\task-workspaces` | No, outside the repository |
| Codex credentials and settings | Managed by the Codex CLI in your user profile | No, never read or copied |

Workspaces and history are kept after agents stop, and the Control Center performs no automatic cleanup. Do not move or delete a Task worktree while its Task is active. To start from a fresh database without touching the existing one, point `CONTROL_CENTER_DB_PATH` at a new file before starting the backend.

## Troubleshooting

### Port 8000 or 5173 is already in use

Uvicorn reports that the address is already in use, or Vite refuses to start because of `--strictPort`. Find the process holding the port:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8000, 5173 | Select-Object LocalPort, OwningProcess
Get-Process -Id <OwningProcess>
```

If it is an earlier Control Center window, switch to that window and press `Ctrl+C`. Only stop processes you recognize. The backend has to use port 8000, because the dashboard calls `http://127.0.0.1:8000`.

### The dashboard shows the wrong agents or an empty list

The backend is probably using a different database, often because `CONTROL_CENTER_DB_PATH` is still set from a test. Check and clear it in the backend window, then restart:

```powershell
Get-ChildItem Env:CONTROL_CENTER_*
Remove-Item Env:CONTROL_CENTER_DB_PATH
```

Opening a new PowerShell window also clears variables set with `$env:`.

### "npm Codex CLI not found at %APPDATA%\npm\codex.cmd"

Install Codex with `npm install -g @openai/codex` using npm's default global prefix, then restart the backend. `npm config get prefix` should print the `npm` folder inside `%APPDATA%`, such as `C:\Users\<you>\AppData\Roaming\npm`.

### Codex starts but immediately fails

Run `codex login status` in a normal PowerShell window. If the error mentions the model or reasoning effort, unset `CONTROL_CENTER_CODEX_MODEL` and `CONTROL_CENTER_CODEX_REASONING_EFFORT`, or choose values your account supports.

### Project registration fails

The path must be the root of an existing Git working tree with at least one commit. It must not overlap another registered Project or the workspace root.

### "Task Workspace Git operation failed"

Check that the Project is still a valid Git repository with a commit. On Windows, this error also appears when the combined path is too long: each Task worktree lives at `<workspace root>\<Project ID>\<Task ID>`, which adds about 75 characters. During verification, a 150-character workspace root failed with Git's `Filename too long`, while the default `%LOCALAPPDATA%` location worked. Keep `CONTROL_CENTER_WORKSPACE_ROOT` and Project paths short.

### Agent launches are refused with a supervision error

`GET http://127.0.0.1:8000/system/process-supervision` should return `supervised` on Windows. If Job Object supervision cannot be established, launches fail closed rather than running unsupervised. See [process supervision](process-supervision.md).

### State-changing API calls from scripts return 403

Every `POST`, `PUT`, `PATCH` and `DELETE` needs the CSRF header. See [scripting the API](api-security.md).
