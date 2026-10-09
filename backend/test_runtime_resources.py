from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import errno
import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager, runtime_resources as runtime, resource_probes
from app.persistence import AgentStore
import test_resource_probes
from test_resource_probes import occupied, real_probe, AVAILABLE, UNAVAILABLE

real_reserve = resource_probes.reserve_port


class RuntimeResourceTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'create', 'replacement', 'output', 'recover', 'resume',
                  'task_for', 'task', 'workspace', 'git_state', 'waiting', 'control', 'state',
                  'status', 'reasons', 'edge', 'request', 'claim', 'release', 'current',
                  'preflight', 'start_existing'):
        locals()[_name] = getattr(test_resource_probes.ProbeTests, _name)
    del _name

    def setUp(self):
        test_resource_probes.ProbeTests.setUp(self)
        stop = patch.object(manager, '_stop_windows_tree', side_effect=lambda process: setattr(process.poll, 'return_value', 0))
        stop.start()
        self.addCleanup(stop.stop)

    def history(self, claim):
        return self.current(claim)['runtime_ownership']

    def ownership(self, claim):
        return self.current(claim)['current_ownership']

    def test_declaration_is_not_physical_ownership_and_unsupported_is_explicit(self):
        task = self.task()
        claim = self.claim(task, 'thing:one', kind='generic', scope='global')
        self.assertIsNone(self.ownership(claim))
        self.assertFalse(claim['runtime_capabilities']['ownership_verifiable'])
        key, _ = self.start_existing(task)
        row = self.ownership(claim)
        self.assertEqual(row['state'], 'coordination_only')
        self.assertEqual(row['capability'], 'unsupported')
        self.assertEqual(row['agent_id'], key)
        self.reservation.assert_not_called()

    def test_real_tcp_udp_reservation_handoff_and_history(self):
        self.reservation.side_effect = real_reserve
        for protocol in ('tcp', 'udp'):
            with occupied(protocol) as (socket, port):
                socket.close()
                task = self.task()
                claim = self.claim(task, port)
                # Commit hook observes sockets genuinely bound, not just a DB label.
                original = manager._store._connection
                from contextlib import contextmanager
                observed = []
                @contextmanager
                def connection():
                    with original() as db:
                        yield db
                        rows = db.execute("SELECT state FROM resource_ownership WHERE claim_id=?", (claim['claim_id'],)).fetchall()
                        if rows and rows[-1][0] == 'reserved_by_controller':
                            observed.append(real_probe('port', port).status)
                with patch.object(manager._store, '_connection', connection):
                    with runtime.launch(manager._store, task['task_id'], 'future-agent'):
                        self.assertEqual(real_probe('port', port).status, 'available')
                        row = self.ownership(claim)
                        self.assertEqual(row['state'], 'handoff_pending')
                        self.assertIsNotNone(row['reserved_at'])
                        self.assertIsNotNone(row['handoff_at'])
                self.assertIn('unavailable', observed)
                self.assertFalse(self.current(claim)['runtime_capabilities']['worker_handoff'])

    def test_later_bundle_failure_closes_sockets_and_records_no_live_partial_ownership(self):
        with occupied() as (socket, port):
            socket.close()
            task = self.task()
            first = self.claim(task, port)
            second = self.claim(task, 'udp:49159')
            def reserve(stack, key):
                if key == second['resource_key']:
                    raise OSError(errno.EADDRINUSE, 'private OS detail')
                real_reserve(stack, key)
            self.reservation.side_effect = reserve
            with self.assertRaises(ValueError), runtime.launch(manager._store, task['task_id'], 'new-agent'):
                self.fail('Must not spawn partial bundle')
            self.assertEqual(real_probe('port', port).status, 'available')
            self.assertIsNone(self.ownership(first))
            self.assertEqual(self.history(first)[0]['reason'], 'bundle_acquisition_failed')
            self.assertEqual(self.reasons(task), {'external_resource_unavailable'})
            self.assertFalse(self.state(task)['replanning_required'])

    def test_unknown_reservation_failure_blocks_without_spawn(self):
        task = self.task()
        claim = self.claim(task)
        self.reservation.side_effect = OSError(errno.EIO, 'secret details')
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_agent('ignored', task_id=task['task_id'])
        spawn.assert_not_called()
        self.assertEqual(self.current(claim)['probe_status'], 'unknown')
        self.assertNotIn('secret', str(self.current(claim)))

    def test_spawn_failure_compensates_only_its_attempt_and_retry_gets_new_generation(self):
        task = self.task()
        claim = self.claim(task)
        with patch.object(manager, '_spawn_process', side_effect=OSError('spawn failed')), self.assertRaises(OSError):
            manager.start_agent('ignored', task_id=task['task_id'])
        self.assertIsNone(self.ownership(claim))
        self.assertEqual(self.history(claim)[0]['reason'], 'launch_failed')
        key, _ = self.start_existing(task)
        self.assertEqual(self.ownership(claim)['generation'], 2)
        self.assertEqual(self.ownership(claim)['agent_id'], key)

    def test_worker_stop_releases_and_preserves_history(self):
        key, process = self.create()
        task = self.task_for(key)
        claim = self.claim(task, lifetime='worker')
        old = self.ownership(claim)
        manager.stop_agent(key)
        self.assertEqual(self.current(claim)['status'], 'released')
        self.assertIsNone(self.ownership(claim))
        self.assertEqual(self.history(claim)[0]['ownership_id'], old['ownership_id'])
        self.assertEqual(self.history(claim)[0]['reason'], 'stopped')

    def test_failed_stop_retains_ownership_and_blocks_other_task(self):
        key, _ = self.create()
        a = self.task_for(key)
        claim = self.claim(a, lifetime='worker')
        b = self.task()
        waiter = self.claim(b)
        before = self.ownership(claim)
        with patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('stop failed')), self.assertRaises(RuntimeError):
            manager.stop_agent(key)
        self.assertEqual(self.ownership(claim), before)
        self.assertEqual(self.current(waiter)['status'], 'waiting')
        self.assertEqual(manager.agent_statuses[key], 'running')

    def test_task_replacement_new_generation_workspace_and_stale_finalizer(self):
        key, process = self.create()
        task = self.task_for(key)
        workspace = manager._store.get_task_workspace(task['task_id'])
        claim = self.claim(task)
        first = self.ownership(claim)
        manager.stop_agent(key)
        self.assertEqual(self.current(claim)['status'], 'active')
        new, _ = self.start_existing(task)
        second = self.ownership(claim)
        self.assertNotEqual(first['assignment_id'], second['assignment_id'])
        self.assertEqual(second['generation'], first['generation'] + 1)
        self.assertEqual(second['agent_id'], new)
        manager._finalize_process(key, process)
        self.assertEqual(self.ownership(claim), second)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)

    def test_pause_resume_revalidates_and_preserves_history(self):
        key, _ = self.create()
        task = self.task_for(key)
        claim = self.claim(task)
        self.control(task, 'paused')
        self.assertIsNone(self.ownership(claim))
        self.probe.return_value = UNAVAILABLE
        self.control(task, 'active')
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.probe.return_value = AVAILABLE
        self.preflight(task)
        self.start_existing(task)
        self.assertEqual(self.ownership(claim)['generation'], 2)

    def test_cancel_and_complete_release_without_deleting_history(self):
        for action in ('cancel', 'complete'):
            key, _ = self.create()
            task = self.task_for(key)
            claim = self.claim(task, str(49200 + len(manager.agents)))
            if action == 'cancel':
                self.control(task, 'canceled')
            else:
                self.output(key, 'codex\nDone', finish=True)
            self.assertIsNone(self.ownership(claim))
            self.assertEqual(self.history(claim)[-1]['state'], 'released')
            self.assertIsNotNone(manager._store.get_task_workspace(task['task_id']))

    def test_waiting_continuations_keep_unverified_same_assignment_without_self_bind(self):
        key, _, task = self.waiting()
        # Declare before a resumed Worker runs, establishing attribution there.
        claim = self.claim(task)
        self.resume(key, lambda agent: manager.reply_agent(agent, 'continue'))
        before = self.ownership(claim)
        self.output(key, 'CONTROL_CENTER_WAITING: Again?', finish=True)
        self.reservation.reset_mock()
        self.resume(key, lambda agent: manager.decide_agent(agent))
        self.reservation.assert_not_called()
        self.assertEqual(self.ownership(claim)['ownership_id'], before['ownership_id'])
        self.assertEqual(self.ownership(claim)['state'], 'runtime_unverified')

    def test_restart_invalidates_waiting_evidence_and_reply_reacquires(self):
        key, _ = self.create()
        task = self.task_for(key)
        claim = self.claim(task)
        self.output(key, 'session id: ' + key + '\ncodex\nCONTROL_CENTER_WAITING: Which?', finish=True)
        question = manager.agent_waiting_questions[key]
        self.recover()
        self.assertEqual(self.history(claim)[0]['state'], 'ownership_lost')
        self.assertIsNone(self.ownership(claim))
        self.assertEqual(manager.agent_waiting_questions[key], question)
        self.probe.return_value = UNAVAILABLE
        with self.assertRaises(ValueError):
            manager.reply_agent(key, 'yes')
        self.probe.return_value = AVAILABLE
        self.resume(key, lambda agent: manager.reply_agent(agent, 'yes'))
        self.assertEqual(self.ownership(claim)['generation'], 2)

    def test_controller_restart_does_not_fabricate_reservation(self):
        task = self.task()
        claim = self.claim(task)
        with manager._store._connection() as db:
            runtime.insert(db, claim, 'interrupted', None, 'reserved_by_controller', 'test_crash')
        self.recover()
        self.assertIsNone(self.ownership(claim))
        self.assertEqual(self.history(claim)[0]['reason'], 'backend_restart_no_reattachment')
        self.assertEqual(self.state(task)['status'], 'pending')

    def test_legacy_waiting_recovery_requires_fresh_evidence(self):
        key, _, task = self.waiting()
        claim = self.claim(task)
        self.assertEqual(self.history(claim), [])
        self.recover()
        row = self.history(claim)[0]
        self.assertEqual(row['state'], 'ownership_lost')
        self.assertEqual(row['reason'], 'recovery_no_runtime_evidence')
        self.assertEqual(row['verification_status'], 'unknown')
        self.assertIsNotNone(row['ended_at'])
        self.assertIsNone(row['reserved_at'])
        self.probe.return_value = UNAVAILABLE
        with self.assertRaises(ValueError):
            manager.reply_agent(key, 'continue')
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.probe.return_value = AVAILABLE
        self.resume(key, lambda agent: manager.reply_agent(agent, 'continue'))
        self.assertEqual(self.ownership(claim)['generation'], 2)

    def test_boundary_loss_and_dependency_blockers_clear_independently(self):
        task, dependency = self.task(), self.task()
        claim = self.claim(task)
        self.edge(task, dependency)
        self.probe.return_value = UNAVAILABLE
        # A prior failed observation on a suspended claim is eligible for recheck.
        with manager._store._connection() as db:
            from app import external_resources
            external_resources.record(db, claim, UNAVAILABLE)
        self.preflight(task)
        self.assertEqual(self.reasons(task), {'dependency_incomplete', 'external_resource_unavailable'})
        self.probe.return_value = AVAILABLE
        self.preflight(task)
        self.assertEqual(self.reasons(task), {'dependency_incomplete'})

    def test_fairness_and_deadlock_remain_managed_not_physical_claims(self):
        ka, _ = self.create()
        kb, _ = self.create()
        a, b = self.task_for(ka), self.task_for(kb)
        x = self.claim(a, 'thing:x', kind='generic', scope='global')
        self.claim(b, 'thing:y', kind='generic', scope='global')
        self.claim(a, 'thing:y', kind='generic', scope='global')
        self.claim(b, 'thing:x', kind='generic', scope='global')
        self.assertTrue(manager._store.resource_deadlocks(task_id=a['task_id']))
        self.release(x)
        self.assertTrue(all(d['status'] != 'open' for d in manager._store.resource_deadlocks(task_id=a['task_id'])))

    def test_concurrent_attempt_cannot_duplicate_live_generation(self):
        task = self.task()
        claim = self.claim(task)
        with runtime.launch(manager._store, task['task_id'], 'first'):
            def contender():
                with runtime.launch(manager._store, task['task_id'], 'second'):
                    pass
            with ThreadPoolExecutor(max_workers=1) as pool, self.assertRaises(ValueError):
                pool.submit(contender).result()
            self.assertEqual(len(self.history(claim)), 1)

    def test_read_inspection_is_stable_and_never_binds(self):
        task = self.task()
        claim = self.claim(task)
        self.start_existing(task)
        self.reservation.reset_mock()
        before = self.current(claim)
        self.assertEqual(self.current(claim), before)
        self.reservation.assert_not_called()

    def test_cross_tree_and_cross_project_global_ownership_conflicts(self):
        from pathlib import Path
        parent = self.task()
        a, sibling = self.task(parent), self.task(parent)
        other_root = self.task()
        other_branch = self.task(other_root)
        root = Path(self.temp.name) / 'other-project'
        root.mkdir()
        project = manager._store.create_project('Other', root)
        foreign = manager._store.create_task('Foreign work', project_id=project['project_id'])
        claim = self.claim(a)
        self.start_existing(a)
        self.assertEqual(self.ownership(claim)['state'], 'runtime_unverified')
        waiters = [self.claim(t) for t in (sibling, other_root, other_branch, foreign)]
        self.assertTrue(all(c['status'] == 'waiting' for c in waiters))
        self.release(claim)
        self.assertEqual(self.current(waiters[0])['status'], 'active')
        self.assertTrue(all(self.current(c)['status'] == 'waiting' for c in waiters[1:]))
        self.assertIsNone(self.ownership(claim))

    def test_old_ownership_uuid_cannot_release_new_generation(self):
        task = self.task()
        claim = self.claim(task)
        key, _ = self.start_existing(task)
        old = self.ownership(claim)
        manager.stop_agent(key)
        self.start_existing(task)
        current = self.ownership(claim)
        with manager._store._connection() as db:
            runtime.finish(db, old['ownership_id'], 'released', 'late_old_finalizer')
        self.assertEqual(self.ownership(claim), current)
        self.assertEqual(self.history(claim)[0]['reason'], 'stopped')

    def test_reservation_persistence_failure_closes_socket_without_spawning(self):
        with occupied() as (socket, port):
            socket.close()
            task = self.task()
            claim = self.claim(task, port)
            self.reservation.side_effect = real_reserve
            with manager._store._connection() as db:
                db.execute("CREATE TRIGGER fail_runtime BEFORE INSERT ON resource_ownership BEGIN SELECT RAISE(ABORT,'test'); END")
            with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(sqlite3.Error):
                manager.start_agent('ignored', task_id=task['task_id'])
            spawn.assert_not_called()
            self.assertEqual(real_probe('port', port).status, 'available')
            self.assertEqual(self.history(claim), [])

    def test_ipv6_controller_reservation_detects_external_occupancy(self):
        import socket
        self.reservation.side_effect = real_reserve
        with occupied('tcp', socket.AF_INET6) as (_, port):
            task = self.task()
            claim = self.claim(task, port)  # Mock preflight simulates the probe-to-use race.
            with self.assertRaises(ValueError), runtime.launch(manager._store, task['task_id'], 'new'):
                self.fail('Occupied IPv6 port must reject launch')
            self.assertIsNone(self.ownership(claim))
            self.assertEqual(self.current(claim)['probe_status'], 'unavailable')

    def test_handoff_persistence_failure_closes_reservation(self):
        with occupied() as (socket, port):
            socket.close()
            task = self.task()
            claim = self.claim(task, port)
            self.reservation.side_effect = real_reserve
            with manager._store._connection() as db:
                db.execute("CREATE TRIGGER fail_handoff BEFORE UPDATE OF state ON resource_ownership WHEN NEW.state='handoff_pending' BEGIN SELECT RAISE(ABORT,'test'); END")
            with self.assertRaises(sqlite3.Error), runtime.launch(manager._store, task['task_id'], 'new'):
                self.fail('Uncommitted handoff cannot spawn')
            self.assertEqual(real_probe('port', port).status, 'available')
            self.assertIsNone(self.ownership(claim))
            self.assertEqual(self.history(claim)[0]['reason'], 'launch_failed')

    def test_uncertain_commit_cannot_advertise_closed_socket_as_reserved(self):
        from contextlib import contextmanager
        with occupied() as (socket, port):
            socket.close()
            task = self.task()
            claim = self.claim(task, port)
            self.reservation.side_effect = real_reserve
            original = manager._store._connection
            @contextmanager
            def uncertain():
                with original() as db:
                    yield db
                raise sqlite3.OperationalError('uncertain commit result')
            with patch.object(manager._store, '_connection', uncertain), self.assertRaises(sqlite3.Error):
                with runtime.launch(manager._store, task['task_id'], 'new'):
                    self.fail('Unknown commit cannot spawn')
            self.assertEqual(real_probe('port', port).status, 'available')
            row = self.ownership(claim)
            self.assertEqual(row['recorded_state'], 'reserved_by_controller')
            self.assertEqual(row['state'], 'ownership_lost')
            self.assertTrue(row['recovery_required'])
            self.assertFalse(row['controller_reservation_live'])
            with self.assertRaises(ValueError), runtime.launch(manager._store, task['task_id'], 'retry'):
                self.fail('Uncertain attempt must be recovered first')
            self.recover()
            self.start_existing(task)
            self.assertEqual(self.ownership(claim)['generation'], 2)

    def test_bundle_change_before_handoff_rejects_spawn_and_releases_attempt(self):
        from contextlib import contextmanager
        from app import resources, dependencies
        task = self.task()
        claim = self.claim(task)
        original = manager._store._connection
        calls = 0
        @contextmanager
        def changing():
            nonlocal calls
            calls += 1
            if calls == 2:
                with original() as db:
                    db.execute('BEGIN IMMEDIATE')
                    resources.create(db, dependencies.task(db, task['task_id']),
                                     resource_type='port', resource_key='udp:49321')
                    dependencies.reconcile(db)
            with original() as db:
                yield db
        with patch.object(manager._store, '_connection', changing), self.assertRaisesRegex(ValueError, 'bundle changed'):
            with runtime.launch(manager._store, task['task_id'], 'new'):
                self.fail('Changed bundle cannot spawn')
        self.assertIsNone(self.ownership(claim))

    def test_reservation_blocker_publishes_only_after_commit(self):
        task = self.task()
        claim = self.claim(task)
        self.reservation.side_effect = OSError(errno.EADDRINUSE, 'occupied')
        observed = []
        with patch.object(manager.changes, 'publish', side_effect=lambda key: observed.append(
                (self.state(task)['status'], self.current(claim)['probe_status']))):
            with self.assertRaises(ValueError):
                manager.start_agent('ignored', task_id=task['task_id'])
        self.assertEqual(observed[-1], ('blocked', 'unavailable'))

    def test_migration_v15_preserves_every_row_without_fake_history(self):
        self.create()
        self.claim(self.task())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE resource_ownership')
            db.execute('PRAGMA user_version=15')
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: db.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid').fetchall() for t in tables}
        AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 19)
            self.assertEqual(db.execute('SELECT * FROM resource_ownership').fetchall(), [])
            for t in tables:
                self.assertEqual(db.execute(f'SELECT rowid,* FROM {t} ORDER BY rowid').fetchall(), before[t], t)

    def test_v16_migration_failure_rolls_back_and_future_rejected(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP TABLE resource_ownership')
            db.execute('PRAGMA user_version=15')
        original = runtime.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('migration failure')
        with patch.object(runtime, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 15)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='resource_ownership'").fetchone())
            db.execute('PRAGMA user_version=20')
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)


if __name__ == '__main__':
    unittest.main()
