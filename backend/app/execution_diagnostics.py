"""Diagnostic-only investigation of unresolved executions (Stage 2D.1b P3).

Observations, never authority: nothing here settles an execution, releases
ownership or terminates a process. P2 recovery (kernel and reboot evidence)
stays the only automatic authority; a human attestation (execution_attestation)
may use these observations only to refuse, or to confirm that nothing relevant
was seen.

Each candidate process is classified:

- `owned`: positively associated with the execution: its exact root identity
  (PID plus creation time), or a member of the execution's verified Job.
- `unrelated`: authoritative exclusion: created before the execution started,
  or running in another Windows session than its controller.
- `uncertain`: might belong to it (a descendant by parent PID, a Codex-like
  command line, a child of its controller, a member of an unverified Job, an
  unreadable creation time) and cannot be excluded.

`no_relevant_candidate` means only that this inspection saw none: a clean scan
is never proof that no process exists. Parent PIDs, command lines and output
markers are never treated as proof of ownership or of termination. Command
lines are matched in memory only and never stored: they may carry secrets.
"""
from datetime import datetime, timezone
import os

from . import process_supervision as ps, recovery_actions

CLASSIFICATIONS = ('verified_owned', 'ownership_uncertain', 'no_relevant_candidate', 'unavailable')
FILETIME_EPOCH = 11644473600  # seconds between 1601-01-01 and 1970-01-01
CLOCK_MARGIN = 2 * 10_000_000  # 2 s in FILETIME units: reservation and kernel clocks may differ slightly


def filetime(iso):
    """FILETIME (100 ns since 1601 UTC) of an ISO timestamp, or None."""
    if not iso:
        return None
    try:
        value = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
    except ValueError:
        return None
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int((value.timestamp() + FILETIME_EPOCH) * 10_000_000)


def _inspect_job(name):
    job = ps.Job.open_existing(name, terminate=False)  # query access only
    if job is None:
        return None
    try:
        return {'limits': job.limits(), 'members': job.members(), 'accounting': job.accounting()}
    finally:
        job.close()


class DiagnosticProbe:
    """Read-only observations (replaceable in tests)."""
    process_status = staticmethod(ps.process_status)
    inspect_job = staticmethod(_inspect_job)
    scan = staticmethod(recovery_actions.scan_processes)
    created_at = staticmethod(ps.created_at_of)
    session_of = staticmethod(ps.session_of)


def _candidate(pid, *, source, classification, basis, created_at=None, name=None, parent=None):
    return {'pid': pid, 'created_at': created_at, 'name': name, 'parent': parent, 'source': source,
            'classification': classification, 'basis': basis}


def _descendants(processes, roots):
    children = {}
    for process in processes:
        children.setdefault(process['parent'], []).append(process['pid'])
    seen, pending = set(), [pid for pid in roots if pid]
    while pending:
        pid = pending.pop()
        for child in children.get(pid, ()):
            if child not in seen:
                seen.add(child)
                pending.append(child)
    return seen


def controller_status(controller, current, probe):
    if controller is None:
        return 'missing'
    if str(controller.get('instance_id', '')).startswith('legacy-'):
        return 'legacy'
    if controller.get('pid') == current.get('pid') and controller.get('created_at') is not None \
            and controller.get('created_at') == current.get('created_at'):
        # Only the exact running instance knows (in memory) what it still tracks. An
        # earlier instance inside this same OS process has not been verified to exit.
        return 'self' if controller.get('instance_id') == current.get('instance_id') else 'superseded_in_process'
    return probe.process_status(controller.get('pid'), controller.get('created_at'))


