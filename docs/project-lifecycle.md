# Project organization and lifecycle

Schema v39 adds the person's own project order, display names, hiding, a real Trash, restore and
permanent deletion. Implementation: `backend/app/project_lifecycle.py`; tests:
`backend/test_project_lifecycle.py` (real temporary folders, junctions and Git repositories).

## What each action does

| Action | Folder on disk | Records | Running work |
| --- | --- | --- | --- |
| Reorder, rename | Unchanged (the folder name is never changed) | Order or display name only | Unaffected |
| Hide | Unchanged | Marked hidden; tasks, agents and history kept | Keeps running; anything that needs you still appears |
| Move to Trash | **Moved** into the Trash | Kept; launches refused | Refused while agents run or wait; "Stop all work" first |
| Restore | Moved back (or to another place you choose) | Visible again, in its old position | Can run again |
| Delete permanently | **Deleted**, with its Task worktrees | Deleted (tasks, agents, output, executions, delegations…) | Only from the Trash, after typing the name |

## Where the Trash is

The folder moves with a single rename on its own drive, so the move is instant and cannot leave
half-copied files. On the drive of the application data it goes to
`%LOCALAPPDATA%\AI Agent Control Center\Trash`; on another drive it goes to a hidden
`.control-center-trash` folder next to the project. The Trash dialog says which. If Windows reports
the folder in use, nothing changes and the message says to close the program that holds it.

## Safety rules (enforced in the backend)

- A drive root, a Windows or user folder, the application's own folder, its database, the worktree
  storage or the Trash itself can never be moved or deleted.
- Deletion never follows links or junctions: a link inside the folder is removed as a link, and
  what it points to is untouched. Read-only files (Git objects) are deleted.
- Nothing is sent to a remote. The deletion dialog lists the remotes, uncommitted changes,
  commits that are on no remote, and stashes, from local Git reads only.
- Records are deleted in one transaction by following foreign keys from the project, then checked
  with `PRAGMA foreign_key_check`. An agent is deleted only when all its assignments belonged to
  the project.
- A failed deletion keeps the records, marks the project "deletion incomplete" with the reason, and
  can be retried. A move, restore or deletion interrupted by a restart is settled at the next start
  from what is on disk.

## Upgrades

Opening a v38 database creates `control_center.sqlite3.schema-v38.bak` next to it before
upgrading. An older Control Center refuses a v39 database; the backup is the way back.

## API

`PATCH /projects/{id}`, `PUT /projects/order`, `POST /projects/{id}/hide|unhide|stop-work|trash|restore`,
`DELETE /projects/{id}` (body `{"confirm_name": …}`), `GET /projects/trash`,
`GET /projects/{id}/lifecycle` (preview, local reads only), and `PATCH /tasks/{id}` (title only).
Refusals are `409` with `{"detail": {"code", "message", "reasons"}}`.
