"""Git plumbing for source snapshots; never touches the user's index or refs."""
import os
from pathlib import Path
import re
import stat
from subprocess import Popen, PIPE
import tempfile

from .project_domain import canonical_path


def _environment(extra_env=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
    env.update(extra_env or {})
    return env


def git(root, *args, data=None, extra_env=None, accepted=(0,), with_status=False):
    env = _environment(extra_env)
    try:
        with Popen(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *args],
                   stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, shell=False) as process:
            output, _ = process.communicate(data)
            if process.returncode not in accepted:
                raise ValueError('Task Workspace Git operation failed; verify repository and workspace integrity')
            return (process.returncode, output) if with_status else output
    except OSError:
        raise ValueError('Task Workspaces require an available Git installation') from None


class RepositoryRefused(ValueError):
    """A folder that cannot hold isolated Tasks, with a reason code the interface can act on.

    str(exc) stays the plain-language message, so every existing caller keeps working.
    """

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def payload(self):
        return {'code': self.code, 'message': self.message, **self.details}


GIT_MISSING = ('Git isn’t installed, or Control Center can’t find it. Install Git for Windows from '
               'git-scm.com, restart Control Center, and add the folder again.')


def probe(root, *args):
    """A Git call that keeps Git's own error text (in English) for diagnosis."""
    env = _environment({'LC_ALL': 'C', 'LANGUAGE': 'C'})
    try:
        with Popen(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *args],
                   stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, shell=False) as process:
            output, error = process.communicate()
            return process.returncode, output, error.decode('utf-8', errors='replace')
    except OSError:
        raise RepositoryRefused('git_missing', GIT_MISSING) from None


def safe_directory_command(root):
    return 'git config --global --add safe.directory "' + Path(root).as_posix() + '"'


def broad_folder(path):
    """A whole drive or the person's user folder: never a sensible single repository or Project."""
    path = Path(canonical_path(path)[0])
    home = os.path.normcase(canonical_path(os.path.expanduser('~'))[0])
    return path.parent == path or os.path.normcase(str(path)) == home


def diagnose(root):
    """Why a folder cannot be a Project root (a RepositoryRefused); returns (root, key) when it can."""
    root, key = canonical_path(root, require_directory=True)
    status, output, error = probe(root, 'rev-parse', '--show-toplevel')
    if status != 0:
        text = error.lower()
        if 'dubious ownership' in text:
            # Git names the repository it distrusts, which may be a parent of the chosen folder.
            quoted = re.search(r"repository at '([^']+)'", error)
            repository_path = canonical_path(quoted.group(1))[0] if quoted else root
            command = safe_directory_command(repository_path)
            raise RepositoryRefused(
                'dubious_ownership',
                'Git refuses to work here because the repository at ' + repository_path + ' belongs to a different '
                'Windows user than the one running Control Center (Git’s “dubious ownership” '
                'protection). If you trust it, run this command in a terminal, then add the folder again: '
                + command + ' — Control Center does not change Git settings for you.',
                command=command, repository_path=repository_path)
        if 'not a git repository' in text:
            raise RepositoryRefused(
                'not_git_repo',
                'This folder isn’t a Git repository yet. A project must be a Git repository with at least one '
                'commit, so agents can work in private copies of it. To fix it, initialize Git in the folder and '
                'make a first commit.')
        detail = next((line.strip() for line in error.splitlines() if line.strip()), '')
        raise RepositoryRefused(
            'git_unusable',
            'Git couldn’t use this folder as a repository' + (' (' + detail + ').' if detail else '.')
            + ' Check the folder in a terminal with “git status”.')
    top = canonical_path(output.decode('utf-8', errors='replace').strip())[0]
    if os.path.normcase(top) != key:
        if broad_folder(top):
            raise RepositoryRefused(
                'not_repo_root',
                'Git treats this folder as part of a repository at ' + top + ', which covers a whole drive or your '
                'user folder — usually created by accident. Don’t add that folder as a project. To fix '
                'it, give this folder its own Git repository with a first commit.',
                top_level=top, accidental_parent=True)
        raise RepositoryRefused(
            'not_repo_root',
            'This folder is inside a Git repository whose top folder is ' + top + '. A project must be the top '
            'folder of its repository, so add that folder instead.',
            top_level=top, accidental_parent=False)
    status, _, _ = probe(root, 'rev-parse', '--verify', '--quiet', 'HEAD^{commit}')
    if status != 0:
        raise RepositoryRefused(
            'no_commits',
            'This folder is a Git repository, but it has no commits yet. Agents start from the latest commit, so '
            'the repository needs at least one. To fix it, make a first commit.')
    return root, key


