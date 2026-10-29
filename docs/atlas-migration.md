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
| M2 | Canonical Task/Agent model; read-only task projection (B1, separately approved) | Not started |
| M3–M11 | Work list and inspector, agent actions, New Task and Projects, Map, Task pages, Connections and Settings, notifications, accessibility and performance, default switch | Not started |

Legacy removal is a separate, explicitly authorized final step. The native folder picker is a
separate milestone after the core migration.

## Opening the Atlas preview

Start the backend and the frontend as described in the [README](../README.md#quick-start-windows),
open the dashboard and choose **Atlas preview** in the top bar. Atlas addresses start with `#/`
(for example `http://127.0.0.1:5173/#/work`). Choose **Classic view** to return; the browser's
Back button returns to the Atlas page you left. Opening the dashboard without an Atlas address
always shows the classic dashboard.

At M1 the preview contains the shell, navigation, appearance settings, the workspace totals and a
design gallery (`#/gallery`, sample content only). Work, Projects, Connections and New task say
which milestone adds them and link back to the classic dashboard. Atlas does not start, stop or
change agents yet, and it sends no state-changing request.

## How Atlas stays isolated

- **Code:** everything lives in `frontend/src/atlas/`. The only classic files touched are
  `main.jsx` (choosing the interface) and `WorkspaceBar.jsx` (the **Atlas preview** button).
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

## Tests

From `frontend/`:

```powershell
npm test               # Node unit tests, including Atlas routing, isolation and fixture contracts
npm run lint
npm run build
npm run test:browser   # builds, then runs the browser tests in Microsoft Edge
```

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
