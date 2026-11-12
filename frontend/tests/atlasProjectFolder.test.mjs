import test from 'node:test'
import assert from 'node:assert/strict'
import { folderRefusal, folderRemedy, formatBytes, setupWarnings, setupWrites } from '../src/atlas/model/projectFolder.mjs'

const refused = (detail) => ({ ok: false, status: 409, data: { detail } })

test('only structured folder reasons are recognized; plain refusals stay plain', () => {
  assert.equal(folderRefusal(refused('Project root must be a valid existing directory')), null)
  assert.equal(folderRefusal(refused({ code: 'something_else', message: 'x' })), null)
  assert.equal(folderRefusal({ offline: true }), null)
  assert.deepEqual(folderRefusal(refused({ code: 'not_repo_root', message: 'Inside a repo', top_level: 'D:\\repo', accidental_parent: false })),
    { code: 'not_repo_root', message: 'Inside a repo', topLevel: 'D:\\repo', accidentalParent: false, command: null })
})

test('each reason offers its own next step, and never a silent one', () => {
  const remedy = (detail) => folderRemedy(folderRefusal(refused(detail)))
  assert.deepEqual(remedy({ code: 'not_git_repo', message: '' }), { kind: 'setup', label: 'Initialize Git and create first commit…' })
  assert.deepEqual(remedy({ code: 'no_commits', message: '' }), { kind: 'setup', label: 'Create the first commit…' })
  assert.equal(remedy({ code: 'not_repo_root', message: '', top_level: 'D:\\repo' }).kind, 'use-top')
  assert.equal(remedy({ code: 'not_repo_root', message: '', top_level: 'C:\\Users\\me', accidental_parent: true }).kind, 'setup')
  assert.deepEqual(remedy({ code: 'dubious_ownership', message: '', command: 'git config --global --add safe.directory "D:/x"' }), { kind: 'command' })
  assert.equal(remedy({ code: 'git_missing', message: 'Install Git' }), null)
  assert.equal(folderRemedy(null), null)
})

const plan = {
  action: 'init_and_commit', commit_message: 'Initial commit', empty: false, file_count: 1204, total_bytes: 52_428_800,
  has_gitignore: false, bulky_directories: ['node_modules'], large_files: [{ path: 'media/clip.mp4', bytes: 104_857_600 }],
  large_file_count: 1, identity: { name: 'Test Person', email: 'test@localhost' }, blocked: null,
  suggested_gitignore: { content: 'node_modules/\n', file_count: 4, total_bytes: 2048 },
}

test('the confirmation states exactly what is written, with and without the suggested .gitignore', () => {
  assert.deepEqual(setupWrites(plan), [
    'A new Git repository: a hidden .git folder inside this folder.',
    'A first commit named “Initial commit” with 1204 files (50 MB) from the folder as it is now.',
    'The commit is made by Test Person <test@localhost>, from your Git settings.',
  ])
  assert.deepEqual(setupWrites(plan, true).slice(0, 3), [
    'A new Git repository: a hidden .git folder inside this folder.',
    'A .gitignore file that leaves out node_modules/.',
    'A first commit named “Initial commit” with 4 files (2.0 KB) from the folder as it is now.',
  ])
  assert.equal(setupWrites({ ...plan, action: 'commit' })[0].startsWith('A first commit'), true)
})

test('an empty folder is told it gets an empty first commit', () => {
  const empty = { ...plan, empty: true, file_count: 0, total_bytes: 0, bulky_directories: [], large_files: [], large_file_count: 0, suggested_gitignore: null }
  assert.equal(setupWrites(empty)[1], 'A first commit named “Initial commit” with no files, because the folder is empty.')
  assert.deepEqual(setupWarnings(empty), [])
})

test('dependency folders and large files are warned about before committing', () => {
  const warnings = setupWarnings(plan)
  assert.equal(warnings.length, 2)
  assert.match(warnings[0], /There is no \.gitignore, so node_modules\/ would be committed too/)
  assert.equal(warnings[1], 'Large files over 50 MB would be committed: media/clip.mp4 (100 MB).')
  assert.equal(setupWarnings(plan, true).length, 1) // the suggested .gitignore removes the folder warning
  const placeholder = setupWarnings({ ...plan, identity: { name: 'Your Name', email: 'you@example.com' }, identity_placeholder: true }, true)
  assert.match(placeholder[0], /^Your Git name and email look like placeholders \(Your Name <you@example\.com>\)/)
})

test('sizes are short and readable', () => {
  assert.deepEqual([formatBytes(0), formatBytes(900), formatBytes(1536), formatBytes(52_428_800), formatBytes(3 * 1024 ** 3)],
    ['0 B', '900 B', '1.5 KB', '50 MB', '3.0 GB'])
})
