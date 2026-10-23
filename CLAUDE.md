# Claude Code instructions — AI Agent Control Center

[AGENTS.md](AGENTS.md) is the shared engineering rulebook for every coding agent in this repository. Read it in full and follow it before modifying the project. Its rules apply to Claude Code exactly as written, even where its wording addresses Codex. This file adds only Claude Code-specific guidance and does not replace or override AGENTS.md. If anything here appears to conflict with AGENTS.md, follow AGENTS.md and raise the conflict with the user.

Work from this Git root. Newer explicit user instructions take precedence, as AGENTS.md describes.

## Before changing anything

- Inspect the current Git branch, status and uncommitted changes, and preserve existing work.
- Read the current source and the relevant documents under `docs/` before proposing a change. Extend existing implementations, APIs and persistence mechanisms; propose a replacement or a new abstraction only after showing why the existing one cannot serve.
- Preserve the architecture and safety invariants listed in AGENTS.md and documented under `docs/`.
- Before implementing a milestone, agree its scope, assumptions, edge cases and architectural implications with the user. Do not start implementation until the user has confirmed them.

## Current behavior versus history

- The current source code is the authority on what is implemented. Current documents describe it; verify their claims against the code.
- [docs/history/](docs/history/stage1-handoff.md) and the Stage labels inside other documents are historical design records. Those labels do not always match the current product roadmap, so never infer that a feature or roadmap stage is complete from a label alone.
- When the status of a feature or stage is unclear, check the code and ask the user rather than assuming.

## Actions that need explicit authorization

Do not stage, commit, push, merge, deploy, publish, change repository visibility, start servers, run live provider (paid) agents, or perform destructive operations such as deleting files outside the agreed change, resetting or discarding changes, removing Git worktrees or stopping processes, unless the user has explicitly authorized that specific action. Keep changes within the agreed scope and avoid unrelated refactoring.

## Reporting

Report the files changed, the checks actually executed with their results, the checks not run and why, limitations and outstanding issues. Never describe a test, lint or build as passing unless it was executed in the current session.
