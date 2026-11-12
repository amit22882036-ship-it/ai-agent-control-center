"""Project organization and lifecycle (schema v39).

Order, display name and visibility are presentation only: they never change a Project's identity,
its folder, its Tasks or its Agents. The Trash is real. Moving a Project to the Trash moves its
folder out of its original location, restoring moves it back, and deleting it permanently removes
the folder, the Task worktrees this application created for it, and its records.

Every rule below is enforced here, whatever an interface shows:
- Only a Project's own registered root moves or is deleted: never a parent folder, and never a
  folder that holds this application, its data, Task worktree storage or another Project.
- Nothing moves or is deleted while work could be writing to the Project.
- Deletion never follows links or junctions out of the folder being deleted.
- No remote repository is contacted: only local Git reads describe what deletion would lose.
"""
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import UUID, uuid4

from .project_domain import canonical_path, contains, project_name

NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
TRASH_STATES = ('moving', 'trashed', 'restoring', 'deleting')
OPEN_EXECUTIONS = ('launching', 'spawned', 'contained', 'running', 'uncertain')
ACTIVE_AGENT_STATUSES = ('running', 'waiting')
IN_FLIGHT_MATERIALIZATION = ('launching', 'recovery_required')
REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
BESIDE_TRASH = '.control-center-trash'
GIT_TIMEOUT = 15
# Windows refuses to move a folder that a program holds open (an editor, a terminal, an indexer).
IN_USE_ERRORS = (5, 32, 33)
CROSS_VOLUME_ERRORS = (17,)


class LifecycleRefused(ValueError):
    """A refused lifecycle request. `code` names the refusal; `reasons` are full sentences."""

    def __init__(self, code, message, reasons=(), **details):
        super().__init__(message)
        self.code = code
        self.reasons = list(reasons)
        self.details = details

    def payload(self):
        return {'code': self.code, 'message': str(self), 'reasons': self.reasons, **self.details}


# ---------------------------------------------------------------- schema

def migrate_v39(db):
    columns = {row['name'] for row in db.execute('PRAGMA table_info(projects)')}
    additions = (
        ('sort_order', 'INTEGER'),
        ('hidden_at', 'TEXT'),
        ('trash_state', "TEXT CHECK(trash_state IS NULL OR trash_state IN ('moving','trashed','restoring','deleting'))"),
        ('trashed_at', 'TEXT'),
        ('original_root_path', 'TEXT'),
        ('trash_path', 'TEXT'),
        ('trash_container', 'TEXT'),
        ('trash_kind', "TEXT CHECK(trash_kind IS NULL OR trash_kind IN ('app','beside'))"),
        ('restore_target', 'TEXT'),
        ('lifecycle_error', 'TEXT'),
    )
    for name, kind in additions:
        if name not in columns:
            db.execute(f'ALTER TABLE projects ADD COLUMN {name} {kind}')
    # Registration order becomes the initial order; nothing else about a Project changes.
    for index, row in enumerate(db.execute('SELECT project_id FROM projects ORDER BY rowid').fetchall()):
        db.execute('UPDATE projects SET sort_order=? WHERE project_id=? AND sort_order IS NULL', (index, row[0]))


def validate_schema(db):
    db.execute('''SELECT sort_order,hidden_at,trash_state,trashed_at,original_root_path,trash_path,
        trash_container,trash_kind,restore_target,lifecycle_error FROM projects LIMIT 0''')


def next_sort_order(db):
    return db.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 FROM projects').fetchone()[0]


def describe(row):
    """The public shape of a Project, including its organization and Trash state."""
    if row is None:
        return None
    keys = row.keys()

    def get(key):
        return row[key] if key in keys else None
    state = get('trash_state')
    return {
        'project_id': row['project_id'], 'name': row['name'], 'root_path': row['root_path'],
        'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'sort_order': get('sort_order'), 'hidden': get('hidden_at') is not None, 'hidden_at': get('hidden_at'),
        'trashed': state is not None,
        'trash': None if state is None else {
            'state': state, 'trashed_at': get('trashed_at'), 'original_root_path': get('original_root_path'),
            'path': get('trash_path'), 'kind': get('trash_kind'), 'error': get('lifecycle_error'),
            # Removed after its folder had already been deleted: only its records are in the Trash.
            'folder_missing': state is not None and get('trash_path') is None},
    }


def _row(db, project_id):
    row = db.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone()
    if row is None:
        raise LookupError('Project not found')
    return row


def list_projects(store, *, trashed=False):
    with store._connection() as db:
        condition = 'trash_state IS NOT NULL' if trashed else 'trash_state IS NULL'
        return [describe(row) for row in db.execute(
            f'SELECT * FROM projects WHERE {condition} ORDER BY sort_order IS NULL, sort_order, rowid')]


def get_project(store, project_id):
    with store._connection() as db:
        return describe(_row(db, project_id))


# ---------------------------------------------------------------- organization