def repository(root):
    root, key = diagnose(root)
    common = git(root, 'rev-parse', '--path-format=absolute', '--git-common-dir').decode().strip()
    return canonical_path(common)[1]


def excluded(name):
    parts = Path(name).parts
    directories = {'.git', 'node_modules', '.venv', 'venv', 'dist', 'build', '__pycache__',
                   '.pytest_cache', '.mypy_cache', '.cache', 'secrets', '.aws', '.ssh'}
    return (any(part.lower() in directories for part in parts)
            or any(part.lower().startswith('.env') for part in parts)
            or Path(name).suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}
            or Path(name).name.lower() in {'credentials', 'credentials.json', 'secrets.json'})


def snapshot_source_tree(root):
    repository(root)
    # Start empty so excluded tracked secrets and ignored files cannot survive
    # from HEAD. Hash actual source bytes without invoking clean/smudge filters.
    paths = set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0')) - {b''}
    ignored = set(git(root, 'ls-files', '-z', '--cached', '--ignored', '--exclude-standard').split(b'\0'))
    modes = {}
    for entry in git(root, 'ls-files', '-sz').split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            modes[name] = meta.split()[0]
    with tempfile.TemporaryDirectory(prefix='control-center-index-') as temp:
        env = {'GIT_INDEX_FILE': str(Path(temp) / 'index')}
        git(root, 'read-tree', '--empty', extra_env=env)
        entries = []
        for raw in sorted(paths - ignored):
            name = os.fsdecode(raw)
            if excluded(name):
                continue
            path = Path(root) / name
            if not path.exists() and not path.is_symlink():
                continue  # deleted tracked file
            if modes.get(raw) == b'120000' or path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != Path(root)):
                raise ValueError('Task snapshots do not yet support symbolic links')
            if not path.is_file():
                raise ValueError('Task snapshots do not yet support submodules or special files')
            mode = b'100755' if os.name != 'nt' and path.stat().st_mode & stat.S_IXUSR else b'100644'
            if os.name == 'nt' and modes.get(raw) == b'100755':
                mode = b'100755'
            blob = git(root, 'hash-object', '-w', '--stdin', data=path.read_bytes()).strip()
            entries.append(mode + b' ' + blob + b'\t' + raw + b'\0')
        git(root, 'update-index', '-z', '--index-info', data=b''.join(entries), extra_env=env)
        tree = git(root, 'write-tree', extra_env=env).strip().decode()
        return tree


def snapshot_commit(root, tree):
    identity = {'GIT_AUTHOR_NAME': 'Control Center', 'GIT_AUTHOR_EMAIL': 'control-center@localhost',
                'GIT_COMMITTER_NAME': 'Control Center', 'GIT_COMMITTER_EMAIL': 'control-center@localhost'}
    return git(root, '-c', 'commit.gpgSign=false', 'commit-tree', tree,
               data=b'Control Center Task source snapshot\n', extra_env=identity).decode().strip()


def snapshot_worktree_source(root):
    return snapshot_commit(root, snapshot_source_tree(root))


def source_tree(root, snapshot):
    return git(root, 'rev-parse', '--verify', snapshot + '^{tree}').decode().strip()


