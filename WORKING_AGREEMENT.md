# Working agreement with Amit

This document preserves the project's collaboration preferences. Read [STAGE1_HANDOFF.md](STAGE1_HANDOFF.md) for the technical checkpoint. A newer explicit instruction from Amit takes precedence over an older documented preference.

## 1. General working style

Work one milestone at a time and keep the current checkpoint clear. Prefer incremental progress with meaningful verification points; do not dump large future implementation plans unless needed. Amit wants to understand what is being built, not merely paste generated code. Explain what/why when asked, using simple examples for architectural concepts. Do not lecture about every routine command.

## 2. PowerShell instructions

Whenever providing terminal instructions, identify the PowerShell window, where to open it, the exact `cd` command and copy-paste-ready commands. Never assume old commands are remembered. Label PowerShell commands, Dashboard task text and Codex prompts separately so prose intended for an agent is not mistaken for a shell command.

Whenever directing Amit to the dashboard, remind him how to start **both** components:

**Backend PowerShell window** — open a dedicated window and run:

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\backend"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

**Frontend PowerShell window** — open a second window and run:

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\frontend"
npm run dev -- --port 5173 --strictPort
```

Keep both windows running. Dashboard: <http://localhost:5173>.

These are instructions for an authorized interactive verification step, not permission for Codex to start servers during an implementation-only task.

## 3. Checkpoints and screenshots

Batch routine commands. Do not ask for output/screenshots after every tiny step. Request results at meaningful checkpoints: completed tests, errors or unexpected behavior, E2E outcomes, Git status and final verification. Reuse available context rather than asking Amit to repeat it.

## 4. Standard milestone workflow

Define milestone → precise Codex implementation prompt → Codex edits without commit/push → review summary and changed files → inspect important code → complete backend tests → complete frontend tests → production build → lint when relevant → `git diff --check` → `git status` → real E2E → only then `git add` / commit / push → verify clean status.

Do not trust a generated summary just because it says tests passed: inspect command results and relevant code. Committing/pushing is a later deliberate user-authorized checkpoint, not an automatic part of implementation. Documentation-only work can use the narrower verification explicitly requested for it.

## 5. Default Codex instructions

- Inspect the existing architecture and working tree first; preserve completed behavior and correct partial work.
- Avoid unrelated refactors and scope expansion. Add dependencies only when necessary and explicitly justified.
- Do not start servers for implementation/review-only tasks. Do not commit or push by default.
- After interruption by usage limits, continue in the same task/conversation where practical. Inspect existing uncommitted changes and resume from the checkpoint; do not restart the milestone or replace correct work.
- Report exactly which files changed, what was verified, failures/limitations and anything not run. Distinguish recorded previous results from newly executed checks.

## 6. Git safety

Review, tests and meaningful E2E precede commit. Explain destructive Git operations before considering them; never discard uncommitted work casually. Stage only expected milestone files, never SQLite runtime data or unrelated files. Check status before and after committing and keep clean, meaningful checkpoints. Use the actual nested repository root, not its parent desktop folder.

## 7. Testing philosophy

Tests are necessary but insufficient. Meaningful features require automated verification, a production build and real E2E. Real bugs deserve focused regression tests, not tests added merely to raise counts. Preserve the complete existing suite. Test process behavior with mocks/shims where appropriate; a passing mock-based suite is not proof of live Codex behavior. Follow explicit instructions limiting verification for documentation-only changes.

## 8. Architecture philosophy

Preserve established invariants. If an issue is genuinely architectural, address it properly instead of layering patches around it. Prefer understandable, maintainable solutions over clever ones. Keep user-facing behavior simple even when internals are sophisticated. Do not broaden scope during robustness work; Stage 2 redesign should not leak into Stage 1 maintenance.

## 9. Windows-specific verification

Use `--reload` for ordinary development. For deliberate backend crash/restart tests, use Uvicorn **without reload** so reloader/worker PIDs do not confuse the test. In the dedicated **backend PowerShell window**, after stopping the existing development backend:

```powershell
cd "C:\Users\User\Desktop\AI agent control center\ai-agent-control-center\backend"
..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Identify the actual listening process before any deliberate crash test. Do not use reserved `$PID` as a custom PowerShell variable; use a name such as `$pid8000`. Never kill unrelated processes merely to free a port. Confirm the intended test and process before destructive testing.

## 10. Communication preferences

Reply to Amit primarily in Hebrew. Code, commands, technical identifiers and Codex prompts can remain English. If mixed-direction text becomes confusing, provide the relevant technical explanation entirely in English when requested. Be direct and maintain continuity. Treat corrections as authoritative; do not make Amit re-explain documented context. Clearly separate implemented behavior from future ideas.

## 11. Learning preference

Amit is also building this project to learn. For questions such as “What is FastAPI?”, “What is a virtual environment?” or “Why are we doing this?”, explain concretely using the current project. For example, FastAPI exposes the dashboard's agent-control endpoints, and `.venv` isolates this backend's Python dependencies. Offer the explanation needed for the question, without turning every routine step into a lesson.

## 12. Product direction

The goal is a serious, polished, user-friendly product, not merely a university/demo prototype. Do not sacrifice correctness for appearance. Develop larger ideas deliberately, milestone by milestone. The handoff records the Stage 2 direction; it is not authorization to implement the whole roadmap at once.

## 13. Fresh conversation rule

Before Stage 2 implementation in a new conversation, read `STAGE1_HANDOFF.md` and `WORKING_AGREEMENT.md`, then confirm the repository's current state. Inherit both technical continuity and collaboration/workflow continuity. Preserve newer work and ask only for context that cannot be recovered from the repository and these documents.
