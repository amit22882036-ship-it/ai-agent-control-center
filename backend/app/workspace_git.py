"""Git plumbing for source snapshots; never touches the user's index or refs."""
import os
from pathlib import Path
import stat
from subprocess import Popen, PIPE
import tempfile

from .project_domain import canonical_path


def git(root, *args, data=None, extra_env=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
    env.update(extra_env or {})
    try:
        with Popen(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *args],
                   stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, shell=False) as process:
            output, _ = process.communicate(data)
            if process.returncode:
                raise ValueError('Task Workspace Git operation failed; verify repository and workspace integrity')
            return output
    except OSError:
        raise ValueError('Task Workspaces require an available Git installation') from None


def repository(root):
    root, key = canonical_path(root, require_directory=True)
    try:
        top = git(root, 'rev-parse', '--show-toplevel').decode().strip()
        git(root, 'rev-parse', '--verify', 'HEAD^{commit}')
    except ValueError:
        raise ValueError('Task isolation requires a Git working tree with a usable HEAD') from None
    if canonical_path(top)[1] != key:
        raise ValueError('Project root must be the Git working-tree root')
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


def snapshot_worktree_source(root):
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
        identity = {'GIT_AUTHOR_NAME': 'Control Center', 'GIT_AUTHOR_EMAIL': 'control-center@localhost',
                    'GIT_COMMITTER_NAME': 'Control Center', 'GIT_COMMITTER_EMAIL': 'control-center@localhost'}
        return git(root, '-c', 'commit.gpgSign=false', 'commit-tree', tree,
                   data=b'Control Center Task source snapshot\n', extra_env=identity).decode().strip()


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