def rename(store, project_id, name):
    name = project_name(name)
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, project_id)
        if row['trash_state'] not in (None, 'trashed'):
            raise LifecycleRefused('project_busy', 'This project is being moved; try again when it has finished.')
        # The display name only: the folder, its path and every reference stay as they are.
        db.execute(f'UPDATE projects SET name=?, updated_at={NOW} WHERE project_id=?', (name, project_id))
        return describe(_row(db, project_id))


def set_hidden(store, project_id, hidden):
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, project_id)
        if row['trash_state'] is not None:
            raise LifecycleRefused('project_in_trash', 'A project in the Trash can be restored, not hidden or shown.')
        # Hiding changes navigation only: no process stops and nothing on disk changes.
        if hidden and row['hidden_at'] is None:
            db.execute(f'UPDATE projects SET hidden_at={NOW}, updated_at={NOW} WHERE project_id=?', (project_id,))
        elif not hidden and row['hidden_at'] is not None:
            db.execute(f'UPDATE projects SET hidden_at=NULL, updated_at={NOW} WHERE project_id=?', (project_id,))
        return describe(_row(db, project_id))


def reorder(store, project_ids):
    if not isinstance(project_ids, list) or len(set(project_ids)) != len(project_ids):
        raise ValueError('The order must name each project exactly once')
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        active = [row[0] for row in db.execute('SELECT project_id FROM projects WHERE trash_state IS NULL')]
        if set(active) != set(project_ids):
            # A project registered, trashed or restored meanwhile: the caller reads the list again.
            raise LifecycleRefused('order_outdated', 'The project list changed meanwhile. Reload it and arrange it again.')
        for index, project_id in enumerate(project_ids):
            db.execute('UPDATE projects SET sort_order=? WHERE project_id=?', (index, project_id))
    return list_projects(store)


# ---------------------------------------------------------------- activity and safety

def project_task_ids(db, project_id):
    return [row[0] for row in db.execute('SELECT task_id FROM tasks WHERE project_id=?', (project_id,))]


def activity(db, project_id, agent_statuses):
    """Work that could still be writing to the Project, from the journal and the live registry."""
    placeholders = ','.join('?' * len(OPEN_EXECUTIONS))
    executions = [dict(row) for row in db.execute(
        f'''SELECT e.execution_id, e.agent_id, e.task_id, e.state, t.title FROM executions e
            JOIN tasks t ON t.task_id=e.task_id WHERE t.project_id=? AND e.state IN ({placeholders})''',
        (project_id, *OPEN_EXECUTIONS))]
    agents = []
    for row in db.execute('''SELECT DISTINCT g.agent_id, g.display_name, a.task_id, t.title FROM task_assignments a
            JOIN agents g ON g.agent_id=a.agent_id JOIN tasks t ON t.task_id=a.task_id
            WHERE t.project_id=? AND a.ended_at IS NULL''', (project_id,)):
        status = agent_statuses.get(row['agent_id'])
        if status in ACTIVE_AGENT_STATUSES:
            agents.append({'agent_id': row['agent_id'], 'name': row['display_name'], 'status': status,
                           'task_id': row['task_id'], 'task_title': row['title']})
    placeholders = ','.join('?' * len(IN_FLIGHT_MATERIALIZATION))
    launches = [row[0] for row in db.execute(
        f'''SELECT d.delegation_id FROM delegations d JOIN delegation_materializations m USING(delegation_id)
            WHERE d.project_id=? AND m.phase IN ({placeholders})''', (project_id, *IN_FLIGHT_MATERIALIZATION))]
    counts = {status: count for status, count in db.execute(
        'SELECT status, COUNT(*) FROM tasks WHERE project_id=? GROUP BY status', (project_id,))}
    return {'executions': executions, 'agents': agents, 'launches': launches,
            'task_counts': counts, 'tasks': sum(counts.values()),
            'open_tasks': sum(count for status, count in counts.items() if status not in ('completed', 'canceled')),
            'busy': bool(executions or agents or launches)}


def _activity_reasons(found):
    reasons = []
    running = [agent for agent in found['agents'] if agent['status'] == 'running']
    waiting = [agent for agent in found['agents'] if agent['status'] == 'waiting']
    if running:
        reasons.append(f"{len(running)} {'agent is' if len(running) == 1 else 'agents are'} working in this project.")
    if waiting:
        reasons.append(f"{len(waiting)} {'agent is' if len(waiting) == 1 else 'agents are'} waiting for your answer.")
    agents = {agent['agent_id'] for agent in found['agents']}
    unlisted = [run for run in found['executions'] if run['agent_id'] not in agents]
    if unlisted:
        reasons.append(f"{len(unlisted)} agent {'run has' if len(unlisted) == 1 else 'runs have'} not been confirmed "
                       'as ended. Check Process ownership on the task first.')
    if found['launches']:
        reasons.append('A helper is being launched right now.')
    return reasons


