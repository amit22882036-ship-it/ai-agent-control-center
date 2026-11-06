---
name: AI Agent Control Center (Lumen)
description: An orienteering map of the person's AI coding agents; what needs them rides above the terrain as one purple course.
colors:
  course-purple: "#8E1C7C"
  course-purple-mark: "#B02A9B"
  forest-green: "#2B7A2F"
  forest-green-live: "#3FAE4A"
  water-blue: "#1F72B0"
  olive: "#6A7220"
  contour-brown: "#A8733F"
  contour-line: "#C4A47F"
  alert-red: "#C3332B"
  open-land-yellow: "#F2C33A"
  black-ink: "#121512"
  on-ink: "#F7F8F4"
  map-paper: "#EEF0EA"
  map-paper-light: "#F7F8F4"
  map-paper-shade: "#E6E9E1"
  sheet-paper: "#FFFFFF"
  forest-black-rail: "#132019"
  rail-hover: "#1D2C23"
  rail-puck: "#28392E"
  rail-ink: "#A3B3A8"
  rail-ink-strong: "#F3F5F1"
  text-secondary: "#454B46"
  text-muted: "#5A615B"
  text-faint: "#98A09A"
  night-map-paper: "#0D100E"
  night-sheet-paper: "#171C18"
  night-rail: "#050806"
  night-black-ink: "#EFF2ED"
  night-on-ink: "#101310"
  night-text-secondary: "#B4BCB5"
  night-text-muted: "#949D96"
  night-text-faint: "#5E6760"
  night-course-purple: "#EE8BDD"
  night-course-purple-mark: "#E070CE"
  night-forest-green: "#6CCB72"
  night-water-blue: "#6AB2EA"
  night-olive: "#BCC45E"
  night-contour-brown: "#C99A68"
  night-contour-line: "#5A4A38"
  night-alert-red: "#FF7468"
typography:
  display:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Display, Segoe UI, system-ui, sans-serif"
    fontSize: "34px"
    fontWeight: 760
    lineHeight: 1.06
    letterSpacing: "-0.04em"
    fontVariation: "'opsz' 96"
  headline:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Display, Segoe UI, system-ui, sans-serif"
    fontSize: "16.5px"
    fontWeight: 680
    lineHeight: 1.3
    letterSpacing: "-0.016em"
  title:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.25
    letterSpacing: "-0.014em"
  body:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 650
    lineHeight: 1
    letterSpacing: "-0.005em"
  numeral:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Display, Segoe UI, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "-0.01em"
    fontVariation: "'wdth' 75"
    fontFeature: "'tnum'"
  control-numeral:
    fontFamily: "Atlas Bricolage Grotesque, Atlas Heebo, Segoe UI Variable Text, Segoe UI, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 700
    lineHeight: 1
    fontVariation: "'wdth' 75"
    fontFeature: "'tnum'"
  mono:
    fontFamily: "Atlas JetBrains Mono, ui-monospace, Consolas, monospace"
    fontSize: "12px"
    fontWeight: 500
    lineHeight: 1
    letterSpacing: "-0.01em"
rounded:
  control: "7px"
  field: "12px"
  row: "13px"
  group: "18px"
  sheet: "26px"
  dialog: "28px"
  pill: "999px"
spacing:
  hairline-gap: "2px"
  xs: "4px"
  sm: "8px"
  md: "12px"
  sheet-inset: "14px"
  lg: "16px"
  indent: "22px"
  course-gutter: "46px"
components:
  button-primary:
    backgroundColor: "{colors.black-ink}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "34px"
  button-ask:
    backgroundColor: "{colors.course-purple-mark}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "34px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.text-secondary}"
    rounded: "{rounded.pill}"
    padding: "0 14px"
    height: "34px"
  ask-reply:
    backgroundColor: "{colors.course-purple}"
    textColor: "{colors.sheet-paper}"
    rounded: "{rounded.pill}"
    padding: "0 10px"
    height: "24px"
  view-switch-selected:
    backgroundColor: "{colors.black-ink}"
    textColor: "{colors.on-ink}"
    rounded: "{rounded.control}"
    padding: "0 11px"
    height: "28px"
  field-search:
    backgroundColor: "{colors.map-paper-shade}"
    textColor: "{colors.black-ink}"
    rounded: "{rounded.pill}"
    height: "34px"
  row-ask:
    backgroundColor: "{colors.sheet-paper}"
    textColor: "{colors.black-ink}"
    rounded: "{rounded.row}"
    height: "84px"
  row-family:
    backgroundColor: "{colors.sheet-paper}"
    textColor: "{colors.black-ink}"
    rounded: "{rounded.row}"
    height: "64px"
  row-member:
    backgroundColor: "{colors.sheet-paper}"
    textColor: "{colors.black-ink}"
    rounded: "{rounded.row}"
    height: "50px"
  row-history:
    backgroundColor: "{colors.sheet-paper}"
    textColor: "{colors.text-muted}"
    rounded: "{rounded.row}"
    height: "44px"
  row-selected:
    backgroundColor: "{colors.open-land-yellow}"
    textColor: "{colors.black-ink}"
    rounded: "{rounded.row}"
  rail:
    backgroundColor: "{colors.forest-black-rail}"
    textColor: "{colors.rail-ink}"
    rounded: "{rounded.sheet}"
    width: "60px"
