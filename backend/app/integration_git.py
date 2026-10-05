"""Isolated three-way source reconciliation and guarded filesystem-only apply."""
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from . import workspace_git as gitops


def tree_entries(root, tree):
    entries = {}
    for item in gitops.git(root, 'ls-tree', '-rz', tree).split(b'\0'):
        if item:
            meta, raw = item.split(b'\t', 1)
            mode, kind, oid = meta.decode().split()
            name = os.fsdecode(raw)
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise ValueError('Unsupported integration source entry')
            safe_path(root, name)
            entries[name] = {'mode': mode, 'oid': oid}
    return entries


def safe_path(root, name):
    parts = PurePosixPath(name).parts
    if not parts or any(p in ('.', '..') for p in parts) or PurePosixPath(name).is_absolute() or gitops.excluded(name):
        raise ValueError('Unsupported integration source path')
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root) or any(p.is_symlink() for p in (path, *path.parents) if p != root):
        raise ValueError('Integration refuses symbolic links or paths outside the destination')
    return path


def blob(root, entry):
    return gitops.git(root, 'cat-file', 'blob', entry['oid'])


def build_candidate(root, base, source, destination, area):
    """Per-path three-way merge, including Git's real textual merge for both-edited files.

    Renames are represented as delete/add. Ambiguous delete/modify, add/add,
    binary, mode, or directory/file cases conflict instead of choosing a side.
    """
    b, l, u = (tree_entries(root, tree) for tree in (base, source, destination))
    result, conflicts = {}, []
    for name in sorted(b.keys() | l.keys() | u.keys()):
        before, local, upstream = b.get(name), l.get(name), u.get(name)
        if local == before:
            chosen = upstream
        elif upstream == before or local == upstream:
            chosen = local
        elif before and local and upstream and before['mode'] == local['mode'] == upstream['mode']:
            paths = [Path(area) / part for part in ('destination', 'base', 'source')]
            for path, entry in zip(paths, (upstream, before, local)):
                path.write_bytes(blob(root, entry))
            code, content = gitops.git(root, 'merge-file', '--stdout', '--', *(str(p) for p in paths),
                                      accepted=tuple(range(128)) + (255,), with_status=True)
            if code:
                conflicts.append(name)
                continue
            chosen = {'mode': before['mode'], 'oid': gitops.git(root, 'hash-object', '-w', '--stdin', data=content).decode().strip()}
        else:
            conflicts.append(name)
            continue
        if chosen:
            result[name] = chosen
    for name in result:
        if any(str(parent) in result for parent in PurePosixPath(name).parents if str(parent) != '.'):
            conflicts.append(name)
    if conflicts:
        return None, sorted(set(conflicts))
    env = {'GIT_INDEX_FILE': str(Path(area) / 'candidate-index')}
    gitops.git(root, 'read-tree', '--empty', extra_env=env)
    data = b''.join(f"{e['mode']} {e['oid']}\t".encode() + os.fsencode(n) + b'\0' for n,e in sorted(result.items()))
    gitops.git(root, 'update-index', '-z', '--index-info', data=data, extra_env=env)
    return gitops.git(root, 'write-tree', extra_env=env).decode().strip(), []


def prepare_apply(root, before, result, area):
    old, new = tree_entries(root, before), tree_entries(root, result)
    plan = []
    for name in sorted(old.keys() | new.keys()):
        if old.get(name) == new.get(name):
            continue
        target = safe_path(root, name)
        if not old.get(name) and (target.exists() or target.is_symlink()):
            raise ValueError('Integration would overwrite an excluded or unmanaged destination path')
        for parent in target.parents:
            if parent == Path(root):
                break
            if parent.exists() and not parent.is_dir():
                raise ValueError('Unsupported directory/file transition')
        entry = {'path': name, 'before': old.get(name), 'after': new.get(name),
                 'created_parents': [str(p.relative_to(root)).replace('\\','/') for p in target.parents if p != Path(root) and p.is_relative_to(root) and not p.exists()]}
        # Both pre-images and result blobs are durable Git objects. Fully stage
        # replacement bytes outside the destination before recording applying.
        if entry['after']:
            (Path(area) / str(len(plan))).write_bytes(blob(root, entry['after']))
        plan.append(entry)
    return plan


def matches(root, name, entry):
    path = safe_path(root, name)
    if entry is None:
        return not path.exists() and not path.is_symlink()
    if not path.is_file() or path.read_bytes() != blob(root, entry):
        return False
    return os.name == 'nt' or bool(path.stat().st_mode & stat.S_IXUSR) == (entry['mode'] == '100755')


_UNCHECKED = object()


def write_entry(root, name, entry, content=None, expected=_UNCHECKED):
    path = safe_path(root, name)
    if entry is None:
        if expected is not _UNCHECKED and not matches(root, name, expected):
            raise ValueError('Destination changed before deletion')
        path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # A same-volume temporary sibling enables atomic replacement of each file;
    # the complete multi-file operation remains covered by the durable journal.
    fd, temporary = tempfile.mkstemp(prefix='.control-center-apply-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(blob(root, entry) if content is None else content)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != 'nt':
            os.chmod(temporary, 0o755 if entry['mode'] == '100755' else 0o644)
        if expected is not _UNCHECKED and not matches(root, name, expected):
            raise ValueError('Destination changed before replacement')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ApplyFailure(RuntimeError):
    def __init__(self, restored):
        super().__init__('Destination apply failed; restored pre-images' if restored else 'Destination apply requires recovery')
        self.restored = restored


def apply_plan(root, plan, area, verify=None):
    changed = []
    try:
        for index, entry in enumerate(plan):
            if not matches(root, entry['path'], entry['before']):
                raise ValueError('Destination path changed during apply')
            # Include the operation before entering it: a failure after rename
            # must still be checked/rolled back, not silently forgotten.
            changed.append(entry)
            content = (Path(area) / str(index)).read_bytes() if entry['after'] else None
            write_entry(root, entry['path'], entry['after'], content, entry['before'])
        if verify:
            verify()
    except Exception as exc:
        restored = True
        for entry in reversed(changed):
            try:
                if matches(root, entry['path'], entry['before']):
                    continue
                if not matches(root, entry['path'], entry['after']):
                    restored = False  # Preserve concurrent external work.
                    continue
                write_entry(root, entry['path'], entry['before'], expected=entry['after'])
            except Exception:
                restored = False
        for entry in reversed(changed):
            for name in entry.get('created_parents', []):
                try:
                    safe_path(root, name).rmdir()
                except OSError:
                    pass  # Never remove another actor's files.
        raise ApplyFailure(restored) from exc