def _environment_paths():
    def path(name, *parts):
        value = os.environ.get(name)
        return Path(value, *parts) if value else None
    profile = path('USERPROFILE')
    known = [profile]
    if profile is not None:
        known += [profile / folder for folder in ('Desktop', 'Documents', 'Downloads', 'Pictures', 'Music', 'Videos',
                                                  'OneDrive', 'source', 'repos', 'AppData')]
    known += [path('SystemRoot'), path('ProgramFiles'), path('ProgramFiles(x86)'), path('ProgramData'),
              path('APPDATA'), path('LOCALAPPDATA'), path('OneDrive'), path('PUBLIC')]
    return [item for item in known if item is not None]


def protected_reasons(root, *, protected=()):
    """Why a registered root must never be moved or deleted, as sentences (empty when it may)."""
    try:
        path, key = canonical_path(root)
    except ValueError:
        return ['The project folder path is not valid.']
    reasons = []
    anchor = Path(path).anchor
    if not anchor or os.path.normcase(os.path.normpath(anchor)) == key or Path(path).parent == Path(path):
        reasons.append('This project is the root of a drive.')
    for item in _environment_paths():
        try:
            other = canonical_path(item)[1]
        except ValueError:
            continue
        if other == key:
            reasons.append('This project is a Windows or user folder, not a project folder.')
            break
        if contains(key, other):
            reasons.append('This project folder contains Windows or user folders.')
            break
    for label, item in protected:
        if item is None:
            continue
        try:
            other = canonical_path(item)[1]
        except ValueError:
            continue
        if contains(key, other):
            reasons.append(f'This project folder contains {label}.')
    return reasons


def application_protections(store, storage_root, trash_root):
    """The locations a project move or deletion may never touch, described for people."""
    application = Path(__file__).resolve().parents[2]
    return (('the Control Center application', application), ('the Control Center database', Path(store.path)),
            ('the Task worktree storage', storage_root), ('the Control Center Trash', trash_root))


# ---------------------------------------------------------------- Git facts (local reads only)

def _git(root, *args):
    """A local Git read. Started through workspace_git's process factory, like every Git call here."""
    from . import workspace_git
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update({'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'})
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    with workspace_git.Popen(['git', '-c', 'core.hooksPath=' + os.devnull, '-C', str(root), *args],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                             shell=False, creationflags=flags) as process:
        try:
            output, _ = process.communicate(timeout=GIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise ValueError('git timed out') from None
        if process.returncode != 0:
            raise ValueError('git failed')
    return output.decode('utf-8', errors='replace')


_CREDENTIALS = re.compile(r'(?<=://)[^/@\s]+@')


def sanitize_remote(url):
    """A remote address without any user name or token embedded in it."""
    return _CREDENTIALS.sub('', url.strip())[:300]


def git_summary(root):
    """What a permanent deletion would lose locally. Never fetches or contacts a remote."""
    summary = {'available': False}
    try:
        remotes = {}
        for line in _git(root, 'remote', '-v').splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes.setdefault(parts[0], sanitize_remote(parts[1]))
        changes = [line for line in _git(root, 'status', '--porcelain=v1', '--untracked-files=normal').splitlines() if line]
        branch = _git(root, 'branch', '--show-current').strip() or None
        unpushed = None
        if remotes:
            unpushed = int(_git(root, 'rev-list', '--count', '--branches', '--not', '--remotes').strip() or 0)
        stashes = len([line for line in _git(root, 'stash', 'list').splitlines() if line])
        summary = {'available': True, 'branch': branch, 'remotes': [{'name': name, 'url': url} for name, url in remotes.items()],
                   'uncommitted_changes': len(changes), 'unpushed_commits': unpushed, 'stashes': stashes}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return summary


# ---------------------------------------------------------------- locations

def trash_root():
    default = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share') / 'AI Agent Control Center' / 'Trash'
    return Path(canonical_path(os.environ.get('CONTROL_CENTER_TRASH_ROOT') or default)[0])


def _volume(path):
    return os.path.normcase(os.path.splitdrive(os.path.abspath(path))[0])


def trash_destination(root, trash_id):
    """Where a folder goes: the application's Trash on the same drive, else beside the folder.

    A move within one drive is a rename, so it is immediate and cannot leave half-copied files.
    The application's Trash is used only when it is on the folder's drive."""
    app = trash_root()
    if _volume(app) == _volume(root):
        container = app / trash_id
        return 'app', container, container / Path(root).name
    container = Path(root).parent / BESIDE_TRASH / trash_id
    return 'beside', container, container / Path(root).name


def _hide(path):
    if os.name == 'nt':
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x2)  # FILE_ATTRIBUTE_HIDDEN
        except (OSError, AttributeError):
            pass


def _is_reparse(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(info, 'st_file_attributes', 0) & REPARSE_POINT) or stat.S_ISLNK(info.st_mode)


def _long(path):
    text = os.path.abspath(str(path))
    if os.name == 'nt' and not text.startswith('\\\\?\\'):
        return '\\\\?\\UNC\\' + text[2:] if text.startswith('\\\\') else '\\\\?\\' + text
    return text