---

# Design System: AI Agent Control Center (Lumen)

## Overview

**Creative North Star: "The Orienteering Map"**

Lumen draws the person's workspace as an orienteering map. The terrain is printed in a small set of inks whose meanings never change from screen to screen, on map-paper white by day and on a night-map charcoal after dark. Over that terrain runs the course: what needs the person, as numbered control circles joined by one purple line, in the same order the A key steps through them. The person reads the legend, runs the course control to control, answers each where it stands, and the rest of the terrain stays quiet.

The system is dense and tabular rather than decorative. Work sits on one paper sheet beside a forest-black rail, in grouped rows on a fixed baseline, and state is carried by symbol, word and ink together, never by colour alone. Finished work does not vanish; it recedes into a faint ledger. The world refuses the grey dashboard with one accent and the stack of floating cards: the sheet is paper, groups are hairline-separated lists, and the only saturated colour on a calm screen is the course.

Code under `frontend/src/atlas` is still named Atlas; the tokens live in `styles/tokens.css` (`--a-*`, scoped to `.atlas`), and the Work styles in `sheet.css`, `map.css` and `lumen.css`.

**Key Characteristics:**
- Fixed ink meanings: purple is attention, green working, blue waiting, olive held, brown hierarchy and finished, a black cross canceled, yellow selection, black ink focus and primary action.
- One course: numbered controls on a single purple line, in A order, in both the List and the Map.
- Paper, not glass: one content sheet, white groups, hairline separators, inset rounded selection.
- Bricolage Grotesque for words with its condensed width for numerals; JetBrains Mono only for times and identifiers; Heebo for Hebrew.
- Three row densities: the ask (tall, says what it needs), the regular row, and the history ledger line.
- Motion is rare and physical: one control circle drawn once, FLIP for rows that move live, nothing when reduced motion is set.

## Colors

A map's inks on map paper: each colour is a legend entry with exactly one meaning, and black ink does the structural work.

### Primary
- **Course Purple** (`course-purple`, with the brighter `course-purple-mark` for strokes and fills): only what needs the person. The course line, control circles and their numerals, the ask bead, the ask line and Reply on an ask row, the Needs-you legend count, the needs section heading, the rail's needs dot. Never decoration, never a brand accent.
- **Black Ink** (`black-ink`, `on-ink` on top of it): text, primary buttons, the selected List/Map switch, and every focus ring (`--a-accent` resolves to black ink by day and to its pale twin at night).

### Secondary
- **Forest Green** (`forest-green`; `forest-green-live` for the live strand mark and rail tile glow): an agent recorded working. The working bead with its pulse ring, the working count, working edges on the Map.
- **Water Blue** (`water-blue`): waiting on helpers. The dashed waiting bead and the waiting strand mark.
- **Olive** (`olive`): on hold, paused or blocked. Paused and blocked beads, held strand marks, Map warning chips.

### Tertiary
- **Contour Brown** (`contour-brown`): finished terrain. The done bead and the finished strand mark (at 45%).
- **Contour Line** (`contour-line`): the hierarchy's lines, the guides between parent and child rows and the edges on the Map and in project thumbnails.
- **Open-Land Yellow** (`open-land-yellow`): selection only. Selected rows sit on it (`--a-sel`, 26% by day, 16% at night) with a 70% yellow inner edge; text selection uses it at 45%; the rail's active puck carries a 3px yellow edge.
- **Alert Red** (`alert-red`): failed tasks (a filled triangle bead) and destructive actions. It is the one ink the contract did not list; the build uses it consistently and only for failure and danger.

