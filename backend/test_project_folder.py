"""Folder checks and the opt-in Git setup for new Projects. Temporary folders only."""
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import project_folder, workspace_git
from app.project_domain import canonical_path
from app.workspace_git import RepositoryRefused
from workspace_test_support import make_repository
import test_task_lifecycle


def isolated_git_home(test, identity=True):
    """Point Git's global configuration at a temporary home so tests never read or change the real one."""
    home = tempfile.TemporaryDirectory()
    test.addCleanup(home.cleanup)
    if identity:
        Path(home.name, '.gitconfig').write_text('[user]\n\tname = Test Person\n\temail = test@localhost\n'
                                                 '[commit]\n\tgpgSign = false\n', encoding='utf-8')
    values = {'HOME': home.name, 'USERPROFILE': home.name, 'XDG_CONFIG_HOME': str(Path(home.name, '.config'))}
    patcher = patch.dict(os.environ, values)
    patcher.start()
    test.addCleanup(patcher.stop)


def system_identity():
    result = subprocess.run(['git', 'config', '--system', '--get', 'user.name'], capture_output=True)
    return result.returncode == 0


def head_files(root):
    return sorted(workspace_git.git(root, 'ls-tree', '-r', '--name-only', 'HEAD').decode().split())


def stop_git_discovery_at(test, base):
    """Temporary folders may sit inside a repository (some machines have one in the user folder).
    A Git ceiling keeps each test folder's answer about the folder itself."""
    original = workspace_git._environment
    patcher = patch.object(workspace_git, '_environment',
                           lambda extra=None: {**original(extra), 'GIT_CEILING_DIRECTORIES': str(base)})
    patcher.start()
    test.addCleanup(patcher.stop)


class FolderCheckTests(unittest.TestCase):
    def setUp(self):
        isolated_git_home(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(canonical_path(temp.name)[0])
        stop_git_discovery_at(self, self.base)

    def folder(self, name, files=None):
        root = self.base / name
        root.mkdir(parents=True)
        for relative, content in (files or {}).items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return root

    def refused(self, root):
        with self.assertRaises(RepositoryRefused) as caught:
            workspace_git.repository(root)
        return caught.exception

    def test_folder_without_git_is_explained_and_check_writes_nothing(self):
        root = self.folder('plain', {'main.py': 'print(1)\n', 'docs/readme.md': 'hi\n'})
        error = self.refused(root)
        self.assertEqual(error.code, 'not_git_repo')
        self.assertIn('isn’t a Git repository', str(error))
        result = project_folder.check(root)
        self.assertEqual((result['state'], result['code']), ('not_git_repo', 'not_git_repo'))
        plan = result['setup']
        self.assertEqual((plan['action'], plan['file_count'], plan['empty']), ('init_and_commit', 2, False))
        self.assertEqual(plan['total_bytes'], len('print(1)\n') + len('hi\n'))
        self.assertEqual(plan['identity'], {'name': 'Test Person', 'email': 'test@localhost'})
        self.assertIsNone(plan['blocked'])
        self.assertFalse(plan['identity_placeholder'])
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['docs', 'main.py'])  # no .git was created

    def test_placeholder_identity_is_flagged_not_changed(self):
        identity = '[user]\n\tname = Your Name\n\temail = you@example.com\n'
        Path(os.environ['HOME'], '.gitconfig').write_text(identity, encoding='utf-8')
        plan = project_folder.check(self.folder('placeholder', {'a.txt': 'a'}))['setup']
        self.assertTrue(plan['identity_placeholder'])
        self.assertIsNone(plan['blocked'])

    def test_repository_without_commits_is_explained(self):
        root = self.folder('unborn', {'a.txt': 'a'})
        workspace_git.git(root, 'init', '-q')
        error = self.refused(root)
        self.assertEqual(error.code, 'no_commits')
        plan = project_folder.check(root)['setup']
        self.assertEqual((plan['action'], plan['file_count']), ('commit', 1))

    def test_subfolder_names_the_repository_top_folder(self):
        root = make_repository(self.base / 'repo')
        nested = root / 'backend'
        nested.mkdir()
        error = self.refused(nested)
        self.assertEqual(error.code, 'not_repo_root')
        self.assertEqual(error.details['top_level'], canonical_path(root)[0])
        self.assertIn(canonical_path(root)[0], str(error))
        self.assertFalse(error.details['accidental_parent'])
        self.assertNotIn('setup', project_folder.check(nested))

    def test_repository_covering_the_user_folder_offers_a_repository_of_its_own(self):
        top = make_repository(self.base / 'pretend-home')
        nested = top / 'project'
        nested.mkdir()
        (nested / 'a.txt').write_text('a', encoding='utf-8')
        broad = lambda path: canonical_path(path)[1] == canonical_path(top)[1]
        with patch.object(workspace_git, 'broad_folder', broad):
            error = self.refused(nested)
            self.assertTrue(error.details['accidental_parent'])
            self.assertIn('Don’t add that folder', str(error))
            plan = project_folder.check(nested)['setup']
            self.assertEqual((plan['action'], plan['file_count']), ('init_and_commit', 1))
            self.assertEqual(project_folder.setup(nested, 'not_repo_root')['state'], 'ready')
        self.assertEqual(head_files(nested), ['a.txt'])
        self.assertEqual(head_files(top), ['source.txt'])  # the covering repository is left as it was

    def test_ready_repository_passes(self):
        root = make_repository(self.base / 'ready')
        self.assertTrue(workspace_git.repository(root))
        self.assertEqual(project_folder.check(root)['state'], 'ready')

    def test_dubious_ownership_shows_the_command_and_changes_nothing(self):
        root = self.folder('foreign')
        calls = []

        def fake_probe(where, *args):
            calls.append(args)
            return 128, b'', "fatal: detected dubious ownership in repository at '" + str(root) + "'\n"
        with patch.object(workspace_git, 'probe', fake_probe):
            error = self.refused(root)
        self.assertEqual(error.code, 'dubious_ownership')
        expected = 'git config --global --add safe.directory "' + Path(canonical_path(root)[0]).as_posix() + '"'
        self.assertEqual(error.details['command'], expected)
        self.assertIn(expected, str(error))
        self.assertEqual(error.details['repository_path'], canonical_path(root)[0])
        self.assertEqual(calls, [('rev-parse', '--show-toplevel')])  # only the read; never `git config`

    def test_missing_git_is_explained(self):
        root = self.folder('anything')
        with patch.object(workspace_git, 'Popen', side_effect=FileNotFoundError):
            error = self.refused(root)
        self.assertEqual(error.code, 'git_missing')
        self.assertIn('Install Git', str(error))

    def test_existing_callers_still_receive_a_value_error_message(self):
        root = self.folder('plain')
        with self.assertRaises(ValueError) as caught:
            workspace_git.repository(root)
        self.assertTrue(str(caught.exception).startswith('This folder isn’t a Git repository'))


