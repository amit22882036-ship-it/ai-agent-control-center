from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
from threading import Barrier
import unittest
from unittest.mock import patch
from uuid import UUID

from app import agent_manager as manager, work_intents
from app.persistence import AgentStore
import test_runtime_resources


class WorkIntentTests(unittest.TestCase):
    for _name in ('setUp', 'tearDown', 'create', 'replacement', 'output', 'recover', 'resume',
                  'task_for', 'task', 'workspace', 'git_state', 'waiting', 'control', 'state',
                  'status', 'reasons', 'edge', 'request', 'claim', 'release', 'current',
                  'preflight', 'start_existing'):
        locals()[_name] = getattr(test_runtime_resources.RuntimeResourceTests, _name)
    del _name

    def intent(self, task, key='auth', namespace='component', **options):
        return manager.create_work_intent(task['task_id'], namespace=namespace, key=key, **options)

    def intents(self, task):
        return manager._store.work_intents(task['task_id'])

    def overlaps(self, task):
        return manager._store.work_intents(task['task_id'], overlaps=True)

    def drop(self, intent):
        return manager.release_work_intent(intent['task_id'], intent['intent_id'])

    def other_project(self):
        root = Path(self.temp.name) / 'other'
        root.mkdir()
        project = manager._store.create_project('Other', root)
        return manager._store.create_task('Other work', project_id=project['project_id'])

    def test_normalization_is_logical_deterministic_and_bounded(self):
        a = self.intent(self.task(), ' / Auth / LOGIN / ', namespace=' Component ')
        self.assertEqual(a['normalized_key'], 'auth/login')
        self.assertEqual(a['namespace'], 'component')
        UUID(a['intent_id'])
        for key in ('', '/', 'a//b', 'a/ /b', '../auth', 'auth/..', './auth', 'a\\b',
                    'C:/auth', '%2e%2e/auth', 'a\x00b', 'a'*65, '/'.join(['a'*64]*5)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.intent(self.task(), key)
        for namespace in ('', 'component/auth', '../x', 'x'*65, 'x:y'):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                self.intent(self.task(), namespace=namespace)

    def test_advisory_roots_do_not_change_task_state_dependencies_or_resources(self):
        ka, _ = self.create()
        kb, _ = self.create()
        a, b = self.task_for(ka), self.task_for(kb)
        self.intent(a)
        before = self.state(a)
        with patch.object(manager, '_stop_windows_tree') as stop:
            self.intent(b, 'auth/login')
        stop.assert_not_called()
        self.assertEqual(self.state(a), before)
        self.assertEqual(self.state(b)['status'], 'in_progress')
        self.assertFalse(self.state(b)['replanning_required'])
        self.assertEqual(manager._store.list_dependencies(b['task_id']), [])
        self.assertEqual(self.overlaps(a)[0]['classification'], 'advisory')

    def test_exact_hierarchy_segment_boundaries_namespace_and_same_task(self):
        a, b = self.task(), self.task()
        self.intent(a, 'auth')
        self.intent(b, 'auth')
        self.intent(b, 'auth/login')
        self.intent(b, 'authentication')
        self.intent(b, 'auth', namespace='concern')
        self.assertEqual({r['relationship'] for r in self.overlaps(a)}, {'equal', 'ancestor'})
        self.intent(a, 'auth/session', mode='single_owner')
        self.assertTrue(all(r['other_task_id'] == b['task_id'] for r in self.overlaps(a)))
        lone = self.task()
        self.intent(lone, 'own', mode='single_owner')
        self.intent(lone, 'own/child', mode='single_owner')
        self.assertEqual(self.overlaps(lone), [])
        self.assertEqual(self.state(lone)['status'], 'pending')

    def test_different_projects_do_not_overlap_but_global_ports_still_conflict(self):
        a, b = self.task(), self.other_project()
        self.intent(a, mode='single_owner')
        self.intent(b, mode='single_owner')
        self.assertEqual(self.overlaps(a), [])
        self.assertEqual(self.state(b)['status'], 'pending')
        self.claim(a)
        self.claim(b)
        self.assertIn('resource_conflict', self.reasons(b))

    def test_later_single_owner_is_gated_by_earlier_advisory(self):
        a, b = self.task(), self.task()
        owner = self.intent(a)
        waiter = self.intent(b, 'auth/login', mode='single_owner')
        self.assertEqual(self.state(b)['status'], 'blocked')
        blocker = self.state(b)['active_blockers'][0]
        self.assertEqual(blocker['waiting_intent_id'], waiter['intent_id'])
        self.assertEqual(blocker['owning_intent_id'], owner['intent_id'])
        self.assertEqual(blocker['reason_code'], 'work_intent_conflict')
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_agent('ignored', task_id=b['task_id'])
        spawn.assert_not_called()
        with patch.object(manager, '_spawn_process') as spawn:
            self.drop(owner)
        spawn.assert_not_called()
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(self.intents(b)[0]['intent_id'], waiter['intent_id'])
        self.start_existing(b)

    def test_later_advisory_is_gated_only_by_explicit_single_owner(self):
        a, b = self.task(), self.task()
        self.intent(a, mode='single_owner')
        self.intent(b)
        self.assertEqual(self.reasons(b), {'work_intent_conflict'})
        self.assertFalse(self.state(b)['replanning_required'])

    def test_waiter_precedence_prevents_promotion_from_preempting_later_running_work(self):
        a, b, c = self.task(), self.task(), self.task()
        first = self.intent(a, 'auth/login', mode='single_owner')
        second = self.intent(b, 'auth', mode='single_owner')
        self.intent(c, 'auth/payments', mode='single_owner')
        self.assertEqual(self.state(c)['status'], 'blocked')
        self.drop(first)
        self.assertEqual(self.state(b)['status'], 'pending')
        self.assertEqual(self.state(c)['status'], 'blocked')
        self.drop(second)
        self.assertEqual(self.state(c)['status'], 'pending')

    def test_siblings_and_undelegated_parent_child_conflict(self):
        parent = self.task()
        a, b = self.task(parent), self.task(parent)
        self.intent(a, mode='single_owner')
        self.intent(b)
        self.intent(parent)
        self.assertEqual(self.state(b)['status'], 'blocked')
        self.assertEqual(self.state(parent)['status'], 'blocked')
        self.assertEqual(self.overlaps(b)[0]['classification'], 'blocking')

    def test_explicit_delegation_and_recursive_chain_allow_ancestors_not_siblings(self):
        parent = self.task()
        a, b = self.task(parent), self.task(parent)
        grandchild = self.task(a)
        source = self.intent(parent, mode='single_owner')
        delegated = self.intent(a, 'auth/login', mode='single_owner', delegated_from_intent_id=source['intent_id'])
        self.intent(grandchild, 'auth/login/form', delegated_from_intent_id=delegated['intent_id'])
        self.assertEqual(self.state(a)['status'], 'pending')
        self.assertEqual(self.state(grandchild)['status'], 'pending')
        self.assertTrue(all(r['classification'] == 'delegated' for r in self.overlaps(grandchild)))
        self.intent(b, 'auth/login', delegated_from_intent_id=source['intent_id'])
        self.assertEqual(self.state(b)['status'], 'blocked')
        classifications = {r['other_task_id']: r['classification'] for r in self.overlaps(b)}
        self.assertEqual(classifications[parent['task_id']], 'delegated')
        self.assertEqual(classifications[a['task_id']], 'blocking')
        self.assertEqual(classifications[grandchild['task_id']], 'advisory')

    def test_invalid_delegation_is_atomic(self):
        parent = self.task()
        child, unrelated, foreign = self.task(parent), self.task(), self.other_project()
        source = self.intent(parent, 'auth/login')
        foreign_source = self.intent(foreign)
        cases = [(unrelated, 'auth/login', 'component', source['intent_id']),
                 (child, 'auth', 'component', source['intent_id']),
                 (child, 'auth/login', 'concern', source['intent_id']),
                 (child, 'auth/login', 'component', foreign_source['intent_id']),
                 (child, 'auth/login', 'component', 'missing')]
        for task, key, namespace, identifier in cases:
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.intent(task, key, namespace, delegated_from_intent_id=identifier)
        self.assertEqual(self.intents(child), [])
        self.assertEqual(self.intents(unrelated), [])

    def test_idempotent_normalized_create_release_and_release_history(self):
        task = self.task()
        a = self.intent(task, 'Auth/Login')
        b = self.intent(task, ' / AUTH / login / ')
        self.assertEqual(a, b)
        released = self.drop(a)
        self.assertEqual(released, self.drop(a))
        self.assertEqual(released['status'], 'released')
        self.assertEqual(released['release_reason'], 'explicit_release')
        self.assertIsNotNone(released['released_at'])
        c = self.intent(task, 'auth/login')
        self.assertNotEqual(c['intent_id'], a['intent_id'])
        self.assertEqual(len(self.intents(task)), 2)
        with self.assertRaises(LookupError):
            manager.release_work_intent(self.task()['task_id'], c['intent_id'])

    def test_stop_and_replacement_keep_intent_and_workspace_stale_finalizer_is_harmless(self):
        key, process = self.create(kind='mock')
        task = self.task_for(key)
        intent = self.intent(task, mode='single_owner')
        workspace = manager._store.get_task_workspace(task['task_id'])
        manager.stop_agent(key)
        self.assertEqual(self.intents(task)[0], intent)
        replacement, _ = self.start_existing(task)
        manager._finalize_process(key, process)
        self.assertEqual(self.intents(task)[0], intent)
        self.assertEqual(manager._store.get_task_workspace(task['task_id']), workspace)
        self.assertEqual(manager.agent_statuses[replacement], 'running')

    def test_pause_suspends_resume_rechecks_without_stealing_current_authority(self):
        a, b = self.task(), self.task()
        first = self.intent(a, mode='single_owner')
        self.control(a, 'paused')
        self.assertEqual(self.intents(a)[0]['status'], 'suspended')
        second = self.intent(b, mode='single_owner')
        self.assertEqual(self.state(b)['status'], 'pending')
        self.control(a, 'active')
        self.assertEqual(self.state(a)['status'], 'blocked')
        self.assertGreater(self.intents(a)[0]['activation_order'], second['activation_order'])
        self.assertEqual(self.intents(a)[0]['intent_id'], first['intent_id'])
        self.drop(second)
        self.assertEqual(self.state(a)['status'], 'pending')

    def test_failed_pause_or_cancel_retains_authority_until_confirmed_stop(self):
        for intent in ('paused', 'canceled'):
            key, _ = self.create()
            a = self.task_for(key)
            scope = 'area' + str(len(manager.agents))
            self.intent(a, scope, mode='single_owner')
            b = self.task()
            self.intent(b, scope)
            with patch.object(manager, '_stop_windows_tree', side_effect=RuntimeError('failed stop')), self.assertRaises(RuntimeError):
                self.control(a, intent)
            self.assertEqual(self.intents(a)[0]['status'], 'active')
            self.assertEqual(self.state(b)['status'], 'blocked')
            self.control(a, intent)
            self.assertEqual(self.state(b)['status'], 'pending')

    def test_complete_cancel_release_and_never_recreate_historical_intents(self):
        for operation in ('completed', 'canceled'):
            key, _ = self.create()
            task = self.task_for(key)
            self.intent(task, 'scope' + str(len(manager.agents)))
            if operation == 'completed':
                self.output(key, 'codex\nDone', finish=True)
            else:
                self.control(task, 'canceled')
            record = self.intents(task)[0]
            self.assertEqual(record['status'], 'released')
            self.assertEqual(record['release_reason'], operation)
            with self.assertRaises(ValueError):
                self.intent(task)
            self.recover()
            self.assertEqual(self.intents(task)[0], record)

    def test_waiting_and_resource_blocked_keep_active_intents(self):
        key, _, task = self.waiting()
        original = self.intent(task, mode='single_owner')
        other = self.task()
        self.claim(other)
        self.claim(task)
        self.assertEqual(self.state(task)['status'], 'blocked')
        self.assertEqual(self.intents(task)[0], original)
        self.assertIsNotNone(manager.agent_waiting_questions[key])

    def test_all_waiting_continuations_obey_the_same_intent_gate(self):
        actions = [lambda key: manager.reply_agent(key, 'yes'), manager.decide_agent,
                   manager.decide_similar_agent, manager.decide_always_agent]
        for action in actions:
            with self.subTest(action=action):
                key, _, task = self.waiting()
                owner = self.intent(self.task(), 'scope' + key, mode='single_owner')
                self.intent(task, 'scope' + key)
                question = manager.agent_waiting_questions[key]
                with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
                    action(key)
                spawn.assert_not_called()
                self.assertEqual(manager.agent_waiting_questions[key], question)
                self.drop(owner)
                self.assertEqual(self.state(task)['status'], 'waiting')
                self.resume(key, action)

    def test_redirect_preserves_intent_assignment_and_session(self):
        key, process = self.create()
        task = self.task_for(key)
        self.output(key, 'session id: ' + key + '\n')
        intent = self.intent(task, mode='single_owner')
        assignment = manager._store.get_active_assignment_for_task(task['task_id'])
        self.resume(key, lambda agent: manager.redirect_agent(agent, 'correct course'))
        self.assertEqual(self.intents(task)[0], intent)
        self.assertEqual(manager.agent_sessions[key], key)
        self.assertEqual(manager._store.get_active_assignment_for_task(task['task_id']), assignment)
        manager._finalize_process(key, process)
        self.assertEqual(self.intents(task)[0], intent)

    def test_child_start_obeys_its_own_gate(self):
        parent = self.task()
        source = self.intent(parent, mode='single_owner')
        child = self.task(parent)
        self.intent(child)
        with patch.object(manager, '_spawn_process') as spawn, self.assertRaises(ValueError):
            manager.start_agent('ignored', task_id=child['task_id'])
        spawn.assert_not_called()
        self.drop(source)
        self.start_existing(child)

    def test_running_conflicting_declaration_rolls_back_without_stop_or_publication(self):
        ka, _ = self.create()
        kb, _ = self.create()
        a, b = self.task_for(ka), self.task_for(kb)
        self.intent(a)
        before_a, before_b = self.state(a), self.state(b)
        with patch.object(manager.changes, 'publish') as publish, patch.object(manager, '_stop_windows_tree') as stop, self.assertRaises(ValueError):
            self.intent(b, 'auth/login', mode='single_owner')
        publish.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(self.intents(b), [])
        self.assertEqual(self.state(a), before_a)
        self.assertEqual(self.state(b), before_b)

    def test_concurrent_running_single_owner_declarations_have_only_one_winner(self):
        ka, _ = self.create()
        kb, _ = self.create()
        tasks = [self.task_for(ka), self.task_for(kb)]
        barrier = Barrier(2)
        def declare(task):
            barrier.wait()
            try:
                return manager._store.create_work_intent(task['task_id'], namespace='component', key='auth', mode='single_owner')[0]
            except ValueError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(declare, tasks))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(sum(len(self.intents(t)) for t in tasks), 1)
        self.assertTrue(all(self.state(t)['status'] == 'in_progress' for t in tasks))

    def test_concurrent_pending_declarations_and_idempotent_duplicates(self):
        a, b = self.task(), self.task()
        def declare(task):
            return manager._store.create_work_intent(task['task_id'], namespace='component', key='auth', mode='single_owner')[0]
        with ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(declare, [a, b, a]))
        self.assertEqual(result[0], result[2])
        self.assertEqual(sorted(self.state(t)['status'] for t in (a, b)), ['blocked', 'pending'])

    def test_resource_and_dependency_blockers_are_independent_no_fake_deadlock(self):
        owner, task, dependency = self.task(), self.task(), self.task()
        intent = self.intent(owner, mode='single_owner')
        self.intent(task)
        self.edge(task, dependency)
        self.assertEqual(self.reasons(task), {'dependency_incomplete', 'work_intent_conflict'})
        self.drop(intent)
        self.assertEqual(self.reasons(task), {'dependency_incomplete'})
        self.assertEqual(manager._store.resource_deadlocks(task_id=task['task_id']), [])
        self.assertEqual(self.intents(task)[0]['status'], 'active')

    def test_multiple_intent_blockers_preserve_provenance_independently(self):
        owner, task = self.task(), self.task()
        a = self.intent(owner, 'auth/login', mode='single_owner')
        b = self.intent(owner, 'auth/session', mode='single_owner')
        self.intent(task, 'auth')
        self.assertEqual(len(self.state(task)['active_blockers']), 2)
        self.drop(a)
        self.assertEqual(self.state(task)['active_blockers'][0]['owning_intent_id'], b['intent_id'])

    def test_recovery_preserves_pending_waiting_paused_and_released_history(self):
        key, _, waiting = self.waiting()
        pending, paused, canceled = self.task(), self.task(), self.task()
        for index, task in enumerate((waiting, pending, paused, canceled)):
            self.intent(task, 'scope' + str(index), mode='single_owner')
        self.control(paused, 'paused')
        self.control(canceled, 'canceled')
        before = [self.intents(t) for t in (waiting, pending, paused, canceled)]
        with patch.object(manager, '_spawn_process') as spawn:
            self.recover()
        spawn.assert_not_called()
        self.assertEqual([self.intents(t) for t in (waiting, pending, paused, canceled)], before)
        self.assertIsNotNone(manager.agent_waiting_questions[key])

    def test_api_validation_inspection_release_and_read_purity(self):
        a, b = self.task(), self.task()
        route = f"/tasks/{a['task_id']}/work-intents"
        first = self.request(route, 'POST', {'namespace': 'component', 'key': 'auth'})
        self.intent(b)
        before = self.state(a)
        with patch.object(work_intents, 'reconcile', side_effect=AssertionError('GET must not reconcile')):
            self.assertEqual(len(self.request(route)['intents']), 1)
            self.assertEqual(len(self.request(f"/tasks/{a['task_id']}/work-overlaps")['overlaps']), 1)
        self.assertEqual(self.state(a), before)
        result = self.request(route + '/' + first['intent_id'], 'DELETE')
        self.assertEqual(result['status'], 'released')
        self.request('/tasks/missing/work-intents', expected=404)
        self.request(route, 'POST', {'namespace': 'component', 'key': 'auth', 'mode': 'invalid'}, expected=422)
        self.request(route, 'POST', {'namespace': 'component', 'key': '../auth'}, expected=409)
        self.request(route + '/missing', 'DELETE', expected=404)

    def test_publication_observes_committed_intent_and_blocker(self):
        a, b = self.task(), self.task()
        self.intent(a, mode='single_owner')
        observed = []
        with patch.object(manager.changes, 'publish', side_effect=lambda key: observed.append((len(self.intents(b)), self.state(b)['status']))):
            self.intent(b)
        self.assertTrue(observed)
        self.assertTrue(all(value == (1, 'blocked') for value in observed))

    def test_creation_persistence_failure_is_atomic(self):
        a, b = self.task(), self.task()
        self.intent(a, mode='single_owner')
        with manager._store._connection() as db:
            db.execute("CREATE TRIGGER fail_intent BEFORE INSERT ON task_blockers WHEN NEW.blocker_type='work_intent' BEGIN SELECT RAISE(ABORT,'test'); END")
        with patch.object(manager.changes, 'publish') as publish, self.assertRaises(sqlite3.Error):
            self.intent(b)
        publish.assert_not_called()
        self.assertEqual(self.intents(b), [])
        self.assertEqual(self.state(b)['status'], 'pending')

    def downgrade_v16(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name='task_blockers_active'").fetchone()[0]
            db.execute('DROP INDEX work_intent_blocker')
            db.execute('DROP INDEX task_blockers_active')
            db.execute(sql.replace(' AND NOT ' + work_intents.BLOCKER, ''))
            db.execute('ALTER TABLE task_blockers DROP COLUMN waiting_intent_id')
            db.execute('ALTER TABLE task_blockers DROP COLUMN owning_intent_id')
            db.execute('DROP TABLE work_intents')
            db.execute('PRAGMA user_version=16')

    def test_v16_migration_preserves_all_rows_and_creates_no_intents(self):
        key, _, task = self.waiting()
        manager.integrate_task(task['task_id'])
        self.claim(task)
        self.downgrade_v16()
        with closing(sqlite3.connect(self.path)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            columns = {t: ','.join('"'+r[1]+'"' for r in db.execute(f'PRAGMA table_info({t})')) for t in tables}
            before = {t: db.execute(f'SELECT rowid,{columns[t]} FROM {t} ORDER BY rowid').fetchall() for t in tables}
        AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 17)
            self.assertEqual(db.execute('SELECT * FROM work_intents').fetchall(), [])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            for table in tables:
                self.assertEqual(db.execute(f'SELECT rowid,{columns[table]} FROM {table} ORDER BY rowid').fetchall(), before[table], table)
        self.assertEqual(AgentStore(self.path).load_agents()[0]['session_id'], key)

    def test_migration_rollback_and_future_version_rejection(self):
        self.downgrade_v16()
        original = work_intents.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('migration failed')
        with patch.object(work_intents, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 16)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='work_intents'").fetchone())
            self.assertNotIn('waiting_intent_id', [r[1] for r in db.execute('PRAGMA table_info(task_blockers)')])
            db.execute('PRAGMA user_version=18')
        with self.assertRaises(RuntimeError):
            AgentStore(self.path)


if __name__ == '__main__':
    unittest.main()