def _move_error(exc):
    code = getattr(exc, 'winerror', None)
    if code in IN_USE_ERRORS or isinstance(exc, PermissionError):
        return LifecycleRefused('folder_in_use', 'Windows did not allow the folder to be moved.', [
            'A program is probably using a file in it, such as an editor, a terminal or a running build. '
            'Close it and try again.'])
    return LifecycleRefused('move_failed', 'The folder could not be moved.', [f'Windows reported: {exc.strerror or exc}.'])


# ---------------------------------------------------------------- safe deletion

def remove_tree(target, container):
    """Delete `target` (inside `container`) without following links or junctions out of it.

    A link or junction found inside is removed as a link; what it points to is never visited.
    Read-only files (Git objects are read-only on Windows) are made writable first. Returns the
    paths that could not be removed, with the reason; an empty list means everything is gone."""
    if not os.path.lexists(target):
        return []
    # Checked before resolving: a link would resolve to wherever it points.
    if _is_reparse(target):
        raise LifecycleRefused('unsafe_delete', 'Deletion refused: the folder itself is a link or junction.')
    target_path, target_key = canonical_path(target)
    _, container_key = canonical_path(container)
    if target_key == container_key or not contains(container_key, target_key):
        raise LifecycleRefused('unsafe_delete', 'Deletion refused: the folder is outside the area it may delete.')
    failures = []

    def unlink(path, is_dir):
        try:
            if is_dir:
                try:
                    os.rmdir(_long(path))
                except PermissionError:
                    os.chmod(_long(path), stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                    os.rmdir(_long(path))
            else:
                try:
                    os.unlink(_long(path))
                except PermissionError:
                    os.chmod(_long(path), stat.S_IWRITE)
                    os.unlink(_long(path))
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append({'path': str(path), 'error': exc.strerror or type(exc).__name__})

    def walk(directory):
        try:
            entries = list(os.scandir(_long(directory)))
        except FileNotFoundError:
            return
        except OSError as exc:
            failures.append({'path': str(directory), 'error': exc.strerror or type(exc).__name__})
            return
        for entry in entries:
            path = Path(directory) / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                failures.append({'path': str(path), 'error': exc.strerror or type(exc).__name__})
                continue
            reparse = bool(getattr(info, 'st_file_attributes', 0) & REPARSE_POINT) or stat.S_ISLNK(info.st_mode)
            if reparse:
                # The link itself goes; a directory junction or symlink is removed with rmdir.
                unlink(path, stat.S_ISDIR(info.st_mode) or entry.is_dir(follow_symlinks=False) or _link_is_dir(path))
            elif stat.S_ISDIR(info.st_mode):
                walk(path)
                unlink(path, True)
            else:
                unlink(path, False)

    walk(Path(target_path))
    unlink(Path(target_path), True)
    return failures


def _link_is_dir(path):
    try:
        attributes = os.lstat(_long(path)).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)


# ---------------------------------------------------------------- Trash

def preview(store, project_id, agent_statuses, storage_root):
    """Everything the person sees before Move to Trash or Delete permanently. Changes nothing."""
    with store._connection() as db:
        row = _row(db, project_id)
        found = activity(db, project_id, agent_statuses)
        workspaces = [r['workspace_path'] for r in db.execute('SELECT workspace_path FROM task_workspaces WHERE project_id=?',
                                                              (project_id,))]
        agents = db.execute('''SELECT COUNT(DISTINCT a.agent_id) FROM task_assignments a JOIN tasks t ON t.task_id=a.task_id
            WHERE t.project_id=?''', (project_id,)).fetchone()[0]
    root = row['root_path']
    folder_exists = os.path.isdir(root)
    protections = protected_reasons(row['original_root_path'] or root,
                                    protected=application_protections(store, storage_root, trash_root()))
    kind, container, destination = trash_destination(row['original_root_path'] or root, 'preview')
    return {
        'project': describe(row),
        'activity': {**found, 'reasons': _activity_reasons(found)},
        'agents': agents, 'worktrees': len(workspaces),
        'folder': {'path': root, 'exists': folder_exists},
        'git': git_summary(root) if folder_exists else {'available': False},
        'protections': protections,
        'trash_location': {'kind': kind, 'container': str(container.parent)},
    }