class FolderSetupTests(unittest.TestCase):
    setUp = FolderCheckTests.setUp
    folder = FolderCheckTests.folder

    def test_initializes_git_and_commits_the_current_files(self):
        root = self.folder('plain', {'main.py': 'print(1)\n', 'src/app.js': 'x\n'})
        result = project_folder.setup(root, 'not_git_repo')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['commit'], workspace_git.git(root, 'rev-parse', 'HEAD').decode().strip())
        self.assertEqual(head_files(root), ['main.py', 'src/app.js'])
        message = workspace_git.git(root, 'log', '-1', '--format=%s|%an').decode().strip()
        self.assertEqual(message, 'Initial commit|Test Person')
        self.assertFalse((root / '.gitignore').exists())
        workspace_git.repository(root)

    def test_empty_folder_gets_an_empty_first_commit(self):
        root = self.folder('empty')
        plan = project_folder.check(root)['setup']
        self.assertEqual((plan['empty'], plan['file_count'], plan['total_bytes']), (True, 0, 0))
        result = project_folder.setup(root, 'not_git_repo')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(head_files(root), [])
        self.assertEqual(sorted(p.name for p in root.iterdir()), ['.git'])

    def test_first_commit_in_an_existing_repository_without_commits(self):
        root = self.folder('unborn', {'a.txt': 'a', 'b.txt': 'b'})
        workspace_git.git(root, 'init', '-q')
        result = project_folder.setup(root, 'no_commits')
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(head_files(root), ['a.txt', 'b.txt'])

    def test_bulky_folders_are_flagged_and_gitignore_is_only_written_on_request(self):
        files = {'main.py': 'x', 'node_modules/pkg/index.js': 'y', 'node_modules/pkg/other.js': 'z',
                 'app/__pycache__/m.pyc': 'c'}
        plan = project_folder.check(self.folder('bulky', files))['setup']
        self.assertEqual(plan['bulky_directories'], ['__pycache__', 'node_modules'])
        self.assertEqual(plan['file_count'], 4)
        self.assertEqual(plan['suggested_gitignore']['content'], 'node_modules/\n__pycache__/\n')
        self.assertEqual(plan['suggested_gitignore']['file_count'], 1)

        kept = project_folder.setup(self.base / 'bulky', 'not_git_repo')
        self.assertEqual(kept['state'], 'ready')
        self.assertFalse((self.base / 'bulky' / '.gitignore').exists())
        self.assertEqual(len(head_files(self.base / 'bulky')), 4)

        root = self.folder('bulky-ignored', files)
        project_folder.setup(root, 'not_git_repo', write_gitignore=True)
        self.assertEqual((root / '.gitignore').read_text(encoding='utf-8'), 'node_modules/\n__pycache__/\n')
        self.assertEqual(head_files(root), ['.gitignore', 'main.py'])

    def test_existing_gitignore_is_respected_in_the_count(self):
        root = self.folder('ignored', {'.gitignore': 'node_modules/\n', 'main.py': 'x', 'node_modules/a.js': 'y'})
        plan = project_folder.check(root)['setup']
        self.assertEqual((plan['file_count'], plan['has_gitignore'], plan['bulky_directories']), (2, True, []))
        self.assertIsNone(plan['suggested_gitignore'])
        with self.assertRaises(project_folder.SetupRefused) as caught:
            project_folder.setup(root, 'not_git_repo', write_gitignore=True)
        self.assertEqual(caught.exception.code, 'gitignore_unavailable')
        self.assertFalse((root / '.git').exists())

    def test_large_files_are_listed(self):
        root = self.folder('large', {'small.txt': 'a', 'video.bin': b'0' * 200})
        with patch.object(project_folder, 'LARGE_FILE_BYTES', 100):
            plan = project_folder.check(root)['setup']
        self.assertEqual(plan['large_files'], [{'path': 'video.bin', 'bytes': 200}])
        self.assertEqual(plan['large_file_count'], 1)

    @unittest.skipIf(system_identity(), 'system Git configuration sets an identity')
    def test_missing_identity_refuses_without_writing_or_configuring(self):
        isolated_git_home(self, identity=False)
        root = self.folder('anonymous', {'a.txt': 'a'})
        plan = project_folder.check(root)['setup']
        self.assertIsNone(plan['identity'])
        self.assertEqual(plan['blocked']['code'], 'git_identity_missing')
        with self.assertRaises(project_folder.SetupRefused) as caught:
            project_folder.setup(root, 'not_git_repo')
        self.assertEqual(caught.exception.code, 'git_identity_missing')
        self.assertIn('git config --global user.name', str(caught.exception))
        self.assertFalse((root / '.git').exists())
        self.assertFalse(Path(os.environ['HOME'], '.gitconfig').exists())

    def test_stale_or_unneeded_setup_is_refused(self):
        root = self.folder('plain', {'a.txt': 'a'})
        with self.assertRaises(project_folder.SetupRefused) as caught:
            project_folder.setup(root, 'no_commits')
        self.assertEqual(caught.exception.code, 'state_changed')
        self.assertFalse((root / '.git').exists())
        ready = make_repository(self.base / 'ready')
        before = workspace_git.git(ready, 'rev-parse', 'HEAD')
        with self.assertRaises(project_folder.SetupRefused) as caught:
            project_folder.setup(ready, 'no_commits')
        self.assertEqual(caught.exception.code, 'not_applicable')
        self.assertEqual(workspace_git.git(ready, 'rev-parse', 'HEAD'), before)
        nested = ready / 'sub'
        nested.mkdir()
        with self.assertRaises(project_folder.SetupRefused) as caught:
            project_folder.setup(nested, 'not_git_repo')
        self.assertEqual((caught.exception.code, caught.exception.details['state']), ('not_applicable', 'not_repo_root'))

    def test_drive_root_or_home_is_never_set_up(self):
        with patch.object(workspace_git, 'broad_folder', return_value=True):
            root = self.folder('pretend-home', {'a.txt': 'a'})
            self.assertEqual(project_folder.check(root)['setup']['blocked']['code'], 'folder_too_broad')
            with self.assertRaises(project_folder.SetupRefused):
                project_folder.setup(root, 'not_git_repo')
        self.assertFalse((root / '.git').exists())


