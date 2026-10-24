"""Planning through synthetic provider events and real transactional storage."""
import io
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, planning
from app.persistence import AgentStore
import test_delegation_protocol as baseline
import test_delegation_hybrid as hybrid


def decision(**changes):
    value = dict(protocol=planning.PROTOCOL, decision_key='initial', base_revision=0,
                 decision='independent', rationale='One small deliverable; coordination adds no benefit.',
                 items=[], references=[])
    return json.dumps(value | changes)


def item(identifier='analysis', **changes):
    return dict(proposal_id=identifier, deliverable='Document verified findings',
                responsibility='Inspect parser only; do not edit runtime code', depends_on=[], parallel=True) | changes


class ValidationTests(unittest.TestCase):
    def test_independent(self):
        self.assertEqual(planning.validate(decision())['items'], [])

    def test_deliverables_boundaries_dependencies(self):
        items = [item(), item('review', depends_on=['analysis'], parallel=False)]
        self.assertEqual(planning.validate(decision(decision='propose', items=items))['items'], items)

    def test_invalid_dependency_graphs(self):
        for items in ([item(depends_on=['missing'])], [item(depends_on=['analysis'])],
                      [item(depends_on=['review']), item('review', depends_on=['analysis'])],
                      [item(), item('review', depends_on=['analysis', 'analysis'])]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                planning.validate(decision(decision='propose', items=items))

    def test_duplicate_proposals(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate proposal'):
            planning.validate(decision(decision='propose', items=[item(), item()]))

    def test_duplicate_json_fields(self):
        for message in (decision().replace('"items": []', '"items": [], "items": []'),
                        decision(decision='propose', items=[item()]).replace('"parallel": true', '"parallel": true, "parallel": false')):
            with self.assertRaises(ValueError):
                planning.validate(message)

    def test_bounds_types_and_closed_schema(self):
        for change in (dict(protocol='control-center.planning.v2'), dict(base_revision=True), dict(base_revision=-1),
                       dict(decision='launch'), dict(rationale=' '), dict(rationale='x'*2001),
                       dict(items=[item()]), dict(decision='propose'), dict(decision='propose', items=[item()]*17),
                       dict(decision='propose', items=[item(parallel=1)]), dict(unknown=True),
                       dict(references=[{'task_id': 'invented'}]), dict(rationale='\ud800'),
                       dict(rationale='a\x00b'), dict(rationale='a\u202eb')):
            with self.subTest(change=change), self.assertRaises(ValueError):
                planning.validate(decision(**change))
        for value in ('{', '[]', 'null', '```json\n' + decision() + '\n```', 'x'*65537):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                planning.validate(value)


class PlanningTests(unittest.TestCase):
    setUp = baseline.RuntimeTests.setUp
    patch = baseline.RuntimeTests.patch
    finish = baseline.RuntimeTests.finish

    def launch(self, **options):
        key = manager.start_agent('Parent work', 'codex', task_id=self.task['task_id'], planning_enabled=True, **options)
        return key, manager.agents[key]

    def stream(self, key, process, messages, *, ending='turn.completed', stderr='', tool=None):
        session = manager.agent_sessions[key] or str(uuid4())
        events = baseline.events('placeholder', session, ending)
        events[2:3] = [dict(type='item.completed', item=dict(id='message_'+str(i), type='agent_message', text=m))
                       for i, m in enumerate(messages)]
        if tool:
            events.insert(2, dict(type='item.completed', item=dict(id='tool', type='command_execution', aggregated_output=tool)))
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
        process.stderr = io.StringIO(stderr)
        manager._read_output(key, process)

    def submit(self, process, message, identifier='decision'):
        return self.store.accept_planning_messages(process._control_center_execution.origin, [(identifier, message)],
                                                  session_id=process._control_center_execution.events.session_id)[0]

    def active(self, **options):
        key, process = self.launch(**options)
        self.stream(key, process, ['Work continues'])
        return key, process

    def records(self):
        return self.store.planning_history(self.task['task_id'])

    def domain_snapshot(self):
        with self.store._connection() as db:
            return {t: [tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in (
                'tasks', 'task_assignments', 'delegations', 'work_intents', 'resource_claims',
                'task_dependencies', 'integrations', 'delegation_rounds')}

    def test_real_runtime_records_intermediate_plan_then_ordinary_completion(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'Work completed and checked.'])
        self.assertEqual(self.records(), [])
        self.finish(key, process)
        self.assertEqual(self.records()[0]['content']['decision'], 'independent')
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(len(self.commands), 1)

    def test_proposals_do_not_mutate_work_or_acquire_authority(self):
        _, process = self.active()
        before = self.domain_snapshot()
        accepted = self.submit(process, decision(decision='propose', items=[item(), item('verify', depends_on=['analysis'])]))
        self.assertEqual(accepted['revision'], 1)
        self.assertEqual(self.domain_snapshot(), before)
        self.assertEqual(len(self.commands), 1)

    def test_plan_only_final_stops_without_completion_or_resume(self):
        key, process = self.launch()
        self.stream(key, process, [decision()])
        self.finish(key, process)
        for _ in range(3):
            manager.get_agent(key)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(self.records(), [])
        self.assertEqual(len(self.commands), 1)
        self.assertIn('Plan-only final response', '\n'.join(self.store.full_output(key)))

    def test_ordinary_completion_needs_no_plan(self):
        key, process = self.launch()
        self.stream(key, process, ['Done'])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.records(), [])

    def test_tool_stderr_and_fence_content_are_not_ingress(self):
        key, process = self.launch()
        self.stream(key, process, ['```json\n'+decision()+'\n```'], tool=decision(), stderr=decision())
        self.finish(key, process)
        self.assertEqual(self.records(), [])

    def test_failed_turn_does_not_accept_any_plan(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'Done'], ending='turn.failed')
        self.finish(key, process)
        self.assertEqual(self.records(), [])
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_incomplete_stream_does_not_accept_plan(self):
        key, process = self.launch()
        events = baseline.events(decision())[:-1]
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
        manager._read_output(key, process)
        self.finish(key, process)
        self.assertEqual(self.records(), [])

    def test_malformed_intermediate_plan_rejects_whole_execution(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), decision(protocol='control-center.planning.v9'), 'Done'])
        self.finish(key, process)
        self.assertEqual(self.records(), [])
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_disabled_older_execution_does_not_ingest_planning(self):
        key = manager.start_agent('Parent work', 'codex', task_id=self.task['task_id'], delegation_protocol_enabled=True)
        process = manager.agents[key]
        self.stream(key, process, [decision(), 'Done'])
        self.finish(key, process)
        self.assertEqual(self.records(), [])
        self.assertFalse(self.store.planning_enabled(key))

    def test_planning_does_not_grant_delegation_permissions(self):
        key, process = self.launch()
        self.stream(key, process, [baseline.envelope()])
        self.finish(key, process)
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        with self.store._connection() as db:
            row = db.execute('SELECT delegation_protocol_enabled,child_materialization_enabled,hybrid_delegation_enabled,recursive_delegation_enabled FROM agents WHERE agent_id=?', (key,)).fetchone()
        self.assertEqual(tuple(row), (0, 0, 0, 0))

    def test_explicit_v1_compatibility_with_planning(self):
        key, process = self.launch(delegation_protocol_enabled=True)
        self.stream(key, process, [decision(), baseline.envelope()])
        self.finish(key, process)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.store.list_delegations(self.task['task_id'])), 1)
        self.assertEqual(len(self.commands), 1)

    def test_revision_history_add_preserve_withdraw(self):
        _, process = self.active()
        self.submit(process, decision(decision='propose', items=[item()]))
        self.submit(process, decision(decision_key='second', base_revision=1, decision='propose', items=[item(), item('next')]), 'second')
        self.submit(process, decision(decision_key='third', base_revision=2), 'third')
        rows = self.records()
        self.assertEqual([r['revision'] for r in rows], [1, 2, 3])
        self.assertEqual(rows[0]['content']['items'][0]['proposal_id'], rows[1]['content']['items'][0]['proposal_id'])
        self.assertEqual(self.store.current_plan(self.task['task_id'])['content']['items'], [])

    def test_exact_replay_and_conflicting_replay(self):
        _, process = self.active()
        first = self.submit(process, decision())
        self.assertEqual(self.submit(process, decision(), 'retry'), first)
        with self.assertRaisesRegex(ValueError, 'Conflicting replay'):
            self.submit(process, decision(rationale='Different'))
        self.assertEqual(len(self.records()), 1)

    def test_message_id_cannot_be_reused_for_different_decision(self):
        _, process = self.active()
        self.submit(process, decision())
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(process, decision(decision_key='next', base_revision=1))
        self.assertEqual(len(self.records()), 1)

    def test_competing_revision_transactions(self):
        _, process = self.active()
        origin = process._control_center_execution.origin
        session = process._control_center_execution.events.session_id
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        def submit(index):
            try:
                stores[index].accept_planning_messages(origin, [(str(index), decision(decision_key=str(index)))], session_id=session)
                return 'accepted'
            except ValueError:
                return 'stale'
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(submit, (0, 1))), ['accepted', 'stale'])
        self.assertEqual(len(self.records()), 1)

    def test_stale_generation_rejected_even_for_replay(self):
        key, process = self.active()
        self.submit(process, decision())
        self.store.begin_delegation_execution(key, str(uuid4()))
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.submit(process, decision())

    def test_replacement_can_revise_plan(self):
        key, process = self.active()
        self.submit(process, decision())
        manager.stop_agent(key)
        new_key, new_process = self.active()
        result = self.submit(new_process, decision(decision_key='revised', base_revision=1), 'new')
        self.assertEqual(result['agent_id'], new_key)
        self.assertNotEqual(new_key, key)
        self.assertEqual(len(self.records()), 2)

    def test_authority_mismatches(self):
        _, process = self.active()
        origin = process._control_center_execution.origin
        for change in (dict(agent_id=str(uuid4())), dict(assignment_id=str(uuid4())), dict(project_id=str(uuid4())), dict(parent_task_id=str(uuid4()))):
            with self.subTest(change=change), self.assertRaises((ValueError, LookupError)):
                self.store.accept_planning_messages(replace(origin, **change), [('one', decision())], session_id=process._control_center_execution.events.session_id)
        self.assertEqual(self.records(), [])

    def test_reference_bounds_and_existing_children_unchanged(self):
        _, process = self.active()
        child = self.store.create_task('Existing child', parent_task_id=self.task['task_id'], project_id=self.project['project_id'])
        before = self.domain_snapshot()
        self.submit(process, decision(references=[{'task_id': child['task_id']}]))
        self.assertEqual(self.domain_snapshot(), before)
        other = self.store.create_task('Unrelated', project_id=self.project['project_id'])
        with self.assertRaisesRegex(ValueError, 'authorized Task tree'):
            self.submit(process, decision(decision_key='next', base_revision=1, references=[{'task_id': other['task_id']}]), 'next')
        with self.assertRaises(LookupError):
            self.submit(process, decision(decision_key='next', base_revision=1, references=[{'task_id': str(uuid4())}]), 'next')

    def test_atomic_batch_failure_and_no_publication(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), decision(decision_key='second', base_revision=9), 'Done'])
        self.finish(key, process)
        self.assertEqual(self.records(), [])
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_database_insert_failure_rolls_back(self):
        _, process = self.active()
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_plan BEFORE INSERT ON planning_revisions BEGIN SELECT RAISE(ABORT,'injected'); END")
        before = self.domain_snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.submit(process, decision())
        self.assertEqual(self.records(), [])
        self.assertEqual(self.domain_snapshot(), before)

    def test_lost_ack_restart_preserves_exact_revision(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'Done'])
        accept = self.store.accept_planning_messages
        def lost(*args, **kwargs):
            accept(*args, **kwargs)
            raise sqlite3.OperationalError('lost response')
        with patch.object(self.store, 'accept_planning_messages', side_effect=lost):
            self.finish(key, process)
        before = self.records()
        recovered = AgentStore(self.store.path)
        recovered.reconcile_task_recovery()
        self.assertEqual(recovered.planning_history(self.task['task_id']), before)
        self.assertEqual(self.submit(process, decision()), before[0])
        self.assertEqual(len(self.commands), 1)

    def test_publication_observes_committed_plan(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'Done'])
        observed = []
        with patch.object(manager, '_emit_agent_change', side_effect=lambda _: observed.append(len(AgentStore(self.store.path).planning_history(self.task['task_id'])))):
            self.finish(key, process)
        self.assertTrue(observed)
        self.assertTrue(all(n == 1 for n in observed))

    def test_reply_keeps_planning_and_current_revision(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'CONTROL_CENTER_WAITING: Choose a path?'])
        self.finish(key, process)
        self.assertIn('current accepted revision: 1', manager._planning_prompt(key))
        self.assertIn('One small deliverable', manager._planning_prompt(key))
        manager.reply_agent(key, 'Continue')
        replacement = manager.agents[key]
        self.assertTrue(replacement._control_center_execution.events.planning_enabled)
        self.assertNotEqual(process._control_center_execution.origin.generation, replacement._control_center_execution.origin.generation)
        self.stream(key, replacement, [decision(decision_key='next', base_revision=1), 'Done'])
        self.finish(key, replacement)
        self.assertEqual(len(self.records()), 2)

    def test_planning_context_failure_stops_spawn_before_prompt(self):
        with patch.object(self.store, 'current_plan', side_effect=sqlite3.OperationalError('context unavailable')):
            with self.assertRaises(sqlite3.OperationalError):
                self.launch()
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(all(p.poll() is not None for p in manager.agents.values()))
        self.assertEqual(self.records(), [])

    def test_runtime_duplicate_completed_item_does_not_duplicate_revision(self):
        key, process = self.launch()
        events = baseline.events('Done')
        plan_event = dict(type='item.completed', item=dict(id='plan', type='agent_message', text=decision()))
        events[2:2] = [plan_event, plan_event]
        process.stdout = io.StringIO('\n'.join(json.dumps(e) for e in events))
        manager._read_output(key, process)
        self.finish(key, process)
        self.assertEqual(len(self.records()), 1)

    def test_runtime_plan_count_is_bounded(self):
        key, process = self.launch()
        self.stream(key, process, [decision(decision_key=str(i), base_revision=i) for i in range(17)] + ['Done'])
        self.finish(key, process)
        self.assertEqual(self.records(), [])
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_stale_process_cannot_publish_a_plan(self):
        key, process = self.launch()
        self.stream(key, process, [decision(), 'Done'])
        self.store.begin_delegation_execution(key, str(uuid4()))
        self.finish(key, process)
        self.assertEqual(self.records(), [])

    def test_ancestor_cancellation_blocks_new_planning(self):
        _, process = self.active()
        self.store.request_work_control(self.task['task_id'], 'canceled')
        with self.assertRaises(ValueError):
            self.submit(process, decision())
        self.assertEqual(self.records(), [])

    def test_mock_rejected_before_spawn(self):
        with self.assertRaises(ValueError):
            manager.start_agent('test', 'mock', planning_enabled=True)
        self.assertEqual(self.commands, [])

    def test_v23_migration_is_default_off_and_preserves_domain(self):
        key = manager.start_agent('Parent work', 'codex', task_id=self.task['task_id'], delegation_protocol_enabled=True)
        before = self.domain_snapshot()
        with self.store._connection() as db:
            db.execute('DROP TABLE planning_revisions')
            db.execute('ALTER TABLE agents DROP COLUMN planning_enabled')
            db.execute('PRAGMA user_version=23')
        AgentStore(self.store.path)
        self.assertEqual(self.domain_snapshot(), before)
        self.assertFalse(self.store.planning_enabled(key))
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
        self.assertEqual(len(self.commands), 1)

    def test_v23_migration_failure_rolls_back_schema_and_version(self):
        with self.store._connection() as db:
            db.execute('DROP TABLE planning_revisions')
            db.execute('ALTER TABLE agents DROP COLUMN planning_enabled')
            db.execute('PRAGMA user_version=23')
        migrate = planning.migrate
        def fail(db):
            migrate(db)
            raise sqlite3.OperationalError('injected migration failure')
        with patch.object(planning, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 23)
            self.assertNotIn('planning_enabled', {r['name'] for r in db.execute('PRAGMA table_info(agents)')})
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='planning_revisions'").fetchone())


