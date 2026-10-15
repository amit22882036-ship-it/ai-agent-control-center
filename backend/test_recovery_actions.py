"""Phase 2.2B: restart supervision evidence and explicit, fail-closed recovery actions."""
import json
import os
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, recovery_actions as recovery, process_supervision
from app.task_workspaces import task_lock
import test_delegation_materialization
import test_persistence
import test_replanning
from test_api_security import call, headers
from test_orchestration_wake import Clock

MARKER = process_supervision.MARKER
CLEAN = patch.object(recovery, 'scan_processes', return_value=[])
CONFIRM = json.dumps({'confirm_ended': True}).encode()


def post(path, body=CONFIRM):
    status, _, payload = call('POST', path, headers(content_type='application/json'), body)
    return status, (json.loads(payload) if payload else None)


def codex(pid, parent=1, command=r'"C:\Windows\System32\cmd.exe" /d /s /c ""C:\npm\codex.cmd" exec --sandbox read-only -"'):
    return dict(pid=pid, parent=parent, name='cmd.exe', command=command)


class EvidenceTests(unittest.TestCase):
    def test_launch_and_resume_positions(self):
        self.assertTrue(recovery.supervised_last_execution([MARKER, 'work']))
        self.assertFalse(recovery.supervised_last_execution(['work', MARKER]))
        self.assertFalse(recovery.supervised_last_execution([]))
        history = [MARKER, 'work', '--- User Reply ---', 'answer', MARKER, 'more']
        self.assertTrue(recovery.supervised_last_execution(history))
        self.assertFalse(recovery.supervised_last_execution([MARKER, 'work', '--- Redirect ---', 'text', 'no marker']))
        self.assertTrue(recovery.supervised_last_execution(['--- Parent continuation: reviewing ---', 'prompt', MARKER]))
        # Only the latest execution counts: an older supervised one is not evidence.
        self.assertFalse(recovery.supervised_last_execution([MARKER, '--- Delegated Decision ---', 'x', 'old output']))

    def test_every_resume_marker_written_by_the_manager_is_known(self):
        import re
        with open(manager.__file__, encoding='utf-8') as handle:
            source = handle.read()
        calls = re.findall(r'_resume_agent\((?:[^()]|\([^()]*\))*\)', source, re.S)
        markers = {m for call in calls for m in re.findall(r'["\'](--- [^"\']+?(?: ---)?)["\']', call)}
        self.assertGreaterEqual(len(markers), 8)  # 7 fixed markers plus the Parent continuation prefix
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertTrue(marker in recovery.RESUME_MARKERS or marker == recovery.CONTINUATION_PREFIX.rstrip()
                                or marker.startswith(recovery.CONTINUATION_PREFIX.strip()), marker)


class ScanTests(unittest.TestCase):
    def test_classification(self):
        self.assertTrue(recovery.is_codex_execution(codex(10)))
        self.assertTrue(recovery.is_codex_execution(dict(pid=1, parent=0, name='node.exe',
            command=r'node C:\npm\node_modules\@openai\codex\bin\codex.js exec resume 123 -')))
        for command, name in ((r'C:\Codex\codex.exe app-server --listen stdio://', 'codex.exe'),
                              (r'C:\Codex\codex.exe exec-server --remote x', 'codex.exe'),
                              (r'"C:\npm\codex.cmd" --version', 'cmd.exe'),
                              ('bash -c "echo codex.cmd exec"', 'bash.exe')):
            with self.subTest(command=command):
                self.assertFalse(recovery.is_codex_execution(dict(pid=1, parent=0, name=name, command=command)))

    def test_descendants_of_supervised_processes_are_excluded(self):
        processes = [dict(pid=100, parent=1, name='cmd.exe', command='x'), codex(101, parent=100), codex(200, parent=50)]
        self.assertEqual(recovery.unsupervised_codex(processes, [100]), [200])
        self.assertEqual(recovery.unsupervised_codex(processes, []), [101, 200])

    def test_preconditions(self):
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'confirm_ended'):
            recovery.require_clean_host(False, [], scan=lambda: [])
        with self.assertRaisesRegex(recovery.RecoveryRefused, 'PIDs: 7, 9'):
            recovery.require_clean_host(True, [], scan=lambda: [codex(9), codex(7)])
        recovery.require_clean_host(True, [], scan=lambda: [])

    def test_scan_failure_refuses_without_fallback(self):
        for effect in (OSError('no powershell'), subprocess.TimeoutExpired('powershell', 30)):
            with self.subTest(effect=type(effect).__name__), patch.object(recovery.subprocess, 'run', side_effect=effect):
                with self.assertRaises(recovery.RecoveryRefused):
                    recovery.require_clean_host(True, [])
        with patch.object(recovery.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'Access denied')):
            with self.assertRaisesRegex(recovery.RecoveryRefused, 'scan failed'):
                recovery.require_clean_host(True, [])

    @unittest.skipUnless(os.name == 'nt', 'Windows process scan')
    def test_real_scan_is_read_only_and_parses(self):
        rows = recovery.scan_processes()
        self.assertTrue(any(row['pid'] == os.getpid() for row in rows))
        self.assertTrue(all({'pid', 'parent', 'name', 'command'} <= set(row) for row in rows))


