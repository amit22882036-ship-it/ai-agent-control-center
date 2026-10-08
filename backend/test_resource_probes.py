from contextlib import closing, contextmanager
import errno
from pathlib import Path
import re
import socket
import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager, external_resources, resource_probes
from app.persistence import AgentStore
import test_resources

real_probe = resource_probes.probe_resource
AVAILABLE = resource_probes.Observation('available', 'test_available')
UNAVAILABLE = resource_probes.Observation('unavailable', 'test_occupied')


@contextmanager
def occupied(protocol='tcp', family=socket.AF_INET):
    with socket.socket(family, socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM) as sock:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(('127.0.0.1' if family == socket.AF_INET else '::1', 0))
        yield sock, f'{protocol}:{sock.getsockname()[1]}'


class ProbeTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'create', 'replacement', 'output', 'recover',
                  'resume', 'task_for', 'task', 'workspace', 'git_state', 'waiting',
                  'control', 'state', 'status', 'reasons', 'edge', 'request', 'claim',
                  'release', 'current'):
        locals()[_name] = getattr(test_resources.ResourceTests, _name)
    del _name

    def preflight(self, task):
        return manager._store.preflight_task(task['task_id'])

    def start_existing(self, task):
        process = self.replacement()
        with patch.object(manager.subprocess, 'Popen', return_value=process), patch.object(manager, 'Thread'):
            key = manager.start_agent('unused', 'mock', task_id=task['task_id'])
        manager.agent_readers[key].is_alive.return_value = False
        return key, process

    def test_real_tcp_and_udp_occupied_then_free_restore_eligibility(self):
        self.probe.side_effect = real_probe
        for protocol in ('tcp', 'udp'):
            with self.subTest(protocol=protocol), occupied(protocol) as (sock, key):
                task = self.task()
                claim = self.claim(task, key)
                self.assertEqual(claim['status'], 'waiting')
                self.assertEqual(claim['probe_status'], 'unavailable')
                self.assertIsNotNone(claim['probe_checked_at'])
                self.assertEqual(self.state(task)['status'], 'blocked')
                self.assertEqual(self.reasons(task), {'external_resource_unavailable'})
                self.assertFalse(self.state(task)['replanning_required'])
                with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
                    manager.start_agent('x', task_id=task['task_id'])
                spawn.assert_not_called()
                sock.close()
                self.assertEqual(self.preflight(task)['status'], 'pending')
                self.assertEqual(self.current(claim)['probe_status'], 'available')
                self.assertEqual(self.current(claim)['status'], 'active')
                self.assertEqual(self.reasons(task), set())

    def test_ipv6_occupancy_is_not_missed(self):
        self.probe.side_effect = real_probe
        for protocol in ('tcp', 'udp'):
            with self.subTest(protocol=protocol), occupied(protocol, socket.AF_INET6) as (_, key):
                self.assertEqual(self.claim(self.task(), key)['probe_status'], 'unavailable')

    def test_unexpected_socket_failure_is_unknown_and_fail_closed(self):
        self.probe.side_effect = real_probe
        with patch.object(resource_probes.socket, 'socket', side_effect=OSError(errno.EIO, 'private details')):
            task = self.task()
            claim = self.claim(task, 'tcp:49152')
        self.assertEqual(claim['probe_status'], 'unknown')
        self.assertEqual(claim['probe_reason'], 'socket_check_failed')
        self.assertNotIn('private', str(claim))
        self.assertEqual(self.state(task)['status'], 'blocked')

    def test_adapter_exception_is_unknown_and_durable(self):
        self.probe.side_effect = RuntimeError('private adapter exception')
        task = self.task()
        claim = self.claim(task)
        self.assertEqual(claim['probe_status'], 'unknown')
        self.assertEqual(claim['probe_reason'], 'probe_failed')
        self.assertEqual(self.reasons(task), {'external_resource_unavailable'})

    def test_temporary_sockets_close_on_success_and_failure(self):
        # Bind immediately after each successful check: no retained reservation.
        for protocol in ('tcp', 'udp'):
            with occupied(protocol) as (sock, key):
                sock.close()
                self.assertEqual(real_probe('port', key).status, 'available')
                kind = socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM
                with socket.socket(socket.AF_INET, kind) as replacement:
                    replacement.bind(('0.0.0.0', int(key.split(':')[1])))
        original = socket.socket
        made = []
        def failing(family, kind):
            if family == socket.AF_INET6:
                raise OSError(errno.EIO, 'failed second family')
            sock = original(family, kind)
            made.append(sock)
            return sock
        with patch.object(resource_probes.socket, 'socket', side_effect=failing):
            self.assertEqual(real_probe('port', 'tcp:0').status, 'unknown')
        self.assertTrue(all(sock.fileno() == -1 for sock in made))

    def test_unsupported_resources_and_advisory_ports_never_bind(self):
        self.probe.side_effect = real_probe
        with patch.object(resource_probes.socket, 'socket') as sockets:
            for kind, key, options in (
                ('file_path', 'main.py', {}), ('database', 'postgres:local/db', {'scope': 'global'}),
                ('docker_resource', 'container:api', {}), ('generic', 'build:cache', {'scope': 'global'})):
                claim = self.claim(self.task(), key, kind, mode='exclusive', **options)
                self.assertEqual(claim['probe_status'], 'not_supported')
                self.assertEqual(claim['status'], 'active')
            advisory = self.claim(self.task(), mode='advisory')
            self.assertIsNone(advisory['probe_status'])
            sockets.assert_not_called()

    def test_multiple_external_blockers_clear_only_matching_claim(self):
        states = {'tcp:49152': UNAVAILABLE, 'udp:49153': UNAVAILABLE}
        self.probe.side_effect = lambda kind, key: states.get(key, AVAILABLE)
        task = self.task()
        a, b = self.claim(task, 'tcp:49152'), self.claim(task, 'udp:49153')
        self.assertEqual(len(self.state(task)['active_blockers']), 2)
        states['tcp:49152'] = AVAILABLE
        self.preflight(task)
        blockers = self.state(task)['active_blockers']
        self.assertEqual([r['waiting_claim_id'] for r in blockers], [b['claim_id']])
        self.assertEqual(self.current(a)['status'], 'waiting')  # Bundle stays atomic.
        states['udp:49153'] = AVAILABLE
        self.preflight(task)
        self.assertTrue(all(self.current(c)['status'] == 'active' for c in (a, b)))

    def test_external_failure_preserves_fair_queue_and_all_or_none_bundle(self):
        self.probe.return_value = UNAVAILABLE
        first = self.claim(self.task())
        second = self.claim(self.task(), mode='shared')
        self.assertEqual(second['status'], 'waiting')
        self.assertIn('resource_conflict', self.reasons({'task_id': second['task_id']}))
        self.probe.return_value = AVAILABLE
        self.preflight({'task_id': second['task_id']})
        self.assertEqual(self.current(first)['status'], 'active')
        self.assertEqual(self.current(second)['status'], 'waiting')
        self.release(first)
        self.assertEqual(self.current(second)['status'], 'active')

    def test_external_clearing_preserves_managed_and_deadlock_reasons(self):
        a, b = self.task(), self.task()
        retained = self.claim(a, '49152')
        self.claim(a, '49153')
        self.claim(b, '49154')
        self.claim(a, '49154')
        self.claim(b, '49153')
        self.probe.side_effect = lambda kind, key: UNAVAILABLE if key == 'tcp:49152' else AVAILABLE
        self.preflight(a)
        self.assertEqual(self.reasons(a), {'resource_conflict', 'resource_deadlock', 'external_resource_unavailable'})
        self.probe.side_effect = None
        self.probe.return_value = AVAILABLE
        self.preflight(a)
        self.assertEqual(self.reasons(a), {'resource_conflict', 'resource_deadlock'})
        self.assertTrue(self.state(a)['replanning_required'])
        self.assertEqual(self.current(retained)['status'], 'active')

    def test_dependency_and_external_blockers_remain_independent(self):
        task, prerequisite = self.task(), self.task()
        self.probe.return_value = UNAVAILABLE
        claim = self.claim(task)
        self.edge(task, prerequisite)
        self.assertEqual(self.reasons(task), {'dependency_incomplete', 'external_resource_unavailable'})
        self.probe.return_value = AVAILABLE
        self.preflight(task)
        self.assertEqual(self.reasons(task), {'dependency_incomplete'})
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.assertEqual(self.current(claim)['status'], 'suspended')
        self.edge(task, prerequisite, remove=True)
        self.assertEqual(self.current(claim)['status'], 'active')

    def test_explicit_release_clears_only_its_external_blocker(self):
        self.probe.return_value = UNAVAILABLE
        task = self.task()
        first, second = self.claim(task, '49152'), self.claim(task, '49153')
        self.release(first)
        self.assertEqual([b['waiting_claim_id'] for b in self.state(task)['active_blockers']], [second['claim_id']])
        self.release(second)
        self.assertEqual(self.reasons(task), set())
        self.assertEqual(self.state(task)['status'], 'pending')

    def test_cross_project_ports_keep_global_managed_identity(self):
        root = Path(self.temp.name) / 'other'
        root.mkdir()
        project = manager._store.create_project('Other', root)
        a = self.task()
        b = manager._store.create_task('Other task', project_id=project['project_id'])
        self.probe.return_value = UNAVAILABLE
        ca, cb = self.claim(a), self.claim(b)
        self.assertEqual(ca['scope_key'], cb['scope_key'])
        self.assertEqual(self.reasons(a), {'external_resource_unavailable'})
        self.assertEqual(self.reasons(b), {'resource_conflict'})
        self.assertEqual(cb['blocked_by_task_ids'], [a['task_id']])

    def test_waiting_question_and_assignment_restore_through_reply(self):
        key, _, task = self.waiting()
        assignment = manager._store.get_active_assignment_for_task(task['task_id'])
        workspace = manager._store.get_task_workspace(task['task_id'])
        self.probe.return_value = UNAVAILABLE
        self.claim(task)
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.probe.return_value = AVAILABLE
        self.resume(key, lambda agent: manager.reply_agent(agent, 'Continue'))
        self.assertEqual(self.state(task)['status'], 'in_progress')
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager._store.get_active_assignment_for_task(task['task_id']), assignment)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_pause_cancel_and_completed_never_resurrect(self):
        for action in ('paused', 'canceled', 'completed'):
            task = self.task()
            self.probe.return_value = UNAVAILABLE
            self.claim(task)
            if action == 'completed':
                self.status(task, action)
            else:
                self.control(task, action)
            self.probe.return_value = AVAILABLE
            with patch.object(manager, '_spawn_process') as spawn:
                self.assertEqual(self.preflight(task)['status'], action)
                spawn.assert_not_called()
            if action == 'paused':
                self.probe.return_value = UNAVAILABLE
                self.assertEqual(self.control(task, 'active')['status'], 'blocked')

    def test_task_scoped_claim_reprobes_before_replacement(self):
        task = self.task()
        claim = self.claim(task)
        key, _ = self.start_existing(task)
        manager.stop_agent(key)
        self.assertEqual(self.current(claim)['status'], 'active')
        self.probe.return_value = UNAVAILABLE
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_agent('ignored', task_id=task['task_id'])
        spawn.assert_not_called()
        self.assertEqual(self.reasons(task), {'external_resource_unavailable'})
        self.probe.return_value = AVAILABLE
        replacement, _ = self.start_existing(task)
        self.assertNotEqual(replacement, key)
        self.assertEqual(self.current(claim)['claim_id'], claim['claim_id'])

    def test_final_preflight_after_workspace_check_rejects_new_occupancy(self):
        task = self.task()
        self.claim(task)
        original = manager.ensure_workspace_current
        def occupied_during_preparation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.probe.return_value = UNAVAILABLE
            return result
        with patch.object(manager, 'ensure_workspace_current', side_effect=occupied_during_preparation), patch.object(manager, '_spawn_process') as spawn:
            with self.assertRaises(ValueError):
                manager.start_agent('unused', task_id=task['task_id'])
            spawn.assert_not_called()
        self.assertEqual(self.reasons(task), {'external_resource_unavailable'})

    def test_child_start_obeys_its_own_required_claims(self):
        parent = self.task()
        child = self.task(parent)
        self.probe.return_value = UNAVAILABLE
        self.claim(child)
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_agent('unused', task_id=child['task_id'])
        spawn.assert_not_called()
        self.assertEqual(self.state(parent)['status'], 'pending')

    def test_all_same_assignment_continuations_skip_existing_active_claim(self):
        for action in (manager.decide_agent, manager.decide_similar_agent, manager.decide_always_agent):
            with self.subTest(action=action.__name__):
                self.probe.return_value = AVAILABLE
                key, _, task = self.waiting()
                claim = self.claim(task, str(49152 + len(manager.agents)))
                self.probe.reset_mock()
                self.probe.return_value = UNAVAILABLE
                self.resume(key, action)
                self.probe.assert_not_called()
                self.assertEqual(self.current(claim)['status'], 'active')
        self.probe.return_value = AVAILABLE
        key, process = self.create()
        task = self.task_for(key)
        self.output(key, f'session id: {key}\n')
        self.claim(task, '49500')
        self.probe.reset_mock()
        self.probe.return_value = UNAVAILABLE
        with patch.object(manager, '_stop_windows_tree', side_effect=lambda p: setattr(p.poll, 'return_value', 0)):
            self.resume(key, lambda agent: manager.redirect_agent(agent, 'Correct course'))
        self.probe.assert_not_called()

    def test_probe_persistence_failure_rolls_back_before_stop_or_publish(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        self.probe.return_value = UNAVAILABLE
        with manager._store._connection() as db:
            db.execute("CREATE TRIGGER reject_probe BEFORE INSERT ON task_blockers WHEN NEW.reason_code='external_resource_unavailable' BEGIN SELECT RAISE(ABORT,'fail'); END")
        with patch.object(manager.changes, 'publish') as publish, self.assertRaises(sqlite3.Error):
            self.claim(task)
        process.terminate.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(manager._store.resource_claims(task['task_id']), [])

    def test_live_assignment_does_not_conflict_with_own_bound_port(self):
        self.probe.side_effect = real_probe
        key, _ = self.create()
        task = self.task_for(key)
        with occupied() as (sock, port):
            sock.close()
            claim = self.claim(task, port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as owned:
                owned.bind(('127.0.0.1', int(port.split(':')[1])))
                self.probe.reset_mock()
                self.preflight(task)
                duplicate = self.claim(task, port, mode='shared')
                self.assertEqual(duplicate['status'], 'active')
                self.assertIsNone(duplicate['probe_status'])  # No invented observation.
                self.output(key, f'session id: {key}\nCONTROL_CENTER_WAITING: Which option?', True)
                self.resume(key, lambda agent: manager.reply_agent(agent, 'yes'))
                self.probe.assert_not_called()
                self.assertEqual(self.current(claim)['status'], 'active')

    def test_running_claim_commits_before_stop_and_drains(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        self.probe.return_value = UNAVAILABLE
        def stop():
            self.assertIn('external_resource_unavailable', self.reasons(task))
            self.assertTrue(self.state(task)['stop_required'])
            process.poll.return_value = 0
        process.terminate.side_effect = stop
        manager.agent_readers[key].join.side_effect = lambda **kwargs: self.output(key, 'final output\n')
        claim = self.claim(task)
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.assertEqual(claim['status'], 'waiting')
        self.assertEqual(manager._store.list_task_assignments(task['task_id'])[0]['ended_reason'], 'external_resource_unavailable')
        self.assertIn('final output', manager._store.full_output(key))

    def test_failed_stop_retains_worker_claim_and_gate(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        held = self.claim(task, '49152', lifetime='worker')
        self.probe.return_value = UNAVAILABLE
        with patch.object(process, 'terminate', side_effect=OSError('stop failed')), self.assertRaises(OSError):
            self.claim(task, '49153')
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(self.current(held)['status'], 'active')
        self.assertTrue(self.state(task)['stop_required'])
        manager.stop_agent(key)
        self.assertEqual(self.current(held)['status'], 'released')

    def test_stale_finalizer_cannot_mutate_replacement_observations(self):
        key, old = self.create(kind='mock')
        task = self.task_for(key)
        claim = self.claim(task)
        manager.stop_agent(key)
        replacement, _ = self.start_existing(task)
        before = self.current(claim)
        self.probe.return_value = UNAVAILABLE
        manager._finalize_process(key, old)
        self.assertEqual(self.current(claim), before)
        self.assertEqual(manager.agent_statuses[replacement], 'running')

    def test_gets_do_not_probe_reconcile_or_change_observations(self):
        task = self.task()
        self.probe.return_value = UNAVAILABLE
        claim = self.claim(task)
        before = (self.state(task), self.current(claim))
        self.probe.reset_mock()
        self.probe.return_value = AVAILABLE
        for _ in range(2):
            self.request('/tasks/' + task['task_id'])
            self.request('/tasks/' + task['task_id'] + '/resource-claims')
            self.request('/tasks/' + task['task_id'] + '/deadlocks')
        self.probe.assert_not_called()
        self.assertEqual((self.state(task), self.current(claim)), before)

    def test_recovery_never_spawns_and_replacement_uses_fresh_observation(self):
        key, _ = self.create(kind='mock')
        task = self.task_for(key)
        claim = self.claim(task)
        self.probe.return_value = UNAVAILABLE
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
            spawn.assert_not_called()
            with self.assertRaises(ValueError):
                manager.start_agent('ignored', task_id=task['task_id'])
            spawn.assert_not_called()
        self.assertEqual(self.current(claim)['probe_status'], 'unavailable')

    def test_probing_does_not_change_workspace_or_canonical_git(self):
        task = self.task()
        workspace = self.workspace(task)
        path = Path(workspace['workspace_path']) / 'local.txt'
        path.write_bytes(b'keep local work')
        canonical = self.git_state(manager._project_root)
        self.probe.return_value = UNAVAILABLE
        self.claim(task)
        self.probe.return_value = AVAILABLE
        self.preflight(task)
        self.assertEqual(path.read_bytes(), b'keep local work')
        self.assertEqual(self.git_state(manager._project_root), canonical)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def downgrade_v14(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP INDEX external_resource_blocker')
            for column in ('probe_status', 'probe_checked_at', 'probe_reason'):
                db.execute('ALTER TABLE resource_claims DROP COLUMN ' + column)
            db.execute('DROP INDEX task_blockers_active')
            db.execute("CREATE UNIQUE INDEX task_blockers_active ON task_blockers(task_id,blocker_type,COALESCE(source_task_id,''),reason_code) WHERE active=1 AND NOT (reason_code='resource_deadlock' AND deadlock_id IS NOT NULL) AND NOT (blocker_type='resource' AND reason_code='resource_conflict' AND waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL)")
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
            indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE tbl_name='task_assignments' AND type='index' AND sql IS NOT NULL")]
            sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v14', sql, count=1)
            db.execute(sql.replace(",'external_resource_unavailable'", ''))
            columns = ','.join('"' + r[1] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
            db.execute(f'INSERT INTO assignments_v14(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
            db.execute('DROP TABLE task_assignments')
            db.execute('ALTER TABLE assignments_v14 RENAME TO task_assignments')
            for index in indexes:
                db.execute(index)
            db.execute('PRAGMA user_version=14')

    def test_real_v14_migration_preserves_all_rows_and_no_invented_observations(self):
        key, _, a = self.waiting()
        child = self.task(a)
        manager.integrate_task(a['task_id'])
        self.control(child, 'paused')
        b = self.task()
        self.claim(a, '49152')
        self.claim(b, '49153')
        self.claim(a, '49153')
        self.claim(b, '49152')
        self.downgrade_v14()
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            columns = {t: ','.join('"'+r[1]+'"' for r in db.execute(f'PRAGMA table_info({t})')) for t in tables}
            before = {t: db.execute(f'SELECT rowid,{columns[t]} FROM {t} ORDER BY rowid').fetchall() for t in tables}
        AgentStore(self.path)
        with manager._store._connection() as db:
            for t in tables:
                self.assertEqual([tuple(r) for r in db.execute(f'SELECT rowid,{columns[t]} FROM {t} ORDER BY rowid')], before[t], t)
            self.assertTrue(all(tuple(r) == (None, None, None) for r in db.execute('SELECT probe_status,probe_checked_at,probe_reason FROM resource_claims')))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            external_resources.migrate(db)
        self.assertEqual(AgentStore(self.path).load_agents()[0]['session_id'], key)

    def test_migration_rollback_and_future_rejection(self):
        self.claim(self.task())
        self.downgrade_v14()
        original = external_resources.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('failed migration')
        with patch.object(external_resources, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 14)
            self.assertNotIn('probe_status', [r[1] for r in db.execute('PRAGMA table_info(resource_claims)')])
            db.execute('PRAGMA user_version=16')
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 16)
            db.execute('PRAGMA user_version=14')
        AgentStore(self.path)


if __name__ == '__main__':
    unittest.main()