def move_to_trash(store, project_id, agent_statuses, storage_root, folder_missing=False):
    """Move the Project's folder into the Trash. With `folder_missing` (the person's explicit choice after
    being told the folder is gone), a Project whose folder no longer exists goes to the Trash as records
    only: nothing on disk is moved or deleted, and its history and Task worktrees are kept."""
    from .task_workspaces import task_lock
    with task_lock(store, f'project:{project_id}', blocking=False):
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = _row(db, project_id)
            if row['trash_state'] is not None:
                raise LifecycleRefused('project_in_trash', 'This project is already in the Trash.')
            root = row['root_path']
            reasons = protected_reasons(root, protected=application_protections(store, storage_root, trash_root()))
            if reasons:
                raise LifecycleRefused('protected_folder', 'This folder cannot be moved to the Trash.', reasons)
            found = activity(db, project_id, agent_statuses)
            if found['busy']:
                raise LifecycleRefused('project_busy', 'Stop the work in this project first.', _activity_reasons(found),
                                       activity=found)
            missing = not os.path.lexists(root)
            if missing and folder_missing:
                # Nothing to move. A placeholder location inside the Trash (never created) releases the
                # original path, so the folder can be added or restored there again.
                placeholder, key = canonical_path(trash_root() / 'missing' / str(uuid4()))
                db.execute(f'''UPDATE projects SET trash_state='trashed', trashed_at={NOW}, original_root_path=?,
                    trash_path=NULL, trash_container=NULL, trash_kind=NULL, lifecycle_error=NULL,
                    root_path=?, root_path_key=?, updated_at={NOW} WHERE project_id=?''',
                           (root, placeholder, key, project_id))
                return describe(_row(db, project_id))
            if folder_missing:
                raise LifecycleRefused('folder_present', 'The project folder exists, so it is moved to the Trash normally.', [
                    f'{root} was found. Choose Move to Trash.'])
            if missing or not os.path.isdir(root) or _is_reparse(root):
                raise LifecycleRefused('folder_missing', 'The project folder no longer exists where it was registered.', [
                    f'Nothing was found at {root}. If you deleted it, remove the project from the Control Center: '
                    'its tasks and history stay in the Trash, and nothing on disk is touched.',
                    'If you moved the folder yourself, move it back first to keep it with the project.'],
                    path=root, can_remove=missing)
            trash_id = str(uuid4())
            kind, container, destination = trash_destination(root, trash_id)
            # Durable intent first: launches are refused from this commit on, and a restart
            # that interrupts the move can tell where the folder went.
            db.execute(f'''UPDATE projects SET trash_state='moving', original_root_path=?, trash_path=?, trash_container=?,
                trash_kind=?, lifecycle_error=NULL, updated_at={NOW} WHERE project_id=?''',
                       (root, str(destination), str(container), kind, project_id))
        try:
            destination = _move_folder(root, container, destination, kind)
        except LifecycleRefused as refusal:
            with store._connection() as db:
                db.execute(f'''UPDATE projects SET trash_state=NULL, original_root_path=NULL, trash_path=NULL,
                    trash_container=NULL, trash_kind=NULL, lifecycle_error=NULL, updated_at={NOW} WHERE project_id=?''', (project_id,))
            raise refusal
        return _finish_trash(store, project_id)


def _move_folder(root, container, destination, kind):
    try:
        container.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise LifecycleRefused('move_failed', 'The Trash location already exists; try again.') from None
    except OSError as exc:
        raise _move_error(exc) from exc
    if kind == 'beside':
        _hide(container.parent)
    try:
        _rename(root, destination)
    except OSError as exc:
        try:
            container.rmdir()
        except OSError:
            pass
        if getattr(exc, 'winerror', None) in CROSS_VOLUME_ERRORS:
            raise LifecycleRefused('move_failed', 'The folder is on a different drive than its Trash location.', [
                'A move across drives would copy every file, so it was not attempted.']) from exc
        raise _move_error(exc) from exc
    return destination


def _rename(source, destination, attempts=4):
    """A same-drive rename. An indexer or antivirus may hold a handle for a moment: retried briefly."""
    import time
    for attempt in range(attempts):
        try:
            os.rename(source, destination)
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in IN_USE_ERRORS or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (attempt + 1))


def _finish_trash(store, project_id):
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, project_id)
        path, key = canonical_path(row['trash_path'])
        db.execute(f'''UPDATE projects SET trash_state='trashed', trashed_at={NOW}, root_path=?, root_path_key=?,
            updated_at={NOW} WHERE project_id=?''', (path, key, project_id))
        return describe(_row(db, project_id))


def _suggest(path):
    base = Path(path)
    for index in range(1, 100):
        candidate = base.with_name(f'{base.name} (restored{"" if index == 1 else " " + str(index)})')
        if not os.path.lexists(candidate):
            return str(candidate)
    return None


