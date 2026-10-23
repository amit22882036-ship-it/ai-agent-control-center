"""Authoritative execution recovery (Stage 2D.1b P2).

Decides, from durable journal evidence plus fresh kernel observations, whether
an execution of a previous controller (or an unverified one of this controller)
has ended. Only these outcomes settle an execution:

- `ended_by_reboot`: positive evidence that Windows restarted after the
  controller started (boot counter increased AND boot time moved past the
  controller's start). Fast Startup, missing or conflicting values: no evidence.
- `ended_kernel_guaranteed`: the execution's verified kill-on-close Job no
  longer exists in the same session and user namespace, the controller that
  held it has terminated, and the root (PID plus creation time) is gone.
- `ended_verified`: the Job still exists, is verified through one handle as
  ours (limits, members created at or after the root, root among them) and is
  terminated through that same handle with no process remaining.
- `launch_aborted`: only for a never-resumed root outside any Job whose exact
  identity is confirmed gone.

Everything else is `uncertain` with a reason code and keeps every gate held.
A `launching` reservation or a `contained` row is never treated as "did not
run". Process scans and output markers are never evidence. No database
transaction is held while a process is observed or terminated.
"""
from datetime import datetime, timedelta, timezone
import json

from . import executions, process_supervision as ps

BOOT_ADVANCE = timedelta(seconds=60)  # Boot time must move forward clearly, not by tick jitter.


def _time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rebooted_since(controller, current):
    """Positive reboot evidence since `controller` started, or None. Never inferred from one value."""
    try:
        old_id, new_id = int(controller['boot_id']), int(current['boot_id'])
    except (KeyError, TypeError, ValueError):
        return None
    started, old_boot, new_boot = (_time(controller.get('started_at')), _time(controller.get('os_boot_time')),
                                   _time(current.get('os_boot_time')))
    if None in (started, old_boot, new_boot) or new_id <= old_id:
        return None
    if new_boot <= started or new_boot < old_boot + BOOT_ADVANCE:
        return None  # Counter and clock disagree (for example Fast Startup): not evidence.
    return {'boot_id_before': old_id, 'boot_id_now': new_id, 'boot_time_before': controller['os_boot_time'],
            'boot_time_now': current['os_boot_time'], 'controller_started_at': controller['started_at']}




def _uncertain(reason, **details):
    return 'uncertain', reason, details


def _job_evidence(row):
    config = row.get('job_config') or {}
    launch = (row.get('evidence') or {}).get('launch') or {}
    return (row.get('containment') == 'job_kill_on_close' and row.get('job_name') and row.get('root_pid')
            and row.get('root_created_at') and config.get('kill_on_job_close') is True
            and config.get('breakaway_ok') is False and config.get('silent_breakaway_ok') is False
            and launch.get('is_process_in_job') is True)


