# Lumen: the default interface

Lumen is the production version of the Atlas interface (code under `frontend/src/atlas`), with the
Lumen visual design, a navigation rail, project organization and the project lifecycle. It is now
the default. The classic interface stays available as a fallback: **Open the classic interface** at
the bottom of the rail, or `?ui=classic` in the address. Everything Atlas could do (see
[Atlas migration](atlas-migration.md)) is kept; this page lists what changed and what is still
missing.

## Feature checklist

| Area | Classic | Atlas preview | Lumen |
| --- | --- | --- | --- |
| Work list and Work Map, inspector, agent actions | Hierarchy view | Yes | Yes, restyled |
| Task pages: request, progress, team, results, Pause / Resume / Cancel | No | Yes | Yes |
| Rename a task (display title) | No | No | Yes: inline on the task page and in the inspector |
| Rename an agent | Yes | Yes | Yes |
| Projects screen | No | Cards | Plates with a map picture, open tasks, agents working, what needs you |
| Rename a project (display name only; the folder is never renamed) | No | No | Yes |
| Reorder projects (drag, or Space and arrow keys, Alt+arrows) | No | No | Yes; saved in the backend and used in every project list |
| Hide / show a project (work keeps running; attention still shows) | No | No | Yes, with a Hidden shelf |
| Move to Trash, Restore, Delete permanently | No | No | Yes, see [project lifecycle](project-lifecycle.md) |
| Stop all work in a project before moving it | No | No | Yes, in the Trash dialog |
| Access levels Read only / Workspace / Full Access | Read only / Workspace write | Read only / Can edit | Yes, per provider, see [agent access](agent-access.md) |
| Helper access for a Full Access lead (Workspace by default) | No | No | Yes |
| Connections (AI Providers), Settings, notifications | Yes | Yes | Yes |
| Windows desktop app with installer | No | No | Yes, see [desktop app](desktop.md) |
| Demo mode with synthetic projects and Mock agents | No | No | Desktop app `--demo` |

## Not done yet

- **Map orientation.** The Work Map draws families top to bottom only; a left-to-right option is
  not implemented.
- **Canceled tasks.** Canceled tasks show the "Canceled" state; there is no struck-through style
  and no separate filter for them.
- **First-run guide.** A new installation opens on an empty Work page ("No tasks yet") with a
  New task button; there is no step-by-step welcome.
- **Folder picker.** The desktop app exposes a native folder picker to the page
  (`window.controlCenterDesktop.chooseFolder`), but the Add project dialog still takes a typed path.
- **Applying a root task's work to its project** is still API only (`POST /tasks/{id}/integrate`),
  as in Atlas.
- **Real-provider runs** of Full Access were not exercised: the flags were checked against each
  installed CLI's help, and the behaviour with fake CLIs. No paid agent was started.

## Tests

`frontend/tests/atlas*.test.mjs` (models, isolation, project lifecycle helpers, access text) and
the Playwright suite in `frontend/e2e` (Edge, synthetic data through the request harness) cover the
interface; `backend/test_project_lifecycle.py` and `backend/test_agent_access.py` cover the backend
with real temporary folders, junctions and Git repositories.
