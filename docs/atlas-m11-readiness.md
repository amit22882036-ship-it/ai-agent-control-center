# Atlas readiness review (M11)

Prepared on 2026-10-31 against `main` at `8d2096d` (M10) plus the uncommitted M11 corrections. It
answers one question: is Atlas ready to replace Classic view as the default? It changes nothing
by itself. Classic stays the default and Atlas stays opt-in until the owner decides otherwise.

**Evidence levels used below.** *Source:* read in the current code. *Fixture:* an automated test
in this repository, with synthetic data, request routing instead of a backend, and no real
provider (browser tests) or temporary databases and fake CLIs (backend tests). *Manual:* a
person on a real Windows machine with the real backend. No manual test has been performed for
this review; every "MANUAL" item below is still to do.

The only authenticated provider evidence is one isolated run on 2026-10-25 (four Claude Code and
four Codex runs, all ending verified; see `providers.md`). It went through the backend API, before
Atlas existed in this form, so it says nothing about Atlas itself and does not cover autonomous
delegation, integration, usage limits or restart recovery.

## Recommendation

**READY FOR USER ACCEPTANCE TESTING. NOT READY FOR DEFAULT** until the conditions below are met.

The implementation is complete for daily use as far as source review and fixture tests can
show, and no Classic capability is missing from Atlas. What is missing is evidence from the real
environment: nothing in Atlas has been exercised against the real backend, real providers, real
projects, real Windows notifications or a screen reader. Those are the conditions:

1. A manual Windows session following the checklist below, with no blocking finding.
2. A screen-reader pass (NVDA or Narrator) following the walkthrough below.
3. The owner's decision on mixed Classic/Atlas notifications (option A, B or C below).

## Classic and Atlas parity

Classified from the current source of both views, with the browser test that exercises the Atlas
side (numbers refer to `e2e/*.spec.mjs` titles quoted in the evidence column).

