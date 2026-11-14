import test from 'node:test'
import assert from 'node:assert/strict'
import { deletionScope, trashSummary } from '../src/atlas/model/projectLifecycle.mjs'

test('a project removed from the Control Center keeps its folder, and deleting it never lists the folder', () => {
  // Moving to the Trash moved the person's code folder; removing now keeps it, and the wording says so.
  const kept = { trash: { state: 'trashed', folder_missing: true, folder_kept: true, original_root_path: 'C:\\code\\shop', path: null } }
  assert.match(trashSummary(kept), /its folder stays at C:\\code\\shop, untouched/)
  const preview = { folder: { path: 'C:\\trash\\missing\\x', exists: false }, worktrees: 1, activity: { tasks: 2 }, agents: 1 }
  const lines = deletionScope(preview, kept)
  assert.ok(!lines.some((line) => /^The folder/.test(line)), lines.join(' | '))
  assert.ok(lines.some((line) => /worktree/.test(line)))
  // A folder moved to the Trash is still listed as deleted.
  assert.match(deletionScope({ ...preview, folder: { path: 'C:\\trash\\shop', exists: true } })[0], /^The folder C:\\trash\\shop/)
})
