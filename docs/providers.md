# Providers (Stage 2F)

The Control Center runs agents through **provider adapters**: Codex CLI, Claude Code, Gemini CLI and the Mock development provider. An adapter knows how to find, check, launch and interpret one CLI. It holds no authority. Task lifecycle, admission, resource and work-intent gates, workspace isolation, process supervision, persistence, delegation and result integration stay in the existing backend, and every launch, resume and retry still passes the same admission transaction.

[Project overview and setup](../README.md) | [Delegations](delegations.md) | [Process supervision](process-supervision.md) | [Local API security](api-security.md)

> **Verification status.** Codex keeps its existing, previously verified integration. Claude Code and Gemini CLI were verified against their documentation, the installed Claude Code `--help` output, and disposable fake CLIs that run under real Windows Job Objects. No authenticated Claude Code or Gemini CLI run has been performed, and the Claude Code `/usage` report and `rate_limit_event` were exercised only through fake CLIs. Model discovery was checked against the installed CLIs only as far as it needs no model request; the integrated discovery, per-model effort and entitlement handling ran against fake CLIs. Real runs need separate, explicit authorization because they use the account's plan.

## Contract

Each adapter (`backend/app/providers.py`) declares the following:

- **Identity and location.** Only pinned locations are used, never a `PATH` search:
  - Codex: `%APPDATA%\npm\codex.cmd`.
  - Claude Code: `%USERPROFILE%\.local\bin\claude.exe` (native installer), or `%APPDATA%\npm\claude.cmd`.
  - Gemini CLI: `%APPDATA%\npm\gemini.cmd`.
- **Readiness probe.** Local, non-billable commands only: `--version`, `--help`, `codex login status` and `claude auth status --json`. Gemini CLI has no documented status command. Its documented `settings.json` is read instead.
- **Command construction.** Only fixed, validated tokens appear on the command line. The prompt is always sent on stdin.
- **Access mapping.** The Control Center's `read-only` and `workspace-write` policies are mapped to each CLI's own permission options. A policy the CLI cannot enforce is refused before any process exists.
- **Environment.** Variables that would silently switch billing to a paid API route are removed from every probe and launch.
- **Stream interpretation.** Claude Code `stream-json` and Gemini CLI `stream-json` are parsed by `provider_events.py` under the same contract as the Codex JSONL parser.
- **Capabilities.** The core capabilities (start, stop, structured state, output history, stable identity, supervision, verified termination and crash recovery) hold for every provider. Each advanced capability is either supported or carries a precise reason.

| Capability | Codex | Claude Code | Gemini CLI | Mock |
| --- | --- | --- | --- | --- |
| Session resume, reply, redirect, waiting, decisions | Yes | Yes (`--resume <id>`) | Yes (`--resume <uuid>`, the reported session is verified) | No |
| Autonomous mode, delegation protocol, Hybrid Parent | Yes | Yes (native `Agent`/`Task` tools denied) | No: its internal sub-agents cannot be verifiably disabled | No |
| Delegated Child | Yes | Yes | Yes (never with recursive authority) | Yes |
| Model selection | Yes (discovered catalog) | Yes (discovered catalog) | Yes (compatibility catalog, unverified) | No |
| Effort selection | Yes, per model | Yes, per model | No (provider default) | No |
| Model change keeps the session | No (new session, confirmed) | Yes | No (new session, confirmed) | n/a |
| Access change | Narrowing only | Narrowing only | Narrowing only | n/a |

### Access policies

| Policy | Codex | Claude Code | Gemini CLI |
| --- | --- | --- | --- |
| `read-only` | `--sandbox read-only` | `--permission-mode dontAsk`, with Bash, PowerShell, WebFetch, WebSearch and every edit tool denied | `--approval-mode default` (headless: no edit or shell tools) |
| `workspace-write` | `--sandbox workspace-write` | `--permission-mode acceptEdits` (edits in the working directory only), with shell and web tools denied | `--approval-mode auto_edit` (file edits only) |

These options are always used: Claude Code runs with `--setting-sources user --strict-mcp-config`, so the hooks, allow rules and MCP servers of the repository being worked on are not loaded. No adapter passes `--dangerously-skip-permissions`, `bypassPermissions`, `yolo`, `danger-full-access` or `--skip-trust`. Windows Job Objects contain processes. They are **not** a sandbox, and the CLIs enforce these policies themselves.

## Readiness and enablement

Each property is evaluated separately: supported by the application, installed, a supported version (every flag the adapter uses must appear in the installed help), signed in, enabled by the user, billing route permitted, and not limited. The resulting states are `ready`, `disabled`, `not_installed`, `invalid_installation`, `unsupported_version`, `not_authenticated`, `incompatible`, `usage_limited`, `temporarily_unavailable`, `billing_blocked` and, before the first check, `unchecked`.

