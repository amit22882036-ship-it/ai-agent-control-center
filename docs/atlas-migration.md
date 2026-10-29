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
| M4–M11 | Agent actions, New Task and Projects, Map, Task pages, Connections and Settings, notifications, accessibility and performance, default switch | Not started |

Legacy removal is a separate, explicitly authorized final step. The native folder picker is a
separate milestone after the core migration.

## Opening the Atlas preview

Start the backend and the frontend as described in the [README](../README.md#quick-start-windows),
open the dashboard and choose **Atlas preview** in the top bar. Atlas addresses start with `#/`
(for example `http://127.0.0.1:5173/#/work`). Choose **Classic view** to return; the browser's
Back button returns to the Atlas page you left. Opening the dashboard without an Atlas address
always shows the classic dashboard.

The preview contains the shell, navigation, appearance settings, a design gallery (`#/gallery`,
sample content only) and, since M3, the Work list with a read-only inspector. Projects,
Connections and New task say which milestone adds them and link back to the classic dashboard.
Atlas does not start, stop or change agents yet, and it sends no state-changing request.

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
