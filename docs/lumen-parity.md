# Lumen: the interface

Lumen (code under `frontend/src/atlas`) is the Control Center's only interface. The earlier classic
interface was removed on 2026-11-06, after an audit showed Lumen covered every capability it offered;
its history is in [Atlas migration](atlas-migration.md) and the
[M11 readiness record](atlas-m11-readiness.md).

## What it does

| Area | In Lumen |
| --- | --- |
| Work List and Work Map, details panel, agent actions (reply, redirect, decide, stop, remove, transfer) | Yes |
| Work at scale | One line per family, a strand showing each family's tasks by state, history folded by day, a virtualized list (thousands of tasks), Map shelf for closed families |
| Live updates | The selected task holds its place and its section while you look at it; the list never jumps under you |
| Go to (Ctrl K or /) | Finds tasks by title, by any agent that held them, by project or id, and pages |
| Task pages: request, progress, team, results, Pause / Resume / Cancel | Yes |
| Apply a finished task to its project | Review the files it changed (and the project's own changes) first, then apply; conflicts are refused, nothing is committed |
| Coordination | Resources and work areas a task holds, overlaps, deadlocks; release a stuck one after confirming |
| Rename tasks, agents and projects (display names only) | Yes |
| Projects: order, hide, Trash, restore, delete permanently | Yes, see [project lifecycle](project-lifecycle.md) |
| Access levels Read only / Workspace / Full Access, helper access | Yes, per provider, see [agent access](agent-access.md) |
| Connections (AI tools), Settings, notifications | Yes |
| Windows desktop app with installer, demo mode | Yes, see [desktop app](desktop.md) |
| Work Map direction: top to bottom or left to right | Both |
| First-run welcome | Until the first task exists |

## Visual language

Lumen is drawn as an orienteering map. Every colour has one meaning that never changes:

| Ink | Means |
| --- | --- |
| Course purple | Only what needs you |
| Forest green | An agent recorded working |
| Water blue | Waiting |
| Olive | On hold or blocked |
| Contour brown | The hierarchy's lines and finished work |
| Open-land yellow | Your selection |

What needs you is laid out as a **course**: numbered controls in the List's left gutter, joined by
one line in the order the A key steps through them; the Map circles the same tasks with the same
numbers. Light and dark ("night map") appearances, reduced motion, reduced transparency and higher
contrast are honoured.

## Not done yet

- **Real-provider runs** of Full Access were not exercised: the flags are checked against each
  installed CLI's help and with fake CLIs (see [agent access](agent-access.md#what-is-verified-and-what-still-needs-a-real-run)).
  No paid agent was started.
- Several backend operations remain API only in every interface: dependency writes, recovery
  re-arm and re-verify, confirming that a process stopped, `continue` and orchestration wake.

## Tests

`frontend/tests/*.test.mjs` (models, isolation, project lifecycle helpers, access text) and the
Playwright suite in `frontend/e2e` (Edge, synthetic data through the request harness) cover the
interface; `backend/test_project_lifecycle.py`, `backend/test_agent_access.py` and
`backend/test_integrations.py` cover the backend with real temporary folders, junctions and Git
repositories.
