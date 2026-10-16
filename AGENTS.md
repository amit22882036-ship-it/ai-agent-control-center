# Codex instructions — AI Agent Control Center

These instructions apply across this repository. Work from this Git root, not its parent directory. Read the current code, [README](README.md), and technical documentation relevant to the current task. Consult the [Stage 1 handoff](docs/history/stage1-handoff.md) only when historical context is relevant. The handoff is historical: do not apply superseded Stage 1 assumptions to current behavior. Follow newer explicit user instructions and preserve still-relevant workflow constraints.

## Public repository security

- This repository is **PUBLIC**. Treat every tracked change as potentially visible to anyone on the internet.
- Never commit API keys, tokens, passwords, private keys, credentials, `.env` files, personal databases, private logs, or sensitive local state. Never force-add ignored files containing secrets or local state.
- Avoid personal identifiers and machine-specific absolute paths in public documentation; use portable repository-relative examples.
- Before an authorized commit or push, review Git status, staged changes, and relevant diffs for sensitive content and unintended files.
- If sensitive information is found, stop and report its location without printing secret values. Never automatically bypass security warnings or GitHub secret protection. A successful scan does not guarantee that all secrets were detected.

## Documentation maintenance

- Keep the README accurate and concise; place detailed architecture, API, and migration explanations under `docs/` and link to them.
- After a meaningful feature or Stage 2 milestone, assess documentation impact. Update docs when functionality, architecture, supported environments, setup, or important limitations change.
- Distinguish implemented UI features, backend-only capabilities, and future plans. Verify claims against source; do not exaggerate maturity or security.
- Never claim tests passed unless executed. Clearly distinguish current verification, previously recorded results, and checks not run.

## Development workflow

- Before each Stage 2 implementation milestone, clarify the intended result, assumptions, edge cases, and architectural implications with the user. Inspect current Git state and preserve existing work; avoid unrelated refactors.
- Preserve durable Agent/Task/assignment/Project identities, per-Task workspace boundaries, canonical-file/index protection, lifecycle and resource execution gates, and persistence-before-event ordering. Keep user prompts on stdin and preserve validated subprocess arguments and sandbox scope.
- Verify changes with relevant tests, lint, builds, and Git diff checks. Meaningful functionality changes require the existing backend/frontend suites and relevant interactive E2E verification; documentation-only work may use explicitly requested narrower checks.
- Do not start servers during implementation/review-only work without authorization. Never stop unrelated processes or discard existing work to make verification pass.
- Do not stage, commit, push, deploy, change repository visibility, or publish releases unless explicitly authorized for that action. Stage only intended files when authorized; review scope before committing.
- Treat the repository as a professional engineering portfolio while preserving technical correctness. Report changed files, verification results, limitations, and outstanding issues.
- After an interruption, inspect existing uncommitted changes and resume from that state; do not restart a milestone or replace correct work.
- Do not trust a generated summary that claims tests passed: inspect actual command results and the relevant code.

## Testing

- Add focused regression tests for real bugs, not tests that only raise counts. Preserve the complete existing suite and never weaken tests to make them pass.
- Mock- and shim-based suites do not prove live Codex behavior. Live provider runs consume paid usage and require explicit approval.
- Use isolated `CONTROL_CENTER_DB_PATH` and `CONTROL_CENTER_WORKSPACE_ROOT` values for experiments; never point tests at a user's real database or worktrees.

## Windows notes

- When giving PowerShell instructions, name the window, its starting directory and the exact `cd`, with copy-paste-ready commands. Keep shell commands, dashboard text and agent prompts visually separate.
- Use Uvicorn without `--reload` for crash, restart or process-supervision tests so reloader processes do not confuse results.
- Identify the actual listening process before stopping anything on a port. Never stop unrelated processes. Do not use PowerShell's reserved `$PID` as a custom variable.
