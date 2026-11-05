# Agent access levels

Every real agent runs with one of three access levels. Each provider enforces them with its own
documented controls, so the same level means different things with different tools. The New task
page says what the chosen level means for the chosen tool before anything starts.

| Level | Claude Code | Codex | Gemini CLI |
| --- | --- | --- | --- |
| **Read only** | `--permission-mode dontAsk`; edits, shell and web tools removed | `--sandbox read-only` | `--approval-mode default` (tools that need approval do not run) |
| **Workspace** (default for real work) | `--permission-mode acceptEdits`; edits in the task's worktree; Bash, PowerShell, WebFetch and WebSearch removed | `--sandbox workspace-write`: edits and commands (tests, builds, Git) inside the Codex sandbox, no network | `--approval-mode auto_edit`: edits; shell needs approval, so it does not run |
| **Full Access** (explicit choice) | `--permission-mode bypassPermissions`, user, project and local settings, the person's MCP servers | `--sandbox danger-full-access` | `--approval-mode yolo` |

All levels keep the Control Center's own guarantees: the prompt travels on standard input, the
process runs under a Windows Job Object, paid-route environment variables are removed, and the
agent works in its task's private Git worktree as its working directory. A confined agent loses
its own provider's paid-route variables; a Full Access agent loses every provider's, because it
can start other programs, another provider's CLI included.

## Capability matrix (verified against the installed CLIs' help on 2026-11-04)

| Capability | Claude Code 2.1.310 directly | Claude Code here, Workspace | Claude Code here, Full Access | Codex 0.154 here, Workspace | Codex here, Full Access |
| --- | --- | --- | --- | --- | --- |
| Read and edit files | Yes (prompts) | Yes, in the worktree | Yes, anywhere the account can | Yes, in the worktree | Yes |
| Run tests, builds, Git | Yes (prompts) | **No**: shell tools are removed | Yes | Yes, sandboxed | Yes |
| Install dependencies (network) | Yes (prompts) | No | Yes | No (no network in the sandbox) | Yes |
| Web search and fetch | Yes (prompts) | No | Yes | Not a Codex tool | Not a Codex tool |
| Provider-internal sub-agents | Yes | Removed in managed (lead) runs, so helpers go through admission | Same | Disabled in managed runs | Same |

Gemini CLI was not installed on the verification machine; its mapping is checked against the
installed CLI's `--help` at readiness time (`yolo` must be listed before Full Access can start).

**Why Workspace cannot run commands with Claude Code on Windows.** Claude Code does not sandbox
its shell tools on Windows, so a command could change files outside the worktree. Rather than
pretend otherwise, Workspace leaves them off; Full Access turns them on and says it is not
sandboxed.

## Helpers

A helper never receives broader access than its lead. Full Access is not inherited by default:
helpers of a Full Access lead work with Workspace access unless the person chose "Full Access
too" when starting the task (stored once per root task in `task_access`). Every delegated launch
is rechecked against this in the same transaction that admits it.

## Existing agents

An existing agent's access may only stay the same or narrow, at a safe boundary (while it waits).
Widening means starting a new task with the wider level. A responsibility transfer offers Read
only and Workspace.

## Provider-native approvals

The Control Center runs providers headless (print or exec mode). Prompts that would ask a person
are answered by the access level: refused in Read only and Workspace, approved in Full Access.
Interactive per-command approval inside the Control Center is not implemented.

## What is verified, and what still needs a real run

`backend/test_agent_access.py` launches every provider at every level as a real supervised process
against fake CLIs that record their arguments and environment (no model is called):

| Checked with fake CLIs | Result |
| --- | --- |
| Each provider receives exactly one access flag with the documented value for each level | Claude `dontAsk` / `acceptEdits` / `bypassPermissions`; Codex `read-only` / `workspace-write` / `danger-full-access`; Gemini `default` / `auto_edit` / `yolo` |
| Claude confined levels remove Bash, PowerShell, WebFetch and WebSearch and use `--strict-mcp-config`; Read only also removes Edit and Write | Yes |
| Claude Full Access loads user, project and local settings and removes no built-in tool | Yes |
| Codex never receives `--dangerously-bypass-approvals-and-sandbox` | Yes |
| Every level runs under a Job Object (`job_kill_on_close`) | Yes |
| Paid-route variables: the provider's own at every level, every provider's with Full Access | Yes |
| A CLI whose help does not document `bypassPermissions` or `yolo` refuses Full Access before anything starts | Yes |
| Helpers of a Full Access lead start with Workspace unless "Full Access too" was chosen | Yes (`HelperAccessTests`) |

These need an authenticated, paid run of the real CLI and have not been done:

- That Claude Code in `bypassPermissions` mode, Codex in `danger-full-access` and Gemini CLI in
  `yolo` mode really run shell commands, builds and network access without prompting when started
  headless by the Control Center.
- That Codex's `workspace-write` sandbox lets tests and builds run in the worktree on Windows, and
  blocks network access, with the installed Codex version.
- That Gemini CLI's `auto_edit` mode edits files but never runs a shell command in headless mode.
- That the CLIs' own approval prompts never appear and block a headless run at any level.
