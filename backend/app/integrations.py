"""Explicit structural integration; no Agent or Task lifecycle transitions."""
from contextlib import ExitStack
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

from . import workspace_git as gitops, integration_git as applygit
from .task_workspaces import task_lock, validate_task_workspace, storage_root, _outside_projects, _outside_workspaces
from .workspace_freshness import changed_files

ACTIVE = ('preparing', 'ready', 'applying', 'recovery_required')


def public(record):
    return {k: v for k,v in record.items() if k not in ('plan', 'integration_order')}


def validate_git_state(root):
    if gitops.git(root, 'ls-files', '--unmerged', '-z'):
        raise ValueError('Integration refuses an unresolved Git index')
    metadata = Path(gitops.git(root, 'rev-parse', '--absolute-git-dir').decode().strip())
    if any((metadata / name).exists() for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD',
                                                   'rebase-apply', 'rebase-merge', 'sequencer')):
        raise ValueError('Integration refuses an in-progress Git operation')


def resolve(store, task_id):
    task = store.get_task(task_id)
    if task is None:
        raise LookupError('Task not found')
    workspace = store.get_task_workspace(task_id)
    if workspace is None:
        raise ValueError('Source Task Workspace is unavailable; integration never provisions workspaces')
    source = validate_task_workspace(store, task, workspace)
    if task['parent_task_id']:
        parent = store.get_task(task['parent_task_id'])
        destination = store.get_task_workspace(task['parent_task_id'])
        if parent is None or parent['project_id'] != task['project_id'] or destination is None:
            raise ValueError('Direct parent Task Workspace is unavailable or belongs to another Project')
        target = validate_task_workspace(store, parent, destination)
        kind, identifier = 'task', parent['task_id']
    else:
        project = store.get_project(task['project_id'])
        if project is None:
            raise ValueError('Source Task Project is unresolved')
        target = Path(project['root_path'])
        if gitops.repository(target) != gitops.repository(source):
            raise ValueError('Source and destination repository mismatch')
        kind, identifier = 'project', project['project_id']
    validate_git_state(source)
    validate_git_state(target)
    return workspace, source, target, kind, identifier


def _locks(store, task_id, kind, identifier):
    # Same keys as worker start/resume, plus one key per canonical destination.
    return sorted({task_id, identifier if kind == 'task' else 'project:' + identifier})