class HybridPlanningTests(unittest.TestCase):
    setUp = baseline.RuntimeTests.setUp
    patch = baseline.RuntimeTests.patch
    finish = baseline.RuntimeTests.finish
    stream = PlanningTests.stream

    def accepted(self, plan=None):
        start = manager.start_agent
        def enabled(*args, **kwargs):
            return start(*args, **kwargs, planning_enabled=True)
        with patch.object(manager, 'start_agent', side_effect=enabled):
            return hybrid.HybridRuntimeTests.accepted(self, plan)

    def read(self, key, process, message, **options):
        messages = [message]
        if self.store.planning_enabled(key):
            task_id = self.store.agent_task_ids(key)[0]
            previous = self.store.current_plan(task_id)
            revision = previous['revision'] if previous else 0
            messages.insert(0, decision(decision_key='round-'+str(revision), base_revision=revision))
        self.stream(key, process, messages)

    def test_planning_preserves_v2_handoff_integration_ack_and_completion(self):
        hybrid.HybridRuntimeTests.test_full_result_integration_ack_and_parent_completion_with_mocked_cli(self)
        rows = self.store.planning_history(self.task['task_id'])
        self.assertEqual([r['revision'] for r in rows], [1, 2, 3])
        self.assertEqual(len({r['agent_id'] for r in rows}), 1)
        self.assertEqual(len({r['generation'] for r in rows}), 3)


if __name__ == '__main__':
    unittest.main()