### Neutral
- **Map Paper** (`map-paper`, graded from `map-paper-light` to `map-paper-shade` in a 168° ground): the window ground behind the rail and sheet.
- **Sheet Paper** (`sheet-paper`): row groups, the legend's chosen state and grouped cards on every page.
- **Forest-Black Rail** (`forest-black-rail`, with `rail-hover`, `rail-puck`, `rail-ink`, `rail-ink-strong`): the navigation rail in both appearances (darker still at night, `night-rail`).
- **Text Secondary / Muted / Faint** (`text-secondary`, `text-muted`, `text-faint`): second lines and section headings; times and finished titles; non-text marks and disabled states.
- **Night map** (`night-*`): the same legend after dark on a neutral green-black, never navy. Each ink lifts in lightness to hold contrast on `night-map-paper` and `night-sheet-paper`; meanings do not change.
- **Agent colour labels** (`--a-agent-*` pastels): an agent's own label, drawn only as a thin ring on a paper disc in the List and on the Map, so colour on Work stays with state.
- **Project hue** (`--hue`, an OKLCH hue per project): only on small project marks (rail tiles, the 7px row dot, project tiles). Never on a state.

### Named Rules
**The Fixed Legend Rule.** Every ink has one meaning on every screen. If a colour would mean something different here than on the legend strip, use black ink and words instead.

**The Course Is Purple Rule.** Course purple appears only on what needs the person. A calm workspace with nothing waiting on them shows no purple at all.

**The Yellow Is Where You Are Rule.** Open-land yellow marks the selection and nothing else; focus is a black ring, never a yellow fill.

## Typography

**Display Font:** Bricolage Grotesque (self-hosted as Atlas Bricolage Grotesque, variable weight 200 to 800 and width 75 to 100%, with Segoe UI Variable fallback)
**Body Font:** Bricolage Grotesque, the same family at text optical size
**Label/Mono Font:** JetBrains Mono (Atlas JetBrains Mono, 400 to 500, with ui-monospace and Consolas)
**Hebrew:** Heebo (Atlas Heebo, 400 to 700), second in every word stack and loaded only for the Hebrew range

**Character:** One grotesque does the whole map's lettering, wide and heavy for titles, condensed for the numbers a map prints beside its symbols. Mono is a measuring instrument, reserved for times and identifiers.

### Hierarchy
- **Display** (760, 34px, 1.06, -0.04em, optical size 96): page titles. The first-run title is the one larger use (40px).
- **Headline** (680, 16.5px, 1.3, -0.016em): card and group titles on the settings and project pages; project names on plates are larger (680, 19px).
- **Title** (650, 15px, 1.25, -0.014em): a family's lead task in the List; member rows are 14px at 600, ask rows 680, history lines 13.5px at 500.
- **Body** (400, 14px, 1.45): running text; long text holds to 60 to 76ch.
- **Label** (650, 13.5px, -0.005em, sentence case): section headings, the pinned section band and Map band headings. Its count is set lighter (600) in faint ink.
- **Numeral** (700, 24px, condensed to 75% width, tabular): legend counts. Control numerals use the same condensed cut at 700, 12px in the List gutter and 15px on the Map.
- **Mono** (500, 12px, tabular): row times, team counts and strand totals (600, 11.5 to 12px), day headings and Go to group heads (600, 11.5px).

### Named Rules
**The Measured Numbers Rule.** Counts are the map's condensed lettering, never hero numbers; times and identifiers are mono. No numeral is ever enlarged for effect.

**The Sentence Case Rule.** Labels are headings in their own right: sentence case, normal tracking. Tracked capitals above a heading are not part of this system.

**The Hebrew Reads Right Rule.** Any title or name that may be Hebrew is set `dir="auto"` with plaintext bidi, and Heebo carries it in the same stack.

## Layout

The window is a fixed forest-black rail on the left (60px collapsed, 252px open, 12px from the edges; pinned open the content pads to 276px) and one content sheet to its right under a transparent title bar (Work, project scope, Go to). Work's sheet carries the header, the legend strip, then the List or the Map. The details panel appears at right only when a task is selected; below 1100px it floats over the sheet without covering the Work header.

**The List** is a single virtualized tree on fixed row heights, scaled with the root font so 200% text keeps the geometry. Rows are a five-column grid on one baseline (task, team, agent, state, time at 2.1fr / 150px / 196px / 1.5fr / 74px, 16px gaps) inset 14px from the sheet. The main List reserves a 46px course gutter on its left edge. Hierarchy indents 22px per level for seven levels, then the row states its depth in a mono numeral instead of indenting further. Narrower containers drop columns from the right inwards (team at 1180px, agent at 900px, state at 560px); the task and its state mark always stay.

Row densities are the system's rhythm:
- **Ask** (84px): the task that needs the person, taller, with the ask line and Reply on the row.
- **Regular** (family lead 64px, member 50px): open work.
- **History ledger** (lead 44px, member 38px): one quiet line per finished task, no second lines; a plainly completed line leaves its state column empty, so only exceptions speak.
- Sections 52px, day rules 36px.

