"""Durable fair queue/deadlock regressions; no real providers or servers."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import unittest
from unittest.mock import patch

from app import agent_manager as manager, dependencies, resource_coordination as coordination
from app.persistence import AgentStore
import test_resources


class CoordinationTests(unittest.TestCase):
    # Reuse fixtures without inheriting/discovering the earlier suite twice.
    for _name in ('setUp', 'tearDown', 'create', 'replacement', 'output', 'recover',
                  'resume', 'task_for', 'task', 'workspace', 'git_state', 'waiting',
                  'control', 'state', 'status', 'reasons', 'edge', 'request', 'claim',
                  'release', 'current', 'race'):
        locals()[_name] = getattr(test_resources.ResourceTests, _name)
    del _name

    def incidents(self, task, status='open'):
        return [d for d in manager._store.resource_deadlocks(task_id=task['task_id']) if d['status'] == status]

    def cycle(self, a=None, b=None):
        a, b = a or self.task(), b or self.task()
        x, y = self.claim(a, '8000'), self.claim(b, '8001')
        ay, bx = self.claim(a, '8001'), self.claim(b, '8000')
        return a, b, x, y, ay, bx

    def test_exclusive_barrier_prevents_new_shared_bypass(self):
        s1, s2 = self.claim(self.task(), mode='shared'), self.claim(self.task(), mode='shared')
        e = self.claim(self.task())
        newcomers = [self.claim(self.task(), mode='shared') for _ in range(4)]
        self.assertTrue(all(c['status'] == 'waiting' for c in newcomers))
        self.assertTrue(all(any(b['edge_kind'] == 'queue_precedence' and b['blocking_claim_id'] == e['claim_id'] for b in c['blocked_by']) for c in newcomers))
        self.release(s1)
        self.assertEqual(self.current(e)['status'], 'waiting')
        self.release(s2)
        self.assertEqual(self.current(e)['status'], 'active')
        self.assertTrue(all(self.current(c)['status'] == 'waiting' for c in newcomers))
        self.release(e)
        self.assertTrue(all(self.current(c)['status'] == 'active' for c in newcomers))

    def test_exact_shared_prefix_and_exclusive_queue_sequences(self):
        for modes in (('shared', 'shared', 'exclusive', 'shared'),
                      ('exclusive', 'shared', 'shared'), ('shared', 'exclusive', 'exclusive')):
            with self.subTest(modes=modes):
                owner = self.claim(self.task())
                queue = [self.claim(self.task(), mode=mode) for mode in modes]
                seq = [c['wait_sequence'] for c in queue]
                self.assertEqual(seq, sorted(set(seq)))
                self.release(owner)
                while queue:
                    prefix = 1
                    if queue[0]['mode'] == 'shared':
                        while prefix < len(queue) and queue[prefix]['mode'] == 'shared':
                            prefix += 1
                    self.assertEqual([self.current(c)['status'] for c in queue], ['active'] * prefix + ['waiting'] * (len(queue) - prefix))
                    for claim in queue[:prefix]:
                        self.release(claim)
                    queue = queue[prefix:]

    def test_pause_reentry_gets_new_episode_without_losing_history(self):
        owner = self.claim(self.task())
        task = self.task()
        first = self.claim(task)
        second = self.claim(self.task())
        self.control(task, 'paused')
        self.assertIsNone(self.current(first)['wait_sequence'])
        self.control(task, 'active')
        self.assertGreater(self.current(first)['wait_sequence'], second['wait_sequence'])
        with manager._store._connection() as db:
            history = list(db.execute('SELECT * FROM resource_waits WHERE claim_id=? ORDER BY wait_sequence', (first['claim_id'],)))
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['outcome'], 'suspended')
        self.release(owner)
        self.assertEqual(self.current(second)['status'], 'active')
        self.assertEqual(self.current(first)['status'], 'waiting')

    def test_two_task_cycle_incidents_provenance_and_inspection(self):
        a, b, x, y, ay, bx = self.cycle()
        incident, = self.incidents(a)
        self.assertEqual(incident['task_ids'], sorted((a['task_id'], b['task_id'])))
        self.assertEqual(incident['triggering_claim_id'], bx['claim_id'])
        self.assertEqual({e['edge_kind'] for e in incident['edges']}, {'active_owner'})
        for task in (a, b):
            self.assertEqual(self.state(task)['status'], 'blocked')
            self.assertEqual(self.state(task)['control_intent'], 'active')
            self.assertTrue(self.state(task)['replanning_required'])
            self.assertIn('resource_deadlock', self.reasons(task))
        self.assertEqual(self.current(x)['status'], 'active')
        self.assertEqual(self.current(y)['status'], 'active')
        self.assertEqual(self.current(ay)['deadlock_ids'], [incident['deadlock_id']])
        for _ in range(3):
            with manager._store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                dependencies.reconcile(db)
        self.assertEqual(self.incidents(a), [incident])
        self.assertEqual(self.request('/resource-deadlocks/' + incident['deadlock_id']), incident)
        self.assertEqual(self.request('/tasks/' + a['task_id'] + '/deadlocks')['deadlocks'], [incident])
        self.request('/resource-deadlocks/missing', expected=404)
        self.request('/tasks/missing/deadlocks', expected=404)

    def test_three_task_cross_tree_cycle_and_rotation_canonicalization(self):
        tasks = [self.task(self.task()) for _ in range(3)]
        for i, task in enumerate(tasks):
            self.claim(task, str(8000 + i))
        for i, task in enumerate(tasks):
            self.claim(task, str(8000 + (i + 1) % 3))
        incident, = self.incidents(tasks[0])
        edges = incident['edges']
        self.assertEqual(len(edges), 3)
        self.assertEqual(dict(coordination.cycles(edges)), dict(coordination.cycles(edges[1:] + edges[:1])))

    def test_cross_project_global_deadlock(self):
        a, b = self.task(), self.task()
        # Use a separately registered Project, as in the resource scope fixture.
        root = Path(self.temp.name) / 'other-project'
        root.mkdir()
        project = manager._store.create_project('Other', root)
        with manager._store._connection() as db:
            db.execute('UPDATE tasks SET project_id=? WHERE task_id=?', (project['project_id'], b['task_id']))
        a, b, *_ = self.cycle(a, b)
        self.assertEqual(len(self.incidents(a)), 1)
        self.assertEqual(self.incidents(a), self.incidents(b))

    def test_noncyclic_chain_fanin_advisory_and_self_overlap(self):
        a, b, c, d = [self.task() for _ in range(4)]
        self.claim(c, '8002')
        self.claim(c, '8002', mode='shared')
        self.claim(b, '8001')
        self.claim(b, '8002')
        self.claim(a, '8001')
        self.claim(d, '8001')
        advisory = self.claim(a, '8002', mode='advisory')
        self.assertIsNone(advisory['wait_sequence'])
        self.assertEqual(advisory['blocked_by'], [])
        self.assertTrue(all(not self.incidents(t) for t in (a, b, c, d)))

    def test_overlapping_cycles_resolve_only_their_own_provenance(self):
        a, b, c = [self.task() for _ in range(3)]
        ax = self.claim(a, '8000')
        self.claim(b, '8001')
        self.claim(c, '8002')
        ab = self.claim(a, '8001')
        self.claim(b, '8000')
        self.claim(a, '8002')
        self.claim(c, '8000')
        self.assertGreaterEqual(len(self.incidents(a)), 2)
        self.release(ab)
        self.assertTrue(self.incidents(a))
        self.assertTrue(self.state(a)['replanning_required'])
        self.assertTrue(self.incidents(a, 'resolved'))
        self.release(ax)
        self.assertFalse(self.incidents(a))
        self.assertFalse(self.state(a)['replanning_required'])

    def test_fairness_precedence_is_part_of_deadlock_graph(self):
        a, b, c = [self.task() for _ in range(3)]
        self.claim(a, '8000')
        owner = self.claim(c, '8001', mode='shared')
        self.claim(b, '8001')
        self.claim(a, '8001', mode='shared')
        self.claim(b, '8000')
        incidents = self.incidents(a)
        self.assertTrue(any(any(e['edge_kind'] == 'queue_precedence' for e in d['edges']) for d in incidents))
        self.release(owner)
        self.assertTrue(self.incidents(a))  # Releasing C cannot break the A/B fairness cycle.

    def test_release_pause_cancel_complete_break_cycles_without_autospawn(self):
        for action in ('release', 'paused', 'canceled', 'completed'):
            with self.subTest(action=action):
                a, b, x, y, ay, bx = self.cycle()
                with patch.object(manager.subprocess, 'Popen') as spawn:
                    if action == 'release':
                        self.release(x)
                    elif action == 'completed':
                        self.status(a, 'completed')
                    else:
                        self.control(a, action)
                    spawn.assert_not_called()
                self.assertFalse(self.incidents(a))
                self.assertFalse(self.state(b)['replanning_required'])
                for task in (a, b):
                    for claim in manager._store.resource_claims(task['task_id']):
                        self.release(claim)

    def test_restart_keeps_open_cycle_queue_and_new_arrival_order(self):
        a, b, x, y, ay, bx = self.cycle()
        incident = self.incidents(a)
        order = self.current(ay)['wait_sequence']
        with patch.object(manager.subprocess, 'Popen') as spawn:
            self.recover()
            spawn.assert_not_called()
        self.assertEqual(self.incidents(a), incident)
        self.assertEqual(self.current(ay)['wait_sequence'], order)
        newcomer = self.claim(self.task(), '8001')
        self.assertGreater(newcomer['wait_sequence'], max(order, bx['wait_sequence']))
        self.assertEqual(self.current(x)['status'], 'active')
        self.assertEqual(self.current(y)['status'], 'active')

    def test_waiting_session_restored_after_deadlock_resolves(self):
        key, _, a = self.waiting()
        b = self.task()
        a, b, x, y, ay, bx = self.cycle(a, b)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertIsNotNone(manager._store.get_active_assignment_for_agent(key))
        self.release(bx)
        self.release(y)
        self.assertEqual(self.state(a)['status'], 'waiting')
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager.agent_waiting_questions[key], 'Which option?')

    def test_running_containment_failed_stop_preserves_physical_ownership(self):
        key, process = self.create(kind='mock')
        a = self.task_for(key)
        b = self.task()
        worker_claim = self.claim(a, '8000', lifetime='worker')
        self.claim(b, '8001')
        with patch.object(process, 'terminate', side_effect=OSError('cannot stop')):
            with self.assertRaises(OSError):
                self.claim(a, '8001')
            with self.assertRaises(OSError):
                self.claim(b, '8000')
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertEqual(self.current(worker_claim)['status'], 'active')
        self.assertTrue(self.incidents(a))
        self.assertEqual(self.state(a)['stop_reason'], 'resource_deadlock')
        with manager._state_lock:
            manager._settle_tasks([a['task_id']])
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.current(worker_claim)['status'], 'released')
        self.assertFalse(self.incidents(a))
        self.assertEqual(manager._store.list_task_assignments(a['task_id'])[0]['ended_reason'], 'resource_deadlock')

    def test_atomic_bundle_waits_for_every_resource_and_older_barrier(self):
        a = self.task()
        x, y = self.claim(a, '8000'), self.claim(a, '8001')
        self.control(a, 'paused')
        owner = self.claim(self.task(), '8001')
        older = self.claim(self.task(), '8001')
        self.control(a, 'active')
        self.assertEqual(self.current(x)['status'], 'waiting')
        self.assertEqual(self.current(y)['status'], 'waiting')
        self.release(owner)
        self.assertEqual(self.current(older)['status'], 'active')
        self.assertEqual(self.current(x)['status'], 'waiting')
        self.release(older)
        self.assertEqual(self.current(x)['status'], 'active')
        self.assertEqual(self.current(y)['status'], 'active')

    def test_concurrent_second_claims_create_single_incident(self):
        a, b = self.task(), self.task()
        self.claim(a, '8000')
        self.claim(b, '8001')
        result = self.race(lambda s: s.create_resource_claim(a['task_id'], resource_type='port', resource_key='8001'),
                           lambda s: s.create_resource_claim(b['task_id'], resource_type='port', resource_key='8000'))
        self.assertEqual(result, ['ok', 'ok'])
        self.assertEqual(len(self.incidents(a)), 1)

    def test_release_new_arrival_race_respects_older_waiter(self):
        owner = self.claim(self.task())
        older = self.claim(self.task())
        new = self.task()
        self.race(lambda s: s.release_resource_claim(owner['task_id'], owner['claim_id']),
                  lambda s: s.create_resource_claim(new['task_id'], resource_type='port', resource_key='8000'))
        self.assertEqual(self.current(older)['status'], 'active')
        self.assertEqual(manager._store.resource_claims(new['task_id'])[0]['status'], 'waiting')

    def test_concurrent_release_and_cycle_creation_has_no_phantom_incident(self):
        a, b = self.task(), self.task()
        x, y = self.claim(a, '8000'), self.claim(b, '8001')
        self.claim(a, '8001')
        self.race(lambda s: s.release_resource_claim(y['task_id'], y['claim_id']),
                  lambda s: s.create_resource_claim(b['task_id'], resource_type='port', resource_key='8000'))
        self.assertFalse(self.incidents(a))
        self.assertNotIn('resource_deadlock', self.reasons(b))

    def test_grant_failure_rolls_back_queue_claim_blocker_and_publication(self):
        owner = self.claim(self.task())
        waiter = self.claim(self.task())
        before = self.current(waiter)
        with patch.object(coordination, 'reconcile', side_effect=sqlite3.OperationalError('crash')), patch.object(manager, '_emit_agent_change') as emit:
            with self.assertRaises(sqlite3.OperationalError):
                self.release(owner)
            emit.assert_not_called()
        self.assertEqual(self.current(waiter), before)
        self.assertEqual(self.current(owner)['status'], 'active')
        self.recover()
        self.assertEqual(self.current(waiter), before)
        self.release(owner)
        self.assertEqual(self.current(waiter)['status'], 'active')

    def test_deadlock_resolution_preserves_unrelated_replanning_and_dependency(self):
        a, b, x, y, ay, bx = self.cycle()
        prerequisite = self.task()
        # Dependency suspension legitimately breaks the resource cycle, but
        # neither that resolution nor release may erase the dependency gate.
        self.edge(a, prerequisite)
        self.control(prerequisite, 'canceled')
        self.assertFalse(self.incidents(a))
        self.assertIn('dependency_canceled', self.reasons(a))
        self.assertTrue(self.state(a)['replanning_required'])
        self.release(y)
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.assertTrue(self.state(a)['replanning_required'])

    def test_subset_superset_and_parallel_edge_cycles_are_distinct(self):
        def edge(a, b, claim):
            return dict(waiting_task_id=a, blocking_task_id=b,
                        waiting_claim_id=claim, blocking_claim_id=b, edge_kind='active_owner')
        graph = [edge('A', 'B', 'ab'), edge('B', 'A', 'ba'),
                 edge('B', 'C', 'bc'), edge('C', 'A', 'ca')]
        self.assertEqual(sorted(len(c) for _, c in coordination.cycles(graph)), [2, 3])
        graph.append(edge('A', 'B', 'ab2'))
        self.assertEqual(sorted(len(c) for _, c in coordination.cycles(graph)), [2, 2, 3, 3])

    def test_concurrent_shared_arrivals_cannot_pass_exclusive_barrier(self):
        owner = self.claim(self.task(), mode='shared')
        exclusive = self.claim(self.task())
        tasks = [self.task() for _ in range(3)]
        self.race(*(lambda s, t=t: s.create_resource_claim(t['task_id'], resource_type='port', resource_key='8000', mode='shared') for t in tasks))
        self.assertTrue(all(manager._store.resource_claims(t['task_id'])[0]['status'] == 'waiting' for t in tasks))
        self.release(owner)
        self.assertEqual(self.current(exclusive)['status'], 'active')

    def test_pause_cancel_complete_racing_release_never_grants_inactive_task(self):
        for action in ('paused', 'canceled', 'completed'):
            with self.subTest(action=action):
                owner = self.claim(self.task())
                task = self.task()
                waiter = self.claim(task)
                def control(store):
                    if action == 'completed':
                        with store._connection() as db:
                            db.execute('BEGIN IMMEDIATE')
                            db.execute("UPDATE tasks SET status='completed' WHERE task_id=?", (task['task_id'],))
                            dependencies.reconcile(db)
                    else:
                        store.request_work_control(task['task_id'], action)
                self.race(control, lambda s: s.release_resource_claim(owner['task_id'], owner['claim_id']))
                self.assertEqual(self.current(waiter)['status'], 'suspended' if action == 'paused' else 'released')
                self.assertIsNone(self.current(waiter)['wait_sequence'])

    def test_worker_recovery_releases_claim_and_resolves_cycle_truthfully(self):
        key, process = self.create(kind='mock')
        a = self.task_for(key)
        b = self.task()
        worker = self.claim(a, '8000', lifetime='worker')
        self.claim(b, '8001')
        with patch.object(process, 'terminate', side_effect=OSError('cannot stop')):
            with self.assertRaises(OSError):
                self.claim(a, '8001')
            with self.assertRaises(OSError):
                self.claim(b, '8000')
        incident, = self.incidents(a)
        self.recover()
        self.assertEqual(self.current(worker)['status'], 'released')
        self.assertFalse(self.incidents(a))
        self.assertEqual(self.incidents(a, 'resolved')[0]['deadlock_id'], incident['deadlock_id'])

    def test_deadlock_blocks_every_continuation_and_stale_finalizer(self):
        key, old, a = self.waiting()
        a, b, *_ = self.cycle(a, self.task())
        before = (self.incidents(a), manager._store.resource_claims(a['task_id']))
        with patch.object(manager.subprocess, 'Popen') as spawn:
            for action in (lambda: manager.reply_agent(key, 'yes'),
                           lambda: manager.redirect_agent(key, 'yes'),
                           lambda: manager.decide_agent(key),
                           lambda: manager.decide_similar_agent(key),
                           lambda: manager.decide_always_agent(key),
                           lambda: manager.start_agent('replacement', task_id=a['task_id']),
                           lambda: manager.start_agent('child', parent_id=key)):
                with self.assertRaises(ValueError):
                    action()
            manager._finalize_process(key, old)
            spawn.assert_not_called()
        self.assertEqual((self.incidents(a), manager._store.resource_claims(a['task_id'])), before)

    def test_running_cycle_commits_before_stop_preserves_workspaces_and_task_claims(self):
        ak, ap = self.create(kind='mock')
        bk, bp = self.create(kind='mock')
        a, b = self.task_for(ak), self.task_for(bk)
        workspaces = [manager._store.get_task_workspace(t['task_id']) for t in (a, b)]
        x, y = self.claim(a, '8000'), self.claim(b, '8001')
        self.claim(a, '8001')
        self.assertEqual(manager.agent_statuses[ak], 'stopped')
        original = bp.terminate.side_effect
        def stop():
            self.assertTrue(self.incidents(a))
            self.assertIn('resource_deadlock', self.reasons(b))
            self.assertTrue(self.state(b)['stop_required'])
            return original()
        bp.terminate.side_effect = stop
        self.claim(b, '8000')
        self.assertEqual(manager.agent_statuses[bk], 'stopped')
        self.assertEqual(manager._store.list_task_assignments(b['task_id'])[0]['ended_reason'], 'resource_deadlock')
        self.assertEqual([manager._store.get_task_workspace(t['task_id']) for t in (a, b)], workspaces)
        self.assertEqual(self.current(x)['status'], 'active')
        self.assertEqual(self.current(y)['status'], 'active')
        self.assertTrue(self.incidents(a))
        # Even an exited stale subprocess cannot alter the contained state.
        before = self.incidents(a)
        manager._finalize_process(ak, ap)
        self.assertEqual(self.incidents(a), before)

    def test_restart_shared_exclusive_order_and_suspended_episode(self):
        owner = self.claim(self.task(), mode='shared')
        exclusive = self.claim(self.task())
        shared = self.claim(self.task(), mode='shared')
        paused = self.task()
        suspended = self.claim(paused)
        self.control(paused, 'paused')
        self.recover()
        self.assertEqual(self.current(exclusive)['wait_sequence'], exclusive['wait_sequence'])
        self.assertEqual(self.current(shared)['wait_sequence'], shared['wait_sequence'])
        self.assertIsNone(self.current(suspended)['wait_sequence'])
        self.release(owner)
        self.assertEqual(self.current(exclusive)['status'], 'active')
        self.assertEqual(self.current(shared)['status'], 'waiting')
        self.release(exclusive)
        self.assertEqual(self.current(shared)['status'], 'active')

    def downgrade_v13(self):
        """Materialize the actual v13 shape, including the old assignment CHECK."""
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DROP INDEX external_resource_blocker')
            for column in ('probe_status', 'probe_checked_at', 'probe_reason'):
                db.execute('ALTER TABLE resource_claims DROP COLUMN ' + column)
            for table, kind in (('task_blockers', 'blocker_type'), ('task_replan_reasons', 'reason_type')):
                db.execute(f'DROP INDEX {table}_active')
                db.execute(f'DROP INDEX {table}_deadlock')
                db.execute(f'ALTER TABLE {table} DROP COLUMN deadlock_id')
                resource = " AND NOT (blocker_type='resource' AND reason_code='resource_conflict' AND waiting_claim_id IS NOT NULL AND owning_claim_id IS NOT NULL)" if table == 'task_blockers' else ''
                db.execute(f"CREATE UNIQUE INDEX {table}_active ON {table}(task_id,{kind},COALESCE(source_task_id,''),reason_code) WHERE active=1{resource}")
            db.execute('ALTER TABLE task_blockers DROP COLUMN edge_kind')
            for table in ('resource_deadlock_members', 'resource_deadlocks', 'resource_waits'):
                db.execute('DROP TABLE ' + table)
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_assignments'").fetchone()[0]
            indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='task_assignments' AND sql IS NOT NULL")]
            sql = re.sub(r'CREATE TABLE "?task_assignments"?', 'CREATE TABLE assignments_v13', sql, count=1)
            db.execute(sql.replace(",'resource_deadlock'", '').replace(",'external_resource_unavailable'", ''))
            columns = ','.join('"' + r[1] + '"' for r in db.execute('PRAGMA table_info(task_assignments)'))
            db.execute(f'INSERT INTO assignments_v13(rowid,{columns}) SELECT rowid,{columns} FROM task_assignments')
            db.execute('DROP TABLE task_assignments')
            db.execute('ALTER TABLE assignments_v13 RENAME TO task_assignments')
            for index in indexes:
                db.execute(index)
            db.execute('PRAGMA user_version=13')

    def test_real_v13_migration_preserves_all_old_rows_backfills_only_waiters(self):
        key, _, task = self.waiting()
        child = self.task(task)
        manager.integrate_task(task['task_id'])
        self.control(child, 'paused')
        owner = self.claim(task)
        one, two = self.claim(self.task()), self.claim(self.task(), mode='shared')
        suspended = self.claim(child, '8002')
        self.release(self.claim(self.task(), '8003'))
        self.downgrade_v13()
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'sqlite_sequence'")]
            columns = {t: ','.join('"' + r[1] + '"' for r in db.execute(f'PRAGMA table_info({t})')) for t in tables}
            before = {t: db.execute(f'SELECT rowid,{columns[t]} FROM {t} ORDER BY rowid').fetchall() for t in tables}
        store = AgentStore(self.path)
        with store._connection() as db:
            for t in tables:
                self.assertEqual([tuple(r) for r in db.execute(f'SELECT rowid,{columns[t]} FROM {t} ORDER BY rowid')], before[t], t)
            waits = list(db.execute('SELECT * FROM resource_waits ORDER BY wait_sequence'))
            self.assertEqual([r['claim_id'] for r in waits], [one['claim_id'], two['claim_id']])
            self.assertTrue(all(r['waiting_since'] is None for r in waits))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM resource_deadlocks').fetchone()[0], 0)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 18)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            coordination.migrate(db)  # Explicit idempotency, including backfill.
            self.assertEqual(len(list(db.execute('SELECT * FROM resource_waits'))), 2)
        self.assertEqual(AgentStore(self.path).list_tasks(), store.list_tasks())
        self.assertEqual(self.current(owner)['status'], 'active')
        self.assertEqual(self.current(suspended)['status'], 'suspended')

    def test_v14_migration_rollback_and_future_version_rejection(self):
        self.claim(self.task())
        self.claim(self.task())
        self.downgrade_v13()
        original = coordination.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('interrupted migration')
        with patch.object(coordination, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 13)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='resource_waits'").fetchone())
            self.assertNotIn('deadlock_id', [r[1] for r in db.execute('PRAGMA table_info(task_blockers)')])
            db.execute('PRAGMA user_version=19')
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)
        # Restore valid fixture for teardown only, after asserting rejection.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('PRAGMA user_version=13')
        AgentStore(self.path)


if __name__ == '__main__':
    unittest.main()
