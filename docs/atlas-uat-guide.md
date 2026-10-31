# Atlas user acceptance testing on Windows

A step-by-step guide for the owner. Nothing here has been carried out yet; every step is for a
person at the Windows machine. Write down each result as **MANUAL TEST PASSED**, a finding, or
**BLOCKED**. The automated fixture results are in [the readiness review](atlas-m11-readiness.md).

Three phases, in this order. Phases 1 and 2 only read. Phase 3 changes data and needs the owner's
separate, explicit go-ahead before it starts. Real providers are not part of this guide.

## Before you start

- Atlas is opt-in: open the dashboard (<http://127.0.0.1:5173>) and choose **Atlas preview** in the
  top bar. **Classic view** returns. Nothing in either phase makes Atlas the default.
- **Do not stop or restart the backend during phases 1 and 2.** Stopping it while agents run ends
  their processes and starts recovery on the next start. If the backend is already running, use
  it as it is; if it is not running, start it as the README describes, without changing anything.
- Use Microsoft Edge first, then Chrome for the steps marked *both browsers*.
- **Which backend is running.** To see what listens on port 8000 without touching it, in any
  PowerShell window:

  ```powershell
  $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
  $listener | Select-Object LocalAddress, OwningProcess
  if ($listener) { Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)" | Select-Object ProcessId, CommandLine }
  Invoke-RestMethod http://127.0.0.1:8000/health
  ```

  Nothing listed means no backend runs. The command line shows which Python and Uvicorn started
  it; the database it uses is whatever `CONTROL_CENTER_DB_PATH` was in the window that started it
  (the repository's `data\control_center.sqlite3` when unset). These commands only read.

## Phase 1: read-only review (real data, nothing changed)

Do not press anything that starts, stops, replies, pauses, resumes, cancels, removes, registers,
installs, signs in or out, checks, refreshes models or usage, or saves. Those controls are listed
under "Do not use" below. Opening pages, selecting tasks, switching views, filtering, searching
and loading older pages only read. **Allowed:** appearance (Light, Dark, Automatic), the details
panel width and other layout choices; they are stored in this browser only and change no runtime
data.

| # | Step | Expected |
| --- | --- | --- |
| 1 | Open the dashboard without an Atlas address | Classic view opens (the default) |
| 2 | Choose **Atlas preview**; then **Classic view**; then the browser's Back | Each switch works; Back returns to the Atlas page |
| 3 | Work: compare the number of task families and "need you" with Classic | They agree; counts labelled "loaded" when older pages exist |
| 4 | Select several tasks, including one with helpers, one finished and one whose agent was replaced or removed | The details panel shows the current agent, or the last one labelled as history |
| 5 | Press **M** and **L**; select a task in the Map | The same task stays selected in both views |
| 6 | **Load older tasks** until none remain (if any) | Older tasks appear in place; nothing jumps |
| 7 | Open a task page (**O**); check request, status, team, result statements | Matches what Classic shows for the same task; nothing claims "verified" |
| 8 | Projects: each registered project with its picture and counts | Every real project appears; paths with spaces or Hebrew display correctly |
| 9 | Connections: each tool's five facts, usage and billing guidance | The state matches Classic's AI Providers view; nothing is checked unless you click |
| 10 | Settings: Light, Dark, Automatic (follows Windows) | The theme changes and is kept after reload, in Classic too |
| 11 | Resize the details panel; reload | The width is kept |
| 12 | Back and Forward across Work, a task page, Connections, Settings (*both browsers*) | Each returns to where it was |
| 13 | Windows display scaling 150 % and 200 %, browser zoom 200 % | Nothing is cut off; the navigation moves to its own row when narrow |
| 14 | A task with an earlier run: details panel, Process ownership | Shows the real state; nothing is released or attested |
| 15 | Notifications bell: open, Escape | Lists what needs you now; focus returns to the bell |

**Do not use in phase 1:** Start task, Add a helper, Start again, Send reply, Decide for me, any
"Decide similar/always", Send correction, Stop agent, Stop branch, Pause, Resume, Cancel task,
Review removal, Choose a new owner, Release/attest in Process ownership, Register a project,
Install/Update/Sign in/Sign out, Check all, Check again, Check for updates, Refresh models, Refresh
usage, Clear a limit, Turn on/off, billing confirmations, Allowed tools and defaults, Remind me
later, Dismiss, and the notification switch (it changes the backend's notification setting).

## Phase 2: screen-reader review (real data, nothing changed)

With NVDA (or Narrator) and Edge, keyboard only. Do not submit any form; for New task, type into
the description and leave the page without starting.

1. Open Atlas. Expected: the page title and the "Work" heading; Tab reaches "Skip to content".
2. Work: Tab to the task tree. Arrows move, Right/Left open and close, Enter selects. Each row reads
   its title, status and agent; the details panel and its tabs are announced.
3. If anything needs you, press **A** to move to the next such task (the bar announces "1 of N");
   if that task asks a question, **R** puts the cursor in "Your answer" (type nothing, send
   nothing). If nothing needs you, the bar is absent: record that and skip this step.
4. Open the bell with Enter: the "Notifications" dialog and its sections; Escape returns to the bell.
5. **M** for the Map: nodes reachable with the arrows, each read with title and status.
6. **O** for the task page: request, status and the separate result statements in order.
7. **N** for New task: description, project picker (Escape returns to it), tool choices, helpers
   switch, summary. Leave without starting.
8. Connections: each tool's facts read as separate statements. Settings: each section heading.
9. Turn on reduced motion in Windows (Settings, Accessibility, Visual effects) and repeat step 5.

Record anything not announced, announced twice, read out of order, or where focus was lost.

## Phase 3: isolated Mock workflows (needs the owner's go-ahead first)

These steps start Mock agents and change data, so they run against a **separate database,
workspace root and disposable project**, never the real ones. Because the dashboard always talks
to `127.0.0.1:8000`, the real backend must not be running during this phase. **Only you decide
when to stop it**, after making sure none of your agents is running (Classic and Atlas both show
running agents). Restart it afterwards in its usual window, without the variables below.

Before this phase, use "Which backend is running" above. If the real backend is listening, it is
yours to stop or leave: if you stop it, press Ctrl+C in **its own** window after Work shows no agent
working, and never delete or move its database. The steps below never touch it.

Setup, in a new PowerShell window in the repository root (example paths; any short paths work):

```powershell
$uat = "$env:LOCALAPPDATA\cc-uat"
New-Item -ItemType Directory -Force "$uat\project" | Out-Null
git -C "$uat\project" init
Set-Content "$uat\project\README.md" "UAT scratch project"
git -C "$uat\project" add .
git -C "$uat\project" -c user.name="UAT" -c user.email="uat@example.invalid" commit -m "UAT scratch"
$env:CONTROL_CENTER_DB_PATH = "$uat\control_center.sqlite3"
$env:CONTROL_CENTER_WORKSPACE_ROOT = "$uat\ws"
cd .\backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check in the backend window that it started with the new database (`Get-ChildItem
Env:CONTROL_CENTER_*` in that window lists both variables). Then, in Atlas:

| # | Step | Expected |
| --- | --- | --- |
| 1 | Projects: register `%LOCALAPPDATA%\cc-uat\project` | Registered; it is the only project listed (the database is the new one) |
| 2 | New task with **Practice (Mock)**, read only, helpers off; Start | Work opens on the task; the agent shows "Working", and about 10 seconds later "Agent finished" |
| 3 | Start another Mock task, press **O** at once, and choose **Pause…** within about 10 seconds; confirm | The impact lists this task and its running agent; afterwards the status is "Paused" and the agent is stopped. If the Mock run ended first, the page offers no Pause and says "Finished work can’t be paused, resumed or canceled": start another one and be quicker |
| 4 | On the paused task: **Resume…**, confirm | The message reads "Resumed. The tasks are back to where they were; start or reply to an agent to continue."; the status is no longer "Paused"; no agent starts by itself |
| 5 | **Cancel task…**, tick the acknowledgement, confirm | The status is "Canceled"; the task stays in Work under history |
| 6 | Settings: turn notifications on; allow the browser prompt | Settings says "As browser notifications from this page" (after the next change, for example starting a task) |
| 7 | Start a Mock task and wait for it to finish | One "Agent finished" browser notification for it; clicking it opens that task in Work |
| 8 | Start a Mock task, then close every dashboard tab before it finishes | About 8 seconds after it finishes, a Windows notification "Agent finished" can appear, if Windows allows notifications for it and Focus assist is off |
| 9 | Focus assist on, repeat 7 and 8 | Windows holds or hides them; the dashboard never says one was shown |
| 10 | Classic in one tab and Atlas in another, same browser; start a Mock task | One "Agent finished" browser notification in total (option B); the other tab's notification setting says another tab shows them |
| 11 | Same as 10, with notifications blocked for the site in one of two browsers | That browser shows none; the other one, or Windows, does |
| 12 | Edge and Chrome both open on the dashboard with notifications on in both; start a Mock task | Each browser may show its own notification: tabs are coordinated within one browser only (expected, documented) |

The Mock provider only prints six progress lines about two seconds apart and finishes: it never
asks a question and never brings in helpers. Replying, "Agent needs your input" notifications and
autonomous delegation can't be exercised with it; they are covered by the fixture tests and need a
real provider, which is outside this guide.

Afterwards: press Ctrl+C in this UAT backend's window and close it (it used only the `cc-uat`
database). Start the real backend as usual, in a window where `CONTROL_CENTER_DB_PATH` and
`CONTROL_CENTER_WORKSPACE_ROOT` are not set (`Get-ChildItem Env:CONTROL_CENTER_*` lists nothing).
`%LOCALAPPDATA%\cc-uat` holds only UAT data; delete it if you no longer need it.

Real-provider tests are not part of this guide and need their own explicit authorization.
