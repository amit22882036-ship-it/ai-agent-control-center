// Where a person checks, in their own provider account, whether use beyond their plan could be
// paid. The Control Center cannot read or change these account settings: it only words where they
// are. Each place and link was checked against the provider's official pages on 2026-10-30
// (docs/atlas-migration.md, "Billing guidance sources"); nothing here is inferred from a plan.
export const GUIDANCE_CHECKED = '2026-10-30'

export const BILLING_GUIDES = {
  claude: {
    short: 'Usage credits, in your Claude account under Settings › Usage, decide whether use past your plan’s limits is paid.',
    summary: 'Claude Code runs on your Claude subscription. Paid use past your plan’s limits comes from usage credits, an account setting on paid Claude plans that you turn on or off yourself.',
    where: [
      'Claude account › Settings › Usage › Usage credits: turn them on or off, and set any spending limit or auto-reload there. Anthropic’s help center calls these usage credits; the article’s address still says extra usage.',
      'In Claude Code, /usage shows the current usage meters (the same report Refresh usage reads here).',
      'Claude Console billing belongs to pay-per-use API keys, which the Control Center refuses.',
    ],
    links: [
      { label: 'Your Claude usage settings', url: 'https://claude.ai/settings/usage' },
      { label: 'Manage usage credits (Claude Help Center)', url: 'https://support.claude.com/en/articles/12429409-extra-usage-for-paid-claude-plans' },
      { label: 'Use Claude Code with your Pro or Max plan', url: 'https://support.claude.com/en/articles/11145838-using-claude-code-with-your-pro-or-max-plan' },
    ],
  },
  codex: {
    short: 'ChatGPT credits, if your plan allows them, decide whether Codex keeps working past your plan’s limits. The Control Center can’t see them.',
    summary: 'Codex runs on your ChatGPT plan. When the plan’s limits are reached, eligible plans can continue on purchased credits; prices and eligibility depend on your plan or agreement.',
    where: [
      'Codex usage dashboard: your current limits, linked from OpenAI’s Codex pricing page.',
      'OpenAI’s help center describes automatic top-up of credits for eligible accounts. Whether it is offered and turned on depends on your account and its settings.',
      'The Control Center never buys credits and cannot see, turn on or limit credits or automatic top-up on your account.',
    ],
    links: [
      { label: 'Your Codex usage dashboard', url: 'https://chatgpt.com/codex/settings/usage' },
      { label: 'Codex pricing and limits', url: 'https://learn.chatgpt.com/docs/pricing' },
      { label: 'Using credits for flexible usage (OpenAI Help)', url: 'https://help.openai.com/en/articles/12642688' },
    ],
  },
  gemini: {
    short: 'Only a Google sign-in is allowed here, and since June 18, 2026 Gemini CLI no longer serves personal Google accounts. Paid API keys and Vertex AI are refused.',
    summary: 'With an organization’s Gemini Code Assist license, Gemini CLI use is covered by that license and its daily request limits. Gemini CLI also accepts paid Gemini API keys and Vertex AI, which bill per use; the Control Center refuses those routes.',
    where: [
      'In Gemini CLI, /stats model shows current usage during a session.',
      'Your organization’s Google Cloud administrator manages Gemini Code Assist licenses and their limits.',
      'A route Gemini CLI supports is not thereby allowed here: the Control Center accepts only the Google sign-in.',
    ],
    links: [
      { label: 'Gemini CLI quotas and pricing', url: 'https://geminicli.com/docs/resources/quota-and-pricing/' },
    ],
  },
}

// What the provider currently offers to which accounts, kept apart from what the Control Center
// allows. Checked against official pages on the same date; nothing here changes a backend rule.
export const ACCOUNT_NOTICES = {
  gemini: {
    title: 'Personal Google accounts no longer work with Gemini CLI',
    lines: [
      'Since June 18, 2026, Gemini CLI no longer serves free, Google AI Pro or Google AI Ultra accounts. Google moved those accounts to Antigravity CLI.',
      'Sign in with Google still works for organizations with a Gemini Code Assist Standard or Enterprise license, which Google says follow their own schedule.',
      'The Control Center allows only that Google sign-in. Paid Gemini API keys and Vertex AI still work in Gemini CLI but stay refused here, because they bill per use.',
      'Antigravity CLI is not a Control Center tool; it can’t be connected here.',
    ],
    links: [
      { label: 'Gemini CLI transition announcement (Google)', url: 'https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli' },
      { label: 'Gemini Code Assist consumer accounts (Google)', url: 'https://developers.google.com/gemini-code-assist/docs/deprecations/code-assist-individuals' },
    ],
  },
}

export function accountNotice(provider) {
  return ACCOUNT_NOTICES[provider] || null
}

export function billingGuide(provider) {
  return BILLING_GUIDES[provider] || null
}