**The Map** lays families in bands on the same paper, node tree per family (nodes 168px wide, helper rows 216 by 54px). From six families in a band (never in the needs band), closed single-task families leave the tree for **the Map shelf**: capsules 272 by 62px on a 12px grid under the band, each carrying its title, project and strand. Finished shelf items are ruled ink lines on the table rather than paper capsules. Beside the Map, an optional 304px side list (248px under 1180px) repeats the List as titles only.

### Named Rules
**The Never Move My Place Rule.** A live change never moves the selection or scroll position; rows that move are animated from where they were, and the window stays where the person left it.

**The Exceptions Speak Rule.** History is never hidden, only quieter. The ledger says nothing a plain completion would repeat.

## Elevation & Depth

Depth is paper on paper: the sheet and its white groups lift off the map ground with soft, low shadows and hairline edges, and floating things (menus, dialogs, the details panel, the open rail) lift further. Nothing glows. Finished and unchosen terrain recedes by opacity and greyscale rather than by shadow.

### Shadow Vocabulary
- **Group** (`--a-group-shadow`: `0 0 0 1px rgba(16,18,24,.05), 0 1px 2px rgba(16,20,34,.05), 0 12px 28px -20px rgba(16,20,34,.32)`): row groups, cards and project plates on the sheet.
- **Low** (`--a-shadow-1`): Map helper rows, map tools, hover-quiet controls.
- **Raised** (`--a-shadow-2`): the details panel, the minimap, map controls, hover on plates.
- **Floating** (`--a-shadow-3`): menus, dialogs, a lifted plate while dragging.
- **Sheet** (`0 0 0 1px var(--a-line-3), 0 1px 0 var(--a-spec) inset, 0 30px 70px -40px rgba(20,32,70,.38)`): the one content sheet.

### Named Rules
**The Paper Not Glass Rule.** The sheet is paper and the Go to field is an ordinary field. No decorative glow, gradient wash or frosted glass on working surfaces.

## Shapes

Soft, generous radii nest from outside in: the rail and sheet at 26px, dialogs at 28px (Go to 22px), row groups at 18px, inset row hover and selection at 12 to 13px, fields at 12px, segmented items and small buttons at 7px, and pills (999px) for buttons, the search field, Reply and chips. Hover and selection are inset rounded shapes inside the row, like a native sidebar, not full-bleed bars. The map's own symbols keep their geometry: control circles are true circles, beads are 16px pictograms (filled circle, dashed ring, pause bars, diamond, triangle, check, cross), and strand marks are 4 by 14px rounded bars.

## Components

### Buttons
Quiet, pill-shaped and pressable.
- **Shape:** full pill (999px), 34px tall, small variant 28px.
- **Primary:** black ink with on-ink text; hover drops to 90% opacity.
- **Ask:** the course purple fill, only for answering what needs the person.
- **Ghost:** transparent with secondary text; hover fills with the faint hover ink.
- **Danger:** alert-red text; in a confirming dialog a solid red fill.
- **Hover / Focus:** press scales to 0.97 on a critically damped spring; focus is a 2px black-ink ring, 2px offset.

### Legend strip (the Now strip)
- **Style:** a row of states, each its bead, condensed numeral and label; it also filters. Zero counts fall to faint ink with a 45% bead.
- **State:** the chosen scope is underlined (2px black-ink inset rule), never carded. The Needs-you count is course purple and carries a control circle around its numeral; Working is forest green. A Go button beside Needs you jumps to the first control, as A does.

### Rows (List)
- **Corner Style:** inset 12 to 13px shape behind the row, groups 18px at their ends.
- **Background:** sheet paper; hairlines (1px) between families start at 56px.
- **States:** hover faint ink; selected open-land yellow with a yellow inner edge; focus a 2px black-ink inset ring.
- **Ask row:** title at 680, an ask line in course purple stating what is needed, and a Reply pill (course purple, 24px). The state column hides because the line says it; narrow widths drop the words before Reply.
- **Canceled:** struck through (1.5px) in muted ink with greyscale faces, the stopped bead a black cross, the strand mark hatched.

### The course (signature)
- **List:** each ask is a numbered control circle in the 46px gutter (paper fill, 2.25px course-purple stroke, condensed numeral), joined top to bottom by one 2px purple line in the order A steps. The current control fills purple with an on-ink numeral.
- **Map:** control circles ring the marked agent faces (2.5px stroke, 4px when current), numbered in 15px condensed purple with a paper halo, joined by one 2.5px leg line.
- **Motion:** the control circle is drawn once (0.62s stroke draw) the first time a task appears as needing the person in the window, then stays still; reduced motion shows it already drawn.