def integrate(store, task_id, assert_quiet):
    _, _, _, kind, identifier = resolve(store, task_id)
    with ExitStack() as guards:
        for key in _locks(store, task_id, kind, identifier):
            guards.enter_context(task_lock(store, key))
        workspace, source, destination, actual_kind, actual_id = resolve(store, task_id)
        if (kind, identifier) != (actual_kind, actual_id):
            raise ValueError('Integration destination changed')
        assert_quiet(task_id)
        if kind == 'task':
            assert_quiet(identifier)
        base = gitops.source_tree(source, workspace['base_snapshot'])
        local = gitops.snapshot_source_tree(source)
        upstream = gitops.snapshot_source_tree(destination)
        record = store.claim_integration({'integration_id': str(uuid4()), 'source_task_id': task_id,
            'destination_kind': kind, 'destination_id': identifier, 'destination_key': kind + ':' + identifier,
            'base_snapshot': base, 'source_snapshot': local, 'destination_before_snapshot': upstream, 'status': 'preparing'})
        if record['status'] == 'applied':
            return public(record)
        integration_id = record['integration_id']
        applying = False
        try:
            # Internal refs retain recovery/provenance objects across git gc;
            # these are not branches and never move a user HEAD or index.
            for label, tree in (('base', base), ('source', local), ('before', upstream)):
                gitops.git(source, 'update-ref', f'refs/control-center/integrations/{integration_id}/{label}',
                           gitops.snapshot_commit(source, tree))
            if local == base:
                return public(store.update_integration(integration_id, 'noop', result_snapshot=upstream))
            candidate_root = storage_root() / 'integration-candidates'
            key = _outside_projects(store, candidate_root)
            _outside_workspaces(store, key)
            candidate_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=integration_id + '-', dir=candidate_root) as directory:
                result, conflicts = applygit.build_candidate(source, base, local, upstream, directory)
                if conflicts:
                    labels = [json.dumps(p, ensure_ascii=True)[1:-1][:240] for p in conflicts[:50]]
                    return public(store.update_integration(integration_id, 'conflict', conflict_paths=labels, conflict_paths_remaining=max(0,len(conflicts)-50),
                                                          failure_reason='Source and destination have conflicting changes'))
                gitops.git(source, 'update-ref', f'refs/control-center/integrations/{integration_id}/result',
                           gitops.snapshot_commit(source, result))
                paths, remaining = changed_files(source, upstream, result)
                # Journal keeps the complete raw relative paths; public summaries are bounded.
                plan = applygit.prepare_apply(destination, upstream, result, directory)
                store.update_integration(integration_id, 'ready', result_snapshot=result, changed_paths=paths, changed_paths_remaining=remaining, plan=plan)
                current_ws, current_source, current_dest, current_kind, current_id = resolve(store, task_id)
                assert_quiet(task_id)
                if kind == 'task':
                    assert_quiet(identifier)
                if (current_kind, current_id, current_source, current_dest) != (kind, identifier, source, destination):
                    raise ValueError('Integration association changed during preparation')
                if current_ws['base_snapshot'] != workspace['base_snapshot'] or gitops.snapshot_source_tree(source) != local:
                    return public(store.update_integration(integration_id, 'source_changed', failure_reason='Source changed; prepare a new integration'))
                if gitops.snapshot_source_tree(destination) != upstream:
                    return public(store.update_integration(integration_id, 'destination_changed', failure_reason='Destination changed; prepare a new integration'))
                store.update_integration(integration_id, 'applying')
                applying = True
                def verify_applied():
                    if gitops.snapshot_source_tree(source) != local or gitops.snapshot_source_tree(destination) != result:
                        raise ValueError('Source or destination changed during apply')
                applygit.apply_plan(destination, plan, directory, verify_applied)
                return public(store.update_integration(integration_id, 'applied'))
        except applygit.ApplyFailure as exc:
            status = 'failed' if exc.restored and gitops.snapshot_source_tree(destination) == upstream else 'recovery_required'
            return public(store.update_integration(integration_id, status, failure_reason=str(exc)))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            # If commit acknowledgment is uncertain, never free the destination
            # claim or guess whether filesystem changes were committed.
            try:
                saved = store.get_integration(integration_id)
                if saved['status'] != 'applied':
                    store.update_integration(integration_id, 'recovery_required' if applying or saved['status'] == 'applying' else 'failed',
                                             failure_reason='Integration interrupted; source and destination require inspection' if applying else 'Integration preparation failed safely')
            except (OSError, sqlite3.Error):
                pass  # Durable preparing/applying remains a blocking recovery journal.
            if isinstance(exc, ValueError) and not applying:
                raise ValueError('Integration refused an unsafe or unsupported source/destination state; inspect its history') from None
            raise RuntimeError('Integration could not complete; inspect its durable history before retrying') from None


def recover_integrations(store):
    """Explicit backend startup only; never runs during ordinary DB construction."""
    for record in store.list_integrations():
        if record['status'] not in ('preparing', 'ready', 'applying'):
            continue
        status = 'recovery_required'
        try:
            workspace, source, destination, kind, identifier = resolve(store, record['source_task_id'])
            if (kind, identifier) != (record['destination_kind'], record['destination_id']):
                raise ValueError('Destination association changed')
            if record['status'] in ('preparing', 'ready'):
                status = 'failed'  # Writes require a committed applying row first.
            else:
                current = gitops.snapshot_source_tree(destination)
                if current == record['destination_before_snapshot']:
                    status = 'failed'
                elif (current == record['result_snapshot'] and gitops.snapshot_source_tree(source) == record['source_snapshot']
                      and gitops.source_tree(source, workspace['base_snapshot']) == record['base_snapshot']
                      and all(applygit.matches(destination, p['path'], p['after']) for p in record['plan'])):
                    status = 'applied'
        except (OSError, ValueError, RuntimeError):
            pass
        store.update_integration(record['integration_id'], status,
                                 failure_reason=None if status == 'applied' else 'Interrupted integration; recovery comparison: ' + status)