def restore(store, project_id, target=None):
    from .task_workspaces import task_lock
    with task_lock(store, f'project:{project_id}', blocking=False):
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = _row(db, project_id)
            if row['trash_state'] != 'trashed':
                raise LifecycleRefused('not_in_trash', 'Only a project in the Trash can be restored.')
            source = row['trash_path']
            original = row['original_root_path']
            if source is None:
                return _restore_without_folder(db, store, project_id, original, target)
            if not os.path.isdir(source) or _is_reparse(source):
                raise LifecycleRefused('trash_folder_missing', 'The folder is no longer in the Trash.', [
                    f'Nothing was found at {source}. It may have been deleted outside the Control Center. '
                    'You can still delete the project permanently to remove its records.'])
            original = row['original_root_path']
            chosen = target or original
            try:
                chosen, chosen_key = canonical_path(chosen)
            except ValueError:
                raise LifecycleRefused('restore_target_invalid', 'That location is not a valid folder path.') from None
            if os.path.lexists(chosen):
                raise LifecycleRefused('restore_target_exists', 'Something already exists where the folder would go.', [
                    f'{chosen} exists. Nothing there is changed. Restore to another location instead.'],
                    suggestion=_suggest(chosen), original=original)
            for other in db.execute('SELECT root_path_key FROM projects WHERE project_id<>?', (project_id,)):
                if contains(other[0], chosen_key) or contains(chosen_key, other[0]):
                    raise LifecycleRefused('restore_target_registered', 'That location overlaps another project.',
                                           suggestion=_suggest(chosen), original=original)
            for other in db.execute('SELECT workspace_path_key FROM task_workspaces'):
                if contains(other[0], chosen_key) or contains(chosen_key, other[0]):
                    raise LifecycleRefused('restore_target_registered', 'That location overlaps Task worktree storage.')
            parent = Path(chosen).parent
            if not parent.is_dir():
                raise LifecycleRefused('restore_parent_missing', 'The folder that held the project no longer exists.', [
                    f'{parent} was not found. Create it, or restore to another location.'],
                    suggestion=None, original=original)
            if _volume(chosen) != _volume(source):
                raise LifecycleRefused('restore_other_drive', 'Choose a location on the same drive as the Trash.', [
                    f'The folder is kept on drive {_volume(source).upper()}; a move across drives would copy every file.'])
            db.execute(f'''UPDATE projects SET trash_state='restoring', restore_target=?, lifecycle_error=NULL,
                updated_at={NOW} WHERE project_id=?''', (chosen, project_id))
        try:
            _rename(source, chosen)
        except OSError as exc:
            with store._connection() as db:
                db.execute(f"UPDATE projects SET trash_state='trashed', restore_target=NULL, updated_at={NOW} WHERE project_id=?",
                           (project_id,))
            raise _move_error(exc) from exc
        return _finish_restore(store, project_id, chosen)


def _restore_without_folder(db, store, project_id, original, target):
    """A Project removed after its folder was deleted comes back once that folder exists again."""
    try:
        chosen, chosen_key = canonical_path(target or original)
    except ValueError:
        raise LifecycleRefused('restore_target_invalid', 'That location is not a valid folder path.') from None
    if not os.path.isdir(chosen) or _is_reparse(chosen):
        raise LifecycleRefused('restore_folder_missing', 'The project folder doesn’t exist yet.', [
            f'This project was removed after its folder was deleted. Put the folder back at {chosen} (or choose '
            'where it is now), then restore the project.'], original=original)
    for other in db.execute('SELECT root_path_key FROM projects WHERE project_id<>?', (project_id,)):
        if contains(other[0], chosen_key) or contains(chosen_key, other[0]):
            raise LifecycleRefused('restore_target_registered', 'That folder is, or overlaps, another project.',
                                   original=original)
    for other in db.execute('SELECT workspace_path_key FROM task_workspaces'):
        if contains(other[0], chosen_key) or contains(chosen_key, other[0]):
            raise LifecycleRefused('restore_target_registered', 'That location overlaps Task worktree storage.')
    db.execute(f"UPDATE projects SET trash_state='restoring', restore_target=?, updated_at={NOW} WHERE project_id=?",
               (chosen, project_id))
    db.commit()
    return _finish_restore(store, project_id, chosen)


def _finish_restore(store, project_id, chosen):
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = _row(db, project_id)
        container = row['trash_container']
        moved = canonical_path(row['original_root_path'])[1] != canonical_path(chosen)[1]
        path, key = canonical_path(chosen)
        db.execute(f'''UPDATE projects SET trash_state=NULL, trashed_at=NULL, hidden_at=NULL, root_path=?, root_path_key=?,
            original_root_path=NULL, trash_path=NULL, trash_container=NULL, trash_kind=NULL, restore_target=NULL,
            lifecycle_error=NULL, updated_at={NOW} WHERE project_id=?''', (path, key, project_id))
        workspaces = [r[0] for r in db.execute('SELECT workspace_path FROM task_workspaces WHERE project_id=?', (project_id,))]
        result = describe(_row(db, project_id))
    _remove_empty(container)
    if moved and workspaces:
        # The Task worktrees still point at the old location; Git rewrites both directions.
        try:
            _git(path, 'worktree', 'repair', *[w for w in workspaces if os.path.isdir(w)])
        except (OSError, ValueError, subprocess.SubprocessError):
            result['warning'] = ('The folder was restored, but its Task worktrees could not be relinked. '
                                 'Run "git worktree repair" in the project folder.')
    return result


def _remove_empty(container):
    if not container:
        return
    try:
        os.rmdir(container)
    except OSError:
        return
    parent = Path(container).parent
    if parent.name == BESIDE_TRASH:
        try:
            os.rmdir(parent)
        except OSError:
            pass