class RestartEvidenceTests(unittest.TestCase):
    setUp = test_persistence.PersistenceTests.setUp
    tearDown = test_persistence.PersistenceTests.tearDown
    replacement = test_persistence.PersistenceTests.replacement
    create = test_persistence.PersistenceTests.create
    recover = test_persistence.PersistenceTests.recover

    def task_of(self, key):
        return manager._store.agent_task_ids(key)[-1]

    def blockers(self, task_id):
        return [b for b in manager._store.get_task(task_id)['active_blockers'] if b['reason_code'] == 'process_ownership_unknown']

    def test_supervised_running_agent_is_stopped_without_block(self):
        with patch.object(manager.process_supervision, 'job_of', return_value=object()):
            key, _ = self.create(kind='mock')
        self.assertEqual(manager.agent_outputs[key][0], MARKER)
        task_id = self.task_of(key)
        self.recover()
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.blockers(task_id), [])

    def test_running_agent_without_evidence_blocks_until_confirmed(self):
        key, _ = self.create(kind='mock')
        task_id = self.task_of(key)
        self.recover()
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(len(self.blockers(task_id)), 1)
        self.assertIn('process_ownership_unknown', manager._store.full_output(key)[-1])
        self.assertEqual(manager._store.get_task(task_id)['status'], 'blocked')
        with self.assertRaises(ValueError):
            manager.start_task_agent(task_id)  # no new worker
        with self.assertRaisesRegex(Exception, 'ownership|refused'):
            manager.integrate_task(task_id)  # no integration
        # Refusals: missing/false confirmation, stray Codex, failed scan, CSRF.
        self.assertEqual(post(f'/tasks/{task_id}/process-ownership/confirm-stopped', b'{}')[0], 422)
        with CLEAN:
            self.assertEqual(post(f'/tasks/{task_id}/process-ownership/confirm-stopped', b'{"confirm_ended": false}')[0], 409)
        with patch.object(recovery, 'scan_processes', return_value=[codex(4242)]):
            status, body = post(f'/tasks/{task_id}/process-ownership/confirm-stopped')
            self.assertEqual(status, 409)
            self.assertIn('4242', body['detail'])
        with patch.object(recovery, 'scan_processes', side_effect=recovery.RecoveryRefused('Process scan failed')):
            self.assertEqual(post(f'/tasks/{task_id}/process-ownership/confirm-stopped')[0], 409)
        self.assertEqual(call('POST', f'/tasks/{task_id}/process-ownership/confirm-stopped',
                              headers(token=None, content_type='application/json'), CONFIRM)[0], 403)
        self.assertEqual(len(self.blockers(task_id)), 1)
        with CLEAN:
            status, body = post(f'/tasks/{task_id}/process-ownership/confirm-stopped')
        self.assertEqual(status, 200, body)
        self.assertEqual(self.blockers(task_id), [])
        self.assertEqual(manager._store.get_task(task_id)['status'], 'pending')
        with CLEAN:
            self.assertEqual(post(f'/tasks/{task_id}/process-ownership/confirm-stopped')[0], 409)  # nothing left