def diagnose(row, controller, current, *, trigger, tracked_roots=(), probe=DiagnosticProbe):
    """Fresh observations for one execution row. Never raises; never changes anything."""
    observed_at = ps._now()
    sources, candidates, complete = {}, [], True
    try:
        sources['controller'] = {'status': controller_status(controller, current, probe),
                                 'provenance': 'kernel: PID plus creation time of the recorded controller'}
        root_pid, root_created = row.get('root_pid'), row.get('root_created_at')
        if root_pid and root_created:
            status = probe.process_status(root_pid, root_created)
            sources['root'] = {'status': status, 'provenance': 'kernel: OpenProcess and GetProcessTimes (PID plus creation time)'}
            if status == 'alive':
                candidates.append(_candidate(root_pid, created_at=root_created, source='root_identity',
                                             classification='owned', basis=['root PID and creation time match the journal']))
            elif status == 'unknown':
                complete = False
                candidates.append(_candidate(root_pid, created_at=root_created, source='root_identity',
                                             classification='uncertain', basis=['root identity could not be checked']))
        else:
            sources['root'] = {'status': 'not_recorded'}
        if row.get('containment') == 'job_kill_on_close' and row.get('job_name'):
            if controller is None or (controller.get('session_id'), controller.get('user_sid')) != \
                    (current.get('session_id'), current.get('user_sid')):
                complete = False
                sources['job'] = {'status': 'not_inspectable',
                                  'reason': 'the Job was created in another Windows session or by another user'}
            else:
                job = probe.inspect_job(row['job_name'])
                if job is None:
                    sources['job'] = {'status': 'not_found',
                                      'provenance': 'kernel: OpenJobObjectW (query access) in this session'}
                else:
                    verified = job['limits'] == row.get('job_config')
                    sources['job'] = {'status': 'found', 'limits_match': verified, 'members': job['members'],
                                      'accounting': job['accounting'],
                                      'provenance': 'kernel: OpenJobObjectW (query access) in this session'}
                    for pid in job['members']:
                        created = probe.created_at(pid)
                        owned = verified and created is not None and root_created is not None \
                            and int(created) >= int(root_created)
                        candidates.append(_candidate(
                            pid, created_at=created, source='job_member', classification='owned' if owned else 'uncertain',
                            basis=['member of the execution\'s verified Job'] if owned
                            else ['member of a Job with this name whose identity is not verified']))
        try:
            processes = probe.scan()
        except Exception as exc:  # A failed scan makes the picture incomplete, never clean.
            complete = False
            sources['scan'] = {'status': 'failed', 'error': f'{type(exc).__name__}: {str(exc)[:200]}'}
        else:
            sources['scan'] = {'status': 'complete', 'processes': len(processes),
                               'provenance': 'heuristic: Win32_Process snapshot; never proof of termination'}
            supervised = _descendants(processes, tracked_roots) | set(tracked_roots)
            from_root = _descendants(processes, [root_pid]) if root_pid else set()
            start = None if row.get('containment') == 'legacy_unknown' else filetime(row.get('created_at'))
            known = {c['pid'] for c in candidates}
            for process in processes:
                pid = process['pid']
                if pid in (0, 4) or pid == os.getpid() or pid in supervised or pid in known:
                    continue
                reasons = []
                if pid in from_root:
                    reasons.append('descendant of the root PID by parent chain (not proof)')
                if recovery_actions.is_codex_execution(process):
                    reasons.append('resembles a Codex execution by name and command line (not proof)')
                if controller is not None and controller.get('pid') and process['parent'] == controller.get('pid'):
                    reasons.append('child of the recorded controller PID (not proof)')
                if not reasons:
                    continue
                created = probe.created_at(pid)
                details = dict(created_at=created, name=process['name'], parent=process['parent'], source='process_scan')
                if created is None:
                    candidates.append(_candidate(pid, classification='uncertain',
                                                 basis=reasons + ['creation time unavailable'], **details))
                elif start is not None and int(created) < start - CLOCK_MARGIN:
                    candidates.append(_candidate(pid, classification='unrelated',
                                                 basis=['created before this execution started'], **details))
                elif controller is not None and controller.get('session_id') is not None \
                        and probe.session_of(pid) not in (None, controller.get('session_id')):
                    candidates.append(_candidate(pid, classification='unrelated',
                                                 basis=['runs in another Windows session than its controller'], **details))
                else:
                    candidates.append(_candidate(pid, classification='uncertain', basis=reasons, **details))
        status = 'complete' if complete else 'incomplete'
    except Exception as exc:
        status = 'failed'
        sources['failure'] = f'{type(exc).__name__}: {str(exc)[:200]}'
    kinds = {c['classification'] for c in candidates}
    if 'owned' in kinds:
        classification = 'verified_owned'
    elif 'uncertain' in kinds:
        classification = 'ownership_uncertain'
    elif status == 'complete':
        classification = 'no_relevant_candidate'
    else:
        classification = 'unavailable'
    return {'execution_id': row['execution_id'], 'trigger': trigger, 'observed_at': observed_at, 'status': status,
            'classification': classification, 'sources': sources, 'candidates': candidates}


REFUSALS = {
    'not_unresolved': 'This execution is not unresolved.',
    'tracked_live': 'This backend still tracks this execution; use Stop.',
    'no_diagnostic': 'No diagnostic has been taken yet.',
    'diagnostic_failed': 'The diagnostic failed; no conclusion is possible.',
    'diagnostics_incomplete': 'Part of the evidence could not be inspected.',
    'verified_owned_alive': 'A live process verified as belonging to this execution exists.',
    'ownership_uncertain_candidate': 'A live process that might belong to this execution cannot be excluded.',
    'job_present': 'Its Job Object still exists; recovery must verify and end it instead.',
    'controller_inconsistent': ('The backend instance that launched it is not this one and has not been verified '
                                'to have exited.'),
}


def eligibility(row, diagnostic, *, tracked=False):
    """Whether a human attestation may be offered. Every reason is a hard refusal."""
    reasons = []
    if row.get('state') != 'uncertain':
        reasons.append('not_unresolved')
    if tracked:
        reasons.append('tracked_live')
    if diagnostic is None:
        reasons.append('no_diagnostic')
    else:
        sources = diagnostic.get('sources') or {}
        if diagnostic['status'] == 'failed':
            reasons.append('diagnostic_failed')
        elif diagnostic['status'] != 'complete':
            reasons.append('diagnostics_incomplete')
        kinds = {c['classification'] for c in diagnostic.get('candidates') or ()}
        if 'owned' in kinds:
            reasons.append('verified_owned_alive')
        if 'uncertain' in kinds:
            reasons.append('ownership_uncertain_candidate')
        if (sources.get('job') or {}).get('status') == 'found':
            reasons.append('job_present')
        if (sources.get('controller') or {}).get('status') not in ('gone', 'self', 'legacy'):
            reasons.append('controller_inconsistent')
    return {'eligible': not reasons, 'reasons': [{'code': code, 'message': REFUSALS[code]} for code in reasons]}
