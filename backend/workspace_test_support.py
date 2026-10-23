"""Temporary repositories only: tests never register worktrees in the developer repo."""
from pathlib import Path
import tempfile
from unittest.mock import patch

from app import agent_manager as manager
from app import workspace_git
from execution_test_support import use_attested_test_doubles


def make_repository(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    workspace_git.git(path, 'init')
    (path / 'source.txt').write_text('original\n', encoding='utf-8')
    workspace_git.git(path, 'add', 'source.txt')
    workspace_git.git(path, '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                      '-c', 'commit.gpgSign=false', 'commit', '-m', 'Fixture')
    return path


def process_test_setup(test):
    use_attested_test_doubles(test)  # explicit test-only evidence for test doubles
    # These are isolated process/protocol unit tests, not workspace integration
    # tests. The workspace boundary is stubbed explicitly (no production bypass).
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    root = Path(temp.name)
    canonical = root / 'canonical'
    canonical.mkdir()
    test.workspace_path = root / 'workspace'
    test.workspace_path.mkdir()
    for name, value in (('_project_root', canonical), ('_execution_workspace', None)):
        mock = patch.object(manager, name, return_value=test.workspace_path) if value is None else patch.object(manager, name, value)
        mock.start()
        test.addCleanup(mock.stop)


def worker_output(lines):
    """Worker output without per-execution supervision markers (asserted separately).

    The unverified-end marker is shown as soon as the end cannot be confirmed,
    so it may precede the last lines its reader has not delivered yet.
    """
    from app.agent_manager import UNVERIFIED_END_MARKER
    from app.process_supervision import MARKER
    return [line for line in lines if line not in (MARKER, UNVERIFIED_END_MARKER)]