| Capability | Classification | Evidence |
| --- | --- | --- |
| Projects: list, register by path, choose | Supported in both; Atlas adds hierarchy previews and per-project counts | Classic `ProjectSelection.jsx`; Atlas `ProjectsPage`, "a project is registered by its path", "Projects show a picture of each project's hierarchy" |
| Start a root task (classic payload) | Equivalent | "a new task is started with the classic payload and opens in Work" |
| Provider, model, effort, unverified-model attempt | Equivalent | "a provider that is not allowed cannot be chosen, and an unverified model needs an explicit attempt" |
| File access (sandbox) | Equivalent | same test, `sandbox` in the payload |
| Helpers (autonomous) and allocation policy | Supported differently: Atlas starts with helpers off, Classic unchanged | "helpers start off for a Claude Code lead"; M5 notes |
| Add a helper, start again on a task | Equivalent (Atlas names them in task terms) | "a helper is added under the current agent with the classic child payload", "a task with nobody on it gets a new agent assignment" |
| Work hierarchy | Atlas improvement: one Task tree from `parent_task_id`, as List and Map | Classic agent tree and hierarchy explorer; Atlas "root and child tasks render as one tree", Map tests |
| Agent identity, history, rename, colour, name history | Equivalent; Atlas also shows each Task's assignment history | "current, last, replaced, removed and missing agents are shown as what they are", "defaults, name and colour are saved" |
| Waiting for input, reply, one-time override | Equivalent; Atlas adds reply from a row, R and the bell | "reply goes to the current agent…", "reply from a row or with R…" |
| Decide for me, similar and always decisions | Equivalent | "decide for me, and delegating future decisions…", "saved decision policies can be turned off" |
| Redirect | Equivalent | "redirect sends a correction to a working agent" |
| Stop, Stop branch | Equivalent | "stop asks first…", "stop branch follows agent lineage…" |
| Process ownership, diagnostics, attestation | Equivalent | "an unresolved run is shown, diagnostics can be refreshed and release needs an attested statement" |
| Responsibility transfer after removal | Equivalent | "a task whose agent was removed gets a new owner only after an explicit confirmation" |
| Agent removal (single, branch), retry | Equivalent | "removal is reviewed first, refuses a changed impact…", "a blocked removal says what is pending" |
| Provider wait, resume, switch tool or model | Equivalent | "a provider wait resumes only on an explicit retry", "switching tool or model while the agent waits…" |
| Auto allocation and per-task provider permissions | Equivalent | "Auto allocation and provider permissions are edited as drafts and saved explicitly" |
| Recursive delegation, plan, promotion, rounds | Display in both; promotion is decided by the backend, no user action in either | Atlas Task page "How it's going"; Classic dashboard docs |
| Task Pause, Resume, Cancel with impact preview | Missing in Classic; Atlas improvement | Classic source has no task lifecycle call; Atlas Task page tests |
| Results and integration state | Display in both; Atlas keeps reported, recorded, verified, combined and applied apart | "results keep reported, recorded, verified, combined and applied apart" |
| Apply a root Task to its project | Backend capability without a user action in either view | `POST /tasks/{id}/integrate`; see `atlas-migration.md`, "Applying work to the project" |
| Output history | Equivalent | "output pages back through earlier lines" |
| Search | Equivalent: both search the loaded list (Atlas also by agent name, tool and model) | Classic "Search agents"; "filters, project scope and search narrow the list" |
| Colour filter | Equivalent | "the colour filter narrows to several colours…" |
| Attention list | Supported differently: Classic attention panel; Atlas bell, "need you" and the attention bar | M9 tests |
| Provider onboarding, setup operations, billing, usage, models, permissions, defaults | Equivalent (M8 parity table) | `atlas-connections.spec.mjs` |
| Provider update notices | Equivalent | "provider updates: review opens setup without installing…" |
| Browser and Windows notifications, preference | Supported differently (see Notifications) | M9 tests |
| Appearance, panel width | Equivalent; Atlas adds a Settings page (keyboard, accessibility, diagnostics) | shell and polish tests |
| Disconnected backend and stale data | Equivalent; Atlas states staleness on every screen | "an unreachable backend is reported plainly", per-screen stale tests |
| Switching interfaces with the selection | Both directions | "opening Classic from a task selects its current agent there…" |

**Missing in Atlas:** nothing found in this review. **Missing in Classic:** Task lifecycle
controls, Task pages and Projects screen. **Backend only, in both:** applying a root Task to its
project, resource claims and work intents.

## Workflow coverage (fixture)

| Journey | Covered by | Notes |
| --- | --- | --- |
| 1 Open the app, 20 switch Classic/Atlas | shell tests | Classic default verified |
| 2 Provider readiness, 16 setup, 17 usage | Connections tests | Synthetic onboarding answers |
| 3 Register or choose a project | create tests | |
| 4–6 Configure, helpers, start | create tests | Mock and fake payloads only |
| 7 List and Map, 8 parent and child | work and map tests | |
| 9 Reply, 10 decide | actions and notifications tests | |
| 11 History, 12 results, 13 unverified information | task-page tests | |
| 14 Pause, cancel | task-page tests | |
| 15 Recover, reassign | manage and start tests | |
| 18 Attention and notifications | notifications tests | Browser API stood in for |
| 19 Return to previous work | polish and work tests | |

Found and fixed while walking the journeys (M11): the model catalog showed a raw timestamp
("Last checked 2026-10-29T11:00:00Z") in New task and agent settings, now a local date; the
project hint said "your folder changes only when you apply a result", which implied an action
neither view offers, now "nothing here writes their changes into your folder". A click on "need
you" before the first task list has arrived opens Work filtered to "Needs you" without a
selection; that is acceptable and left as is.

## Safety and authorization

Atlas holds no authority of its own: every change goes through `api/agentClient.mjs` with Classic's
endpoints and payloads, the CSRF-aware `apiFetch`, and the backend's own eligibility checks
(isolation tests enforce one mutating module, one event connection and no remote hosts). Frontend
eligibility only decides what is shown.

