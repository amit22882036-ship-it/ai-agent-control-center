---
name: AI Agent Control Center (Lumen)
description: Living light. A premium, calm workspace for AI coding agents where every state reads at a glance and real work in progress is shown as light.
colors:
  lumen-ultramarine: "#4255FF"
  lumen-ultramarine-text: "#3346F0"
  lumen-light: "#8E99FF"
  amber-needs-you: "#FF7A00"
  amber-needs-you-text: "#B04A00"
  violet-waiting: "#9A5CF5"
  violet-waiting-text: "#7A3FD6"
  mint-finished: "#12B76A"
  mint-finished-text: "#067647"
  coral-failed: "#C4320A"
  resting-grey: "#8A93A5"
  ink: "#101828"
  text-secondary: "#344054"
  text-muted: "#565F70"
  porcelain: "#F3F4F7"
  surface: "#FFFFFF"
  dark-ground: "#0E1016"
  dark-surface: "#171A24"
  dark-ultramarine: "#7C8AFF"
  dark-amber: "#FF8A1F"
  dark-violet: "#B48CFF"
  dark-mint: "#32C17C"
  dark-coral: "#FF7A6E"
labels:
  red: "#F04438"
  orange: "#F79009"
  yellow: "#FAC515"
  green: "#17B26A"
  cyan: "#06AED4"
  blue: "#2E90FA"
  violet: "#7A5AF8"
  pink: "#EE46BC"
typography:
  text: "Segoe UI Variable Text (SF Pro on macOS), Heebo for Hebrew"
  display: "Segoe UI Variable Display"
  mono: "JetBrains Mono, for times and identifiers only"
---

# Design System: AI Agent Control Center (Lumen)

## Overview

**Creative North Star: "Living light"**

The Control Center is where a person runs a team of AI coding agents. They glance at it between other work, so the first second matters: does anything need me, is the work moving, where should I look. Lumen answers like a well-made instrument. The canvas is calm porcelain and white; every state is said three times at once, in a word, a familiar symbol and one colour, so nothing needs a legend; and the only things that move are the agents that are really working, drawn as light. The name is the idea: work in progress is light.

The bar is premium desktop craft (the owner's reference point is Apple-level quality) without copying any platform. The identity is our own: one signature colour, Lumen ultramarine; amber light reserved for the person; our own state marks; and the living light that travels round a running agent.

How we got here. Three directions were built in the real app and judged by the owner. An orienteering map (interesting, not an identity) and an architectural massing model with a 3D Site (cumbersome: every state had to be decoded from a material) were rejected. A plain "clear signal" palette read as dull. The lessons are the rules of this system: **no metaphor the person has to learn; vivid, confident colour with one meaning each; the "wow" comes from craft and from light that shows real activity.**

Code under `frontend/src/atlas` is still named Atlas. Tokens live in `styles/tokens.css` (`--a-*`, scoped to `.atlas`, the "Lumen, living light" block is the active layer); the last stylesheet, `styles/signal.css`, holds the shared language, the sidebar, the Map treatment and the living light.

## Colour

| Meaning | Mark (light) | Text (light) | Mark (dark) |
|---|---|---|---|
| Needs you (a question, a new owner) | amber `#FF7A00`, filled "!" | `#B04A00` | `#FF8A1F` |
| Working, reviewing helper results | ultramarine `#4255FF`, half-filled ring | `#3346F0` | `#7C8AFF` |
| Waiting (helpers, provider, dependencies) | violet `#9A5CF5`, three dots | `#7A3FD6` | `#B48CFF` |
| Finished | mint `#12B76A`, check | `#067647` | `#32C17C` |
| Failed | coral, filled "×" | `#C4320A` | `#FF7A6E` |
| Resting: paused, blocked, not started, canceled | grey `#8A93A5` | text grey | `#8A93A5` |

Rules
- **Ultramarine is the brand.** Selection, focus, the primary action (white on `#3346F0`, 6.4:1) and agents at work.
- **Amber is spent only on the person.** A calm screen has no amber at all.
- **Colour is never alone.** Every mark carries a symbol and every state has words beside it. Text meets 4.5:1 and marks 3:1 in both appearances (checked with axe).
- **Colour labels are the person's own grouping.** The eight label colours (above) are vivid and appear as a bold tag on the edge of every labelled row and Map card, and as a list in the sidebar.

## Living light (the signature)

- A gleam of light travels round the mark of every task whose agent is recorded running.
- On the Map, a ring of light turns round a working agent's face.
- In the Now bar, a sheen crosses the working share of the workspace.
- Nothing else moves by itself. Nothing is lit unless the backend recorded the agent running. Reduced motion keeps every light still.

## State marks

All marks are 16 px circles drawn in `components/identity.jsx` (`StatusBead`; `live` adds the gleam).

| State | Symbol |
|---|---|
| Needs you | white "!" on a filled amber circle |
| Working | a ring, half filled |
| Waiting | a ring with three dots |
| Paused | a ring with two bars |
| Blocked | a ring with one bar across |
| Not started | a dashed ring |
| Finished | a ring with a check |
| Failed | white "×" on a filled coral circle |
| Canceled | a ring with a slash |
| Agent removed | a dashed ring with a small "×" |

## Layout

- **Sidebar:** docked and translucent in wide windows (folds to icons below 1200 px, or with `\`). Pages; smart lists with live counts (Needs you, Working, Waiting, On hold, Done); projects with their colour tiles; colour labels with counts. The current place is a soft ultramarine pill.
- **Top bar:** Go to (Ctrl K), the questions waiting for the person, notifications, connection state, New task.
- **Work header:** project, colour filter, search, List / Map.
- **Now strip:** counts of loaded tasks, each a toggle that scopes the page, with Next (A) to step through what needs you. Under it one bar shows open work in proportion.
- **List:** Needs you first, then open work, then history folded by day. A family is one row with its progress bar and "finished/total"; helpers open beneath with tree guides; a question carries its Reply button in the row.
- **Map:** the same families as a hierarchy on a clean canvas: what needs the person in one band, open work as compact cards, history quieter. Same marks, bars and tags as the List.
- **Quiet state:** with nothing filtered and nothing open: "Nothing is running or waiting for you right now".

## Typography

The platform's text face (Segoe UI Variable on Windows, SF Pro on macOS) for its legibility and native feel; Heebo for Hebrew, every user-written string laid out `dir="auto"`; JetBrains Mono only for times and identifiers. Hierarchy comes from weight and size together: counts 24 px bold, page titles 30 px bold, row titles 15 px semibold, status 13 px in its colour, metadata 12.5 px muted.

## Motion

Critically damped springs (`--a-spring`). Rows that move because of a live change glide from where they were while the selected row stays still; progress segments animate their share. The living light is the only ambient motion, and only for real activity.

## Do

- Say the state in words next to its mark.
- Keep counts honest: loaded tasks only, and say so when older pages remain.
- Let a colour label read as a group: same tag, same colour, everywhere.

## Don't

- Don't add a metaphor that must be learned to read a state.
- Don't use amber for anything except what needs the person.
- Don't animate anything that did not change, or light anything that is not running.
- Don't show percentages, estimates or verification the backend did not record.