def refresh_source(root, base, tree, revalidate):
    """Two-tree clean update, with Git's overwrite checks and raw-byte attributes.

    An isolated Git directory prevents repository filters/line conversions from
    executing. Only this Task's index and detached HEAD are updated. No force,
    reset, clean, merge of local edits, or upstream checkout is involved.
    """
    root = Path(root)
    head = git(root, 'rev-parse', 'HEAD').strip().decode()
    if git(root, 'rev-parse', '--abbrev-ref', 'HEAD').strip() != b'HEAD':
        raise ValueError('Task Workspace refresh requires a detached HEAD')
    index = Path(git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'index').decode().strip())
    lock = index.with_name('index.lock')
    snapshot = snapshot_commit(root, tree)
    # Exclusive index reservation also makes concurrent external Git operations
    # fail safely instead of losing staging changes during refresh.
    with lock.open('xb') as reserved:
        owns_lock = True
        try:
            with tempfile.TemporaryDirectory(prefix='control-center-refresh-') as temp:
                isolated = Path(temp) / 'git'
                git(root, 'init', '--bare', '--template=', str(isolated))
                (isolated / 'info').mkdir(exist_ok=True)
                (isolated / 'info' / 'attributes').write_text('* -text -filter -ident -working-tree-encoding\n')
                temporary_index = Path(temp) / 'index'
                temporary_index.write_bytes(index.read_bytes())
                objects = git(root, 'rev-parse', '--path-format=absolute', '--git-path', 'objects').decode().strip()
                env = {'GIT_DIR': str(isolated), 'GIT_WORK_TREE': str(root),
                       'GIT_INDEX_FILE': str(temporary_index), 'GIT_OBJECT_DIRECTORY': objects,
                       'GIT_ATTR_NOSYSTEM': '1'}
                old_tree = source_tree(root, base)
                if git(root, 'write-tree', extra_env=env).strip().decode() != old_tree:
                    raise ValueError('Task Workspace staging differs from its base; reconciliation is required')
                git(root, '-c', 'core.autocrlf=false', '-c', 'core.attributesFile=' + os.devnull,
                    'update-index', '--refresh', extra_env=env)
                revalidate()
                old_paths = set(git(root, 'ls-tree', '-rz', '--name-only', old_tree).split(b'\0'))
                new_paths = set(git(root, 'ls-tree', '-rz', '--name-only', tree).split(b'\0'))
                for raw in new_paths - old_paths:
                    target = root / os.fsdecode(raw)
                    # Git may otherwise overwrite an ignored untracked path.
                    # Exclusion from source snapshots is not permission to delete.
                    if raw and (target.exists() or target.is_symlink()):
                        raise ValueError('Task Workspace refresh would overwrite an existing local path; '
                                         'reconciliation is required')
                git(root, '-c', 'core.autocrlf=false', '-c', 'core.attributesFile=' + os.devnull,
                    'read-tree', '-m', '-u', old_tree, tree, extra_env=env)
                reserved.write(temporary_index.read_bytes())
            reserved.close()
            os.replace(lock, index)
            owns_lock = False
            # Never follow a symbolic HEAD into a user branch.
            if git(root, 'rev-parse', '--abbrev-ref', 'HEAD').strip() != b'HEAD':
                raise ValueError('Task Workspace HEAD changed during refresh')
            git(root, 'update-ref', '--no-deref', 'HEAD', snapshot, head)
            return snapshot
        finally:
            reserved.close()
            if owns_lock:
                lock.unlink(missing_ok=True)


def materialize(root, target, snapshot):
    git(root, 'worktree', 'add', '--detach', '--no-checkout', str(target), snapshot)
    git(target, 'read-tree', snapshot)
    # Writing blobs directly preserves captured bytes and avoids checkout hooks,
    # filters, inherited executable tooling, and line-ending conversions.
    for entry in git(target, 'ls-tree', '-rz', snapshot).split(b'\0'):
        if not entry:
            continue
        meta, raw = entry.split(b'\t', 1)
        mode, kind, oid = meta.split()
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('Unsupported Task snapshot entry')
        path = Path(target) / os.fsdecode(raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(git(target, 'cat-file', 'blob', oid.decode()))
        if os.name != 'nt' and mode == b'100755':
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
