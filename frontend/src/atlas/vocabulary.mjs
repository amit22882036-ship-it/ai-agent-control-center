// Shared Atlas vocabulary. The backend keeps its own identifiers (root/child, mock, display_color);
// these are only the words and visual labels people read.
export const TOOLS = {
  claude: { label: 'Claude Code', mark: 'Cl' },
  codex: { label: 'Codex', mark: 'Cx' },
  gemini: { label: 'Gemini CLI', mark: 'Gm' },
  mock: { label: 'Practice', mark: 'Pr' },
}

// The nine agent colour labels the backend accepts (agents.display_color).
export const AGENT_COLORS = ['neutral', 'violet', 'blue', 'cyan', 'green', 'yellow', 'orange', 'red', 'pink']

// Status beads. Each state has its own shape, so colour is never the only signal.
export const STATES = {
  work: 'Working',
  ask: 'Needs your reply',
  helpers: 'Waiting for helpers',
  paused: 'Paused',
  blocked: 'Blocked',
  pending: 'Not started',
  failed: 'Didn’t finish',
  done: 'Finished',
  review: 'Ready for review',
  stopped: 'Stopped',
  removed: 'Removed',
}

export const THEME_LABELS = { automatic: 'Automatic', light: 'Light', dark: 'Dark' }

export function nextTheme(mode) {
  return { automatic: 'light', light: 'dark', dark: 'automatic' }[mode] || 'automatic'
}

export function toolMark(tool) {
  return TOOLS[tool]?.mark || '?'
}

export function colorLabel(color) {
  return AGENT_COLORS.includes(color) ? color : 'neutral'
}
