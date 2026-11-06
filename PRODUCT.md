# Product

<!-- impeccable:product-schema 1 -->

Written from the owner's design brief of 2026-11-05 and the repository's own documentation; facts
marked *(inferred)* are reasonable readings of that brief rather than separately confirmed answers.

## Platform

web

## Users

- **Primary:** a software engineer on their own Windows machine who runs several AI coding agents
  (Claude Code, Codex, Gemini CLI) on their own repositories, often at the same time, and needs to
  steer them: answer their questions, redirect or stop them, review and apply their results. They
  keep the app open for long working sessions next to an editor and terminals and come back to it
  between other work *(inferred)*.
- **Evaluators:** recruiters, software engineers and AI engineers who see the project through its
  README, screenshots and a live demonstration by the owner.

## Product Purpose

Turn several terminal agents into supervised, durable, inspectable work. The person describes work
in plain language; an agent starts in an isolated Git worktree; a lead agent may bring in helper
agents, which the backend admits under explicit limits; results come back through conflict-checked
integration; and everything that needs the person is surfaced and answerable in place. Success: the
person always knows what is happening, what needs them and what changed, and can act without hunting,
whether there are five tasks or a thousand.

## Positioning

A local, single-user control center that owns the hard parts itself: admission, process supervision
(Windows Job Objects), per-task isolation, journaled integration and recovery. It never trusts an
agent's claim of being done, never estimates, and never shows activity that was not recorded.

## Operating Context

- A Windows desktop app (Electron around the FastAPI backend, which serves the interface); the same
  interface runs in a browser during development. Live updates arrive over one event connection.
- Tasks are read newest first in pages of 200 (up to 10,000 loaded); older pages load on request.
- Work is organised as task families: a top-level task, its helper tasks, and their helpers, each held
  by an agent (current, last, replaced or removed), inside registered projects.

## Capabilities and Constraints

- Task lifecycle (pause, resume, cancel, start again), agent lifecycle (reply, redirect, stop, remove,
  transfer responsibility), delegation and integration review, task and project renaming, project
  order, hide, Trash, restore and permanent deletion, agent access levels, provider connections,
  notifications, Work List and Work Map (top-to-bottom and left-to-right).
- Only backend-reported facts are shown; no percentages, no progress estimates, no simulated activity.
- Backend contracts and task lifecycle semantics are fixed for interface work.
- A live change must never move the person's selection or scroll position.

## Brand Commitments

- Name: "AI Agent Control Center". The interface family is called Lumen (its code is still named Atlas).
- Voice: plain and exact; says what is known and what is not, never reassures without evidence.
- English interface; titles and names in Hebrew (right to left) must display correctly.

## Evidence on Hand

Synthetic demonstration data only (the browser-test scenarios and the desktop app's demo mode). No
customers, testimonials, benchmarks or usage statistics exist, and none may be invented.

## Product Principles

1. The person's attention is the scarcest resource: what needs them comes first and can be answered where it is shown.
2. Truth over reassurance: recorded state, labelled uncertainty, nothing implied.
3. Calm under load: a busy workspace reads as clearly as a quiet one.
4. Never lose the person's place: selection, scroll and context survive live updates and view changes.
5. Power without ceremony: every common action is a keystroke or one click from where the work is shown.

## Accessibility & Inclusion

WCAG 2.2 AA contrast, complete keyboard operation with visible focus, meaningful screen reader names
and polite announcements, reduced motion and reduced transparency honoured, reflow to 320 px at 400 %
text (an existing automated check), and correct bidirectional text.