def _created(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _verify_and_terminate(job, row, probe):
    """Identity checks and termination on the same handle; returns a decision."""
    limits = job.limits()
    if limits != row['job_config']:
        return _uncertain('job_identity_unverified', limits=limits)
    root_created = _created(row['root_created_at'])
    members = job.members()
    root = probe.process_status(row['root_pid'], row['root_created_at'])
    if root == 'unknown':
        return _uncertain('job_identity_unverified', members=members, root='unknown')
    if root == 'alive' and int(row['root_pid']) not in members:
        return _uncertain('job_identity_unverified', members=members, root='alive_outside_job')
    for pid in members:
        if pid == int(row['root_pid']) and root == 'alive':
            continue
        try:
            handle_created = probe.member_created(pid)
        except OSError:
            return _uncertain('job_identity_unverified', members=members, unreadable_member=pid)
        if handle_created is not None and root_created is not None and handle_created < root_created:
            return _uncertain('job_identity_unverified', members=members, older_member=pid)
    try:
        ended = job.terminate()
    except OSError as exc:
        return _uncertain('termination_unconfirmed', members=members, failure=str(exc)[:300])
    return 'ended_verified', None, {'members': members, **ended}


def _member_created(pid):
    """Creation time of a current Job member (None if it exited meanwhile)."""
    handle = ps._OpenProcess(ps.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        if ps.ctypes.get_last_error() == ps.ERROR_INVALID_PARAMETER:
            return None
        raise ps.SupervisionError('Job member could not be opened')
    try:
        return int(ps.process_created_at(handle))
    finally:
        ps._CloseHandle(handle)


class Probe:
    """Fresh, read-mostly kernel observations (replaceable in tests)."""
    process_status = staticmethod(ps.process_status)
    open_job = staticmethod(ps.Job.open_existing)
    terminate_exact = staticmethod(ps.terminate_exact)
    member_created = staticmethod(_member_created)


def decide(row, controller, current, probe=Probe):
    """(state, uncertainty, details) for one open or blocking execution row. Never raises."""
    try:
        return _decide(row, controller, current, probe)
    except Exception as exc:  # A probe failure is never evidence.
        return _uncertain('resolution_failed', failure=f'{type(exc).__name__}: {str(exc)[:200]}')


def _decide(row, controller, current, probe):
    if controller is None:
        return _uncertain('controller_unknown')
    reboot = rebooted_since(controller, current)
    if reboot:
        return 'ended_by_reboot', None, reboot
    if row['containment'] == 'legacy_unknown':
        return _uncertain('legacy')
    if row['state'] in ('launching', 'spawned'):
        # A reservation proves an attempt, not that no process ran or survived.
        return _uncertain('launching_unresolved', job_name=row.get('job_name'))
    own = (controller.get('pid') == current.get('pid') and controller.get('created_at') is not None
           and controller.get('created_at') == current.get('created_at'))
    if row['containment'] == 'none':
        end = (row.get('evidence') or {}).get('end') or {}
        if (row['state'] == 'uncertain' and row.get('uncertainty') == 'launch_abort_unverified'
                and end.get('worker_code_may_have_run') is False and row.get('root_pid') and row.get('root_created_at')):
            # A never-resumed root outside any Job: once its exact identity is
            # gone nothing can remain (it never ran, so it had no descendants).
            if probe.process_status(row['root_pid'], row['root_created_at']) == 'gone' or \
                    probe.terminate_exact(row['root_pid'], row['root_created_at']):
                return 'launch_aborted', None, {'root': 'gone', 'never_resumed': True}
            return _uncertain('root_alive_outside_job')
        # No Job: nothing proves this tree ended (decision 6).
        return _uncertain(row['uncertainty'] if row['state'] == 'uncertain' and row.get('uncertainty')
                          else 'unsupervised_restart')
    if not _job_evidence(row):
        return _uncertain('containment_unverified')
    if (controller.get('session_id'), controller.get('user_sid')) != (current.get('session_id'), current.get('user_sid')) \
            or controller.get('session_id') is None or controller.get('user_sid') is None:
        # Local\\ names are per session: another namespace would wrongly look empty.
        return _uncertain('namespace_mismatch')
    if not own:
        status = probe.process_status(controller.get('pid'), controller.get('created_at'))
        if status != 'gone':
            return _uncertain('controller_state_unknown', controller=status)
    job = probe.open_job(row['job_name'])
    if job is None:
        root = probe.process_status(row['root_pid'], row['root_created_at'])
        if root == 'gone':
            return 'ended_kernel_guaranteed', None, {'job': 'not_found', 'root': 'gone',
                                                     'controller': 'self' if own else 'gone'}
        return _uncertain('root_alive_without_job' if root == 'alive' else 'root_state_unknown')
    try:
        return _verify_and_terminate(job, row, probe)
    finally:
        job.close()


def candidates(db, current_instance, *, agent_ids=None, task_ids=None):
    """Rows needing a decision: open rows of other controllers, and every blocking uncertain row."""
    sql = f'''SELECT * FROM executions WHERE (state IN ('launching','spawned','contained','running')
        AND controller_instance_id<>?) OR ({executions._BLOCKING}) ORDER BY rowid'''
    rows = [executions._row(r) for r in db.execute(sql, (current_instance,))]
    if agent_ids is not None or task_ids is not None:
        agents, tasks = set(agent_ids or ()), set(task_ids or ())
        rows = [r for r in rows if r['agent_id'] in agents or (r['task_id'] and r['task_id'] in tasks)]
    return rows


def resolve(store, current, *, rows=None, probe=Probe, skip=None, sync_blockers=True):
    """Decide each row without holding a transaction, then record each outcome conditionally.

    `current` is this controller's identity row. Rows of this controller are
    included only when the caller passes them (current-controller reconcile).
    Returns [(execution_id, state, uncertainty)].
    """
    if rows is None:
        with store._connection() as db:
            rows = [r for r in candidates(db, current['instance_id'])
                    if r['controller_instance_id'] != current['instance_id']]
    controllers = {}
    results = []
    for row in rows:
        if skip is not None and skip(row):
            continue
        key = row['controller_instance_id']
        if key not in controllers:
            with store._connection() as db:
                controllers[key] = executions.controller(db, key)
        state, uncertainty, details = decide(row, controllers[key], current, probe)
        evidence = {'recovery': {'decided_at': ps._now(), 'by_controller': current['instance_id'],
                                 'outcome': state, 'reason': uncertainty, **details}}
        if state == row['state'] and uncertainty == row.get('uncertainty'):
            results.append((row['execution_id'], state, uncertainty))
            continue  # Nothing new was learned: leave the row (and its history) as it is.
        with store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = executions.transition(db, row['execution_id'], state, (row['state'],), evidence=evidence,
                                            uncertainty=uncertainty, replace_uncertainty=True,
                                            sync_blocker=sync_blockers)
        results.append((row['execution_id'], state if changed else row['state'], uncertainty))
    return results


def classify_legacy(store, current):
    """Durable uncertainty for pre-journal work that may still own a process (idempotent).

    Running Agents with no journaled execution, Tasks with the old
    process_ownership_unknown blocker and removals awaiting confirmation get
    one `legacy_unknown` row under a legacy controller that records the boot
    evidence observed now, so a later verified reboot can settle them. No
    containment evidence is invented.
    """
    from .recovery_actions import OWNERSHIP_BLOCKER
    with store._connection() as db:
        db.execute('BEGIN IMMEDIATE')
        targets = []
        for (agent_id,) in db.execute("SELECT agent_id FROM agents WHERE status='running'").fetchall():
            if not db.execute('SELECT 1 FROM executions WHERE agent_id=?', (agent_id,)).fetchone():
                targets.append((agent_id, 'agent_running_without_journal'))
        for (task_id,) in db.execute('SELECT task_id FROM task_blockers WHERE blocker_type=? AND reason_code=? AND active=1',
                                     OWNERSHIP_BLOCKER).fetchall():
            latest = db.execute('SELECT agent_id FROM task_assignments WHERE task_id=? ORDER BY rowid DESC LIMIT 1',
                                (task_id,)).fetchone()
            # Only a blocker the journal knows nothing about is pre-journal history;
            # a journaled one is decided (and cleared or kept) from its rows.
            if latest and not db.execute('SELECT 1 FROM executions WHERE task_id=? OR agent_id=?',
                                         (task_id, latest[0])).fetchone():
                targets.append((latest[0], 'legacy_process_ownership_unknown'))
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_removal_members'").fetchone():
            for (agent_id, failure) in db.execute("""SELECT agent_id,failure FROM agent_removal_members
                    WHERE phase='recovery_required'""").fetchall():
                code = (failure or '').split(':', 1)[0]
                if code in ('restart_interrupted_execution', 'process_ownership_unknown', 'no_process_handle') \
                        and not db.execute('SELECT 1 FROM executions WHERE agent_id=?', (agent_id,)).fetchone():
                    targets.append((agent_id, 'legacy_removal_confirmation'))
        seen, created = set(), []
        legacy = None
        for agent_id, basis in targets:
            if agent_id in seen:
                continue
            seen.add(agent_id)
            if legacy is None:
                legacy = executions.legacy_controller(db, current)
            created.append(executions.record_legacy(db, legacy, agent_id, basis))
        return created
