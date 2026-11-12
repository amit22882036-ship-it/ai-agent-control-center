// Access levels for a new agent, as each provider really enforces them (backend `access_modes`).
// The same word means different things with different tools, so every level is described per tool;
// the backend stays the authority and refuses what a tool or version cannot do.

export const ACCESS_LEVELS = [
  { value: 'read-only', label: 'Read only' },
  { value: 'workspace-write', label: 'Workspace' },
  { value: 'full-access', label: 'Full Access' },
]

const TEXT = {
  claude: {
    'read-only': 'Reads and searches files. No file changes, no commands and no web access.',
    'workspace-write': 'Edits files in the task’s private copy without asking. Commands (PowerShell, Bash) and web access stay off: Claude Code cannot confine them to the copy on Windows.',
    'full-access': 'Every Claude Code tool without approval prompts: PowerShell and Bash commands, web search and fetch, and the repository’s own Claude settings (your MCP servers only when the task allows external integrations).',
  },
  codex: {
    'read-only': 'Reads files and runs commands that cannot change anything, inside the Codex sandbox.',
    'workspace-write': 'Edits files and runs commands such as tests, builds and Git inside the Codex sandbox, limited to the task’s private copy. No network access.',
    'full-access': 'Codex runs commands without its sandbox, with network access (danger-full-access).',
  },
  gemini: {
    'read-only': 'Reads files. Tools that would need approval do not run.',
    'workspace-write': 'Edits files without asking. Shell commands need approval in Gemini CLI, so they do not run.',
    'full-access': 'Gemini CLI’s yolo mode: every tool runs without approval, including shell commands.',
  },
}

export function accessText(provider, level) {
  if (provider === 'mock') return 'Mock agents only simulate work; access does not apply.'
  return TEXT[provider]?.[level] || ''
}

// Full Access is never sandboxed: said plainly, with the exact scope, before it can be chosen.
export const FULL_ACCESS_WARNING = 'Full Access is not sandboxed. The agent can run any command, use the network and change any file your '
  + 'Windows account can change, outside the task’s private copy too. Choose it only for work you would run yourself.'

export function accessSummary(level) {
  if (level === 'full-access') return 'has Full Access (not sandboxed)'
  if (level === 'workspace-write') return 'may edit files there'
  return 'can only read files'
}