def delete_permanently(store, project_id, confirm_name, agent_statuses, storage_root):
    """Delete the folder, this application's worktrees for it, and its records. Irreversible."""
    from .task_workspaces import task_lock
    with task_lock(store, f'project:{project_id}', blocking=False):
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = _row(db, project_id)
            if row['trash_state'] not in ('trashed', 'deleting'):
                raise LifecycleRefused('not_in_trash', 'Move the project to the Trash before deleting it permanently.')
            if not isinstance(confirm_name, str) or confirm_name.strip() != row['name']:
                raise LifecycleRefused('confirmation_mismatch', 'Type the project name exactly to confirm.')
            found = activity(db, project_id, agent_statuses)
            if found['busy']:
                raise LifecycleRefused('project_busy', 'Work in this project is still running.', _activity_reasons(found),
                                       activity=found)
            reasons = protected_reasons(row['original_root_path'] or row['root_path'],
                                        protected=application_protections(store, storage_root, trash_root()))
            if reasons:
                raise LifecycleRefused('protected_folder', 'This folder cannot be deleted.', reasons)
            workspaces = [r[0] for r in db.execute('SELECT workspace_path FROM task_workspaces WHERE project_id=?', (project_id,))]
            db.execute(f"UPDATE projects SET trash_state='deleting', lifecycle_error=NULL, updated_at={NOW} WHERE project_id=?",
                       (project_id,))
            folder, container = row['trash_path'], row['trash_container']
        failures = []
        try:
            if folder and container:
                failures += remove_tree(folder, container)
            project_storage = Path(storage_root) / str(UUID(project_id))
            for workspace in workspaces:
                if contains(canonical_path(project_storage)[1], canonical_path(workspace)[1]):
                    failures += remove_tree(workspace, project_storage)
            if os.path.isdir(project_storage) and not _is_reparse(project_storage):
                failures += remove_tree(project_storage, storage_root)
        except LifecycleRefused as refusal:
            failures.append({'path': folder or '', 'error': str(refusal)})
        if failures:
            message = f'{len(failures)} item{"" if len(failures) == 1 else "s"} could not be deleted.'
            with store._connection() as db:
                db.execute(f'UPDATE projects SET lifecycle_error=?, updated_at={NOW} WHERE project_id=?',
                           (message + ' ' + failures[0]['path'] + ': ' + failures[0]['error'], project_id))
            raise LifecycleRefused('delete_incomplete', 'Deletion is not complete.', [
                message + ' A program may be using them. Close it and choose Delete permanently again.'],
                failures=failures[:20])
        _remove_empty(container)
        removed = delete_records(store, project_id)
    return {'deleted': True, 'project_id': project_id, 'records': removed, 'folder': folder}


