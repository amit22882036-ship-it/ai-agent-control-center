// Why a folder can't be added as a project (POST /projects refusals), and the plain-language plan
// for the opt-in Git setup (POST /projects/folder-check, then /projects/folder-setup). Nothing here
// writes anything; the dialog sends setup only after the person confirms this exact plan.

const CODES = new Set(['not_git_repo', 'no_commits', 'not_repo_root', 'dubious_ownership', 'git_missing', 'git_unusable',
  'already_registered', 'overlaps_project'])

// The structured reason from a refused POST /projects, or null for any other refusal.
export function folderRefusal(result) {
  const detail = result?.data?.detail
  if (!detail || typeof detail !== 'object' || !CODES.has(detail.code)) return null
  return {
    code: detail.code,
    message: typeof detail.message === 'string' ? detail.message : '',
    topLevel: typeof detail.top_level === 'string' ? detail.top_level : null,
    accidentalParent: Boolean(detail.accidental_parent),
    command: typeof detail.command === 'string' ? detail.command : null,
    // The registered project this folder already is, or overlaps.
    projectId: typeof detail.project_id === 'string' ? detail.project_id : null,
    projectName: typeof detail.name === 'string' ? detail.name : null,
  }
}

// Which next step the dialog offers for a reason: set up Git here, use the repository's top folder,
// or nothing it can do (the message says what the person can do themselves).
export function folderRemedy(refusal) {
  if (!refusal) return null
  if (refusal.code === 'not_git_repo' || (refusal.code === 'not_repo_root' && refusal.accidentalParent)) {
    return { kind: 'setup', label: 'Initialize Git and create first commit…' }
  }
  if (refusal.code === 'no_commits') return { kind: 'setup', label: 'Create the first commit…' }
  if (refusal.code === 'not_repo_root' && refusal.topLevel) return { kind: 'use-top', label: 'Use the repository’s top folder' }
  if (refusal.code === 'dubious_ownership' && refusal.command) return { kind: 'command' }
  if ((refusal.code === 'already_registered' || refusal.code === 'overlaps_project') && refusal.projectId) {
    return { kind: 'use-existing', label: refusal.projectName ? `Use “${refusal.projectName}”` : 'Use that project' }
  }
  return null
}

export function formatBytes(bytes) {
  const value = Number(bytes) || 0
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = value / 1024
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1 }
  return `${size >= 10 ? Math.round(size) : size.toFixed(1)} ${units[unit]}`
}

const files = (count) => `${count} ${count === 1 ? 'file' : 'files'}`

// Exactly what setup will write into the folder, as short sentences, for the confirmation step.
export function setupWrites(plan, writeGitignore = false) {
  if (!plan) return []
  const useIgnore = Boolean(writeGitignore && plan.suggested_gitignore)
  const count = useIgnore ? plan.suggested_gitignore.file_count : plan.file_count
  const bytes = useIgnore ? plan.suggested_gitignore.total_bytes : plan.total_bytes
  const lines = []
  if (plan.action === 'init_and_commit') lines.push('A new Git repository: a hidden .git folder inside this folder.')
  if (useIgnore) lines.push(`A .gitignore file that leaves out ${plan.bulky_directories.map((name) => `${name}/`).join(', ')}.`)
  if (plan.empty || count === 0) {
    lines.push(`A first commit named “${plan.commit_message}” with no files, because the folder is empty.`)
  } else {
    lines.push(`A first commit named “${plan.commit_message}” with ${files(count)} (${formatBytes(bytes)}) from the folder as it is now.`)
  }
  if (plan.identity) lines.push(`The commit is made by ${plan.identity.name} <${plan.identity.email}>, from your Git settings.`)
  return lines
}

// Things worth a second look before committing: dependency/build folders and large files.
export function setupWarnings(plan, writeGitignore = false) {
  if (!plan) return []
  const warnings = []
  const useIgnore = Boolean(writeGitignore && plan.suggested_gitignore)
  if (plan.bulky_directories?.length && !useIgnore) {
    warnings.push(`${plan.has_gitignore ? 'These folders are not ignored' : 'There is no .gitignore'}, so ${plan.bulky_directories.map((name) => `${name}/`).join(', ')} would be committed too. They usually hold installed packages or build output, not source.`)
  }
  if (plan.identity_placeholder) {
    warnings.push(`Your Git name and email look like placeholders (${plan.identity.name} <${plan.identity.email}>), so the first commit would be made under them. To use your own, run git config --global user.name "Your real name" and git config --global user.email "your@email" in a terminal, then go Back and review again.`)
  }
  if (plan.large_file_count > 0) {
    const names = plan.large_files.map((item) => `${item.path} (${formatBytes(item.bytes)})`).join(', ')
    const more = plan.large_file_count > plan.large_files.length ? ` and ${plan.large_file_count - plan.large_files.length} more` : ''
    warnings.push(`Large files over 50 MB would be committed: ${names}${more}.`)
  }
  return warnings
}
