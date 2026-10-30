# Atlas migration (Stage 2J)

Atlas is the new dashboard design selected in Stage 2J. It is being built next to the existing
("classic") dashboard in reviewable milestones. The classic dashboard remains the default and keeps
all of its functionality until Atlas reaches functional and visual parity and its removal is
explicitly approved.

The approved visual reference is the Atlas V2 prototype in [design/atlas-v2](design/atlas-v2/README.md),
with a few representative screenshots. It uses fictional data; the implementation uses only what
the backend reports.

## Milestones

| Milestone | Scope | State |
| --- | --- | --- |
| M0 | Baseline audit, existing test results, synthetic fixtures | Done |
| M1 | Design tokens, local fonts, primitives, application shell, routing, Atlas/classic switch, component gallery | Done |
| M2 | Canonical Task/Agent model; read-only task projection (B1, separately approved) | Done |
| M3 | Work list and read-only inspector | Done |
| M4 | Agent actions and management in the inspector | Done |
| M5 | New Task and Projects; Add a helper; Start again; Work colour filter | Done |
| M6 | Work Map beside the List; project hierarchy previews; family context; design continuity review | Done |
| M7 | Task pages: request, progress, team as List and Map, result review, Pause/Resume/Cancel | Done |
| M8 | Connections (full parity with Classic's AI Providers view) and Settings | Done |
| M9 | Notifications and attention: the bell, provider update notices, "next that needs you", reply from Work, browser notifications | Implemented, awaiting review |
| M10–M11 | Accessibility, responsiveness and performance; parity review and default switch | Not started |

Legacy removal is a separate, explicitly authorized final step. The native folder picker is a
separate milestone after the core migration.

## Opening the Atlas preview

Start the backend and the frontend as described in the [README](../README.md#quick-start-windows),
open the dashboard and choose **Atlas preview** in the top bar. Atlas addresses start with `#/`
(for example `http://127.0.0.1:5173/#/work`). Choose **Classic view** to return; the browser's
Back button returns to the Atlas page you left. Opening the dashboard without an Atlas address
always shows the classic dashboard.

The preview contains the shell, navigation, appearance settings and a design gallery (`#/gallery`,
sample content only); Work as a List and as a Map with the task details panel and its agent
actions (M3, M4, M6); New task and Projects (M5); a page for every Task (M7); Connections and
Settings (M8); and notifications and attention (M9). Every
state-changing request goes through `api/agentClient.mjs` with the classic endpoints and payloads.

## How Atlas stays isolated

- **Code:** everything lives in `frontend/src/atlas/`. The only classic files touched are
  `main.jsx` (choosing the interface), `WorkspaceBar.jsx` (the **Atlas preview** button) and,
  since M3, `App.jsx`, which reads the Atlas hand-off below once when it opens.
- **Loading:** Atlas is a separate chunk loaded only when someone opens it, so the classic bundle's
  CSS is unchanged.
- **Styles:** every Atlas rule is scoped to the `.atlas` root and Atlas classes use an `a-` prefix.
  A unit test fails if any selector is unscoped, if keyframes or font families could collide, or
  if a stylesheet references a remote URL.
- **Fonts:** Hanken Grotesk, Heebo (Hebrew) and JetBrains Mono are served from local WOFF2 files
  under the SIL Open Font License 1.1 (`frontend/src/atlas/fonts/`); no font service is contacted.
- **Live updates:** only one interface is mounted at a time, and Atlas opens its single event
  connection through the existing `connectAgentEvents`; there is never a connection per view.
- **Appearance:** Atlas reads and writes the same appearance preference as the classic dashboard.

## Task model (M2)

Atlas reads Tasks, not Agents. `GET /workspace/tasks` (B1, documented in the
[dashboard read models](dashboard.md#read-models)) returns Task pages with the ancestors needed to
place them. `frontend/src/atlas/model/` turns those pages into one normalized store that every Atlas
view will share:

- `tasks.mjs`: normalization into Tasks, assignments and Agents; merging pages; the Task forest
  (edges only from `parent_task_id`), with each top-level entry marked as a root, a Task whose
  parent is not loaded yet (more pages exist), a Task whose existing parent the backend could not
  include (`parent_unresolved`), a Task whose parent does not exist, or part of a cycle; loaded paths
  and per-family counts, including children reported but not loaded.
- `taskStatus.mjs`: one status per Task from one precedence order (attention, canceled,
  integrated or completed, paused, waiting for a new owner, the backend's waiting reason, reviewing,
  working, not started, stopped). Integration, recorded results, helpers and planned work are
  separate views. Nothing is inferred from Agent text, no percentage is shown, "stopped" never
  means failed and finished work is never called verified.
- `selection.mjs`: selection and expansion by Task id, kept in the address (`#/work?task=…`).
- `taskSource.mjs`: reads every page through the shared `apiFetch` client. It opens no event
  connection; the shell's single connection decides when to read again.

## Work list and inspector (M3)

`#/work` shows every task family from `GET /workspace/tasks`, one card per family, grouped into
**Needs your attention**, **In progress** and **Completed and history** from the status model.
Hierarchy lines come only from `parent_task_id`; planned work is a count on its lead, never a row.
Each row shows the task, its current agent or, marked as such, its last one (replaced and removed
agents included, or "No agent"), what is happening now and an honest time. Completed work says
whether it was combined into its lead or applied to the project, and that it was not
independently verified.

- **Find and filter:** All, Needs you, In progress and Done; one project or all; search over the
  loaded tasks (titles, agent names, tools and models), which opens the path to each match. The
  project menu lists every registered project from `GET /projects`, including projects with no
  loaded task, and says so while the list is loading, when none is registered or when it can't be
  read; loaded tasks never stand in for it. While older pages remain, the section counts are
  marked as covering the loaded tasks only ("Counts cover the 200 loaded tasks of 1,234"); no
  page is fetched just to complete a count.
- **Pages:** the newest 200 tasks load first; **Load older tasks** adds a page and later refreshes
  keep it. A lead on a page that isn't loaded, a lead that couldn't be loaded and a lead that no
  longer exists are each said plainly; nothing is attached to a guessed parent.
- **Keyboard:** the list is one WAI-ARIA tree with one Tab stop. Up and Down move, Right and Left
  open, close or move to the parent, Home and End jump, Enter selects.
- **Selection:** the selected task is in the address (`#/work?task=…`, with `filter` and
  `project`), so Back, Forward and reloads restore it. A task that isn't loaded yet or no longer
  exists is explained; nothing else is selected in its place. A selection hidden by a filter stays
  open with a **Show it** action.
- **Inspector:** Now (state notes, the question asked, responsibility, family, delegated work,
  planned work, result and integration), Activity (agents on the task and recorded activity),
  Output (the latest 200 lines) and Details. It reads the existing `GET /tasks/{id}`,
  `/tasks/{id}/assignments`, `/agents/{id}/activity` and `/agents/{id}/output`. Activity of an
  agent that has since moved to other work is not shown as this task's. Every action stays in
  Classic view for now, and the inspector says so.
- **Opening Classic from a task:** when the selected task has a current agent that is not
  removed, **Open Classic view** leaves a short-lived (30 second), one-shot record of that agent in
  this tab's session storage and Classic opens with it selected; Classic clears the record as it
  reads it. A task with no current agent (its last agent finished, was replaced or was removed)
  opens Classic without a selection, and the inspector says which will happen. The top bar's
  **Classic view** never carries a selection.
- **Live updates:** the shell's single event connection bumps a version; the list re-reads once per
  burst, reads never overlap, an older answer never replaces a newer one, and a failed read keeps
  the last list with a notice. Selection, expansion and scroll survive refreshes.
- **Rendering:** measured in Microsoft Edge with 1,000 tasks fully expanded (about 22,800 DOM
  nodes): the list appears in under a second including page load, a keyboard step takes about
  65 ms and selecting a task about 170 ms, so the list is not virtualized.

## Agent actions (M4)

The inspector acts on the selected task's **current** agent only (`model/agentTarget.mjs`): a
replaced, ended or removed agent is history, and a task without a current agent offers no agent
action. Each control appears under the same condition as in the classic inspector
(`model/agentCapabilities.mjs`) and reads the agent's own read model (`GET /agents/{id}`); the
backend still decides and its refusals are shown in its own words. Every request goes through
`api/agentClient.mjs`, the only Atlas module that changes state, with the shared `apiFetch`
client and the classic endpoints and payloads. One action runs at a time per inspector, a second
click sends nothing, and each finished action re-reads the agent and the task list. A request is
not sent if, according to the latest task list Atlas has read, the task's agent changed after the
controls were drawn. That check uses local state, not a fresh server read; the backend's own
eligibility and identity checks remain the authority and refuse stale requests. A reply or
correction written for an agent that was replaced meanwhile is refused once with an explanation and
kept. For a task's last agent (history), Atlas keeps the recovery the backend supports: settling
its unresolved runs and retrying its blocked removal.

| Classic capability | Atlas (M4) |
| --- | --- |
| Reply, with a one-time model or effort, explicit attempt and new-session confirmation | Now: question card |
| Decide for me; decide similar; always decide; turning those off | Now: question card; Settings: Questions |
| Redirect, with the same one-time options | Now: Redirect |
| Stop; Stop branch (agent lineage), partial results by name | Footer, each with a confirmation that it does not pause or cancel the task |
| Provider wait: facts, explicit retry, unverified default attempt, Resume; Claude usage refresh | Now: provider card |
| Change provider, model and access while waiting | Now: Switch tool or model |
| Agent defaults (model and effort), latest execution facts, defaults history | Settings: Model and effort |
| Auto allocation decision, held children, Auto policy editor (providers, models, preferences, profiles) | Settings: Helpers |
| Provider restrictions of the task subtree | Settings: Provider permissions |
| Rename, colour label, name history (also for an agent that is history, labelled as such) | Settings: Name and colour |
| Removal: impact review, single or branch, reason, confirmation bound to the reviewed impact, stale impact, blocked removal and retry | Settings: Remove agent (dialog); Now: removal progress |
| Responsibility transfer to a new owner, completed child work guidance | Now: Task responsibility |
| Process ownership: unresolved runs, diagnostics refresh, observations, human attestation | Now: Process ownership |
| Output history with earlier pages; technical details and provider changes | Output; Settings: Technical details |

Not in M4, as planned: starting a child agent ("Add a helper", M5), the hierarchy explorer (Map,
M6), task Pause and Cancel with their impact preview (Task page, M7; Classic has no such control
either), AI Providers setup (M8) and notifications (M9). Classic's agent list colour filter is not
yet in Atlas.

## New Task, Projects and starting agents (M5)

New Task (`#/new`, `pages/NewTaskPage.jsx`) follows the Atlas V2 layout: the description,
project, lead tool, model, effort, file access and helpers on the left; a sticky "Before you
start" card with a picture, a plain summary, the usage note and Start on the right. It reuses the
classic rules rather than restating them (`model/newTask.mjs` over `autonomousCreation`,
`modelPresentation`, `providerPresentation`, `onboardingPresentation` and
`allocationPresentation`): a project is required; a provider that is not allowed or not ready can't
be chosen; models come from the provider's catalog and an unverified model needs an explicit
attempt; effort is offered only where the model lists it; the saved default provider applies once
and is never silently swapped; helpers exist only for a Claude Code or Codex lead, with two
policies ("Follow the lead's choices", "Also allow automatic choice"). Helpers start switched
off in Atlas, because more agents can mean more usage across provider accounts; this is Atlas's own
initial choice, Classic keeps its default, and nothing is remembered between tasks. The page does not imply that
helpers inherit the lead's model or effort and does not claim to know provider overage settings.

Starting sends the classic `POST /agents/start` payload once (a second click or Ctrl+Enter during
the request sends nothing), after reading the provider permissions again. Ctrl+Enter works only
from the New Task page itself, never while a dialog, popover or menu is open or from the header. A refusal is shown in the
backend's words. A dropped connection is reported as an unknown outcome and the draft is kept;
another start then needs an explicit "Start it anyway". Success is claimed only after the backend
returns the agent; Atlas then reads the agent's task and opens it in Work, or says the task started
but could not be opened.

Projects (`#/projects`) lists, searches and refreshes registered projects, opens a project's work or
a new task in it, and registers a project by a typed folder path (`POST /projects`), showing the
backend's validation errors. If a registration request fails in transit, its outcome is reported as
unknown: the typed values are kept, nothing is sent again by itself, and "Check the projects list"
reads the list again and says whether the folder is now registered before another attempt. There is no folder browser, drag and drop or file-system API; the
native folder picker remains a separate milestone.

From the inspector, "Add a helper…" starts a manual child agent under the current agent
(`POST /agents/{id}/children/start`, not autonomous), and "Start a new agent…" starts a new
assignment on a task that has no current agent (`POST /tasks/{id}/start-agent`). Starting again
does not resume an earlier agent or create another task, and it is not offered for a completed or
canceled task, nor for one waiting for a new owner (that goes through the responsibility transfer,
which the backend requires). Work's colour filter (`colors` in the address) selects several agent
colours, and "No colour" covers tasks without an agent and tasks whose agent was removed.

## Work Map (M6)

Work shows the same Tasks two ways, switched with **List / Map** in the Work header (or the L and
M keys): the List, structured and compact, and the Map, the same hierarchy drawn as a picture.
Both read one store, one set of visible rows (`visibleRows`) and one set of open branches, so they
always contain the same Tasks; the Map's layout (`model/mapLayout.mjs`) is a pure function of
those rows. The selection, filters, project, colours, focus, the view (`lens=map`) and the list
beside the Map (`side=1`) live in the address, so Back/Forward and reloads restore them; the
camera is the Map's own state and is not put in the address.

- **What is drawn.** One node per Task; threads only from `parent_task_id`. A family whose lead
  is not loaded, missing or in a recorded cycle gets no thread above it and says why. Each node
  shows the current agent (or the last one, labelled; a removed agent dashed; no agent as an
  empty outline), the status shape and words, and notes for closed branches ("+3"), helpers on
  pages not loaded yet, and work the lead planned that is not a Task yet ("2 planned"). Planned
  work is never drawn as a node.
- **Layout.** A lead sits centred over its helpers; when all of a lead's shown helpers are true
  leaves they stack as a short list under a shared thread. Families are grouped in bands that
  follow the List's sections (needs you, in progress, completed) and wrap into rows; inside a band
  they run oldest to newest, so a new family is added at the end.
- **Navigation.** Click selects (the same details panel and agent controls as the List);
  double-click or F focuses the branch, Escape leaves it. Drag pans, the wheel zooms around the
  pointer; zoom buttons, "Fit the selected task's family" and "Show everything" sit at the bottom
  left, with an overview of the whole map at the bottom right. "Jump to" lists the families. The
  Map is a tree for assistive technology: arrows follow the drawing (up to the lead, down to the
  first helper, left and right between Tasks at the same level), Enter selects, Space opens or
  closes a branch, plus and minus zoom and Shift with an arrow pans.
- **The list beside the Map.** "Show list" adds the compact List on the left; both stay
  selected and highlighted together. Below 1180 px the side list and the details panel get
  narrower instead of squeezing the map, and the camera keeps the selection in the uncovered part.
- **Stability.** A refresh never re-fits the map: the selected Task stays where it was on screen
  even when its family moves between bands. A new selection is brought into view only when it is
  off screen. The List/Map switch moves each visible avatar from one view to the other (at most
  70); with reduced motion everything changes at once and the working animations stop.
- **Partial data.** The Map says how many tasks are loaded and offers "Load older tasks"; missing
  and unloaded parents are explained on the node, never connected. Above 300 shown Tasks only
  the ones near the view are drawn (the selected and keyboard-focused Task always are).
- **Elsewhere.** List rows offer "Show in map" on pointer hover; the details panel has "Show this
  task in the map" and "Focus this branch", and its family picture shows the lead, the Task and up
  to five helpers with their status shapes, the Task's level and position, and "Show in map".
  Project cards draw each project's hierarchy in miniature from the loaded tasks only, within
  fixed bounds whatever the family size (six families, 36 tasks, six helpers under one lead, four
  levels; `model/projectPreview.mjs`). Helpers left out are counted beside their lead ("+394") and
  the card says how many tasks the picture shows. The card's counts are of individual tasks: open
  tasks, agents working (a running agent on a working task, not merely a current assignment), and
  tasks that need you, with waiting and blocked tasks and the task and family totals underneath.

Measured in Edge on the development machine (`atlas-map.spec.mjs`, 1,000 synthetic tasks in 50
families, 1440 × 900, four runs): switching to the Map 651–768 ms including the test driver
(1,627 ms once while the whole browser suite ran in parallel); 176 of 1,000 nodes drawn (3,420
elements in the map); selecting a node 87–97 ms (250 ms in the parallel run); a wheel zoom step
settles within two to four frames (median 32–39 ms); collapsing all families 58–323 ms. The unit test
requires the layout of 1,000 tasks to take under 250 ms, and a project preview of a family with
1,000 helpers under 50 ms. These are runs on one machine, not guarantees.

## Task pages (M7)

Every Task has its own page (`#/task?id=<task>`), opened with **Open task** from the details
panel (or the O key), from a List row, from a helper in the details panel, and from the team on
another Task page. Selecting a Task stays a selection; opening is a separate step. Each Task page
is its own entry in the browser history, and its **Work** link returns to the Work view the person
left (project, filters, colours, List or Map, side list and focus), with the Task selected.

The page is about the Task, not its agent. It reads only this Task's branch and the ancestors
that place it (`GET /workspace/tasks?root=<task>`), plus its own endpoints: `GET /tasks/{id}`,
`/assignments`, `/orchestration`, `/integrations`, `/control-operations`, `/dependencies`,
`/dependents`, the lead's `/delegations` for a helper's recorded result, and the current or last
agent's `/activity`. All are re-read when the shared event connection reports a change, never
polled; a failed read keeps the last good answer and the page says it may be stale.

- **Identity and responsibility.** The header names the current agent, or the last one as such,
  or nobody. "Everyone who held it" lists the assignment history newest first: replaced and
  removed agents are history, an unknown earlier agent is shown as "Earlier agent", and a
  replacement never creates another Task.
- **Request.** The Task's description, labelled by who wrote it: the person (a root Task or a
  helper the person added) or its lead (a delegated helper, the agent's words).
- **How it's going.** The status in words, pause and cancel intent, unconfirmed stops, what holds
  it (blockers and reasons to replan, from the backend's codes), the helper continuation phase,
  the plan decision and its revision, the lead's rationale quoted as its own words, each planned
  part's disposition (a planned part is never shown as a Task), rounds with helpers, helper-start
  problems and the agent's recorded activity for this Task.
- **Who's on it.** The Task's helper tasks as a List or a Map, from the same store, rows and
  layout as Work. A team of more than 60 tasks opens with its direct helpers, and the List draws
  150 rows at a time. Selecting a member shows its state with **Open task** and **Show in Work**.
- **Result,** in five separate statements: what the agent reported (a helper's recorded result
  and summary, quoted as its words; a lead's own account is in its output), what the Control
  Center recorded, what was independently verified (nothing: the backend never runs your tests),
  what was combined into the lead's copy (a helper's integration) and what was applied to the
  project (a root Task's integration), with changed and conflicting files and the backend's
  reasons. A child integration and a project application are never merged into one "done".
- **Lifecycle.** Pause, Resume and Cancel appear only when the backend's rules allow them, with
  the reason otherwise. Pause and Cancel show the backend's own impact preview
  (`POST /tasks/{id}/control-impact`): tasks paused or canceled, agents stopped, finished tasks
  left as they are, tasks elsewhere that become blocked or must replan. The preview is read again
  just before sending; if it changed, the new impact is shown and confirmed again. Cancel needs an
  explicit acknowledgement. Resume says which pause it releases and is offered only for a pause
  this Task owns (a pause started from a task above it is resumed there). One request at a time;
  a lost connection is reported as an unknown outcome and never retried by itself; refusals are
  shown in the backend's words. Agent actions (reply, stop, removal) stay in the Work details panel,
  linked as **Agent controls in Work**. `POST /tasks/{id}/pause` and `/cancel` take no reference to
  the previewed impact and apply the impact current at that moment; re-reading just before sending
  narrows that window but cannot close it. A token bound to the previewed impact, as removal plans
  have, would.
- **Advanced.** Lifecycle and control intent, stop state, continuation, decision key and source,
  wake state, helper starts and control operations, closed by default.

### Applying work to the project: unavailable in Atlas, by decision

`POST /tasks/{id}/integrate` prepares and applies a root Task's changes to the registered project
in one call. The changed files are recorded during that call; there is no read-only step that
shows them before anything is written, and no token that ties an application to what was
reviewed. A confirmation dialog is not a review of file changes, so Atlas shows the integration
history read-only and says why it cannot apply. Classic view has no apply control either.

The smallest safe backend capability: a read-only **integration preview** for a Task that builds
the same three-way candidate in the existing temporary candidate area without touching the
destination, and returns the destination, the base, source and destination snapshots, the changed
paths with their kind (added, modified, deleted) and a bounded text diff per file, any conflicts,
and a preview token bound to the source and destination snapshots. `POST /tasks/{id}/integrate`
would then accept that token and refuse when either snapshot changed since the preview, in the
same way removal plans are bound to their preview today.

Measured in Edge on the development machine (`atlas-task.spec.mjs`, a Task with 600 helpers and
deep chains, 1,561 tasks in its branch, 1440 × 900, two runs): the Task page ready in 983–1,151 ms
including the test driver; switching its team to the Map 207–561 ms (17 nodes drawn near the
view); opening a Task nine levels deep 16–54 ms. Runs on one machine, not a guarantee.

## Connections and Settings (M8)

**Connections** (`#/connections`) holds everything Classic's AI Providers view does, for Claude
Code, Codex and Gemini CLI, with the same endpoints, payloads and rules (it reuses
`onboardingPresentation`, `providerPresentation`, `modelPresentation` and `useModelCatalog`; every
change goes through `api/agentClient.mjs`). Each tool has its own page (`#/connections?provider=claude`,
with `&section=setup|usage|billing|models|ready|advanced`), so Back and Forward work and other pages
can link to it. One read, `GET /providers/onboarding`, serves the page and the navigation: on the
page it follows every change the event connection reports; elsewhere it re-reads at most every 15
seconds after a reported change, for the attention mark only. It never polls on its own and never
probes a tool: checks run only from Check all or Check again.

Each tool answers three questions:

- **Is it ready for tasks?** Installed, signed in, turned on, allowed for tasks and ready are five
  separate facts, each marked yes, no or not known. The tool's one condition comes from a closed
  set: Ready for tasks, Setup running, Requires attention, Usage-limited, Temporarily unavailable,
  Not allowed for tasks, Turned off / Not set up, Not checked yet. A tool the person has not turned
  on or allowed is "not in use", never an error.
- **What usage is known?** Only what the tool reported: meters with their used percentage (a bar
  only when a percentage was reported), the provider's own reset time or "Reset time not reported",
  recorded limits, and when each reading was observed (stale readings say so). Remaining usage and
  cost are always "Not available"; nothing is estimated.
- **Where could paid use occur?** The place in the person's own account where it is checked, with
  official links (see Billing guidance sources). The Control Center can't read or change those
  settings and says so. The billing confirmation or acknowledgment is the person's own statement,
  shown separately from the route the last check verified.

The navigation marks Connections with a dot when a tool needs attention (an unverified setup
operation, a usage limit, an outage, or a blocker on a tool that is turned on or allowed) or the
saved default can't be used. It names the tools in words for screen readers and its tooltip; it
shows no number.

### Parity with Classic's AI Providers view

| Classic function | Atlas | Notes |
| --- | --- | --- |
| Discovery, installation state, method, version, "also found elsewhere" | Yes | Tool page, Installation and sign-in; location and npm in Advanced. |
| Update status and Check for updates | Yes | Overview header; reads release information, installs nothing. |
| Install, Update, Sign in, Sign out (with channel) | Yes | Prepared as a plan, shown in full, approved once by plan id and digest; a manual-only plan shows its steps and has no Run button. |
| Running operation and Cancel | Yes | |
| Operation whose end is not verified, and "Confirm it ended" | Yes | Blocks new setup until confirmed; refused by the backend while processes run. |
| Latest operation result | Yes | |
| Active work that delays updates and sign-in | Yes | Agent names isolated for right-to-left text. |
| Check all, Check again | Yes | Local, non-billable checks only. |
| Next step and every other blocker | Yes | From `nextStep`, unchanged. |
| Enable and Disable ("turned on") | Yes | Claude Code and Gemini CLI turn on only with the billing confirmation (`{enabled: true, billing_confirmed: true}`); Codex turns on separately and records its acknowledgment separately (`{billing_confirmed: true}`). Turning on never allows a tool for tasks. |
| Billing confirmation / acknowledgment text | Yes | The backend's own text, as in Classic. |
| Usage summary, meters, staleness, recorded limits | Yes | |
| Refresh usage (Claude Code) | Yes | Resumes no agent. |
| Clear recorded limit | Yes | Behind a confirmation that repeats the backend's rule. |
| Model catalog, freshness, account verification, refresh | Yes | Each model with the effort levels it lists itself; Gemini CLI has no reasoning control. |
| Allowed providers (Permissions) | Yes | Sent with the loaded revision; every widening and narrowing is listed before saving; a conflict keeps the draft and offers "Discard and load the newer version". |
| Default provider, model and effort for new tasks | Yes | Says that defaults never change running agents. |
| Saved default that can't be used | Yes | Shown, never replaced; also an attention reason. |
| Mock described | Yes | "Practice (Mock)". |
| Provider documentation link | Yes | Advanced (the backend's `docs_url`). |
| Backend unreachable, last good data kept, Retry | Yes | Mutations disabled with the reason while unreachable or stale, enabled again when it answers. |
| Update notifications (bell: dismiss, remind later, review update) | Yes (M9) | In Atlas's notifications panel; see Notifications and attention (M9). |
| Polling every 3 s while an operation runs | Different | Atlas follows the event connection (operations publish on start and end, and the connection falls back to polling itself); it adds no timer. |

Mutation safety is the same as M4–M7: one request at a time per page (a second click sends
nothing), a lost connection or a 500/503 is reported as an unknown outcome and read again, never
retried; refusals are shown in the backend's words prefixed by "Nothing was changed"; an approval
is cleared as soon as it is sent.

### Billing guidance sources

Checked on 2026-10-30 against the providers' official pages (`src/providerGuidance.mjs`, outside
`src/atlas` because Atlas code holds no remote addresses; these are links the person opens, never
requests the app makes):

- **Claude Code:** Claude Help Center, "Manage usage credits for paid Claude plans"
  (article 12429409): an individual account setting on paid plans, Settings › Usage › Usage credits,
  turned on or off there, with spend limits and auto-reload; the article now says "usage credits"
  (its address still says "extra usage"). "Use Claude Code with your Pro or Max plan" (article
  11145838): Console billing belongs to API credits. Settings page `claude.ai/settings/usage`.
- **Codex:** OpenAI's pricing page (`learn.chatgpt.com/docs/pricing`): Plus and Pro users who reach
  their limit can buy credits; prices depend on the plan or agreement; limits are in the usage
  dashboard (`chatgpt.com/codex/settings/usage`, linked from that page). The page does not mention
  auto top-up. OpenAI Help article 12642688 ("Using credits for flexible usage") describes auto
  top-up for eligible accounts but refuses automated reading, so the guidance only says the help
  center describes it and that it depends on the account and its settings.
- **Gemini CLI:** Google's transition announcement (Google Developers Blog, May 19, 2026) and
  "Gemini Code Assist consumer accounts" (Google for Developers): since June 18, 2026 Gemini CLI no
  longer serves free, Google AI Pro or Google AI Ultra individual accounts, which move to Antigravity
  CLI; Gemini Code Assist Standard and Enterprise licenses keep access (on their own schedule), and
  Gemini CLI remains available with paid Gemini API keys. "Gemini CLI: Quotas and pricing"
  (`geminicli.com`): daily request limits per license, `/stats model` for usage, API keys and
  Vertex AI pay per use. The Connections page keeps this provider availability apart from the
  Control Center's own rule, unchanged in the backend: only the Google sign-in (`oauth-personal`) is
  accepted, API keys and Vertex AI are refused even though Gemini CLI supports them, and Antigravity
  CLI is not a Control Center tool. A notice on the Gemini CLI page and in its sign-in review says
  so. The quotas page does not mention the `overageStrategy` setting; that advice remains only in the
  backend's own setup note, shown as such.

### Settings

`#/settings?section=…` (Back and Forward move between sections): Appearance (Automatic, Light, Dark;
shared with Classic), Interface (return to Classic, reset the details panel width, open
Connections), Notifications, Keyboard (everywhere, Work, New task), Accessibility (reduced motion,
contrast, right-to-left text, screen readers, as the system reports them), About and diagnostics
(backend and live updates, process supervision from `GET /system/process-supervision`, tools, and
"Copy diagnostics": states only, never task text, paths or names).

Notifications use Classic's preference (the same stored key and rule: on only while the browser
allows notifications; the browser asks only from the switch). Atlas shows no browser notifications
and keeps no notification list yet (M9). It tells the backend the preference
(`POST /notifications/preferences`, as Classic does), so the backend's Windows notifications follow
it while Atlas is open; an "on" preference is sent again when the backend answers after being
unreachable, and nothing is sent for an "off" preference the person did not change here. The page
explains the browser's site permission and the Windows notification settings it depends on.

## Notifications and attention (M9)

Atlas says what needs the person without keeping a list of its own. Everything comes from the
sources Classic already uses: the canonical Task list (`GET /workspace/tasks`, read by the shell
once and shared with Work), the workspace summary (`overview.needs_input`, the backend's count of
agents waiting for a reply), the tools' state (`GET /providers/onboarding`) and the provider update
notices (`GET /providers/notifications`). It opens no extra event connection and polls nothing: the
Task list and the notices are read again when the shared connection reports a change (the notices
closely while the panel or Connections is open, at most every 15 seconds otherwise).

- **Bell (notifications panel).** A small non-modal panel from the top bar: **Needs you** (Tasks
  waiting for a reply, for a new owner, for inspection, or whose work couldn't be combined, each
  named in words with the Task's current agent), **Tools** (tools that need attention, linking to
  Connections), **Tool updates** (Classic's notices) and the notification status with a link to
  Settings. It is derived from live data each time, so it holds no history and cannot duplicate an
  item. The bell shows a dot, never a number (amber when something needs the person, grey when
  only an update is available); its accessible name says what and how many, using the backend's
  count for questions and saying "among loaded tasks" for anything counted from loaded pages.
  Escape or a click outside closes it and focus returns to the bell.
- **Provider update notices.** The same notices, choices and endpoints as Classic's bell: **Review
  update** opens that tool's setup in Connections (`#/connections?provider=…&section=setup`), where
  any update is still prepared as a plan and approved explicitly; **Remind me later** and
  **Dismiss** send `{version, mode}` to `POST /providers/{p}/notifications/dismiss`, so both stay
  scoped to that version. A stale check says "may be out of date"; a refusal is shown in the
  backend's words; a lost connection is an unknown outcome and the notices are read again.
  Atlas never creates a notice and says that updating is optional.
- **"N need you" and "next that needs you".** The top-bar button shows the backend's workspace
  count of questions and goes to the next Task waiting for a reply, in the List's order, after the
  selected one; it keeps the Work view unless a filter would hide that Task (then only the filters
  are cleared). In Work, a bar above the List and Map shows where the selected Task is among the
  Tasks that need the person **in this view** (project, focus, filter, colour and search
  respected), with Previous and Next (Shift A, A) and Reply (R). Each move is a navigation (Back
  returns), selects the Task by its canonical id, opens the path to it, and keeps List and Map on
  the same selection. When pages are not all loaded, the bar and the panel say so and offer to look
  on older pages; the backend's question count is the only workspace-wide count shown.
- **Reply from Work.** A row waiting for a reply has a Reply shortcut (pointer), the R key and the
  bar's Reply button do the same with the keyboard, and the panel's Reply opens the Task from
  anywhere: each selects the Task and puts the cursor in the answer of the details panel, which is
  the M4 reply form (current agent only, the backend's eligibility, one-time model or effort
  override, Decide for me, one request at a time, the stale-agent check before sending). Drafts are
  kept per Task in memory while moving between Tasks. If the Task's agent changes while a reply is
  written, the text stays, a note says so, and the first Send is refused for review. A lost
  connection or a server failure now reports that the reply may or may not have arrived and keeps
  the text (the backend refuses a second reply once the agent is no longer waiting). Focus moves
  only on an explicit request, never on a refresh.

### Browser and Windows notifications

The preference is Classic's (same stored key, the same rule: on only while the browser allows
notifications; the browser asks only from the switch). The backend notifies an agent that starts
waiting for a reply or finishes: it waits 8 seconds and shows a Windows notification only if no page
has sent `POST /notifications/heartbeat` in the last 6 seconds. Atlas now shows browser
notifications too, and sends Classic's heartbeat only while it can really show them: the preference
is on, the browser allows them, showing one has not failed in this page, this is the Atlas tab
holding the Web Lock `control-center-atlas-notifications`, the backend answers, the Task list is
current, and every page of Tasks is loaded (otherwise a change on an unloaded page could be missed).
In every other case no heartbeat is sent, and the backend's Windows notification is the fallback
when available. So an event is not shown by both; whether a Windows notification actually appears
depends on Windows settings, Focus assist, the backend being reachable and the notifier itself, and
the wording never promises it ("Windows notifications are the fallback when available"). While the
backend isn't answering, Atlas says that nothing can be sent until it returns. Without the Web Locks
API (an insecure address or an old browser) Atlas tabs can't agree on one of them, so none shows
browser notifications and none sends the heartbeat.

Which changes are announced follows Classic and the backend (an agent starting to wait for the
person, an agent finishing), once per transition. Atlas's own rule for identity: the first reading
is a baseline (nothing is replayed); an agent seen for the first time is announced only when its
Task was already being watched (a replacement agent, a new assignment) or the Task was created after
watching began, so loading older pages never replays old states; the same state reported again (a
repeated event, a reconnect) is not announced. Each notification is tagged by
`control-center:{agent}:{state}`, so the same event shown by two tabs replaces itself. A click opens
the Task (never an Agent) in Work, so a stale notification shows whoever holds the Task now, or that
it no longer exists. New questions are also announced politely to screen readers.

### Differences from Classic

| Classic | Atlas | Why |
| --- | --- | --- |
| Bell badge with the number of provider updates | A dot; counts in the accessible name and in the panel | Updates are calm information; questions use the backend's own count. |
| Bell lists provider updates only | Panel adds what needs the person and tools that need attention | One place for attention, derived from live data, no history. |
| Browser notification tracker over the polled agent list | Transition tracker over the canonical Task list | Atlas reads Tasks a page at a time; first-seen rules avoid replays. |
| Heartbeat whenever the page polled in the last 4 seconds | Heartbeat only while every page is loaded, the list is current and this tab holds the lock | Never suppress a Windows notification for an event Atlas could miss. |
| Each Classic tab shows its own notifications | One Atlas tab shows them (Web Locks); without Web Locks none does; tags make repeats replace each other | Fewer duplicates. A Classic tab and an Atlas tab open together still each show one (Classic is unchanged). |
| A click selects the Agent | A click opens the Task | The Task is the stable identity; an Agent may have been replaced. |

Limitations, carried to the M11 gate: the Windows notifier cannot open a task (it has no click
action); a Classic tab and an Atlas tab open at the same time can each show the same event (Classic
neither takes the lock nor tags its notifications, and Classic is unchanged); a workspace too large
to load completely gets Windows notifications only. **Failure window:** when the browser fails to
show a notification, Atlas stops the heartbeat at once, so the backend's 8-second wait usually ends
with the last heartbeat more than 6 seconds old and the Windows fallback is tried; a failure the
browser reports more than about 2 seconds after the event can leave that one event shown by neither
(Classic has the same window). Closing it needs a backend change (for example an explicit
acknowledgement per event), which is outside the existing contract and needs separate approval.

## Design continuity review (V2 → Atlas, at M7, updated at M8 and M9)

Atlas V2 is the approved direction, not a pixel specification. Status: **A** implemented as
designed; **B** implemented differently for a reason; **C** added in M6; **D** planned for M7–M11;
**E** worth keeping but not yet in a milestone; **F** not supported by the real system.

| V2 concept | Status | Notes |
| --- | --- | --- |
| Top navigation, appearance switch, Classic switch, New task button | A | |
| "N need you" in the top bar | A / C (M9) | It goes to the next Task waiting for a reply; Work has Previous/Next (A, Shift A). |
| Work List: sections, family cards, threads, columns, keyboard tree | A | M3. |
| Work Map, List/Map switch, L and M keys | C | |
| Avatars travelling between List and Map | C | Simplified: at most 70 avatars, skipped with reduced motion. |
| Compact list beside the Map | C / B | Overlays the map; narrower panels below 1180 px rather than three squeezed columns. |
| Delegation-only threads, stacked leaf helpers, status rings and badges, thread styles | C | Closed branches keep their node form so siblings never rearrange. |
| One endless row of families | B | Bands by section that wrap: readable with many families, stable when tasks are added. |
| Focus a branch (F), breadcrumbs, Leave (Esc) | C | Shared by both views; breadcrumbs widen the focus. |
| Zoom, fit, show everything, overview, drag, wheel, "Jump to" | C | |
| Hover in one view highlights the other | C | |
| Row quick actions | C | "Open task" and "Show in map" (M6, M7); Reply on rows waiting for a reply (M9, also R). |
| View options: expand all, collapse | C | |
| View option: hide removed agents | B | Tasks are never hidden because their agent was removed; the colour filter's "No colour" selects them. |
| Rotating latest output line in each row | F | Would need a stream per agent; Atlas keeps one event connection. The Output tab has the history. |
| Inspector: header, tabs, footer actions, resize | A | M3/M4. |
| Inspector: focus branch, show in map, family picture | C | The family picture now has status shapes, position and "Show in map". |
| Inspector: "More actions" menu | B | The same actions live in the tabs and the footer. |
| Task page: request, progress, result and verification, "Who's on it" List/Map | C (M7) | The team view reuses the Work List and Map; planned parts are notes, never tasks. |
| Task page: "Apply to project" | F | Not offered until the backend can preview the exact changes; see Task pages (M7). |
| Task page: "Ask for changes", "Open the task's copy" | E | Need new backend support or a local-file action; not in a milestone yet. |
| Projects: cards with hierarchy pictures and counts | C | From the loaded tasks only, bounded, and saying what is left out; counts are of tasks. |
| Projects: add a project | A / F | Typed path (M5). The native folder picker is its own milestone; dropping a folder is not possible in a browser. |
| New Task layout and "Before you start" picture | A | M5. |
| Connections: readiness, usage, billing guidance, nav badge | C (M8) | Five separate readiness facts instead of one badge; the nav mark is a dot with words, never a number. |
| Settings: keyboard reference, about | C (M8) | Also notifications preference, accessibility, process supervision and diagnostics. The full keyboard review stays M10. |
| Notifications | C (M9) | A panel derived from live data (needs you, tools, updates), no stored inbox; browser notifications coordinated with Windows ones. |
| Global search (Ctrl K) over tasks and agents | E (open decision) | Work searches the loaded tasks; searching unloaded tasks and agents needs a backend search. A product decision for a future approved milestone, to be settled before M11. |
| N for New task | D | M10 keyboard review. |

### Checklist for later milestones

- **M7 Task pages (done):** remaining from V2 are "Ask for changes", opening the task's copy and
  applying to the project, all waiting on backend support (see open product decisions).
- **M8 Connections and Settings (done):** remaining are the provider update notices from
  Classic's bell (M9) and the full keyboard review (M10).
- **M9 Notifications and attention (implemented):** remaining is a durable notification history,
  which the backend does not keep (not planned), and a click action for Windows notifications.
- **M10 Accessibility, responsiveness, performance, polish:** a full keyboard pass including N,
  zoomed text beyond 150 %, very large workspaces on slower machines, visual polish against V2.
- **M11 Parity and default switch:** functional parity review against Classic; Atlas becomes the
  default only after approval; Classic removed only after explicit approval.
- **Open product decisions:** global search across unloaded tasks and agents (needs a backend
  search); applying work to the project from Atlas (needs the trustworthy integration preview
  described in Task pages (M7)); "Ask for changes" on a Task page; safe access to a task's working
  copy (opening its private copy needs a local-file action that the browser can't take); native
  Windows folder selection for Projects (its own milestone after the core migration).

## Tests

From `frontend/`:

```powershell
npm test               # Node unit tests, including Atlas routing, isolation, fixture contracts and the task model
npm run lint
npm run build
npm run test:browser   # builds, then runs the browser tests in Microsoft Edge
```

`e2e/atlas-work.spec.mjs` covers the Work list and inspector with the recorded contract and with
synthetic scenarios in `e2e/scenarios.mjs` that use the endpoint's exact node shape.
`e2e/atlas-actions.spec.mjs` and `e2e/atlas-manage.spec.mjs` cover agent actions (M4);
`e2e/strict-runner.spec.mjs` runs the action runner under React StrictMode in a development build;
`e2e/atlas-task.spec.mjs` covers Task pages (M7); `backend/test_task_detail_contract.py` records the
real task-detail responses of the synthetic scenario into `tests/fixtures/task-detail.contract.json`
and fails when they drift, and `tests/atlasTaskPage.test.mjs` reads them.
`e2e/atlas-create.spec.mjs` and `e2e/atlas-start.spec.mjs` cover New Task, Projects, Add a helper,
Start again and the colour filter (M5); `e2e/atlas-map.spec.mjs` covers the Map, the side list,
focus, project previews and measured performance with 1,000 tasks (M6).
`e2e/atlas-connections.spec.mjs` covers Connections and Settings (M8) with synthetic onboarding
answers from `e2e/providers.mjs`, shaped like the backend's; `tests/atlasConnections.test.mjs`
covers the Connections and Settings wording and payloads. `e2e/atlas-notifications.spec.mjs` covers
the notifications panel, provider update notices, attention navigation, reply from Work, browser
notifications with the heartbeat and two tabs (M9); `tests/atlasAttention.test.mjs` covers the
attention model, counts and the notification transition rules.

The browser tests start no server. Playwright serves the production build and a fixture backend
through request routing, so no provider or agent can run. They check the classic default, the
switch in both directions, a single event connection, Back/Forward, appearance persistence,
keyboard focus, reduced motion, mixed-direction text, unreachable-backend handling, the 1440, 1180
and 1024 pixel layouts and an axe-core accessibility scan in both appearances. Set
`ATLAS_SCREENSHOTS` to a folder to also capture review screenshots. These are simulated checks, not
an end-to-end run against a real backend.

## Starting state (M0, 2026-10-28)

Recorded against `main` at `2d8fa3f` plus the uncommitted Stage 2 orchestration work (backend and
documentation files, untouched by the Atlas work):

- Frontend: 218 unit tests passed; lint and production build passed.
- Backend: all 1,618 tests passed (`unittest` discovery, about 35 minutes; temporary databases,
  repositories and fake provider CLIs, no real provider).
- `git diff --check` passed.
- `npm audit` reports one high-severity advisory in `zod`, a development dependency of
  `eslint-plugin-react-hooks`; it predates the Atlas work and was left unchanged.