class ResolveLaunchTests(unittest.TestCase):
    setUp = test_delegation_materialization.RuntimeMaterializationTests.setUp
    patch = test_delegation_materialization.RuntimeMaterializationTests.patch
    read = test_delegation_materialization.RuntimeMaterializationTests.read
    finish = test_delegation_materialization.RuntimeMaterializationTests.finish
    accepted = test_delegation_materialization.RuntimeMaterializationTests.accepted

    def phase(self, identifier, phase=None, **values):
        with self.store._connection() as db:
            if phase:
                db.execute('UPDATE delegation_materializations SET phase=? WHERE delegation_id=?', (phase, identifier))
            for column, value in values.items():
                db.execute(f'UPDATE delegation_materializations SET {column}=? WHERE delegation_id=?', (value, identifier))
            return dict(db.execute('SELECT * FROM delegation_materializations WHERE delegation_id=?', (identifier,)).fetchone())

    def test_launch_with_record_resolves_to_started(self):
        _, records = self.accepted()
        identifier = records[0]['delegation_id']
        launched, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(launched['phase'], 'started')
        manager.agents[launched['launch_agent_id']].poll.return_value = 0  # its process ended
        self.phase(identifier, 'recovery_required')
        with CLEAN:
            status, body = post(f'/delegations/{identifier}/recovery/resolve-launch')
        self.assertEqual((status, body['phase']), (200, 'started'), body)

    def test_unrecorded_launch_is_parked_and_never_relaunched_automatically(self):
        _, records = self.accepted()
        identifier = records[0]['delegation_id']
        with patch.object(manager, '_execution_workspace', side_effect=OSError('provision failed')):
            manager.materialize_delegations(self.task['task_id'])
        self.phase(identifier, 'recovery_required', launch_agent_id=None)
        base = len(self.commands)
        with CLEAN:
            status, body = post(f'/delegations/{identifier}/recovery/resolve-launch')
        self.assertEqual((status, body['phase']), (200, 'workspace_ready'), body)
        row = self.phase(identifier)
        self.assertEqual(row['attempts'], manager.delegation_materialization.configured_limits().preparation_attempts)
        # A normal wake and the 2 s / 10 s follow-ups must not launch it.
        clock = Clock()
        with patch.object(manager, '_waker', manager.orchestration_waker.OrchestrationWaker(
                manager._orchestration_pass, thread_factory=clock.thread, timer_factory=clock.timer)):
            manager.wake_orchestration(self.task['task_id'], reason='operator')
            while clock.timers:
                clock.fire()
        self.assertEqual(len(self.commands), base)
        with self.store._connection() as db:
            self.assertNotIn(row['delegation_id'], {r for r in manager.delegation_safety.occupied(db)})
        # The only relaunch path is a separate explicit operator action.
        manager.materialize_delegations(self.task['task_id'], retry=True)
        self.assertEqual(len(self.commands), base + 1)

    def test_refusals(self):
        _, records = self.accepted()
        identifier = records[0]['delegation_id']
        with CLEAN:
            self.assertEqual(post('/delegations/missing/recovery/resolve-launch')[0], 404)
        with patch.object(manager, '_execution_workspace', side_effect=OSError('provision failed')):
            manager.materialize_delegations(self.task['task_id'])
        with CLEAN:
            self.assertEqual(post(f'/delegations/{identifier}/recovery/resolve-launch')[0], 409)  # not recovery_required
        self.phase(identifier, 'recovery_required')
        with patch.object(recovery, 'scan_processes', return_value=[codex(31)]):
            status, body = post(f'/delegations/{identifier}/recovery/resolve-launch')
        self.assertEqual(status, 409)
        self.assertIn('31', body['detail'])
        self.assertEqual(self.phase(identifier)['phase'], 'recovery_required')