class FolderApiTests(unittest.TestCase):
    setUp = test_task_lifecycle.TaskLifecycleTests.setUp
    tearDown = test_task_lifecycle.TaskLifecycleTests.tearDown
    request = test_task_lifecycle.TaskLifecycleTests.request

    def test_add_project_reports_a_reason_then_setup_on_request_lets_it_register(self):
        isolated_git_home(self)
        stop_git_discovery_at(self, canonical_path(self.temp.name)[0])
        root = Path(self.temp.name) / 'new-project'
        root.mkdir()
        (root / 'notes.txt').write_text('n', encoding='utf-8')
        error = self.request('/projects', {'name': 'New', 'root_path': str(root)}, 409)
        self.assertEqual(error['detail']['code'], 'not_git_repo')
        self.assertIn('message', error['detail'])
        self.assertFalse((root / '.git').exists())

        found = self.request('/projects/folder-check', {'root_path': str(root)})
        self.assertEqual((found['state'], found['setup']['file_count']), ('not_git_repo', 1))
        self.assertFalse((root / '.git').exists())
        self.request('/projects/folder-setup', {'root_path': str(root), 'expected_state': 'something'}, 422)
        done = self.request('/projects/folder-setup', {'root_path': str(root), 'expected_state': 'not_git_repo'})
        self.assertEqual(done['state'], 'ready')
        again = self.request('/projects/folder-setup', {'root_path': str(root), 'expected_state': 'not_git_repo'}, 409)
        self.assertEqual(again['detail']['code'], 'not_applicable')
        project = self.request('/projects', {'name': 'New', 'root_path': str(root)})
        self.assertEqual(project['root_path'], canonical_path(root)[0])

    def test_a_deleted_and_recreated_folder_points_back_to_its_existing_project(self):
        isolated_git_home(self)
        stop_git_discovery_at(self, canonical_path(self.temp.name)[0])
        root = make_repository(Path(self.temp.name) / 'FrameSeek')
        project = self.request('/projects', {'name': 'FrameSeek', 'root_path': str(root)})
        self.request('/tasks', {'title': 'Earlier work', 'description': 'Earlier work', 'project_id': project['project_id']})
        shutil.rmtree(root, onerror=lambda func, path, _: (os.chmod(path, 0o700), func(path)))
        root.mkdir()  # The person recreated an empty folder at the same path.
        refused = self.request('/projects', {'name': 'FrameSeek', 'root_path': str(root)}, 409)
        self.assertEqual(refused['detail']['code'], 'not_git_repo')
        self.request('/projects/folder-setup', {'root_path': str(root), 'expected_state': 'not_git_repo'})
        again = self.request('/projects', {'name': 'FrameSeek again', 'root_path': str(root)}, 409)['detail']
        self.assertEqual((again['code'], again['project_id'], again['name'], again['task_count']),
                         ('already_registered', project['project_id'], 'FrameSeek', 1))
        self.assertIn('old Git history', again['message'])
        # Nothing was removed or duplicated: the one project record remains and is usable as it is.
        listed = self.request('/projects')['projects']
        self.assertEqual([p['project_id'] for p in listed], [project['project_id']])
        self.assertEqual(self.request('/projects/' + project['project_id'])['root_path'], canonical_path(root)[0])

    def test_an_overlapping_folder_names_the_project_it_overlaps(self):
        root = make_repository(Path(self.temp.name) / 'outer')
        project = self.request('/projects', {'name': 'Outer', 'root_path': str(root)})
        inner = make_repository(root / 'inner')
        detail = self.request('/projects', {'name': 'Inner', 'root_path': str(inner)}, 409)['detail']
        self.assertEqual((detail['code'], detail['project_id'], detail['name']), ('overlaps_project', project['project_id'], 'Outer'))
        self.assertIn(canonical_path(root)[0], detail['message'])

    def test_subfolder_refusal_carries_the_top_folder(self):
        root = make_repository(Path(self.temp.name) / 'repo')
        nested = root / 'web'
        nested.mkdir()
        error = self.request('/projects', {'name': 'Web', 'root_path': str(nested)}, 409)
        self.assertEqual((error['detail']['code'], error['detail']['top_level']), ('not_repo_root', canonical_path(root)[0]))
        missing = self.request('/projects/folder-check', {'root_path': str(root / 'missing')}, 409)
        self.assertEqual(missing['detail'], 'Project root must be a valid existing directory')


if __name__ == '__main__':
    unittest.main()