- **Defaults.** Mock and Codex are enabled, which preserves the pre-2F behavior (the migration records `pre_2f_default`), but new Codex executions need the one-time billing acknowledgment. Claude Code and Gemini CLI are disabled until the user enables them. Enabling them requires an explicit billing confirmation.
- **When checks run.** Readiness is checked before any Task, workspace, reservation or process is created. Enablement, the billing confirmation, usage blocks and any newer negative readiness are checked again inside the admission transaction, immediately before `Popen`. No subprocess runs while that lock is held.
- **Scope.** The same rules apply to root Agents, user-started children, delegated children, Hybrid continuations, replies, resumes, retries and provider changes. A refused provider is never replaced by another one.
- **Check again.** `GET /providers` never probes. `POST /providers/check` and `POST /providers/{id}/check` run fresh probes, and a provider becomes ready only on positive evidence. A verified sign-in clears an earlier authentication block.
- **Caching.** A cached result is keyed to the located executable and the configuration the probe validated (Codex `config.toml`, Gemini system and user settings). Installing, updating or removing a CLI therefore changes the result immediately.

Nothing is installed, signed in or configured automatically. The provider panel shows the reason, the next step and an official documentation link. The full onboarding wizard belongs to Stage 2G.

## Usage, quota and billing

No paid request is ever made to check quota or usage. Unknown usage is shown as unknown: it is neither zero nor confirmed available.

### Billing routes

- **Codex.**
  - **Route, not only sign-in.** `codex login status` reporting ChatGPT is authentication. The model route is checked separately against the documented configuration: `model_provider` must be absent or `openai` (the built-in provider, which config reference says cannot be overridden), and `openai_base_url`, `chatgpt_base_url`, a non-ChatGPT `forced_login_method` or a legacy default profile that changes any of these are refused. Project `.codex/config.toml` files of the execution workspace are checked too, conservatively, although Codex ignores provider keys there. An unreadable configuration fails closed.
  - **Pinned at launch.** Every launch passes the documented override `-c model_provider=openai`, never passes `--profile`, `--oss` or `--local-provider`, and removes `CODEX_API_KEY`, `OPENAI_API_KEY` and `OPENAI_BASE_URL`.
  - **One-time acknowledgment.** New Codex executions need your acknowledgment that Codex runs through your ChatGPT subscription and its limits; that existing credits or automatic credit purchases on your OpenAI account may still affect billing outside the Control Center; that the Control Center never buys credits, never enables a paid fallback and never uses an API key; and that it cannot enforce account-side spending limits OpenAI does not expose. The acknowledgment is stored durably and separately from the verified route, and it never permits a pay-per-use route.
  - **Limitations.** The Codex system or managed configuration location on Windows is not documented, so it is not inspected.