class ContinuationRecoveryTests(unittest.TestCase):
    setUp = test_replanning.ReplanningTests.setUp
    patch = test_replanning.ReplanningTests.patch
    stream = test_replanning.ReplanningTests.stream
    finish = test_replanning.ReplanningTests.finish
    launch = test_replanning.ReplanningTests.launch
    plan = test_replanning.ReplanningTests.plan
    message = test_replanning.ReplanningTests.message
    rows = test_replanning.ReplanningTests.rows
    workspace_fixture = test_replanning.ReplanningTests.workspace_fixture
    accepted = test_replanning.ReplanningTests.accepted
    change = test_replanning.ReplanningTests.change
    decision = test_replanning.ReplanningTests.decision
    submit = test_replanning.ReplanningTests.submit
    transitions = test_replanning.ReplanningTests.transitions

    def continuation(self, phase=None):
        with self.store._connection() as db:
            if phase:
                db.execute("UPDATE delegation_continuations SET phase=?,last_error='x' WHERE task_id=?", (phase, self.task['task_id']))
            return manager.delegation_orchestration.state(db, self.task['task_id'])

    def test_rearm_from_recovery_required_and_stopped(self):
        self.process.poll.return_value = 0  # no live Parent handle
        base = len(self.commands)
        for phase in ('recovery_required', 'stopped'):
            with self.subTest(phase=phase):
                self.continuation(phase)
                with CLEAN:
                    status, body = post(f"/tasks/{self.task['task_id']}/recovery/rearm")
                self.assertEqual(status, 200, body)
                current = self.continuation()
                self.assertEqual((current['phase'], current['last_error'], current['launch_id']), ('waiting', None, None))
        self.assertEqual(len(self.commands), base)  # rearm itself never launches

    def test_rearm_refusals(self):
        task = self.task['task_id']
        self.continuation('recovery_required')
        with CLEAN:
            status, body = post(f'/tasks/{task}/recovery/rearm')
        self.assertEqual(status, 409)  # live Parent handle
        self.assertIn('live', body['detail'])
        self.process.poll.return_value = 0
        self.continuation('independent')
        with CLEAN:
            self.assertEqual(post(f'/tasks/{task}/recovery/rearm')[0], 409)
        self.continuation('stopped')
        self.store.request_work_control(task, 'paused')
        with CLEAN:
            self.assertEqual(post(f'/tasks/{task}/recovery/rearm')[0], 409)
        self.assertEqual(self.continuation()['phase'], 'stopped')

    def test_rearm_refuses_a_delivery_that_already_delivered_results(self):
        self.process.poll.return_value = 0
        task = self.task['task_id']
        self.continuation('recovery_required')
        result = str(uuid4())
        with sqlite3.connect(self.store.path) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('''INSERT INTO delegation_results(result_id,delegation_id,child_task_id,assignment_id,agent_id,outcome,
                output_after,output_through,phase,summary) VALUES (?,?,?,?,?,'completed',-1,0,'delivered','s')''',
                (result, str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())))
            db.execute('''INSERT INTO delegation_deliveries(delivery_id,task_id,assignment_id,result_ids,context,status)
                VALUES (?,?,?,?,'{}','recovery_required')''', (str(uuid4()), task, str(uuid4()), json.dumps([result])))
        db.close()
        with CLEAN:
            status, body = post(f'/tasks/{task}/recovery/rearm')
        self.assertEqual(status, 409)
        self.assertIn('delivered', body['detail'])
        self.assertEqual(self.continuation()['phase'], 'recovery_required')

    def test_reverify_moves_only_confirmed_transitions_and_never_stops_anything(self):
        task = self.task['task_id']
        self.submit()
        row, = self.transitions()
        with self.store._connection() as db:
            db.execute("UPDATE replan_transitions SET phase='recovery_required' WHERE transition_id=?", (row['transition_id'],))
        with CLEAN, patch.object(manager, '_settle_tasks', side_effect=AssertionError('no stop')):
            status, body = post(f'/tasks/{task}/recovery/reverify')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['transitions'][0]['phase'], 'recovery_required')  # Child not canceled yet
        with self.store._connection() as db:
            db.execute("UPDATE tasks SET status='canceled',control_intent='canceled',stop_required=0 WHERE task_id=?", (row['task_id'],))
        with CLEAN:
            status, body = post(f'/tasks/{task}/recovery/reverify')
        self.assertEqual(body['transitions'][0]['phase'], 'stopping')
        result, = manager.advance_replanning(task)
        self.assertEqual(result['phase'], 'applied')  # completed by the existing pass
        with CLEAN:
            self.assertEqual(post(f'/tasks/{task}/recovery/reverify')[0], 409)  # nothing left


if __name__ == '__main__':
    unittest.main()
