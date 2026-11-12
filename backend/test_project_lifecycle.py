"""Project order, names, visibility, Trash, restore and permanent deletion (schema v39).

Every folder here is a disposable temporary fixture: real Git repositories, real junctions and
real moves and deletions, never a developer's project. Providers are fakes."""
import asyncio
import json
import os
import shutil
from pathlib import Path
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from app import agent_manager as manager
from app import project_lifecycle, workspace_git
from app.main import app
from app.persistence import AgentStore, SCHEMA_VERSION
from app.project_domain import canonical_path
from app.task_workspaces import provision_task_workspace, validate_task_workspace
from api_test_support import request_headers
from workspace_test_support import make_repository
import test_task_lifecycle


def junction(link, target):
    """A directory junction (no administrator rights needed), as Windows tools create them."""
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)


class ProjectLifecycleTests(unittest.TestCase):
    setUp_base = test_task_lifecycle.TaskLifecycleTests.setUp
    tearDown = test_task_lifecycle.TaskLifecycleTests.tearDown
    replacement = test_task_lifecycle.TaskLifecycleTests.replacement
    task_for = test_task_lifecycle.TaskLifecycleTests.task_for

    def setUp(self):
        self.setUp_base()
        self.trash = Path(self.temp.name) / 'app-data' / 'Trash'
        patch.dict(os.environ, {'CONTROL_CENTER_TRASH_ROOT': str(self.trash)}).start()

    def call(self, method, path, body=None, expected=200):
        async def run():
            messages = []

            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b'', 'more_body': False}

            async def send(message):
                messages.append(message)
            await app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                       'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': b'',
                       'headers': request_headers(method), 'root_path': '', 'client': ('127.0.0.1', 1234),
                       'server': ('localhost', 8000)}, receive, send)
            self.assertEqual(messages[0]['status'], expected, messages)
            return json.loads(b''.join(item.get('body', b'') for item in messages))
        return asyncio.run(run())

    def project(self, name='Shop'):
        root = make_repository(Path(self.temp.name) / 'projects' / name)
        (root / 'notes.txt').write_text(f'{name} notes\n', encoding='utf-8')
        return self.call('POST', '/projects', {'name': name, 'root_path': str(root)}), root

    def start(self, project_id, task='Scoped work'):
        process = self.replacement()
        process.terminate.side_effect = lambda: setattr(process.poll, 'return_value', 0)
        with patch.object(manager, '_spawn_process', return_value=process), patch.object(manager, 'Thread') as thread, \
                patch.object(manager, '_codex_command', return_value='synthetic'):
            thread.return_value.is_alive.return_value = False
            result = self.call('POST', '/agents/start', {'task': task, 'project_id': project_id, 'agent_type': 'codex'})
        manager.agent_readers[result['agent_id']].is_alive.return_value = False
        return result['agent_id']

    def stop(self, project_id):
        # The fake process tree ends when asked, as a supervised one does.
        with patch.object(manager, '_stop_windows_tree', side_effect=lambda process: setattr(process.poll, 'return_value', 0)):
            result = self.call('POST', f'/projects/{project_id}/stop-work', {})
        self.assertEqual(result['failures'], [])
        return result

    def test_a_stop_that_is_not_confirmed_is_reported_and_keeps_the_project(self):
        project, root = self.project()
        agent = self.start(project['project_id'])
        with patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('Could not terminate; stop was not confirmed.')):
            result = self.call('POST', f"/projects/{project['project_id']}/stop-work", {})
        self.assertEqual([item['agent_id'] for item in result['failures']], [agent])
        self.assertEqual(result['stopped'], [])
        self.assertEqual(self.call('POST', f"/projects/{project['project_id']}/trash", {}, 409)['detail']['code'], 'project_busy')
        self.assertTrue(root.is_dir())
        self.stop(project['project_id'])

    # ------------------------------------------------------------ organization

    def test_rename_order_and_hide_change_presentation_only(self):
        shop, shop_root = self.project('Shop')
        blog, _ = self.project('Blog')
        self.assertEqual([p['name'] for p in self.call('GET', '/projects')['projects']], ['Shop', 'Blog'])
        renamed = self.call('PATCH', f"/projects/{shop['project_id']}", {'name': '  Storefront '})
        self.assertEqual(renamed['name'], 'Storefront')
        self.assertEqual(renamed['root_path'], shop['root_path'])
        self.assertTrue((shop_root / 'notes.txt').is_file())
        for name in ('', ' ', 'x' * 101, 'line\nbreak'):
            self.call('PATCH', f"/projects/{shop['project_id']}", {'name': name}, 422)
        self.call('PATCH', '/projects/missing', {'name': 'Anything'}, 404)

        ordered = self.call('PUT', '/projects/order', {'project_ids': [blog['project_id'], shop['project_id']]})['projects']
        self.assertEqual([p['name'] for p in ordered], ['Blog', 'Storefront'])
        self.assertEqual([p['name'] for p in self.call('GET', '/projects')['projects']], ['Blog', 'Storefront'])
        stale = self.call('PUT', '/projects/order', {'project_ids': [blog['project_id']]}, 409)
        self.assertEqual(stale['detail']['code'], 'order_outdated')
        self.call('PUT', '/projects/order', {'project_ids': [blog['project_id'], blog['project_id']]}, 409)
        third, _ = self.project('Docs')
        self.assertEqual([p['name'] for p in self.call('GET', '/projects')['projects']], ['Blog', 'Storefront', 'Docs'])

        hidden = self.call('POST', f"/projects/{third['project_id']}/hide", {})
        self.assertTrue(hidden['hidden'])
        listed = {p['project_id']: p for p in self.call('GET', '/projects')['projects']}
        self.assertTrue(listed[third['project_id']]['hidden'])
        self.assertFalse(self.call('POST', f"/projects/{third['project_id']}/unhide", {})['hidden'])

    def test_hiding_never_stops_work(self):
        project, _ = self.project()
        agent = self.start(project['project_id'])
        self.call('POST', f"/projects/{project['project_id']}/hide", {})
        self.assertEqual(manager.agent_statuses[agent], 'running')
        self.stop(project['project_id'])

    def test_task_rename_changes_title_only(self):
        project, _ = self.project()
        agent = self.start(project['project_id'], 'Fix the checkout\nwith more details')
        task = self.task_for(agent)
        renamed = self.call('PATCH', f"/tasks/{task['task_id']}", {'title': '  Checkout   fix '})
        self.assertEqual(renamed['title'], 'Checkout fix')
        stored = manager._store.get_task(task['task_id'])
        self.assertEqual(stored['description'], task['description'])
        self.assertEqual(stored['updated_at'], task['updated_at'])
        self.assertEqual(stored['parent_task_id'], task['parent_task_id'])
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['agent_id'], agent)
        for title in ('', '   ', 'x' * 101):
            self.call('PATCH', f"/tasks/{task['task_id']}", {'title': title}, 422)
        self.call('PATCH', '/tasks/missing', {'title': 'Anything'}, 404)
        self.stop(project['project_id'])

    # ------------------------------------------------------------ Trash

    def test_trash_is_refused_while_work_runs_and_then_moves_the_real_folder(self):
        project, root = self.project()
        agent = self.start(project['project_id'])
        refused = self.call('POST', f"/projects/{project['project_id']}/trash", {}, 409)['detail']
        self.assertEqual(refused['code'], 'project_busy')
        self.assertIn('1 agent is working in this project.', refused['reasons'])
        self.assertTrue(root.is_dir())
        preview = self.call('GET', f"/projects/{project['project_id']}/lifecycle")
        self.assertTrue(preview['activity']['busy'])
        self.assertEqual(preview['protections'], [])
        stopped = self.stop(project['project_id'])
        self.assertEqual([item['agent_id'] for item in stopped['stopped']], [agent])

        trashed = self.call('POST', f"/projects/{project['project_id']}/trash", {})
        self.assertTrue(trashed['trashed'])
        self.assertEqual(trashed['trash']['state'], 'trashed')
        self.assertEqual(trashed['trash']['original_root_path'], canonical_path(root)[0])
        self.assertFalse(root.exists())
        moved = Path(trashed['trash']['path'])
        self.assertTrue((moved / 'notes.txt').is_file())
        self.assertTrue(canonical_path(moved)[1].startswith(canonical_path(self.trash)[1]))
        self.assertEqual(self.call('GET', '/projects')['projects'], [])
        self.assertEqual([p['project_id'] for p in self.call('GET', '/projects/trash')['projects']], [project['project_id']])
        # No launch passes admission while the Project is in the Trash.
        task = self.task_for(agent)
        with manager._store._connection() as db:
            with self.assertRaisesRegex(ValueError, 'in the Trash'):
                project_lifecycle.require_runnable(db, task['task_id'])
        self.call('POST', f"/projects/{project['project_id']}/hide", {}, 409)
        self.call('POST', f"/projects/{project['project_id']}/trash", {}, 409)

    def test_restore_returns_the_folder_and_handles_an_occupied_location(self):
        project, root = self.project()
        agent = self.start(project['project_id'])
        task = self.task_for(agent)
        workspace = manager._store.get_task_workspace(task['task_id'])
        self.stop(project['project_id'])
        self.call('PUT', '/projects/order', {'project_ids': [project['project_id']]})
        self.call('POST', f"/projects/{project['project_id']}/hide", {})
        self.call('POST', f"/projects/{project['project_id']}/trash", {})

        # Someone created a different folder at the original location meanwhile.
        root.mkdir(parents=True)
        (root / 'unrelated.txt').write_text('keep me', encoding='utf-8')
        conflict = self.call('POST', f"/projects/{project['project_id']}/restore", {}, 409)['detail']
        self.assertEqual(conflict['code'], 'restore_target_exists')
        self.assertEqual((root / 'unrelated.txt').read_text(encoding='utf-8'), 'keep me')
        suggestion = conflict['suggestion']
        self.assertTrue(suggestion.endswith('Shop (restored)'))

        restored = self.call('POST', f"/projects/{project['project_id']}/restore", {'target_path': suggestion})
        self.assertFalse(restored['trashed'])
        self.assertFalse(restored['hidden'])
        self.assertEqual(restored['root_path'], canonical_path(suggestion)[0])
        self.assertTrue((Path(suggestion) / 'notes.txt').is_file())
        self.assertEqual((root / 'unrelated.txt').read_text(encoding='utf-8'), 'keep me')
        self.assertNotIn('warning', restored)
        # The Task worktree was relinked to the new location by Git.
        validate_task_workspace(manager._store, manager._store.get_task(task['task_id']),
                                manager._store.get_task_workspace(task['task_id']))
        self.assertEqual(workspace_git.repository(workspace['workspace_path']), workspace_git.repository(suggestion))

    def test_restore_to_the_original_location(self):
        project, root = self.project()
        self.call('POST', f"/projects/{project['project_id']}/trash", {})
        restored = self.call('POST', f"/projects/{project['project_id']}/restore", {})
        self.assertEqual(restored['root_path'], canonical_path(root)[0])
        self.assertTrue((root / 'notes.txt').is_file())
        self.assertFalse(any(self.trash.iterdir()))
        self.call('POST', f"/projects/{project['project_id']}/restore", {}, 409)

    def test_a_folder_in_use_is_reported_and_nothing_changes(self):
        project, root = self.project()
        error = OSError(32, 'The process cannot access the file because it is being used by another process')
        error.winerror = 32
        with patch.object(project_lifecycle.os, 'rename', side_effect=error):
            refused = self.call('POST', f"/projects/{project['project_id']}/trash", {}, 409)['detail']
        self.assertEqual(refused['code'], 'folder_in_use')
        self.assertTrue((root / 'notes.txt').is_file())
        after = self.call('GET', f"/projects/{project['project_id']}")
        self.assertFalse(after['trashed'])
        self.assertEqual(after['root_path'], canonical_path(root)[0])

    # ------------------------------------------------------------ permanent deletion

    def test_permanent_deletion_removes_folder_worktrees_and_records_only_for_that_project(self):
        project, root = self.project('Doomed')
        keep, keep_root = self.project('Kept')
        # A local "remote" that deletion must never touch.
        remote = Path(self.temp.name) / 'remote.git'
        workspace_git.git(Path(self.temp.name), 'init', '--bare', str(remote))
        workspace_git.git(root, 'remote', 'add', 'origin', 'https://someone:secret-token@example.invalid/doomed.git')
        workspace_git.git(root, 'remote', 'add', 'backup', str(remote))
        workspace_git.git(root, 'push', 'backup', 'HEAD:refs/heads/main')
        remote_refs = workspace_git.git(remote, 'show-ref')
        doomed_agent = self.start(project['project_id'], 'Doomed work')
        kept_agent = self.start(keep['project_id'], 'Kept work')
        doomed_task = self.task_for(doomed_agent)
        kept_task = self.task_for(kept_agent)
        doomed_workspace = Path(manager._store.get_task_workspace(doomed_task['task_id'])['workspace_path'])
        kept_workspace = Path(manager._store.get_task_workspace(kept_task['task_id'])['workspace_path'])
        self.stop(project['project_id'])
        self.stop(keep['project_id'])

        preview = self.call('GET', f"/projects/{project['project_id']}/lifecycle")
        self.assertTrue(preview['git']['available'])
        self.assertEqual({r['name']: r['url'] for r in preview['git']['remotes']}['origin'], 'https://example.invalid/doomed.git')
        self.assertEqual(preview['worktrees'], 1)

        self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Doomed'}, 409)  # Not in the Trash yet.
        trashed = self.call('POST', f"/projects/{project['project_id']}/trash", {})
        folder = Path(trashed['trash']['path'])
        mismatch = self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'doomed'}, 409)['detail']
        self.assertEqual(mismatch['code'], 'confirmation_mismatch')
        self.assertTrue(folder.is_dir())

        result = self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Doomed'})
        self.assertTrue(result['deleted'])
        self.assertFalse(folder.exists())
        self.assertFalse(doomed_workspace.exists())
        self.assertFalse(any(self.trash.iterdir()))
        self.call('GET', f"/projects/{project['project_id']}", expected=404)
        self.assertNotIn(doomed_agent, manager.agents)
        with manager._store._connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM tasks WHERE task_id=?', (doomed_task['task_id'],)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM agents WHERE agent_id=?', (doomed_agent,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM output WHERE agent_id=?', (doomed_agent,)).fetchone())
            self.assertIsNone(db.execute('SELECT 1 FROM executions WHERE agent_id=?', (doomed_agent,)).fetchone())
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertIsNotNone(db.execute('SELECT 1 FROM agents WHERE agent_id=?', (kept_agent,)).fetchone())
        # The other project, its worktree, its agent and the remote are untouched.
        self.assertTrue((keep_root / 'notes.txt').is_file())
        self.assertTrue(kept_workspace.is_dir())
        self.assertIn(kept_agent, manager.agents)
        self.assertEqual(self.call('GET', f"/projects/{keep['project_id']}")['name'], 'Kept')
        self.assertEqual(workspace_git.git(remote, 'show-ref'), remote_refs)

    def test_deletion_removes_junctions_without_following_them(self):
        project, root = self.project()
        precious = Path(self.temp.name) / 'precious'
        (precious / 'inner').mkdir(parents=True)
        (precious / 'inner' / 'keep.txt').write_text('outside the project', encoding='utf-8')
        junction(root / 'linked', precious)
        readonly = root / 'readonly.txt'
        readonly.write_text('read only', encoding='utf-8')
        os.chmod(readonly, 0o444)
        trashed = self.call('POST', f"/projects/{project['project_id']}/trash", {})
        self.assertTrue((Path(trashed['trash']['path']) / 'linked' / 'inner' / 'keep.txt').is_file())
        self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Shop'})
        self.assertFalse(Path(trashed['trash']['path']).exists())
        self.assertEqual((precious / 'inner' / 'keep.txt').read_text(encoding='utf-8'), 'outside the project')

    def test_remove_tree_refuses_targets_outside_its_container_and_links(self):
        container = Path(self.temp.name) / 'container'
        (container / 'inside').mkdir(parents=True)
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        with self.assertRaises(project_lifecycle.LifecycleRefused):
            project_lifecycle.remove_tree(outside, container)
        with self.assertRaises(project_lifecycle.LifecycleRefused):
            project_lifecycle.remove_tree(container, container)
        junction(container / 'link', outside)
        with self.assertRaises(project_lifecycle.LifecycleRefused):
            project_lifecycle.remove_tree(container / 'link', container)
        self.assertTrue(outside.is_dir())
        self.assertEqual(project_lifecycle.remove_tree(container / 'inside', container), [])
        self.assertFalse((container / 'inside').exists())

    def test_a_failed_deletion_keeps_the_records_and_can_be_retried(self):
        project, _ = self.project()
        trashed = self.call('POST', f"/projects/{project['project_id']}/trash", {})
        folder = Path(trashed['trash']['path'])
        failure = [{'path': str(folder / 'locked.bin'), 'error': 'in use'}]
        with patch.object(project_lifecycle, 'remove_tree', return_value=failure):
            refused = self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Shop'}, 409)['detail']
        self.assertEqual(refused['code'], 'delete_incomplete')
        still = self.call('GET', f"/projects/{project['project_id']}")
        self.assertEqual(still['trash']['state'], 'deleting')
        self.assertIn('could not be deleted', still['trash']['error'])
        self.assertEqual(self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Shop'})['deleted'], True)
        self.assertFalse(folder.exists())

    def test_a_folder_already_deleted_outside_the_app_still_deletes_its_records(self):
        project, _ = self.project()
        trashed = self.call('POST', f"/projects/{project['project_id']}/trash", {})
        project_lifecycle.remove_tree(trashed['trash']['path'], self.trash)
        missing = self.call('POST', f"/projects/{project['project_id']}/restore", {}, 409)['detail']
        self.assertEqual(missing['code'], 'trash_folder_missing')
        self.assertTrue(self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Shop'})['deleted'])

    def delete_folder(self, root):
        shutil.rmtree(root, onerror=lambda func, path, _: (os.chmod(path, 0o700), func(path)))

    def test_a_project_whose_folder_was_deleted_by_hand_can_be_removed_and_keeps_its_history(self):
        project, root = self.project()
        agent = self.start(project['project_id'])
        self.stop(project['project_id'])
        self.delete_folder(root)
        others = set(Path(self.temp.name, 'projects').iterdir())
        refused = self.call('POST', f"/projects/{project['project_id']}/trash", {}, 409)['detail']
        self.assertEqual((refused['code'], refused['can_remove'], refused['path']),
                         ('folder_missing', True, canonical_path(root)[0]))
        self.assertIn('remove the project from the Control Center', refused['reasons'][0])
        self.assertIn('move it back', refused['reasons'][1])
        removed = self.call('POST', f"/projects/{project['project_id']}/trash", {'folder_missing': True})
        self.assertTrue(removed['trashed'])
        self.assertEqual((removed['trash']['folder_missing'], removed['trash']['path'], removed['trash']['original_root_path']),
                         (True, None, canonical_path(root)[0]))
        # Nothing on disk was created, moved or deleted; the history stays with the project in the Trash.
        self.assertFalse(root.exists())
        self.assertEqual(set(Path(self.temp.name, 'projects').iterdir()), others)
        self.assertFalse(self.trash.exists() and any(self.trash.rglob('*')))
        self.assertEqual([p['project_id'] for p in self.call('GET', '/projects/trash')['projects']], [project['project_id']])
        self.assertTrue(self.task_for(agent))
        # The original path is free again: the folder can be added as a new project.
        make_repository(root)
        added = self.call('POST', '/projects', {'name': 'Shop again', 'root_path': str(root)})
        self.assertNotEqual(added['project_id'], project['project_id'])

    def test_removing_requires_the_folder_to_be_really_missing(self):
        project, root = self.project()
        refused = self.call('POST', f"/projects/{project['project_id']}/trash", {'folder_missing': True}, 409)['detail']
        self.assertEqual(refused['code'], 'folder_present')
        self.assertTrue((root / 'notes.txt').is_file())
        self.assertFalse(self.call('GET', f"/projects/{project['project_id']}")['trashed'])
        self.call('POST', f"/projects/{project['project_id']}/trash", {'folder_missing': 'yes'}, 422)

    def test_a_removed_project_restores_once_its_folder_is_back_and_can_be_deleted(self):
        project, root = self.project()
        self.delete_folder(root)
        self.call('POST', f"/projects/{project['project_id']}/trash", {'folder_missing': True})
        waiting = self.call('POST', f"/projects/{project['project_id']}/restore", {}, 409)['detail']
        self.assertEqual(waiting['code'], 'restore_folder_missing')
        make_repository(root)
        restored = self.call('POST', f"/projects/{project['project_id']}/restore", {})
        self.assertEqual((restored['trashed'], restored['root_path']), (False, canonical_path(root)[0]))
        self.delete_folder(root)
        self.call('POST', f"/projects/{project['project_id']}/trash", {'folder_missing': True})
        deleted = self.call('DELETE', f"/projects/{project['project_id']}", {'confirm_name': 'Shop'})
        self.assertTrue(deleted['deleted'])
        self.assertIsNone(deleted['folder'])
        self.call('GET', f"/projects/{project['project_id']}", expected=404)

    # ------------------------------------------------------------ safety and recovery

    def test_protected_locations(self):
        root = Path(self.temp.name) / 'folder'
        (root / 'app').mkdir(parents=True)
        reasons = project_lifecycle.protected_reasons(root, protected=(('the Control Center application', root / 'app'),))
        self.assertEqual(reasons, ['This project folder contains the Control Center application.'])
        drive = Path(Path(self.temp.name).anchor)
        self.assertIn('This project is the root of a drive.', project_lifecycle.protected_reasons(drive))
        profile = os.environ.get('USERPROFILE')
        if profile:
            self.assertTrue(project_lifecycle.protected_reasons(profile))
        # The application's own source folder can never be trashed.
        own = Path(project_lifecycle.__file__).resolve().parents[2]
        self.assertTrue(project_lifecycle.protected_reasons(own, protected=project_lifecycle.application_protections(
            manager._store, Path(self.temp.name) / 'workspaces', self.trash)))

    def test_interrupted_moves_are_settled_at_startup(self):
        project, root = self.project('Moved')
        other, other_root = self.project('Unmoved')
        # A move that completed on disk but not in the database.
        destination_container = self.trash / 'interrupted'
        destination_container.mkdir(parents=True)
        destination = destination_container / 'Moved'
        os.rename(root, destination)
        with manager._store._connection() as db:
            db.execute('''UPDATE projects SET trash_state='moving', original_root_path=?, trash_path=?, trash_container=?,
                trash_kind='app' WHERE project_id=?''', (str(root), str(destination), str(destination_container), project['project_id']))
            # A move that never reached the disk.
            db.execute('''UPDATE projects SET trash_state='moving', original_root_path=?, trash_path=?, trash_container=?,
                trash_kind='app' WHERE project_id=?''', (str(other_root), str(self.trash / 'never' / 'Unmoved'),
                                                        str(self.trash / 'never'), other['project_id']))
        settled = dict(project_lifecycle.recover(manager._store))
        self.assertEqual(settled[project['project_id']], 'trashed')
        self.assertEqual(settled[other['project_id']], 'active')
        self.assertEqual(self.call('GET', f"/projects/{project['project_id']}")['trash']['state'], 'trashed')
        self.assertFalse(self.call('GET', f"/projects/{other['project_id']}")['trashed'])
        self.assertTrue((other_root / 'notes.txt').is_file())

    def test_git_summary_sanitizes_remote_credentials(self):
        self.assertEqual(project_lifecycle.sanitize_remote('https://user:token@github.com/a/b.git'), 'https://github.com/a/b.git')
        self.assertEqual(project_lifecycle.sanitize_remote('git@github.com:a/b.git'), 'git@github.com:a/b.git')


class SchemaUpgradeTests(unittest.TestCase):
    def test_v38_database_upgrades_with_a_backup_and_keeps_registration_order(self):
        import tempfile
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'old.sqlite3'
        AgentStore(path)  # A current database, then turned back into its v38 shape.
        db = sqlite3.connect(path)
        for column in ('sort_order', 'hidden_at', 'trash_state', 'trashed_at', 'original_root_path', 'trash_path',
                       'trash_container', 'trash_kind', 'restore_target', 'lifecycle_error'):
            db.execute(f'ALTER TABLE projects DROP COLUMN {column}')
        for index, name in enumerate(('First', 'Second', 'Third')):
            db.execute('INSERT INTO projects(project_id,name,root_path,root_path_key) VALUES (?,?,?,?)',
                       (f'p{index}', name, f'C:\\fixture\\{name}', f'c:\\fixture\\{name.lower()}'))
        db.execute('PRAGMA user_version = 38')
        db.commit()
        db.close()
        store = AgentStore(path)
        backup = path.with_name('old.sqlite3.schema-v38.bak')
        self.assertTrue(backup.is_file())
        old = sqlite3.connect(backup)
        self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], 38)
        self.assertEqual(old.execute('SELECT COUNT(*) FROM projects').fetchone()[0], 3)
        old.close()
        with store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual([row[0] for row in db.execute('SELECT name FROM projects ORDER BY sort_order')],
                             ['First', 'Second', 'Third'])
        # A second opening neither migrates nor backs up again.
        backup.unlink()
        AgentStore(path)
        self.assertFalse(backup.exists())

    def test_a_newer_database_is_refused_unchanged(self):
        import tempfile
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / 'new.sqlite3'
        AgentStore(path)
        db = sqlite3.connect(path)
        db.execute(f'PRAGMA user_version = {SCHEMA_VERSION + 1}')
        db.commit()
        db.close()
        with self.assertRaisesRegex(RuntimeError, 'Unsupported control-center database schema version'):
            AgentStore(path)


if __name__ == '__main__':
    unittest.main()
