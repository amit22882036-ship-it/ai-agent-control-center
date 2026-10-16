"""Phase 2.2B: restart evidence and explicit, fail-closed recovery actions.

No migration: evidence is the per-execution supervision marker at its exact
position in the durable output; unknown ownership is an ordinary hard Task
blocker. Every recovery action requires `confirm_ended`, no live handle, and a
clean read-only Windows process scan. None of them launches anything.
"""
import json
import os
import re
import subprocess
from pathlib import Path

from .process_supervision import MARKER

OWNERSHIP_BLOCKER = ('process_ownership', 'process_ownership_unknown')
# Every marker `_resume_agent` writes before [text, supervision marker].
RESUME_MARKERS = ('--- User Reply ---', '--- Redirect ---', '--- Delegated Decision ---',
                  '--- Similar Decisions Enabled ---', '--- Always Decide Enabled ---',
                  '--- Automatic Similar Decision ---', '--- Automatic Always Decision ---')
CONTINUATION_PREFIX = '--- Parent continuation: '
SCAN_TIMEOUT_SECONDS = 30
_EXEC = re.compile(r'(?<![\w-])exec(?![\w-])', re.IGNORECASE)
_LAUNCHERS = ('codex.cmd', 'codex.exe', 'codex.js', '@openai\\codex', '@openai/codex', '\\npm\\codex')


class RecoveryRefused(ValueError):
    """A precondition for an explicit recovery action is not met."""


def _resume_marker(line):
    return line in RESUME_MARKERS or (line.startswith(CONTINUATION_PREFIX) and line.endswith(' ---'))


def supervised_last_execution(lines):
    """True if the latest execution's supervision marker is at its exact expected position.

    Launch: line 0. Resume: [resume marker, text, supervision marker]. Worker
    output is worker-controlled, so a worker printing that exact sequence could
    spoof evidence; the position rule narrows but cannot exclude that.
    """
    for index in range(len(lines) - 1, -1, -1):
        if _resume_marker(lines[index]) and index + 1 < len(lines):
            return index + 2 < len(lines) and lines[index + 2] == MARKER
    return bool(lines) and lines[0] == MARKER


def scan_processes():
    """Read-only Win32_Process snapshot. Any failure raises (never a fallback)."""
    if os.name != 'nt':
        raise RecoveryRefused('Process scan is only available on Windows; recovery refused')
    command = ('Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine '
               '| ConvertTo-Json -Compress')
    shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    try:
        result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=SCAN_TIMEOUT_SECONDS, shell=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise RecoveryRefused('Process scan failed; recovery refused')
        rows = json.loads(result.stdout)
    except RecoveryRefused:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RecoveryRefused(f'Process scan unavailable ({type(exc).__name__}); recovery refused') from None
    rows = rows if isinstance(rows, list) else [rows]
    return [dict(pid=int(r.get('ProcessId') or 0), parent=int(r.get('ParentProcessId') or 0),
                 name=str(r.get('Name') or ''), command=str(r.get('CommandLine') or '')) for r in rows]


def is_codex_execution(process):
    name, command = process['name'].lower(), process['command'].lower()
    return (name in ('node.exe', 'codex.exe', 'cmd.exe') and any(l in command for l in _LAUNCHERS)
            and bool(_EXEC.search(process['command'])))


def unsupervised_codex(processes, supervised_roots):
    """Codex executions that are not descendants of a process this backend supervises."""
    children = {}
    for process in processes:
        children.setdefault(process['parent'], []).append(process['pid'])
    supervised, pending = set(), [pid for pid in supervised_roots if pid]
    while pending:
        pid = pending.pop()
        if pid not in supervised:
            supervised.add(pid)
            pending.extend(children.get(pid, ()))
    return sorted(p['pid'] for p in processes if is_codex_execution(p) and p['pid'] not in supervised)


def require_clean_host(confirm_ended, supervised_roots, scan=scan_processes):
    """All three preconditions; a clean scan is a precondition, not proof."""
    if confirm_ended is not True:
        raise RecoveryRefused('Explicit confirmation {"confirm_ended": true} is required')
    stray = unsupervised_codex(scan(), supervised_roots)
    if stray:
        raise RecoveryRefused('Unsupervised Codex processes are running (PIDs: '
                              + ', '.join(map(str, stray)) + '); recovery refused')
