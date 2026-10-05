"""Durable Task-owned worktrees. No status-driven deletion or integration."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
from threading import Lock, RLock
from uuid import UUID, uuid4

from .project_domain import canonical_path, contains
from . import workspace_git

_guard = Lock()
_locks = {}


@contextmanager
def task_lock(store, task_id, *, blocking=True):
    key = (canonical_path(store.path)[1], task_id)
    with _guard:
        lock, users = _locks.get(key, (RLock(), 0))
        _locks[key] = (lock, users + 1)
    try:
        acquired = lock.acquire(blocking=blocking)
        if not acquired:
            raise ValueError("Task Workspace operation already in progress; retry shortly")
        try:
            yield
        finally:
            lock.release()
    finally:
        with _guard:
            _, users = _locks[key]
            if users == 1:
                del _locks[key]
            else:
                _locks[key] = (lock, users - 1)


def storage_root():
    default = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local' / 'share') / 'AI Agent Control Center' / 'task-workspaces'
    return Path(canonical_path(os.environ.get('CONTROL_CENTER_WORKSPACE_ROOT') or default)[0])


def _outside_projects(store, path):
    key = canonical_path(path)[1]
    for project in store.list_projects():
        root = canonical_path(project['root_path'])[1]
        if contains(root, key) or contains(key, root):
            raise ValueError('Task Workspace storage must not overlap a canonical Project')
    return key


def _outside_workspaces(store, key, task_id=None):
    for workspace in store.list_task_workspaces():
        if workspace['task_id'] != task_id:
            other = workspace['workspace_path_key']
            if contains(key, other) or contains(other, key):
                raise ValueError('Task Workspace paths must not overlap')


def validate_task_workspace(store, task, workspace):
    if workspace['task_id'] != task['task_id'] or workspace['project_id'] != task['project_id']:
        raise ValueError('Task Workspace ownership mismatch')
    if workspace['source_task_id'] is not None and workspace['source_task_id'] != task['parent_task_id']:
        raise ValueError('Task Workspace source association mismatch')
    project = store.get_project(task['project_id'])
    if project is None:
        raise ValueError('Task Project is unresolved')
    path = Path(workspace['workspace_path'])
    _, key = canonical_path(path, require_directory=True)
    if key != workspace['workspace_path_key']:
        raise ValueError('Task Workspace path identity changed')
    _outside_projects(store, path)
    _outside_workspaces(store, key, task['task_id'])
    common = workspace_git.repository(project['root_path'])
    if workspace_git.repository(path) != common or not (path / '.git').is_file():
        raise ValueError('Task Workspace is not a linked worktree of its Project')
    registered = workspace_git.git(project['root_path'], 'worktree', 'list', '--porcelain', '-z')
    roots = [os.fsdecode(item[len(b'worktree '):]) for item in registered.split(b'\0') if item.startswith(b'worktree ')]
    if key not in {canonical_path(root)[1] for root in roots}:
        raise ValueError('Task Workspace is not registered with its Project')
    return path


def _compensate(store, project, target, key):
    # Only the directory reserved by this provisioning attempt may be removed.
    # Never clean on spawn failure or delete an already durable Workspace.
    if store.workspace_for_path(key) is not None:
        return
    if canonical_path(target)[1] != key:
        raise RuntimeError('Workspace cleanup refused: path identity changed')
    _outside_projects(store, target)
    _outside_workspaces(store, key)
    if (target / '.git').is_file():
        if workspace_git.repository(target) != workspace_git.repository(project['root_path']):
            raise RuntimeError('Workspace cleanup refused: repository identity changed')
        workspace_git.git(project['root_path'], 'worktree', 'remove', '--force', str(target))
    elif target.exists():
        shutil.rmtree(target)


def provision_task_workspace(store, task_id, *, origin_kind='legacy_project_snapshot'):
    task = store.get_task(task_id)
    if task is None:
        raise LookupError('Task not found')
    if not task['project_id']:
        raise ValueError('Task has unresolved Project ownership')
    existing = store.get_task_workspace(task_id)
    if existing is not None:
        validate_task_workspace(store, task, existing)
        return existing
    # Provision ancestry before taking the child lock; no child->parent lock cycle.
    source = None
    if origin_kind == 'parent_task_snapshot':
        parent = store.get_task(task['parent_task_id'])
        if parent is None or parent['project_id'] != task['project_id']:
            raise ValueError('Child Task must belong to its parent Project')
        source = provision_task_workspace(store, parent['task_id'])
    with task_lock(store, task_id):
        existing = store.get_task_workspace(task_id)
        if existing is not None:
            validate_task_workspace(store, task, existing)
            return existing
        project = store.get_project(task['project_id'])
        if project is None:
            raise ValueError('Task Project is unresolved')
        workspace_git.repository(project['root_path'])
        root = storage_root()
        _outside_projects(store, root)
        target = root / str(UUID(project['project_id'])) / str(UUID(task_id))
        key = _outside_projects(store, target)
        _outside_workspaces(store, key)
        source_path = source['workspace_path'] if source else project['root_path']
        snapshot = workspace_git.snapshot_worktree_source(source_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir()  # Exclusive reservation; never reuse an unrecorded path.
        except FileExistsError:
            raise ValueError('Unrecorded Task Workspace already exists; manual recovery is required') from None
        try:
            workspace_git.materialize(project['root_path'], target, snapshot)
            record = {'workspace_id': str(uuid4()), 'task_id': task_id, 'project_id': task['project_id'],
                      'workspace_path': str(target), 'workspace_path_key': key, 'base_snapshot': snapshot,
                      'origin_kind': origin_kind, 'source_task_id': task['parent_task_id'] if source else None}
            store.save_task_workspace(record)
        except Exception:
            # Query before cleanup also handles a commit whose acknowledgment failed.
            # If storage cannot answer, preserve files for recovery rather than guess.
            _compensate(store, project, target, key)
            raise
        return store.get_task_workspace(task_id)