def delete_records(store, project_id):
    """Every record that belongs to the Project, in one transaction, verified by foreign keys."""
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('PRAGMA defer_foreign_keys = ON')
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]

        def key_of(parent):
            # A reference without a column names the parent's primary key.
            keys = [r['name'] for r in db.execute(f'PRAGMA table_info("{parent}")') if r['pk']]
            return keys[0] if len(keys) == 1 else 'rowid'
        references = {table: [(r['table'], r['from'], r['to'] or key_of(r['table']))
                              for r in db.execute(f'PRAGMA foreign_key_list("{table}")')] for table in tables}
        db.execute('CREATE TEMP TABLE IF NOT EXISTS doomed (tbl TEXT NOT NULL, rid INTEGER NOT NULL, PRIMARY KEY (tbl, rid))')
        db.execute('DELETE FROM doomed')
        db.execute("INSERT INTO doomed SELECT 'projects', rowid FROM projects WHERE project_id=?", (project_id,))

        def cascade():
            while True:
                added = 0
                for table, edges in references.items():
                    for parent, column, target in edges:
                        added += db.execute(f'''INSERT OR IGNORE INTO doomed SELECT ?, c.rowid FROM "{table}" c
                            WHERE c."{column}" IN (SELECT p."{target}" FROM "{parent}" p
                            WHERE p.rowid IN (SELECT rid FROM doomed WHERE tbl=?))''', (table, parent)).rowcount
                if not added:
                    return
        cascade()
        # An Agent belongs to the Project when every one of its assignments does.
        db.execute('''INSERT OR IGNORE INTO doomed SELECT 'agents', g.rowid FROM agents g
            WHERE EXISTS (SELECT 1 FROM task_assignments a JOIN doomed d ON d.tbl='task_assignments' AND d.rid=a.rowid
                          WHERE a.agent_id=g.agent_id)
              AND NOT EXISTS (SELECT 1 FROM task_assignments a WHERE a.agent_id=g.agent_id
                              AND a.rowid NOT IN (SELECT rid FROM doomed WHERE tbl='task_assignments'))''')
        cascade()
        foreign = db.execute('''SELECT COUNT(*) FROM tasks WHERE rowid IN (SELECT rid FROM doomed WHERE tbl='tasks')
            AND project_id IS NOT ?''', (project_id,)).fetchone()[0]
        foreign += db.execute('''SELECT COUNT(*) FROM projects WHERE rowid IN (SELECT rid FROM doomed WHERE tbl='projects')
            AND project_id<>?''', (project_id,)).fetchone()[0]
        foreign += db.execute('''SELECT COUNT(*) FROM agents g WHERE g.rowid IN (SELECT rid FROM doomed WHERE tbl='agents')
            AND EXISTS (SELECT 1 FROM task_assignments a JOIN tasks t ON t.task_id=a.task_id
                        WHERE a.agent_id=g.agent_id AND t.project_id IS NOT ?)''', (project_id,)).fetchone()[0]
        if foreign:
            raise LifecycleRefused('cross_project_records', 'Some records are shared with another project.', [
                'Nothing was deleted from the database. The folder is already gone; the project stays in the Trash.'])
        tasks = [r[0] for r in db.execute("SELECT task_id FROM tasks WHERE rowid IN (SELECT rid FROM doomed WHERE tbl='tasks')")]
        agents = [r[0] for r in db.execute("SELECT agent_id FROM agents WHERE rowid IN (SELECT rid FROM doomed WHERE tbl='agents')")]
        executions = [r[0] for r in db.execute(
            "SELECT execution_id FROM executions WHERE rowid IN (SELECT rid FROM doomed WHERE tbl='executions')")]
        counts = {}
        for table in tables:
            deleted = db.execute(f'DELETE FROM "{table}" WHERE rowid IN (SELECT rid FROM doomed WHERE tbl=?)', (table,)).rowcount
            if deleted:
                counts[table] = deleted
        # References without a foreign key: history rows that would otherwise point nowhere.
        for chunk in _chunks(tasks):
            marks = ','.join('?' * len(chunk))
            db.execute(f'DELETE FROM provider_policy_revisions WHERE task_id IN ({marks})', chunk)
        for chunk in _chunks(agents):
            marks = ','.join('?' * len(chunk))
            db.execute(f'UPDATE provider_usage_blocks SET agent_id=NULL WHERE agent_id IN ({marks})', chunk)
            db.execute(f'UPDATE agents SET parent_id=NULL WHERE parent_id IN ({marks})', chunk)
        for chunk in _chunks(executions):
            marks = ','.join('?' * len(chunk))
            db.execute(f'UPDATE provider_usage_blocks SET execution_id=NULL WHERE execution_id IN ({marks})', chunk)
            db.execute(f'UPDATE model_denials SET execution_id=NULL WHERE execution_id IN ({marks})', chunk)
        db.execute('DELETE FROM doomed')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise LifecycleRefused('delete_records_failed', 'The records could not be deleted consistently.')
    return {'tasks': len(tasks), 'agents': len(agents), 'executions': len(executions), 'tables': counts,
            'agent_ids': agents}


def _chunks(items, size=400):
    for index in range(0, len(items), size):
        yield items[index:index + size]


# ---------------------------------------------------------------- gates and recovery

def require_runnable(db, task_id):
    """Launch admission: no work starts in a Project that is in, or moving to or from, the Trash."""
    row = db.execute('''SELECT p.trash_state FROM tasks t JOIN projects p ON p.project_id=t.project_id
        WHERE t.task_id=?''', (task_id,)).fetchone()
    if row is not None and row['trash_state'] is not None:
        raise ValueError('This project is in the Trash. Restore it to run work in it.')


def recover(store):
    """Settle a move, restore or deletion that a restart interrupted, from what is on disk."""
    settled = []
    with store._connection() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM projects WHERE trash_state IN ('moving','restoring','deleting')")]
    for row in rows:
        original, trashed = row['original_root_path'], row['trash_path']
        state = row['trash_state']
        if state == 'moving':
            if trashed and os.path.isdir(trashed) and not os.path.lexists(original):
                _finish_trash(store, row['project_id'])
                settled.append((row['project_id'], 'trashed'))
            elif original and os.path.isdir(original):
                with store._connection() as db:
                    db.execute(f'''UPDATE projects SET trash_state=NULL, original_root_path=NULL, trash_path=NULL,
                        trash_container=NULL, trash_kind=NULL, updated_at={NOW} WHERE project_id=?''', (row['project_id'],))
                _remove_empty(row['trash_container'])
                settled.append((row['project_id'], 'active'))
        elif state == 'restoring':
            target = row['restore_target'] or original
            if trashed and os.path.isdir(trashed):
                with store._connection() as db:
                    db.execute(f"UPDATE projects SET trash_state='trashed', restore_target=NULL, updated_at={NOW} WHERE project_id=?",
                               (row['project_id'],))
                settled.append((row['project_id'], 'trashed'))
            elif target and os.path.isdir(target):
                _finish_restore(store, row['project_id'], target)
                settled.append((row['project_id'], 'active'))
        elif state == 'deleting':
            with store._connection() as db:
                db.execute(f'''UPDATE projects SET lifecycle_error=COALESCE(lifecycle_error,
                    'Deletion was interrupted. Choose Delete permanently again to finish it.'), updated_at={NOW}
                    WHERE project_id=?''', (row['project_id'],))
    return settled
