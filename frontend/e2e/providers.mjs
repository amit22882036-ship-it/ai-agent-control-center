// Synthetic GET /providers/onboarding answers for the Connections tests, shaped like the backend's
// (agent_manager.onboarding_view, providers.status, usage_view, provider_onboarding). No real tool
// is discovered, checked, installed or signed in: every answer is built here.
const CONFIRMATION = 'I confirm that this provider uses my existing subscription sign-in and that paid overage, extra usage, credits and API billing are not enabled for the Control Center.'
const CODEX_ACK = 'I understand that Codex runs through my ChatGPT subscription and that its usage limits apply. Existing credits or automatic credit purchases configured on my OpenAI account may still affect billing outside the Control Center.'
const SOURCES = {
  claude: ['exact_when_reported', 'Claude Code reports usage meters through its documented /usage command and reports the active limit during runs.'],
  codex: ['exhaustion_signal', 'Codex exposes no documented usage interface to the Control Center. Managed runs report usage-limit errors.'],
  gemini: ['none', 'Gemini CLI exposes no documented usage or quota interface; failures are not classified as quota.'],
}
const LABELS = { claude: 'Claude Code', codex: 'Codex', gemini: 'Gemini CLI' }
const ROUTES = { claude: ['claude_ai', 'subscription'], codex: ['chatgpt', 'subscription (built-in openai provider)'], gemini: ['oauth_personal', null] }

export const NOW = '2026-10-30T09:00:00Z'

export function meter(label, used, extra = {}) {
  return { window_id: label.toLowerCase().replace(/\W+/g, '_'), group_name: 'claude', scope_key: '', label, status: used >= 100 ? 'exhausted' : 'available',
    used_percent: used, reset_at: '2026-10-30T14:00:00Z', observed_at: '2026-10-30T08:55:00Z', fresh: true, model_scope: null, applies_to: 'Shared or scope not reported', ...extra }
}

// One provider's entry. `state` is the backend's readiness state.
export function entry(provider, { state = 'ready', enabled = true, allowed = true, authenticated = true, installed = true, confirmed = true,
  windows = [], blocks = [], operations = [], activeWork = [], reason = null, catalogFresh = true } = {}) {
  const [authRoute, billingRoute] = ROUTES[provider]
  const [source, explanation] = SOURCES[provider]
  const exhausted = windows.some((window) => window.status === 'exhausted')
  const overall = blocks.length || exhausted ? 'limited' : windows.length && windows.every((window) => window.status === 'available' && window.fresh) ? 'available' : 'unknown'
  return {
    provider, allowed, operations, active_work: activeWork,
    discovery: { provider, state: installed ? 'installed' : 'not_installed', location: installed ? 'fixture/bin/tool.exe' : null, method: installed ? 'npm' : null,
      version: installed ? '2.1.300' : null, elsewhere: [], prerequisites: { npm: 'fixture/nodejs/npm.cmd' }, os_supported: true },
    catalog: { fresh: catalogFresh, last_success_at: '2026-10-30T08:00:00Z' },
    status: {
      provider, label: LABELS[provider], docs_url: null, install_guidance: null, auth_guidance: null,
      billing_guidance: provider === 'gemini' ? 'Only a Google sign-in route is allowed; Gemini API keys and Vertex AI bill per use.' : null,
      documented_models: [], capabilities: { reply: { supported: true, reason: null }, delegation: { supported: provider !== 'gemini', reason: provider === 'gemini' ? 'Not supported by this provider adapter.' : null } },
      state: { provider, state, reason, guidance: null, installed, authenticated: installed ? authenticated : null, auth_route: authenticated ? authRoute : null,
        billing_route: authenticated && confirmed ? billingRoute : null, version: installed ? '2.1.300' : null },
      settings: { enabled, billing_confirmed: confirmed, billing_confirmed_at: confirmed ? '2026-10-24T10:00:00Z' : null, models: [] },
      usage: { source, explanation, overall, windows, blocks, unpersisted: 0, last_observed_at: windows.length ? windows[0].observed_at : null,
        refresh_supported: provider === 'claude', remaining: null, remaining_source: 'not_reported_by_provider' },
      confirmation_text: provider === 'codex' ? CODEX_ACK : CONFIRMATION, acknowledgment_kind: provider === 'codex' ? 'acknowledgment' : 'confirmation',
    },
  }
}

export function onboarding({ claude = {}, codex = {}, gemini = {}, policy = {}, updates = {} } = {}) {
  const providers = [
    { provider: 'mock', status: { provider: 'mock', state: { state: 'ready' }, settings: { enabled: true } } },
    entry('codex', codex), entry('claude', claude), entry('gemini', gemini),
  ]
  const allowed = ['codex', 'claude', 'gemini'].filter((id) => providers.find((item) => item.provider === id).allowed)
  return {
    providers,
    updates: { claude: { status: 'up_to_date', installed_version: '2.1.300', latest_version: '2.1.300', channel: 'latest', checked_at: '2026-10-30T07:00:00Z', stale: false },
      codex: { status: 'unknown', checked_at: null, stale: true }, gemini: { status: 'unknown', checked_at: null, stale: true }, ...updates },
    policy: { allowed, default_provider: null, default_model: null, default_effort: null, revision: 4, default_problem: null, ...policy },
  }
}

// The default answer: Claude Code ready with meters, Codex ready with only limit signals, Gemini CLI not in use.
export const DEFAULT_ONBOARDING = onboarding({
  claude: { windows: [meter('Session limit', 42), meter('Weekly limit (all models)', 18, { reset_at: '2026-11-03T00:00:00Z' })] },
  gemini: { state: 'disabled', enabled: false, allowed: false, authenticated: null, confirmed: false },
})

export function plan(provider, action, extra = {}) {
  return { plan_id: `plan-${provider}-${action}`, digest: 'd'.repeat(64), provider, action, method: 'npm', executable: 'fixture/nodejs/npm.cmd',
    command: ['npm.cmd', 'install', '--global', `@fixture/${provider}@2.1.301`, '--ignore-scripts'], current_version: '2.1.300', target: 'latest',
    target_version: '2.1.301', package: `@fixture/${provider}`, registry: 'the npm registry', source: 'https://www.npmjs.com/', console: action === 'login',
    changes: ['Replaces the installed version with 2.1.301.'], dependencies: [], risks: ['Running agents of this tool are not affected; new work waits until it finishes.'],
    manual: [], blockers: [], ...extra }
}