- **Claude Code.**
  - **Accepted route.** Only a first-party subscription sign-in is accepted, recognized from `claude auth status --json`. Those field values are not a documented contract, so they are used only to block: an unknown value fails closed.
  - **Removed variables.** In `-p` mode an API key always wins over the subscription login, so `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, the Bedrock, Vertex and Foundry switches, profiles and `ANTHROPIC_BASE_URL` are removed.
  - **Your confirmation.** You confirm that paid extra usage is not enabled on your account, because that setting is not observable locally.
- **Gemini CLI.**
  - **Effective configuration.** The documented layers are merged in precedence order: system defaults, user, project (the execution workspace's `.gemini/settings.json`), then system settings. The project layer is applied even though folder trust may ignore it (fail closed). Only `security.auth.selectedType = oauth-personal` (Sign in with Google) is approved; the other known values (`gemini-api-key`, `vertex-ai`, `cloud-shell`, `compute-default-credentials`, `gateway`) and any unknown value are refused. The effective `billing.overageStrategy` must be `never`. Unreadable settings fail closed.
  - **Per workspace.** Readiness checks the system and user layers. The execution workspace's own layer and `.env` files are checked again before each process is created, so a result for one workspace is never reused for another.
  - **Your confirmation.** You confirm the route, because the sign-in itself has no documented status command. Unpaid and Google One accounts moved to Antigravity CLI on 2026-06-18.

Every validated command carries a snapshot of the configuration it was validated with. The admission transaction compares it again and refuses the launch if Codex or Gemini configuration changed in between. These checks reduce the risk of unexpected charges. They are not a financial guarantee that the external providers do not themselves provide.

### What usage each provider exposes

| Provider | Class | Source |
| --- | --- | --- |
| Claude Code | Exact when reported | The documented `/usage` local command in print mode returns `usage_report.rate_limits.limits` rows (meter kind, group, percent used, provider reset, model or surface scope). It needs Claude Code 2.1.273 or later (the SDK release that added the report); an older CLI is not asked, because it could answer `/usage` with a model turn. The SDK marks this report shape experimental. During runs, the documented `rate_limit_event` reports the active limit's status and reset. When an event also carries `rateLimitType`, its raw value is kept verbatim as the window id (`limit:<value>`), with no meaning such as a duration attached; without it, the event describes only the active limit. |
| Codex | Limit signal only | No documented usage interface reaches the Control Center. Managed (JSONL) runs report usage-limit error codes; ordinary runs use text output and cannot be classified. Session rollout files and the experimental app-server expose more, but they are undocumented or experimental and are not used. |
| Gemini CLI | None | No documented usage or quota interface; result errors are shown but not classified as quota. |

### Usage windows

A provider-reported window is stored per provider, window id and scope (`provider_usage_windows`): label, meter kind and group, model or entitlement scope, duration (only when reported), used percent (only when reported), remaining percent or amount (only when reported, never derived from used), `available`, `exhausted` or `unknown`, provider-reported reset, observation time and source. Nothing assumes exactly two windows, and no reset is computed from "five hours" or "seven days". A reading older than 15 minutes is shown as stale and never confirms availability.

- **Applicability.** A window without a model scope applies to every model. A model-scoped window applies unless the Agent's explicitly selected model is recognizably another one; for the provider's default model the scope is uncertain, so it applies. No model is switched automatically.
- **Blocking.** Any applicable exhausted window blocks new executions, even when another window has capacity (an `unknown` window never lifts a confirmed exhaustion). A limit is **confirmed** while its provider-reported reset is still ahead, or while a fresh reading reported it exhausted without a reset. A confirmed limit cannot be bypassed.
- **Refresh usage.** For Claude Code only, `POST /providers/claude/usage/refresh` runs `/usage` (no model request) and stores its rows. A recorded limit is resolved only when the report positively covers it: the same window identity is now reported available. Windows the report omits, limits recorded without a window identity and the unidentified active limit stay, even if every reported meter has capacity. If a run produces anything other than a usage report, refresh is disabled for that installation so it is never repeated. A refresh never resumes anything.

### Limits that cannot be saved

A known limit (quota, rate, billing or sign-in) blocks this backend immediately in memory, before its durable write is attempted, following the Stage 2D.1b ownership-guard principle. Every admission checks it inside its transaction, so concurrent launches are refused, and an explicit retry can never override it. It leaves memory only after a later write of the same limit commits (reconciled on every launch, status read and resume); a successful read never removes it. A crash before that write loses the in-memory block: the waiting Agent's own state is still persisted when the database accepts writes, but the provider would then need its limit observed again.

### Limits during a run

Failures are classified only from documented structured fields: Claude Code `rate_limit_event` and `system/api_retry` categories, Codex JSONL error codes and Gemini CLI result status. Model prose is never treated as billing information.

- **Ordinary Claude Code and Gemini CLI executions.** On a rate limit, quota, sign-in, billing or availability failure, the Agent keeps its Task, assignment, output, workspace and provider session and waits (`waiting` with a provider condition naming the provider, model, blocking limits, provider-reported reset and whether the session can be resumed). The execution is settled by the existing supervision, and ownership is released only after the Job confirms that no process remains. Completed output is kept; the Task is never completed by a failed run.
- **A limit before any session.** A limit reported before the provider created a session still makes the Agent wait. Resume then continues in a new session, with the Task and workspace as context.
- **Managed and delegated (structured) runs.** They stop as before (their orchestration semantics are unchanged) and record the same limit.
- **Other work.** Limits are per provider (account-wide for that provider) and, where reported, per model scope. Agents of other providers and other models outside a scoped limit keep working.

### Manual resume

Nothing resumes automatically: not when a reset time passes, not at startup, not after a refresh. The actions are separate:

- **Refresh usage** updates readings (Claude Code only).
- **Clear recorded limit** (`POST /providers/{id}/usage/allow`) resolves recorded limits by your decision and resumes nothing. Confirmed limits and limits not yet saved cannot be cleared.
- **Resume task** (`POST /agents/{id}/resume`) resumes one waiting Agent. It rechecks enablement, installation, sign-in and route (a fresh probe), the acknowledgment, every applicable limit, model and capabilities, that the previous execution is settled, and Task, assignment and resource admission, then continues the same provider session where supported. A limit that is recorded but not confirmed (no reset ahead, no fresh reading) needs an **explicit retry**: the verified subscription route is used and the provider may report the limit again. Each request is journaled in `agent_resume_requests` with its mode and outcome; the Agent is shown as resumed only after admission succeeds, and the overridden limits are marked superseded in the same transaction as the launch.

### Automatic recovery

The only automatic retry covers a provider-unavailable failure (`overloaded` or `server_error`) that occurred **before any work** (no tool call and no assistant message). There are at most two retries, 30 and 120 seconds apart. Each retry goes through every readiness, policy and admission check again (it can never override a limit) and runs only if nothing changed in the meantime. Retry timers live in memory, and startup never launches work, so a restart cannot duplicate one. Sign-in, permission, quota, billing and model failures are never retried.

## Models and effort

Each Agent has its own provider, default model and default effort. Several Agents of the same provider can run concurrently with different models and efforts, and no Agent ever changes another Agent's configuration or the user's global CLI configuration.

### Three levels

- **Agent defaults.** Chosen at creation (`model`, `effort`, both optional: empty means the provider default) and changed only explicitly with `POST /agents/{id}/defaults`. A change applies from the Agent's next instruction; a running execution is never changed. Every change is journaled in `agent_provider_changes` (`boundary = 'defaults'`, with `from_effort` and `to_effort`).
- **Next-instruction override.** Reply, Redirect and Resume accept `model_override`/`model` and `effort_override`/`effort`. The override applies to that one instruction; the next one uses the Agent defaults again. Saving it as the new default is a separate, explicit defaults change.
- **Effective execution snapshot.** Every execution records what it requested and what the provider reported: `model`, `requested_effort`, `config_source` (`agent_default` or `override`), `entitlement_state`, `catalog_scope` and `catalog_revision`, the catalog's `resolved_model`, the provider-reported `effective_model`, `effective_effort` and an `effective_note`. A value the provider does not report stays empty; it is never assumed. Claude Code reports its model in the `init` event and Gemini CLI in its result statistics; no provider reports the effort it actually applied (Claude Code organization caps can lower it silently), so `effective_effort` stays unknown.

Effort priority for Codex: the instruction's override, then the Agent default, then the legacy `CONTROL_CENTER_CODEX_REASONING_EFFORT` (still honored when nothing is selected), then Codex's own default. The legacy variable no longer couples every Codex Agent to one global effort. The same applies to `CONTROL_CENTER_CODEX_MODEL`. Both legacy values are resolved once, before authorization: admission validates exactly the model and effort the command then uses, so a restricted, unlisted or unsupported environment default is refused like a selection, and the execution records it (`config_source` ends in `+environment`). An Agent whose default is "provider default" keeps it; the environment value is read again for each execution and never shared between Agents.

### Model catalog

The model list is discovered per provider and cached per account; there is no static model list in the frontend.

| Provider | Source | What it proves |
| --- | --- | --- |
| Claude Code | The SDK `initialize` control request in `stream-json` input mode (`-p --input-format stream-json`), stdin closed without a user message. Verified against the installed Claude Code 2.1.198 that this starts no model turn. Merged with a versioned catalog of the models and aliases the [Claude Code model configuration](https://code.claude.com/docs/en/model-config) documentation lists. | The models, aliases (`resolvedModel` from 2.1.197), effort levels and account (subscription type) the CLI reports for this sign-in. The wire format belongs to the SDK, not a documented CLI contract, so it is parsed strictly and anything unrecognized fails discovery. The documentation proves only that a model exists (see "Documented Claude Code models"). |
| Codex | `codex debug models` (named by the installed 0.154 help as "Render the raw model catalog as JSON"), run with `-c model_provider=openai`. | The catalog the CLI uses for this sign-in, including effort levels per model, whether an effort change can apply to an existing session, hidden models (excluded) and locked models with the provider's own upgrade text. It is a debug interface, not a stable contract. |
| Gemini CLI | A versioned compatibility catalog of the documented `--model` aliases (`auto`, `pro`, `flash`, `flash-lite`). | Nothing about the account: Gemini CLI exposes no model or entitlement list. `auto` is "Availability not verified"; a specific model is "Exact model not enforceable" (see Provider specifics). |

No discovery sends a prompt, and none is a model request. Discovery uses the same scrubbed environment as a launch and a 25-second bound (Claude Code's runs in an empty temporary directory), and the whole process tree is ended on a timeout.

- **Account scope.** A catalog is stored under a scope key: a hash of the account identity (Claude Code: e-mail, organization, token source, API provider and reported plan; Codex: the reported identity, or else the state of its sign-in file, never its content), the CLI version and the configuration fingerprint. No token, e-mail or secret is stored as a key or value. Another account, plan, CLI version or configuration is a different scope, so one account's entitlement is never reused for another.
- **Account changes without a refresh.** Freshness is also bound to local account evidence, read on every check: the Codex `auth.json` account id and mode, the Claude Code `oauthAccount` identifiers and plan-tier fields in `.claude.json`, and Gemini CLI's active Google account. Only a hash of these identifiers is used; tokens are never read into the evidence. A sign-in to another account, organization or plan tier makes the cached list stale at once, admission rediscovers it (bypassing the refresh rate limit), and the previous account's eligibility is never admitted. These files are not documented contracts. When the identity is not exposed (no such file or field), an account change cannot be noticed locally, so that catalog is trusted for two minutes only and is otherwise rediscovered before a launch.
- **Refresh.** A refresh runs when the user asks (**Refresh models**), when a picker opens on a stale or missing list, when a provider is enabled, after a verified sign-in check, and before admission when the cached evidence is stale. Concurrent requests share one discovery, automatic refreshes are rate limited (one per provider per minute), and a disabled provider is discovered only on the user's request. The list is current for `CONTROL_CENTER_MODEL_CATALOG_TTL_SECONDS` (default six hours, minimum five minutes) and only while the CLI and configuration are unchanged.
- **Stale or failed discovery.** Freshness is evaluated before any account-specific claim. The last-known list stays visible as history, but every entry of a stale or previous-account list reads "Availability not verified" (status `catalog_stale`), whatever it said before: an old restriction or plan text is never presented as a restriction of the current account, and an old listing never grants access (it needs an explicit attempt, like an unverified model). Current-account discovery restores account-specific statuses. A model that disappears is kept and marked removed; a changed alias resolution keeps the previous value.
- **Provider denials and omissions.** A provider error that a model is unavailable (`model_not_found`) records a denial for that model in the current scope, marks the catalog stale and, once discovery succeeds, shows "The provider refused this model for this account" (`unavailable_subscription`). The denial stays for that account scope until the account, plan, CLI version or configuration changes. A documented model missing from the account's list is an omission, shown separately as "Not offered to this account" (`not_offered`). Neither names a plan unless the provider states one.

### Documented Claude Code models

Claude Code reports only the models it offers to the signed-in account, so a model an account may not use would be invisible. The Control Center merges a versioned list of the models and aliases the Claude Code documentation names (revision `claude-code-model-config-2026-10-23`), without treating it as permission:

- A documented family alias (`fable`, `opus`, `sonnet`, `haiku`) the installed CLI does not offer to this account is shown disabled as "Not offered to this account": "Documented by Claude Code, but the installed CLI does not offer it to this account. Required plan not specified by provider." It is never admitted, even with an explicit attempt.
- A documented mode (`best`, `opusplan`) or full model name (for example `claude-opus-4-6`) that the account's list does not include stays unverified: Claude Code's list does not enumerate every runnable version, so absence proves nothing and only an explicit attempt can run it.
- The documentation's own statements are shown with the entry, never turned into a plan requirement. For example, Fable usage "can bill to usage credits instead of drawing on your plan's included limits" depending on plan and seat tier.
- The 1M-context variants (`sonnet[1m]`, `opus[1m]`) are not listed: their identifiers fall outside the safe model-token format the launcher accepts.
- No official, account-independent, machine-readable Claude model list exists for subscription accounts (the Models API needs an API key, which the Control Center never uses), so this list is updated with the code and labelled with its revision.

### Status, plan labels and explicit attempts

Capability, entitlement, billing and quota stay separate. A model's status is one of: Available, Deprecated, Not available on this account, Requires a specific plan, Restricted by organization, Not supported by installed CLI, Provider disabled, Provider not signed in, Removed by provider, Temporarily unavailable, Model quota exhausted, Shared quota exhausted, Billing policy blocked, Availability not verified, or Catalog information stale.

- **Plans are never inferred.** A plan is named only with the provider's own words (the Codex catalog's upgrade text) or as the plan the CLI reports for the current account. Otherwise the reason reads "Required plan not specified by provider".
- **Unavailable models stay visible.** They are listed, disabled, under "Unavailable with current account", with the provider's reason.
- **Unverified models** (Gemini `auto`, stale entries, identifiers typed under "Other model identifier", and models the catalog does not list) can run only after an **explicit attempt**: the user confirms that it uses the approved subscription route, may use included usage and is never retried or replaced automatically. Nothing probes eligibility in the background, and automated launches (delegated Children, Hybrid continuations, automatic retries) never attempt an unverified model. An Agent whose default model is unverified needs the confirmation with each instruction.
- **Effort.** Only levels the catalog lists for the chosen model are accepted. An unlisted model, a model with no listed levels and Codex's unnamed default model run with the provider default effort. For Claude Code's default model, the levels listed for `default` apply. Gemini CLI has no documented reasoning control, so it always uses the provider default and an effort is refused.

The backend admits every request again (provider, model, effort, entitlement, the explicit attempt, billing route and quota); the frontend only reflects the result.

### Provider specifics

- **Codex.** Model and effort are passed per execution (`--model`, `-c model_reasoning_effort=<level>`), which never edits `config.toml`.
- **Claude Code.** `--model` and `--effort` (levels from the catalog: `low`, `medium`, `high`, `xhigh`, `max` on the installed version). Every launch adds a Control Center-owned `--settings` file with `switchModelsOnFlag: false` and an empty `fallbackModel`, so print mode never re-runs on another model. A `fallbackModel` chain in the user's or managed settings could still apply, so an explicitly selected model is refused while one exists. When the reported model differs from the listed resolution, the execution records that the requested model was not enforced.
- **Gemini CLI.** Only **Auto** (`--model auto`, intentional provider routing, an explicit attempt) and the **provider default** (no `--model`: Gemini CLI's configured model or Auto, never presented as a pin) can run. A specific model (`pro`, `flash`, `flash-lite` or a typed identifier) is shown disabled as "Exact model not enforceable" and refused before launch, with or without an explicit attempt, so no run starts that could fall back first. Why:
  - The Gemini CLI documentation says a `--model` selection "will always be used", but also that fallback to another model is on by default when the selected model fails, documents no setting or flag to disable it, and does not say what happens in headless mode. Gemini CLI is not installed on the verification machine, so no other mechanism could be confirmed.
  - Its output cannot prove which model produced the response: the `init` event carries only the configured model, and the result's usage statistics aggregate every model used, including models for internal utility calls, without naming the primary one.
  - The Control Center keeps these sources apart. The `init` model is recorded only as "configured at start"; `effective_model` holds the models from the usage statistics.
  - As a defense in depth, a run with an explicit Gemini model that somehow reached a launch completes only when every model in the usage statistics is the selected one. A mismatch, alternative models or missing statistics leave the Task incomplete, and the Agent waits with "Selected model not verified" for your decision. Nothing restarts automatically, and no other model is chosen for you.
  - Auto and the provider default are subject to the same billing route and quota policy as every other launch. Unrestricted fallback for an explicitly selected model is not implemented and needs a product decision first.

### Sessions

| Change | Claude Code | Codex | Gemini CLI |
| --- | --- | --- | --- |
| Effort only | Same session | Same session if the catalog says the model accepts effort updates; otherwise a new session | n/a (no effort control) |
| Model | Same session (`--resume` with `--model`) | New session | New session |

A change the provider cannot apply to the current session is refused unless the instruction confirms a new session (`confirm_new_session`). The new session receives a continuity note with the authoritative Task, the workspace and the previous question; the previous session memory is not carried over. A provider change always starts a new session.

## Provider and model selection

- **At creation.** Each Agent has a provider, an optional model and effort, and the access policy. `CONTROL_CENTER_CODEX_MODEL` remains Codex's default when no model is selected. The per-provider `models` setting is an allowlist of extra identifiers the user names.
- **Changes.** `POST /agents/{id}/provider` changes the provider, model, effort or (narrower) access only at a safe boundary: while the Agent waits with a settled execution, never during a run, with unresolved ownership or for managed or delegated work.
- **Identity and history.** Agent and Task identities, the assignment, output and history stay. Every change is journaled in `agent_provider_changes`, and each execution keeps the configuration it ran with.
- **Sessions.** A different provider never receives the previous provider's session. The next run starts a new session whose prompt carries the authoritative Task and points to the workspace.

## Auto allocation for delegated Children

A Parent can ask the backend to choose a delegated Child's provider, model and effort ("Auto allocation"). This is not Gemini CLI's `auto` model alias, and `auto` is never a provider. The design is quality first, usage second: suitability for the specific work decides; usage only separates comparably suitable candidates.

- **Authorization.** Only the user enables Auto, as a durable policy on a Task and its subtree (`auto_allocation_policies`): when creating an autonomous root agent ("Child agents: Auto"), or later in the inspector. The policy lists the allowed providers and, optionally, allowed models per provider, a tie-break preference order and a per-category suitability profile. A worker cannot grant or widen it: a promotion item that asks for Auto without a policy is held with "Auto allocation is not authorized". Existing Agents and explicit requests are unchanged.
- **The request.** A promotion item may carry `allocation` instead of `provider`/`model`: `{"mode":"auto","category":...,"complexity":"low|medium|high","capabilities":[] or ["coordination"],"candidates":[{"provider","model"?,"effort"?,"suitability":"preferred|acceptable","rationale"?}]}`. The candidates are the Parent's own assessment of this work, produced by its existing planning, not by an extra model call. The planning prompt lists the current authorized options from the model catalog. Without candidates, the user's profile for the category is used; without either, the item is held with "No suitability evidence", never guessed.
- **Where the decision is made.** In the existing promotion preparation (`promotion.prepare`), inside its transaction, before any Child admission, using durable state and the cached catalog only (no discovery, probe or model request). The decision is recorded in `allocation_decisions` (linked to the Delegation, proposal and plan revision) and written to the Delegation's `child_provider`, `child_model` and new `child_effort` fields, so the unchanged launch and admission path enforces it like an explicit request.
- **Hard eligibility first.** A candidate is excluded when it is outside the policy, its provider is disabled, unacknowledged, known not ready or unable to run a delegated Child (or to coordinate Children when that is required), the model or effort is not admitted by the catalog for an automated launch (never an unverified or restricted model, never a stale or other account's listing, never a Gemini exact pin), or any known usage limit applies: a confirmed exhausted window (five-hour, weekly, model-scoped, shared or any other) or a recorded limit whose renewal is not confirmed (automation never retries it). Suitability never bypasses these.
- **Suitability second.** Eligible `preferred` candidates are chosen before `acceptable` ones; an unlisted provider is never chosen, however much usage it has. No model is ranked by name, age, price or provider, and new models get no assumed quality.
- **Usage only among comparable candidates.** Within the chosen level, a fresh provider reading that shows capacity comes before an unknown reading, then the user's preference order, then the assessment's order, then a deterministic order. Usage percentages are never compared across providers or converted into task capacity. Low but nonzero quota stays eligible: the Child may start, and if its quota runs out the existing Stage 2F waiting and manual resume apply.
- **Effort.** An effort named by the assessment must be one the model lists (otherwise the candidate is excluded, never changed). Otherwise the stated complexity maps to the same-named level (low, medium, high) when the model lists it, else the provider default; low quota never lowers it. Gemini always uses its provider default.
- **Stability.** A recorded selection is kept while it stays eligible; a new ranking never reshuffles it. Before admission, a selection that became ineligible (provider disabled, a confirmed limit, a catalog change) or a held request is reconsidered, and the superseded decision stays in the history. Once the Child's launch has begun, prepare returns early, so the provider never changes; a later change uses the existing provider-change or responsibility-transfer controls. Exact replays of a promotion return its receipt without a new decision, and the existing Task locks, materialization journal and promotion identities prevent duplicate Children.
- **Authorization at launch.** A prepared disposition is not authority. Every admission boundary (the materialization admission and the final admission transaction, in which the process is created) checks again, from durable state only, that the Delegation still carries its current Auto selection and that the user's effective policy (own or inherited from an ancestor Task) is still enabled and still allows that provider and model. A revoked or narrowed policy refuses the launch before any process exists, substitutes nothing, keeps the Task, Delegation and decision history, and returns the unstarted launch to preparation, where the next bounded pass reconsiders it. Policy writes and the final admission are serialized by the same database write lock. Started Children are never re-judged, and explicit requests are unaffected.
- **Partial admission.** Each item is decided on its own: an eligible item proceeds, a held item stays blocked with its reason, and dependent items keep their dependency gates.
- **Explanation.** Each decision stores a compact explanation: the selection, its suitability source and rationale, comparable alternatives, required capabilities, every excluded candidate with its reason, whether usage was known, that execution may pause on a usage limit, and uncertainties such as unchecked readiness. No numerical score is stored or invented. The inspector shows it under "Provider allocation".
- **Ordinary delegations (`control-center.delegation.v3`).** A separate protocol version; v1 (closed request schema) and v2 (Hybrid continuation and acknowledgement) are unchanged and never accept an allocation. A v3 envelope is exactly `{protocol, action:"request_delegations", requests, continuation}`: continuation is `null` or v2's Hybrid continuation (Hybrid-enabled Parents only), and each request is v1's `request_key` and `instruction` plus an optional `allocation`. Without it the Child uses the Parent's provider, exactly as in v1. `{"mode":"explicit","provider",...}` is that exact provider, model and effort, set on the Delegation at acceptance, validated at launch and never substituted (a refusal keeps its actual reason). `{"mode":"auto",...}` is the same request as in promotion, decided by the same engine in the existing materialization preparation, before any Child Task exists; a held request creates no Child and keeps its reason in `admission_error`. All v1 bounds apply (duplicate keys, 256 KiB, 1-16 requests, request keys). The normalized allocation is stored in `delegations.allocation_request` and is part of the request's identity: an identical replay reuses the record and decision, a replay with the same key and a different allocation is refused. Decisions record their `origin` (`delegation` or `promotion`) and no plan revision. The same authorization check runs at the materialization admission and the final admission transaction, so a revoked or narrowed policy refuses the launch before any process exists and the next pass reconsiders it. The v3 contract with explicit allocation is shown to every delegation-enabled Parent; the Auto request is added only where the user's effective policy authorizes it, and an unauthorized Parent is never told it could enable Auto. Result delivery, acknowledgement and the known limitations of ordinary non-Hybrid delegation are unchanged.
- **Configuration.** The creation form sets Auto and its allowed providers. The inspector ("Provider allocation") edits every policy field as an isolated draft saved only by an explicit Save: allowed providers; per-provider model allowlists from the current catalog, as an explicit choice between "All otherwise eligible models are allowed" (no list, the backend's meaning of an absent list) and "Only the models selected below" (unchecking the last model never widens the policy: Save stays blocked until a model is selected or "All" is chosen; restricted or unknown models cannot be newly allowed, and saved identifiers missing from the catalog are kept and shown); the preference order (structured provider, model and effort with keyboard-operable move and remove; `provider` matches any model and effort, `provider/model` any effort of that model, and `provider[/model]@effort` only that effort); and per-category suitability profiles (preferred and acceptable candidates, categories from the backend). An inherited policy is shown with its owner; saving from a child scope creates a local override and never changes the ancestor's policy. "Remove this task's policy" (`DELETE /tasks/{id}/auto-allocation`) returns the subtree to its ancestor's policy. Writes carry `expected_revision`, a monotonic revision (`auto_allocation_policy_revisions`, never reused, independent of timestamps), so a change made elsewhere since the editor opened is refused and the draft is kept. The editor starts from the effective state: an inherited disabled policy, or no policy, opens with Auto off, and enabling it is always an explicit action.
- **Limits of this assessment.** Suitability is only as good as the Parent's assessment or the user's profile; the Control Center has no benchmark data and does not claim that the choice is objectively best.

## Mixed hierarchies

A hierarchy may mix providers, for example a Claude Code Parent, a Codex Child and a Gemini CLI grandchild, within the existing depth and capacity limits. Each Agent keeps its own model and effort; a Child never inherits or changes its Parent's. Children are created in two ways:

- **User-started children** under any Agent, with any ready provider.
- **Promotion items** that request one. A promotion item may add an optional `provider` (and `model`). The request is stored on the Delegation and checked at admission. A requested provider that is disabled or limited leaves the item `blocked` with an actionable reason, and no other provider is substituted. Without a request, the Child uses the Parent's provider.

Coordination always uses the existing records: Delegations, the Hybrid continuation, assignments, dependencies, resource claims, work intents and integration. Recursive authority passes only to a Child whose provider can coordinate Children. Otherwise it is narrowed, never widened, and access always equals the Parent's.

Ordinary (non-Hybrid) delegation requests keep their existing `{request_key, instruction}` shape: their Children use the Parent's provider. The known limitation of ordinary Delegations (no review or acknowledgement protocol) is unchanged.

## API

| Route | Effect |
| --- | --- |
| `GET /providers` | Support, capabilities, settings, usage and the last check (never probes) |
| `POST /providers/check`, `POST /providers/{id}/check` | Explicit, non-billable local checks |
| `PUT /providers/{id}/settings` | `enabled`, `billing_confirmed`, `models` (Mock has none) |
| `POST /providers/{id}/usage/refresh` | Claude Code `/usage` readings (no model request); resumes nothing |
| `POST /providers/{id}/usage/allow` | Clear recorded, unconfirmed limits by your decision; resumes nothing |
| `GET /providers/{id}/models` | The cached catalog with statuses, effort levels, freshness and provenance (never runs discovery) |
| `POST /providers/{id}/models/refresh` | Non-billable discovery, coalesced and rate limited (`?stale_only=true` for an automatic refresh) |
| `GET /tasks/{id}/auto-allocation` | The effective Auto policy (own or inherited) and the Children's allocation decisions and history |
| `PUT /tasks/{id}/auto-allocation` | The user's Auto policy for the Task subtree (`enabled`, `providers`, `models`, `preferences`, `profile` and the required `expected_revision`: the loaded revision, or `null` only to create a Task's first own policy; omitted is `428`, stale is `409`) |
| `DELETE /tasks/{id}/auto-allocation` | Remove the Task's own policy (it then follows an ancestor's, if any); the required `expected_revision` query parameter |
| `POST /agents/{id}/defaults` | Change one Agent's default model and/or effort (`model_set`, `model`, `effort_set`, `effort`, `attempt_unverified`) |
| `POST /agents/{id}/provider` | Provider, model, effort or narrower access change at a safe boundary |
| `POST /agents/{id}/resume` | Resume one waiting Agent (`{"explicit_retry": true}` for an unconfirmed limit) after every check |
| `POST /agents/{id}/continue` | Same as resume without an explicit retry (kept for compatibility) |

`POST /agents/start`, child starts and `POST /tasks/{id}/start-agent` accept `agent_type` (`mock`, `codex`, `claude`, `gemini`), an optional `model` and `effort`, and `attempt_unverified`. Reply, Redirect and Resume accept the next-instruction fields `model_override`, `model`, `effort_override`, `effort`, `confirm_new_session` and `attempt_unverified`. Every write requires the CSRF token. A provider refusal is a `409` with an actionable message.

## Storage

Schemas v31 to v36 are additive and idempotent:

- `agents.model` and `agents.provider_condition`.
- `executions.model`.
- `delegations.child_provider` and `delegations.child_model`.
- The `provider_settings`, `provider_usage_blocks` and `agent_provider_changes` tables.
- A rebuild of `responsibility_transfers` that widens its provider check while preserving every row.
- v32: `provider_usage_windows`, `agent_resume_requests`, and `window_id` and `clear_reason` on `provider_usage_blocks`.
- v33: `agents.effort` and `agents.session_config` (the model and effort the current provider session started with); the execution snapshot columns (`requested_effort`, `effective_model`, `effective_effort`, `config_source`, `catalog_scope`, `catalog_revision`, `entitlement_state`, `resolved_model`, `effective_note`, `configured_at`); `from_effort` and `to_effort` on `agent_provider_changes`; and the `model_catalog`, `model_catalog_refreshes` and `model_denials` tables.
- v34: the `auto_allocation_policies` and `allocation_decisions` tables and `delegations.child_effort`.
- v35: `delegations.allocation_request` (a v3 request's allocation) and `allocation_decisions.origin` (existing decisions are marked `promotion`).
- v36: `auto_allocation_policies.revision` and the `auto_allocation_policy_revisions` journal (existing policies receive a revision).

Historical executions and sessions are not rewritten.