| Operation | Safeguard | Evidence | Server-side improvement worth considering |
| --- | --- | --- | --- |
| Provider install, update, sign-in, sign-out | Reviewed plan, approved once by plan id and digest; manual-only plans can't run | Connections tests; `test_provider_onboarding` | none |
| Billing confirmation and acknowledgment | Sent only from the person's explicit check; separate from route verification | Connections tests | none |
| Task creation | Policy re-read before sending; unknown outcome needs "Start it anyway" | create tests | an idempotency key would make a retried start safe |
| Reply, redirect, decide | Current assignment only; stale-target check; one request at a time | actions tests | none |
| Stop, Stop branch | Confirmation; partial success reported by name | actions tests | none |
| Pause, Cancel | Impact preview re-read before sending; Cancel needs acknowledgement | task-page tests | a token binding the action to the previewed impact (documented at M7) |
| Agent removal | Preview token; a changed impact is refused | manage tests | none |
| Responsibility transfer, recovery, attestation | Explicit confirmations; attested statements | manage tests | none |
| Unknown network outcomes | Reported as unknown, read again, never retried | per-screen tests | none |
| Duplicate submissions | One action at a time per runner | `strict-runner.spec.mjs` | none |

Targeted backend contract tests (`test_api_security`, `test_task_detail_contract`,
`test_system_notifications`, `test_provider_onboarding`) pass: 107 tests, temporary databases.

## Notifications

**Guaranteed today:** the preference is stored per browser and told to the backend; the backend
considers only an agent that starts waiting for the person or finishes; it waits 8 seconds and
attempts a Windows notification only if no page sent a heartbeat in the last 6 seconds; Atlas sends
the heartbeat only while it can show the notification itself. Nothing guarantees that a
notification appears: Windows settings, Focus assist, browser permission and the backend being
reachable all decide that.

**Best effort:** one notification per event across Atlas tabs (Web Locks); the shared tag
replacing a repeat; Windows fallback when Atlas can't show one.

**Reproducible limitations:**

1. A Classic tab and an Atlas tab open together each show a browser notification for the same event
   (Classic neither takes the lock nor uses the tag). Reproducible by design.
2. A browser that reports a failure more than about 2 seconds after showing the event can leave it
   shown by neither (Classic has the same window). Reproducible only with a delayed failure.
3. Without Web Locks no Atlas tab shows browser notifications; Windows is the fallback.
4. With pages of tasks not loaded, Atlas shows none; Windows is the fallback.

**Can the duplicate be solved in the frontend?** Mostly, yes, without a backend change: if Classic's
browser notifications used the same tag (`control-center:{agent}:{state}`) the operating system
would replace one with the other, and if Classic also took the same Web Lock only one tab would
show them at all. Both views share one origin, which tags and locks require. This touches Classic's
notification tracker (`agentNotifications.js`, `useAgentNotifications.js`), so it is not done here.
Closing limitation 2 needs a new backend acknowledgement per event.

**Options for the owner:**

- **A. Accept the documented limitation** for initial default use. After the switch most people
  run one interface, so duplicates become rare. Cost: an occasional duplicate when both are open.
- **B. Require a small frontend correction first** (Classic adopts the tag and the lock). Removes
  the duplicates while Classic stays reachable. Cost: a change to Classic and its tests.
- **C. Block the switch until a backend reliability design** is approved (per-event
  acknowledgement). Removes limitation 2 as well. Cost: a new protocol and a longer path.

For a local single-user tool, A or B is proportionate; B is the reviewer's suggestion if Classic
stays available after the switch, A if it is hidden. The decision is the owner's.

## Performance

Measured in Edge on this machine (`e2e/atlas-performance.spec.mjs`, production build, event to the
second frame): at 1,000 loaded tasks every measured interaction is under 330 ms; at 5,000 the
slowest are switching from the List to the Map (about 1.5 s) and opening Work (about 1.1 s). Work
loads 200 tasks per page and more only on request, so 5,000 means the person loaded 25 pages. No
long-lived-session memory test was run; heap after the 5,000-task run was 200–360 MB.