### The family strand
- **Style:** a family's tasks as one 4 by 14px mark each, lead first and wider (8px), in its state ink: purple needs, live green working, blue waiting, olive held, outlined pending, brown at 45% finished, hatched canceled; followed by a mono finished/total count. Never a percentage.
- **Use:** the List's team column (with a screen-reader sentence of the counts), under Map nodes, and on shelf capsules at reduced size; history rows shorten it to 9px.

### Inputs / Fields
- **Style:** the Work search is a 34px pill on the sunken ink with no border; other fields 12px radius.
- **Focus:** the field turns to paper with a 2px black-ink-line ring and a 5px soft halo; caret in black ink.
- **Error:** alert-red text under the field.

### Navigation
- **Rail:** forest-black, icons 46px tall, 16px inset shapes. Hover lifts to `rail-hover`; the current page sits on a sliding puck with a 3px open-land-yellow edge. Project tiles carry their project hue; a live project glows green, a project that needs the person fills and shows the needs dot. Opening widens it on a spring with staggered labels.
- **Go to:** a 38px pill in the title bar opening a 680px dialog of grouped results with yellow active selection.

### Map nodes and shelf
- **Nodes:** agent face as a paper disc with its label ring, a state ring (green pulse working, purple ask, dashed idle, black when selected), title, state in its ink, and the strand. Unchosen families grey out (0.9 greyscale, 82%) when one is anchored; finished ones draw at 55 to 62% until pointed at.
- **Shelf capsule:** 16px radius, title, project in faint ink, small strand; finished capsules become ruled lines.

## Do's and Don'ts

### Do:
- **Do** keep every ink to its legend meaning: course purple for what needs the person, forest green working, water blue waiting, olive held or blocked, contour brown for hierarchy lines and finished terrain, a black cross for canceled, open-land yellow for selection, black ink for text, primary action and focus.
- **Do** pair every state colour with a shape and a word (beads are images with names unless the word sits beside them).
- **Do** draw what needs the person as the course: numbered controls on one purple line in A order, in the List gutter and on the Map.
- **Do** choose the row density by meaning: ask (84px), regular (64px lead, 50px member), history ledger (44px lead, 38px member).
- **Do** set counts in the condensed Bricolage numeral and times and identifiers in JetBrains Mono, tabular.
- **Do** animate a row that moved live from where it was (FLIP, 320ms, `cubic-bezier(.2,.8,.2,1)`), fade a new one in from 6px (260ms), and keep selection and scroll still.
- **Do** honour reduced motion (no animation or transition anywhere), reduced transparency (opaque surfaces, no blur), and more contrast (heavier lines and muted text).
- **Do** keep the List one tree with one Tab stop, arrow-key navigation, levels, set sizes and positions.

### Don't:
- **Don't** use course purple for brand, decoration, links or anything that does not need the person.
- **Don't** give a state a colour without its symbol and word, or a project hue to a state.
- **Don't** hide history; quiet it into the ledger instead.
- **Don't** show percentages, progress estimates or enlarged hero numbers.
- **Don't** put tracked capitals above a heading; labels are sentence case.
- **Don't** add decorative glow, gradient washes or frosted glass to the sheet or working surfaces.
- **Don't** redraw or pulse the control circle after it first appears; it is drawn once.
- **Don't** fill agent faces with their pastel labels on Work; the label is a thin ring on paper.

## Known divergences from the direction contract

Recorded so later work does not mistake them for rules:
- **Mobile shell not built.** Below 760px the rail stays a 60px fixed column and the title bar wraps; there is no mobile navigation or single-column Work shell yet.
- **Map semantic zoom not built.** Zoomed out past 0.62 the Map only drops secondary text (state lines, chips, second title lines); families do not re-symbolize by scale.
- **Rail needs text is yellow.** On the rail, the "needs you" project and link captions use open-land yellow (and rail focus rings are yellow), not course purple and black ink, because neither reads on the forest-black rail. This bends the Fixed Legend Rule and is unresolved.
- **Leftover Lumen 1 glass.** Menus, dialogs, the details panel, the open rail and project plates still carry backdrop blur from the earlier glass direction; the working sheet does not. Not a rule.
- **Tracked capitals remain** on the rail's section head and some secondary pages (about 19 uppercase rules in the stylesheets).
- **Faint ink on text.** Section counts and some labels use `text-faint`, which is below 4.5:1 on paper.
