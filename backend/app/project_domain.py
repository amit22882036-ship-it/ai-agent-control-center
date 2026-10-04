"""Canonical Workspace identity, not filesystem or global-resource isolation."""
import os
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def project_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise ValueError('Project name must contain 1 to 100 characters')
    if any(ord(char) < 32 for char in value):
        raise ValueError('Project name must not contain control characters')
    return value.strip()


def canonical_path(value, *, require_directory=False):
    """Keep a readable absolute root and a platform-specific comparison key."""
    try:
        path = os.fspath(value)
        if not path or not path.strip() or '\0' in path:
            raise ValueError
        root = os.path.normpath(os.path.realpath(os.path.abspath(path)))
        if require_directory and not os.path.isdir(root):
            raise ValueError
        return root, os.path.normcase(root)
    except (OSError, TypeError, ValueError):
        raise ValueError('Project root must be a valid existing directory' if require_directory
                         else 'Invalid Project root path') from None


def contains(root_key, path_key):
    """Comparison keys must come from canonical_path; different drives are disjoint."""
    try:
        return os.path.commonpath((root_key, path_key)) == root_key
    except ValueError:
        return False