**Virtualizing the List** would remove most of the 5,000-task cost but changes: tree semantics
(only rendered rows exist for `aria-setsize`/`aria-posinset`, which must be set explicitly), keyboard
navigation (moving to an unrendered row must render it first), find-in-page (unrendered rows can't
be found), screen readers (browse mode sees only rendered rows), dynamic row heights (family cards
vary), and the List/Map transition (avatars off screen don't exist). **Recommendation: safe to
defer; a separately approved change** if real workspaces regularly exceed about 1,000 loaded tasks.

## Accessibility

**Tested (fixture):** keyboard-only use of every screen (tree, map, dialogs, popovers, bell),
focus return after dialogs and popovers, the shortcut rule, visible focus, polite announcements,
reduced motion, contrast and roles through axe (no serious issue at 1440 and 640 pixels in both
themes), reflow from 1600 to 320 pixels (400 % text), right-to-left titles and names.

**Not tested:** a real screen reader. Axe does not establish screen-reader compatibility.

**Screen-reader walkthrough (NVDA or Narrator, Edge):** with the Mock provider only.

1. Open Atlas; the page title and "Work" heading are announced; Tab reaches "Skip to content".
2. In Work, Tab to the task tree; arrows move, Right and Left open and close, Enter selects; each row
   announces its title, status and agent; the details panel opens and its tabs are announced.
3. Press A; the next task that needs you is selected and announced; press R; focus lands in "Your
   answer"; type and send a reply; the result is announced once.
4. Open the bell with Enter; the "Notifications" dialog and its sections are announced; Escape
   returns to the bell.
5. Switch to the Map with M; nodes are reachable with the arrows and announce title and status.
6. Open a task page with O; the request, status and result statements are read in order.
7. New task with N: the description field, project picker (Escape returns), tool radio group,
   helpers switch and the summary are announced; Ctrl Enter is not used.
8. Connections: each tool's facts are read as separate statements; Settings: each section heading.
9. Record anything not announced, announced twice, or announced out of order.

## Windows manual checklist

Status for every item: **MANUAL TEST REQUIRED** unless marked otherwise. Items marked *fixture*
also have an **AUTOMATED FIXTURE PASS**, which does not replace the manual check. Read-only items
first; items that change runtime data or start an agent need the owner's separate go-ahead.

| # | Check | Changes data? | Status |
| --- | --- | --- | --- |
| 1 | Start the backend and frontend as in the README; both answer on 127.0.0.1 | No | MANUAL |
| 2 | Classic opens by default; Atlas preview opens and returns to Classic (*fixture*) | No | MANUAL |
| 3 | Work shows existing projects and task history correctly; counts match Classic | No | MANUAL |
| 4 | Edge and Chrome: Back, Forward, reload on Work, a Task page, Connections, Settings (*fixture*) | No | MANUAL |
| 5 | Appearance Light, Dark, Automatic following Windows (*fixture*) | No | MANUAL |
| 6 | Resize the details panel; it is kept after reload (*fixture*) | No | MANUAL |
| 7 | Windows display scaling 150 % and 200 %, and browser zoom 200 % (*fixture* for CSS widths) | No | MANUAL |
| 8 | Connections shows each installed tool's real state; no check is run without clicking | No | MANUAL |
| 9 | Paths with spaces and Hebrew folder names display correctly in Projects and New task | No | MANUAL |
| 10 | Stop the backend: Atlas says it isn't answering, keeps the last data, disables actions; restart: it recovers (*fixture*) | No | MANUAL |
| 11 | Process ownership for a task with an earlier run shows the real state | No | MANUAL |
| 12 | Notifications: turn on in Settings; the browser asks once; permission denied is reported | Preference only | MANUAL |
| 13 | With a Mock agent waiting for a reply: Atlas shows one browser notification; close Atlas and repeat: a Windows notification appears | Starts a Mock agent | MANUAL, needs go-ahead |
| 14 | Focus assist on: notifications are held by Windows; nothing claims they were shown | Starts a Mock agent | MANUAL, needs go-ahead |
| 15 | Classic and Atlas open together: note duplicates (limitation 1) | Starts a Mock agent | MANUAL, needs go-ahead |
| 16 | Reply, Pause, Resume, Cancel on Mock tasks; results read correctly | Changes Mock tasks | MANUAL, needs go-ahead |
| 17 | A real provider task end to end | Uses a paid plan | MANUAL, needs explicit go-ahead |

## Open product decisions

| Decision | User value | Workaround today | Dependency | Risk if deferred | Classification |
| --- | --- | --- | --- | --- | --- |
| Mixed Classic/Atlas notifications | No duplicate alerts | Use one interface at a time | Frontend change to Classic (B) or backend protocol (C) | Occasional duplicates | ESSENTIAL BEFORE DEFAULT (a decision, not necessarily code) |
| Stronger delivery guarantees | No lost alert in the failure window | Watch the bell | Backend acknowledgement protocol | Rare missed alert | REQUIRES SEPARATE ARCHITECTURAL APPROVAL |
| Safe preview before Apply to project | Apply from the UI with confidence | API endpoint; neither view applies | Backend preview and token | None compared with Classic | REQUIRES SEPARATE ARCHITECTURAL APPROVAL |
| Global search across unloaded tasks | Find old work fast | Load older pages, then find | Backend search endpoint | Slower search in large histories | SAFE TO IMPLEMENT LATER (backend part needs approval) |
| Ask for changes | Iterate on a result in place | Start again or reply | New backend workflow | None compared with Classic | REQUIRES SEPARATE ARCHITECTURAL APPROVAL |
| Safe access to a task's working copy | Inspect files directly | Path in technical details | Local-file action outside the browser | None compared with Classic | REQUIRES SEPARATE ARCHITECTURAL APPROVAL |
| Native Windows folder picker | Fewer path typos | Type or paste the path | Local helper | None | SAFE TO IMPLEMENT LATER |
| List virtualization | Fast Map switch at 5,000 tasks | Fewer loaded pages | Frontend rewrite with accessibility work | Slow switch only in very large sessions | SAFE TO DEFER; separate approval |

## Design continuity

The approved concepts are present: the premium shell, Work as List and Map with the same selection,
project hierarchy previews, task-centric navigation and Task pages, agent controls in the details
panel, progressive disclosure, a calm notification panel, and Connections and Settings. Deliberate
differences are recorded in `atlas-migration.md` (bands instead of one endless row, no rotating
output line, no Apply). No significant regression or unassigned gap was found in the M10 and M11
screenshots.

## Readiness matrix

| Area | Implementation (source and fixture) | Real runtime |
| --- | --- | --- |
| Classic parity | PASS | NOT TESTED |
| Workflow completeness | PASS WITH CONDITIONS (Apply, Ask for changes absent in both) | NOT TESTED |
| Backend safety | PASS | NOT TESTED |
| Provider action safety | PASS | NOT TESTED (no real install or sign-in) |
| Task and Agent identity | PASS | NOT TESTED |
| Data correctness | PASS WITH CONDITIONS (loaded-only counts are labelled) | NOT TESTED |
| Accessibility | PASS WITH CONDITIONS (no screen-reader session) | NOT TESTED |
| Performance | PASS WITH CONDITIONS (5,000-task Map switch about 1.5 s) | NOT TESTED on other hardware |
| Error handling | PASS | NOT TESTED |
| Notification reliability | PASS WITH CONDITIONS (limitations 1–4) | NOT TESTED |
| Windows platform behaviour | NOT TESTED | NOT TESTED |
| Unresolved decisions | BLOCKED on the owner's notification decision | n/a |

## Validation run for this review

See the final M11 report for the exact commands and counts; all were run on 2026-10-31 on this
machine (Windows, Microsoft Edge through Playwright, Node, the backend virtual environment), with
synthetic data only.
