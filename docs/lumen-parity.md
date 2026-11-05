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
| Work Map direction: top to bottom or left to right (remembered; arrow keys turn with it) | No | Top to bottom | Both |
| Canceled tasks struck through, with their own "Canceled" filter (shown when there are any) | No | No | Yes |
| First-run welcome: check tools, add a project, describe the work | No | No | Yes, until the first task exists |
| System folder picker in Add a project | No | No | Desktop app (a browser still takes a typed path) |

## Not done yet

- **Applying a root task's work to its project** is still API only (`POST /tasks/{id}/integrate`),
  as in Atlas.
- **Resources and work intents** have no screens; they are API only, as in Classic.
- **Real-provider runs** of Full Access were not exercised: the flags are checked against each
  installed CLI's help and with fake CLIs (see [agent access](agent-access.md#what-is-verified-and-what-still-needs-a-real-run)).
  No paid agent was started.

## Look and feel

The app is laid out as one window: a sidebar material on the left (navigation and projects, each
with its own colour), a transparent title bar, and one content sheet. Work sits in grouped lists
(rounded groups with hairline separators) in place of a stack of cards, and selection is an inset
rounded shape as in a native sidebar. Pages share the same sheet; their sections are groups on it.
Light and dark appearances, reduced motion, reduced transparency and higher contrast are honoured.

## Tests

`frontend/tests/atlas*.test.mjs` (models, isolation, project lifecycle helpers, access text) and
the Playwright suite in `frontend/e2e` (Edge, synthetic data through the request harness) cover the
interface; `backend/test_project_lifecycle.py` and `backend/test_agent_access.py` cover the backend
with real temporary folders, junctions and Git repositories.
