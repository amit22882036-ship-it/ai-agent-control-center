"""Checking a folder before it becomes a Project, and the one opt-in setup Control Center offers.

Checking never writes to the folder. When Git is missing from a folder, or a repository has no
commits, `setup` can initialize Git and make the first commit, but only when the person asked for it
after seeing `check`'s plan. It never changes Git configuration, never adds a .gitignore unless asked,
and never touches a folder that already has commits.
"""
import os
from pathlib import Path
import tempfile

from . import workspace_git
from .project_domain import canonical_path
from .workspace_git import RepositoryRefused

SETUP_STATES = ('not_git_repo', 'no_commits', 'not_repo_root')


def _offers_setup(refusal):
    # Inside an ordinary repository the answer is its top folder; only a repository that accidentally
    # covers a whole drive or user folder is worked around, by giving this folder its own repository.
    return refusal.code in ('not_git_repo', 'no_commits') or (
        refusal.code == 'not_repo_root' and refusal.details.get('accidental_parent'))
# Folders that usually hold installed dependencies or build output rather than source.
BULKY_DIRECTORIES = ('node_modules', '.venv', 'venv', '__pycache__', 'dist', 'build')
LARGE_FILE_BYTES = 50 * 1024 * 1024
COMMIT_MESSAGE = 'Initial commit'


class SetupRefused(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def payload(self):
        return {'code': self.code, 'message': self.message, **self.details}


def _paths(root, state, extra_excludes=None):
    """The files a first `git add -A` would include, using the folder's own ignore rules.

    A folder without Git is listed through a throwaway repository outside it, so nothing is written there.
    """
    excludes = ['--exclude-from=' + str(extra_excludes)] if extra_excludes else []
    if state == 'no_commits':
        listed = workspace_git.git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', *excludes)
    else:
        with tempfile.TemporaryDirectory(prefix='control-center-folder-check-') as temp:
            scratch = Path(temp) / 'scratch.git'
            workspace_git.git(temp, 'init', '--bare', '-q', str(scratch))
            listed = workspace_git.git(root, '--git-dir=' + str(scratch), '--work-tree=' + str(root),
                                       'ls-files', '-z', '--others', '--exclude-standard', *excludes)
    return sorted({os.fsdecode(item) for item in listed.split(b'\0') if item})


def _summary(root, paths):
    total, large, bulky = 0, [], set()
    for name in paths:
        parts = Path(name).parts
        bulky.update(part for part in parts[:-1] if part in BULKY_DIRECTORIES)
        try:
            size = os.lstat(Path(root) / name).st_size
        except OSError:
            continue
        total += size
        if size > LARGE_FILE_BYTES:
            large.append({'path': name.replace('\\', '/'), 'bytes': size})
    large.sort(key=lambda item: -item['bytes'])
    return {'file_count': len(paths), 'total_bytes': total, 'large_files': large[:10],
            'large_file_count': len(large), 'bulky_directories': sorted(bulky)}


def _identity(root):
    values = {}
    for key in ('user.name', 'user.email'):
        status, output, _ = workspace_git.probe(root, 'config', '--get', key)
        values[key.split('.')[1]] = output.decode('utf-8', errors='replace').strip() if status == 0 else ''
    return values


def _suggested_gitignore(bulky):
    return ''.join(name + '/\n' for name in BULKY_DIRECTORIES if name in bulky)


def _plan(root, state):
    paths = _paths(root, state)
    plan = {'action': 'commit' if state == 'no_commits' else 'init_and_commit',
            'commit_message': COMMIT_MESSAGE, 'empty': not paths,
            'has_gitignore': (Path(root) / '.gitignore').exists(), **_summary(root, paths)}
    identity = _identity(root)
    plan['identity'] = identity if identity['name'] and identity['email'] else None
    # Git's documentation placeholders: configured, but probably not who the person wants to commit as.
    plan['identity_placeholder'] = bool(plan['identity']) and (
        identity['email'].lower().endswith('@example.com') or identity['name'].strip().lower() == 'your name')
    plan['suggested_gitignore'] = None
    if not plan['has_gitignore'] and plan['bulky_directories']:
        suggested = _suggested_gitignore(plan['bulky_directories'])
        with tempfile.TemporaryDirectory(prefix='control-center-folder-check-') as temp:
            rules = Path(temp) / 'suggested-ignore'
            rules.write_text(suggested, encoding='utf-8')
            reduced = _summary(root, _paths(root, state, rules))
        plan['suggested_gitignore'] = {'content': suggested, 'file_count': reduced['file_count'],
                                       'total_bytes': reduced['total_bytes']}
    plan['blocked'] = None
    if workspace_git.broad_folder(root):
        plan['blocked'] = {'code': 'folder_too_broad', 'message': 'This is a whole drive or your user folder. '
                           'Choose the folder of one project instead.'}
    elif plan['identity'] is None:
        plan['blocked'] = {'code': 'git_identity_missing', 'message': identity_message()}
    return plan


def identity_message():
    return ('Git doesn’t know who you are yet, so it can’t make a commit. Run these two commands in a '
            'terminal with your own name and email, then try again: git config --global user.name "Your Name" '
            'and git config --global user.email "you@example.com". Control Center doesn’t change Git settings '
            'for you.')


def check(root_path):
    """What is wrong with a folder as a Project root, and what setup would write. Read-only."""
    root = canonical_path(root_path, require_directory=True)[0]
    try:
        workspace_git.diagnose(root)
    except RepositoryRefused as exc:
        result = {'root_path': root, 'state': exc.code, **exc.payload()}
        if _offers_setup(exc):
            result['setup'] = _plan(root, exc.code)
        return result
    return {'root_path': root, 'state': 'ready', 'code': 'ready', 'message': 'This folder is ready to be a project.'}


def _run(root, *args):
    status, _, error = workspace_git.probe(root, *args)
    if status != 0:
        detail = next((line.strip() for line in error.splitlines() if line.strip()), 'no details')
        raise SetupRefused('git_failed', 'Git couldn’t finish setting up the folder (' + detail + '). '
                           'Check the folder in a terminal with “git status”.')


def setup(root_path, expected_state, write_gitignore=False):
    """Initialize Git and/or make the first commit, after the person confirmed `check`'s plan."""
    found = check(root_path)
    root = found['root_path']
    if 'setup' not in found:
        raise SetupRefused('not_applicable', 'Control Center can’t set up Git for this folder. ' + found['message'],
                           state=found['state'])
    if found['state'] != expected_state:
        raise SetupRefused('state_changed', 'The folder changed since it was checked. Review it again before '
                           'setting it up.', state=found['state'])
    plan = found['setup']
    if plan['blocked']:
        raise SetupRefused(plan['blocked']['code'], plan['blocked']['message'])
    ignore = Path(root) / '.gitignore'
    if write_gitignore and (plan['suggested_gitignore'] is None or ignore.exists()):
        raise SetupRefused('gitignore_unavailable', 'There is no suggested .gitignore for this folder. Review it again.')
    if plan['action'] == 'init_and_commit':
        _run(root, 'init', '-q')
    if write_gitignore:
        with ignore.open('x', encoding='utf-8', newline='\n') as handle:
            handle.write(plan['suggested_gitignore']['content'])
    _run(root, 'add', '-A')
    # --allow-empty: an empty folder still gets a first commit to start from.
    _run(root, 'commit', '-q', '--allow-empty', '-m', COMMIT_MESSAGE)
    after = check(root)
    commit = workspace_git.git(root, 'rev-parse', 'HEAD').decode().strip() if after['state'] == 'ready' else None
    return {'root_path': root, 'state': after['state'], 'commit': commit}
