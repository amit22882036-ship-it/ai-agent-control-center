# Atlas V2 design reference

The approved Stage 2J visual and interaction reference for the Atlas dashboard. It is a **design
prototype**, not the application: one static HTML file with fictional data that talks to no
backend. The working Atlas interface is built in `frontend/src/atlas/` (see
[the migration plan](../../atlas-migration.md)).

| File | What it shows |
| --- | --- |
| [prototype.html](prototype.html) | The full interactive prototype. Open it in a browser; it loads its fonts from Google Fonts. |
| [work-list-light.png](work-list-light.png) | Work as a hierarchical list (light). |
| [work-map-dark.png](work-map-dark.png) | Work as a map (dark). |
| [new-task-light.png](new-task-light.png) | New task with a lead agent and helpers allowed. |
| [connections-light.png](connections-light.png) | Connections: readiness, usage limits and paid usage. |

Names, projects, counts, usage readings and paths in the prototype are invented. Simulated
behaviour (the folder browser, dropping a folder, starting a task) only demonstrates the intended
interaction. Where the implementation and this reference differ, the backend's actual semantics
win; the migration plan records each deliberate difference.
