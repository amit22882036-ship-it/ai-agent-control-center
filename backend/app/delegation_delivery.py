"""One bounded orchestration pass using existing workspace integration services.

The manager serializes runtime controls for the supplied quiet/integrate/resume
callbacks. This service never chooses new work, providers, children or permissions.
"""
from . import delegation_orchestration as domain, dependencies, integrations, workspace_git
from .task_workspaces import task_lock


class DeliveryService:
    def __init__(self, store, quiet, integrate, resume, publish):
        self.store, self.quiet, self.integrate = store, quiet, integrate
        self.resume, self.publish = resume, publish

    def _result(self, task_id, result):
        key = result['result_id']
        if result['outcome'] != 'completed':
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                domain.owner(db, task_id)
                db.execute("UPDATE delegation_results SET phase='ready' WHERE result_id=? AND phase='recorded'", (key,))
            return
        child_id = result['child_task_id']
        # Parent lock is held by run(); integration uses the same reentrant locks.
        with task_lock(self.store, child_id, blocking=False):
            self.quiet(child_id)
            workspace, source, _, kind, destination_id = integrations.resolve(self.store, child_id)
            if kind != 'task' or destination_id != task_id:
                raise ValueError('Child integration destination changed')
            source_snapshot = workspace_git.snapshot_source_tree(source)
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                _, _, provider = domain.owner(db, task_id)
                child = dependencies.task(db, child_id)
                assignment = db.execute('SELECT * FROM task_assignments WHERE assignment_id=?', (result['assignment_id'],)).fetchone()
                if (child['status'] != 'completed' or child['control_intent'] != 'active'
                        or not assignment or assignment['ended_reason'] != 'completed'
                        or db.execute('SELECT 1 FROM task_assignments WHERE task_id=? AND ended_at IS NULL', (child_id,)).fetchone()):
                    raise ValueError('Child result no longer matches successful Task completion')
                if result['source_snapshot'] and result['source_snapshot'] != source_snapshot:
                    raise ValueError('Child source changed since result validation; inspection required')
                if provider['agent_type'] == 'codex' and provider['sandbox'] != 'workspace-write':
                    base = workspace_git.source_tree(source, workspace['base_snapshot'])
                    if source_snapshot != base:
                        raise ValueError('Read-only Parent does not authorize automatic filesystem integration')
                db.execute('UPDATE delegation_results SET source_snapshot=? WHERE result_id=?', (source_snapshot, key))
            # Existing service owns destination serialization, three-way merge,
            # snapshots, compensation, idempotency and recovery. Even a no-change
            # result has a validated noop journal; no files are modified for it.
            applied = self.integrate(child_id)
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                domain.owner(db, task_id)
                evidence = db.execute('SELECT * FROM integrations WHERE integration_id=?', (applied['integration_id'],)).fetchone()
                if (not evidence or evidence['source_task_id'] != child_id or evidence['destination_id'] != task_id
                        or evidence['destination_kind'] != 'task' or evidence['source_snapshot'] != source_snapshot):
                    raise ValueError('Integration evidence does not match this Child result')
                success = evidence['status'] in ('applied', 'noop')
                db.execute("UPDATE delegation_results SET phase='ready',integration_id=?,last_error=? WHERE result_id=?",
                           (evidence['integration_id'], None if success else 'Integration requires inspection: ' + evidence['status'], key))
                if success:
                    db.execute(f"UPDATE delegations SET status='result_ready',updated_at={domain.NOW} WHERE delegation_id=? AND status='materialized'", (result['delegation_id'],))

    def run(self, task_id):
        # No startup caller. Existing live-run events or an explicit backend pass
        # invoke this; unknown launch phases cannot be reset by a retry.
        with task_lock(self.store, task_id, blocking=False):
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                domain.collect(db, task_id)
                current = domain.state(db, task_id)
                if not current or current['phase'] != 'waiting':
                    return current
                domain.owner(db, task_id)
                results = domain.result_rows(db, task_id)
            self.publish(None)
            self.quiet(task_id)
            for result in results:
                if result['phase'] != 'recorded':
                    continue
                try:
                    self._result(task_id, result)
                except (ValueError, RuntimeError) as exc:
                    with self.store._connection() as db:
                        db.execute('BEGIN IMMEDIATE')
                        domain.owner(db, task_id)
                        # Preserve ambiguous integration journals. Reporting this
                        # failure is permitted; accepting it is not.
                        db.execute("UPDATE delegation_results SET phase='ready',last_error=? WHERE result_id=? AND phase='recorded'",
                                   (str(exc)[:500], result['result_id']))
                self.publish(None)
            with self.store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                launch = domain.prepare_delivery(db, task_id)
            self.publish(None)
            if launch:
                try:
                    self.resume(task_id, launch)
                except Exception:
                    with self.store._connection() as db:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute("UPDATE delegation_continuations SET phase='recovery_required',last_error='Parent resume outcome requires inspection' WHERE task_id=? AND launch_id=? AND phase='launching'",
                                   (task_id, launch['launch_id']))
                    self.publish(None)
                    raise
            with self.store._connection() as db:
                return domain.state(db, task_id)
