"""Synthetic provider ingress and real transactional promotion/orchestration."""
import io
import json
import sqlite3
import unittest
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, promotion, planning, delegation_materialization, delegation_orchestration
from app.persistence import AgentStore
import test_delegation_protocol as baseline
import test_planning as plans
import test_delegation_hybrid as hybrid


def selection(key='a', **changes):
    return dict(proposal_id=key, scope=dict(namespace='component', key=key, from_intent_id=None),
                resources=[], depends_on_task_ids=[], reuse_task_id=None) | changes


class PromotionTests(unittest.TestCase):
    patch = baseline.RuntimeTests.patch
    stream = plans.PlanningTests.stream
    finish = baseline.RuntimeTests.finish

    def setUp(self):
        baseline.RuntimeTests.setUp(self)
        self.patch(patch.object(manager, '_schedule_materialization'))
        self.patch(patch.object(manager, '_schedule_hybrid_for_agent'))
        self.patch(patch.object(manager, '_notify_hybrid_child_end'))

    def launch(self, **changes):
        options = dict(planning_enabled=True, delegation_protocol_enabled=True,
                       child_materialization_enabled=True, hybrid_delegation_enabled=True)
        sandbox = changes.pop('sandbox', 'read-only')
        options.update(changes)
        key = manager.start_agent('Parent', 'codex', sandbox, task_id=self.task['task_id'], **options)
        return key, manager.agents[key]

    def plan(self, items=None, **changes):
        return plans.decision(decision='propose', items=items or [plans.item('a'), plans.item('b')], **changes)

    def message(self, items=None, **changes):
        return json.dumps(dict(protocol=promotion.PROTOCOL, task_id=self.task['task_id'], revision=1,
                              decision_key='choose', items=items or [selection()], continuation=None) | changes)

    def submit(self, process, message=None, plan_messages=()):
        context = process._control_center_execution
        return self.store.accept_promotion_message(context.origin, message or self.message(),
            session_id=context.events.session_id, message_id='promotion', plans=plan_messages)

    def active(self, plan=None, **options):
        key, process = self.launch(**options)
        self.stream(key, process, ['Work continues'])
        self.store.accept_planning_messages(process._control_center_execution.origin,
            [('plan', plan or self.plan())], session_id=manager.agent_sessions[key])
        return key, process

    def rows(self):
        return self.store.list_promotions(self.task['task_id'])

    def accepted(self, items=None, plan=None, continuation=None):
        key, process = self.launch()
        self.stream(key, process, [plan or self.plan(), self.message(items, continuation=continuation)])
        self.finish(key, process)
        return key, process

    def test_same_execution_ingress_selected_only_and_parent_handoff(self):
        key, process = self.accepted()
        row, = self.rows()
        self.assertEqual(row['proposal_id'], 'a')
        self.assertEqual(row['disposition'], 'ready')
        self.assertIsNotNone(row['child_task_id'])
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')
        self.assertEqual(self.store.get_active_assignment_for_agent(key)['assignment_id'], process._control_center_execution.origin.assignment_id)
        self.assertEqual(len(self.commands), 1)

    def test_persisted_plan_and_exact_retry(self):
        _, process = self.active()
        receipt = self.submit(process)
        before = self.rows()
        self.assertEqual(self.submit(process), receipt)
        self.assertEqual(self.rows(), before)

    def test_conflicting_retry(self):
        _, process = self.active()
        self.submit(process)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            self.submit(process, self.message([selection('b')]))
        self.assertEqual(len(self.rows()), 1)

    def test_concurrent_identical_requests_share_one_allocation(self):
        _, process = self.active()
        context = process._control_center_execution
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        def submit(index):
            return stores[index].accept_promotion_message(context.origin, self.message(),
                session_id=context.events.session_id, message_id='promotion')
        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(submit, (0, 1)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_malformed_selection_rolls_back_same_execution_plan(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message([selection(), selection('missing')])])
        self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.planning_history(self.task['task_id']), [])
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_structural_validation_all_or_nothing(self):
        _, process = self.active()
        bad = [self.message([selection(), selection()]), self.message(protocol='control-center.promotion.v9'),
               self.message(revision=True), self.message(extra='field'),
               self.message().replace('"revision": 1', '"revision": 1, "revision": 1'),
               self.message([selection(), selection('b', resources=[{}])])]
        for message in bad:
            with self.subTest(message=message), self.assertRaises(ValueError):
                self.submit(process, message)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.store.list_tasks()), 1)

    def test_missing_permission_rejected(self):
        _, process = self.active(hybrid_delegation_enabled=False)
        with self.assertRaisesRegex(ValueError, 'authority'):
            self.submit(process)
        self.assertEqual(self.rows(), [])

    def test_stale_generation_and_claimed_task_rejected(self):
        _, process = self.active()
        with self.assertRaisesRegex(ValueError, 'trusted'):
            self.submit(process, self.message(task_id=str(uuid4())))
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.store.accept_promotion_message(replace(process._control_center_execution.origin, generation=str(uuid4())),
                self.message(), session_id=process._control_center_execution.events.session_id, message_id='x')
        self.assertEqual(self.rows(), [])

    def test_superseded_revision_is_rejected(self):
        _, process = self.active()
        self.store.accept_planning_messages(process._control_center_execution.origin,
            [('second', self.plan(decision_key='second', base_revision=1))], session_id=process._control_center_execution.events.session_id)
        with self.assertRaisesRegex(ValueError, 'current accepted'):
            self.submit(process)

    def test_planning_alone_and_normal_completion_nonexecuting(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), 'Done'])
        self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(manager.agent_statuses[key], 'finished')

    def test_tool_stderr_and_fences_are_not_promotion_ingress(self):
        key, process = self.launch()
        message = self.message()
        self.stream(key, process, ['```json\n' + message + '\n```'], stderr=message, tool=message)
        self.finish(key, process)
        self.assertEqual(self.rows(), [])

    def test_nonfinal_promotion_is_rejected(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message(), 'Done'])
        self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_partial_ambiguous_and_safe_items(self):
        self.accepted([selection(), selection('b', scope=None)])
        rows = self.rows()
        self.assertEqual([r['disposition'] for r in rows], ['ready', 'conflict'])
        self.assertIsNone(rows[1]['child_task_id'])
        with self.store._connection() as db:
            self.assertTrue(delegation_orchestration.outstanding(db, self.task['task_id']))

    def test_unauthorized_scope_source_is_durable_not_queued(self):
        self.accepted([selection(scope=dict(namespace='component', key='a', from_intent_id=str(uuid4()))), selection('b')])
        rows = self.rows()
        self.assertEqual([r['disposition'] for r in rows], ['unauthorized', 'ready'])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(self.rows()[0]['disposition'], 'unauthorized')

    def test_project_overlap_with_distant_root_is_blocked(self):
        other = self.store.create_task('Other root', project_id=self.project['project_id'])
        self.store.create_work_intent(other['task_id'], namespace='component', key='a', mode='single_owner')
        self.accepted([selection(), selection('b')])
        self.assertEqual([r['disposition'] for r in self.rows()], ['conflict', 'ready'])

    def test_equal_scopes_within_one_batch_do_not_allocate_twice(self):
        self.accepted([selection(), selection('b', scope=dict(namespace='component', key='a', from_intent_id=None))])
        self.assertEqual([r['disposition'] for r in self.rows()], ['ready', 'conflict'])

    def test_independent_scopes_are_admitted(self):
        self.accepted([selection(), selection('b')])
        self.assertEqual([r['disposition'] for r in self.rows()], ['ready', 'ready'])

    def test_global_resource_conflict_reuses_existing_claims(self):
        other_temp = tempfile.TemporaryDirectory()
        self.addCleanup(other_temp.cleanup)
        other_root = Path(other_temp.name)
        project = self.store.create_project('Other', other_root)
        other = self.store.create_task('Private other work', project_id=project['project_id'])
        claim = dict(resource_type='generic', resource_key='build:shared', scope='global', mode='exclusive', recursive=False)
        self.store.create_resource_claim(other['task_id'], **claim)
        self.accepted([selection(resources=[claim]), selection('b')])
        self.assertEqual([r['disposition'] for r in self.rows()], ['blocked', 'ready'])
        with self.store._connection() as db:
            self.assertNotIn(other['task_id'], promotion.context(db, self.task['task_id']))
            self.assertNotIn('Private other', promotion.context(db, self.task['task_id']))

    def test_unpromoted_dependency_blocks_without_fake_task(self):
        self.accepted(plan=self.plan([plans.item('a', depends_on=['b']), plans.item('b')]))
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertEqual(len(self.store.list_tasks()), 2)
        with self.store._connection() as db, self.assertRaises(ValueError):
            promotion.require_ready(db, row['delegation_id'])

    def test_selected_dependency_installs_real_edge(self):
        self.accepted([selection('b'), selection()], self.plan([plans.item('a', depends_on=['b']), plans.item('b')]))
        rows = {r['proposal_id']: r for r in self.rows()}
        with self.store._connection() as db:
            edges = [tuple(r) for r in db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies')]
        self.assertEqual(edges, [(rows['a']['child_task_id'], rows['b']['child_task_id'])])
        self.assertEqual(rows['a']['disposition'], 'blocked')

    def test_fresh_gate_rejects_released_scope(self):
        self.accepted()
        row, = self.rows()
        with self.store._connection() as db:
            db.execute("UPDATE work_intents SET status='released',holds_authority=0 WHERE task_id=?", (row['child_task_id'],))
            with self.assertRaisesRegex(ValueError, 'authoritative'):
                promotion.require_ready(db, row['delegation_id'])

    def test_lost_commit_ack_keeps_handoff_and_no_poll_retry(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message()])
        original = self.store.accept_promotion_message
        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('lost response')
        with patch.object(self.store, 'accept_promotion_message', side_effect=lost):
            self.finish(key, process)
        before = self.rows()
        for _ in range(3):
            manager.get_agent(key)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')
        self.assertEqual(len(self.commands), 1)

    def test_transaction_failure_rolls_back_plan_and_handoff(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message()])
        with patch.object(promotion, 'prepare', side_effect=sqlite3.OperationalError('crash')):
            self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.planning_history(self.task['task_id']), [])
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_restart_retains_mixed_dispositions_without_spawn(self):
        key, _ = self.accepted([selection(), selection('b', scope=None)])
        before = self.rows()
        manager.agents[key] = None
        manager.initialize_persistence(self.store.path)
        self.assertEqual(self.rows(), before)
        self.assertEqual(len(self.commands), 1)

    def test_cancellation_before_acceptance_wins(self):
        _, process = self.active()
        self.store.request_work_control(self.task['task_id'], 'canceled')
        with self.assertRaises(ValueError):
            self.submit(process)
        self.assertEqual(self.rows(), [])

    def test_publication_sees_committed_provenance(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message()])
        observed = []
        with patch.object(manager, '_emit_agent_change', side_effect=lambda _: observed.append(
                len(AgentStore(self.store.path).list_promotions(self.task['task_id'])))):
            self.finish(key, process)
        self.assertTrue(observed)
        self.assertTrue(all(n == 1 for n in observed))

    def test_schema_migration_failure_rolls_back(self):
        with self.store._connection() as db:
            db.execute('DROP TABLE promotion_items')
            db.execute('DROP TABLE promotion_decisions')
            db.execute('PRAGMA user_version=24')
        original = promotion.migrate
        def fail(db):
            original(db)
            raise sqlite3.OperationalError('crash')
        with patch.object(promotion, 'migrate', side_effect=fail), self.assertRaises(RuntimeError):
            AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 24)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='promotion_items'").fetchone())
        AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 26)
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def workspace_fixture(self):
        def workspace(task_id, origin='legacy_project_snapshot'):
            task = self.store.get_task(task_id)
            if self.store.get_task_workspace(task_id) is None:
                key = str(uuid4())
                self.store.save_task_workspace(dict(workspace_id=key, task_id=task_id, project_id=task['project_id'],
                    workspace_path=str(self.root.parent / key), workspace_path_key=key, base_snapshot='fixture',
                    origin_kind=origin, source_task_id=task['parent_task_id']))
            return self.root
        self.patch(patch.object(manager, '_execution_workspace', side_effect=workspace))

    def test_partial_batch_launches_only_ready_child_and_retry_is_idempotent(self):
        self.accepted([selection(), selection('b', scope=None)])
        self.workspace_fixture()
        progress = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(progress[0]['phase'], 'started', progress)
        self.assertNotIn('launch_agent_id', progress[1])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(len(self.store.list_tasks()), 2)

    def test_workspace_failure_keeps_attachment_without_worker(self):
        self.accepted()
        with patch.object(manager, '_execution_workspace', side_effect=OSError('workspace failure')):
            progress, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(progress['phase'], 'attached')
        self.assertIn('workspace failure', progress['last_error'])
        self.assertEqual(len(self.commands), 1)

    def test_uncertain_launch_is_not_repeated(self):
        self.accepted()
        self.workspace_fixture()
        with patch.object(manager, '_spawn_process', side_effect=OSError('uncertain launch')) as spawn:
            first, = manager.materialize_delegations(self.task['task_id'])
            manager.materialize_delegations(self.task['task_id'], retry=True)
        self.assertEqual(first['phase'], 'recovery_required')
        self.assertEqual(spawn.call_count, 1)

    def test_capacity_preserves_unlaunched_required_work(self):
        self.accepted([selection(), selection('b')])
        self.workspace_fixture()
        progress = manager.materialize_delegations(self.task['task_id'],
            limits=delegation_materialization.Limits(concurrent_children=1))
        self.assertEqual(progress[0]['phase'], 'started')
        self.assertIn('capacity', progress[1]['last_error'])
        self.assertEqual(len(self.commands), 2)

    def test_hybrid_independent_resume_preserves_assignment_and_session(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        key, original = self.accepted(continuation=dict(instruction='Review separate parent area', intent_ids=[intent]))
        assignment = self.store.get_active_assignment_for_agent(key)
        session = manager.agent_sessions[key]
        state = manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'], 'independent', state)
        self.assertIsNot(original, manager.agents[key])
        self.assertEqual(self.store.get_active_assignment_for_agent(key), assignment)
        self.assertIn(session, self.commands[-1])
        self.stream(key, manager.agents[key], ['Done with independent part'])
        self.finish(key, manager.agents[key])
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')

    def test_later_round_cannot_duplicate_retained_proposal(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        key, _ = self.accepted(continuation=dict(instruction='Independent parent work', intent_ids=[intent]))
        first = self.rows()[0]
        manager.advance_delegations(self.task['task_id'])
        process = manager.agents[key]
        self.stream(key, process, [self.plan(decision_key='revised', base_revision=1),
            self.message([selection(), selection('b')], decision_key='round2', revision=2)])
        self.finish(key, process)
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['child_task_id'], first['child_task_id'])
        self.assertEqual(rows[0]['revision'], 1)
        self.assertEqual(rows[1]['revision'], 2)

    def test_new_round_cannot_change_promoted_responsibility(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        key, _ = self.accepted(continuation=dict(instruction='Independent parent work', intent_ids=[intent]))
        first = self.rows()[0]
        manager.advance_delegations(self.task['task_id'])
        process = manager.agents[key]
        self.stream(key, process, [self.plan([plans.item('a', responsibility='Changed scope')], decision_key='second', base_revision=1),
            self.message(decision_key='round2', revision=2)])
        self.finish(key, process)
        self.assertEqual(self.rows()[0]['proposal'], first['proposal'])
        self.assertEqual(len(self.store.planning_history(self.task['task_id'])), 1)

    def test_completed_work_is_reuse_candidate_not_automatic_success(self):
        target = self.store.create_task('Existing result', project_id=self.project['project_id'])
        with self.store._connection() as db:
            db.execute("UPDATE tasks SET status='completed' WHERE task_id=?", (target['task_id'],))
        self.accepted([selection(reuse_task_id=target['task_id'])])
        row, = self.rows()
        self.assertEqual(row['disposition'], 'reuse_available')
        self.assertIsNone(row['child_task_id'])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 1)

    def test_competing_root_promotions_are_serialized(self):
        _, first = self.active()
        original_task = self.task
        self.task = self.store.create_task('Second root', project_id=self.project['project_id'])
        _, second = self.active()
        messages = [self.message(task_id=original_task['task_id']), self.message()]
        processes = [first, second]
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        def submit(index):
            ctx = processes[index]._control_center_execution
            return stores[index].accept_promotion_message(ctx.origin, messages[index],
                session_id=ctx.events.session_id, message_id='promotion')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(submit, (0, 1)))
        rows = self.rows() + self.store.list_promotions(original_task['task_id'])
        self.assertEqual(sorted(r['disposition'] for r in rows), ['conflict', 'ready'])

    def test_cycles_across_revisions_are_rejected_atomically(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        key, _ = self.accepted(plan=self.plan([plans.item('a', depends_on=['b']), plans.item('b')]),
            continuation=dict(instruction='Independent work', intent_ids=[intent]))
        manager.advance_delegations(self.task['task_id'])
        process = manager.agents[key]
        self.stream(key, process, [self.plan([plans.item('a'), plans.item('b', depends_on=['a'])], decision_key='second', base_revision=1),
            self.message([selection('b')], revision=2, decision_key='second')])
        self.finish(key, process)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.store.planning_history(self.task['task_id'])), 1)

    def test_parent_cancellation_after_acceptance_prevents_child_launch(self):
        self.accepted()
        self.workspace_fixture()
        self.store.request_work_control(self.task['task_id'], 'canceled')
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 1)

    def test_plan_revision_withdrawal_does_not_cancel_admitted_work(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        key, _ = self.accepted(continuation=dict(instruction='Independent work', intent_ids=[intent]))
        before = self.rows()[0]['delegation_id']
        manager.advance_delegations(self.task['task_id'])
        process = manager.agents[key]
        self.stream(key, process, [plans.decision(decision_key='withdraw', base_revision=1), 'Independent work done'])
        self.finish(key, process)
        self.assertEqual(self.store.current_plan(self.task['task_id'])['content']['items'], [])
        self.assertEqual(self.rows()[0]['delegation_id'], before)
        self.assertEqual(self.rows()[0]['delegation_status'], 'materialized')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')

    def test_interrupted_and_failed_provider_never_promotes(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message()], ending='turn.failed')
        self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.planning_history(self.task['task_id']), [])

    def test_ancestor_dependency_is_structurally_invalid(self):
        _, process = self.active()
        with self.assertRaisesRegex(ValueError, 'ancestor'):
            self.submit(process, self.message([selection(depends_on_task_ids=[self.task['task_id']])]))
        self.assertEqual(self.rows(), [])

    def test_schema_v24_data_and_permissions_are_preserved(self):
        key, _ = self.active()
        before = self.store.load_agents()
        plans_before = self.store.planning_history(self.task['task_id'])
        with self.store._connection() as db:
            db.execute('DROP TABLE promotion_items')
            db.execute('DROP TABLE promotion_decisions')
            db.execute('PRAGMA user_version=24')
        migrated = AgentStore(self.store.path)
        self.assertEqual(migrated.load_agents(), before)
        self.assertEqual(migrated.planning_history(self.task['task_id']), plans_before)
        self.assertEqual(migrated.list_promotions(self.task['task_id']), [])
        self.assertTrue(migrated.planning_enabled(key))

    def test_context_is_bounded_and_inspection_exposes_decision_history(self):
        self.accepted()
        task = self.store.get_task(self.task['task_id'])
        self.assertEqual(task['promotion_decisions'][0]['content']['decision_key'], 'choose')
        with self.store._connection() as db:
            self.assertLessEqual(len(promotion.context(db, self.task['task_id']).encode()), promotion.MAX_CONTEXT)

    def test_selective_context_includes_only_related_project_work(self):
        related = self.store.create_task('Related scope', project_id=self.project['project_id'])
        unrelated = self.store.create_task('Unrelated scope', project_id=self.project['project_id'])
        self.store.create_work_intent(related['task_id'], namespace='component', key='a', mode='single_owner')
        self.store.create_work_intent(unrelated['task_id'], namespace='component', key='other', mode='single_owner')
        self.accepted()
        with self.store._connection() as db:
            context = json.loads(promotion.context(db, self.task['task_id']))
        self.assertEqual([r['task_id'] for r in context['related_work']], [related['task_id']])
        self.assertEqual(context['promotions'][0]['disposition'], 'conflict')

    def test_replacement_retains_original_promotion_identity(self):
        key, process = self.active()
        self.submit(process)
        original = self.rows()[0]
        # Trusted receipt recovery under a newly authorized generation does not
        # transfer provenance or reset the existing Hybrid cursor.
        with self.store._connection() as db:
            db.execute('UPDATE agents SET execution_generation=? WHERE agent_id=?', ('replacement', key))
        ctx = process._control_center_execution
        receipt = self.store.accept_promotion_message(replace(ctx.origin, generation='replacement'), self.message(),
            session_id=ctx.events.session_id, message_id='retry')
        self.assertEqual(self.rows()[0], original)
        self.assertEqual(receipt['generation'], ctx.origin.generation)

    def test_duplicate_field_ingress_cannot_commit_intermediate_plan(self):
        key, process = self.launch()
        message = self.message().replace('"revision": 1', '"revision": 1, "revision": 1')
        self.stream(key, process, [self.plan(), message])
        self.finish(key, process)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.store.planning_history(self.task['task_id']), [])

    def test_canceled_prerequisite_never_releases_dependent(self):
        self.accepted([selection('b'), selection()], self.plan([plans.item('a', depends_on=['b']), plans.item('b')]))
        rows = {r['proposal_id']: r for r in self.rows()}
        self.store.request_work_control(rows['b']['child_task_id'], 'canceled')
        self.workspace_fixture()
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 1)
        with self.store._connection() as db, self.assertRaises(ValueError):
            promotion.require_ready(db, rows['a']['delegation_id'])

    def test_cancel_racing_with_promotion_cannot_escape_subtree(self):
        _, process = self.active()
        def promote():
            try:
                self.submit(process)
            except ValueError:
                pass
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(promote), pool.submit(self.store.request_work_control, self.task['task_id'], 'canceled')]
            for future in futures:
                future.result()
        self.workspace_fixture()
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 1)
        self.assertTrue(all(t['control_intent'] == 'canceled' for t in self.store.list_tasks()))

    def test_revoked_scope_source_retires_only_unlaunched_claims(self):
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='whole', mode='single_owner')
        self.accepted([selection(scope=dict(namespace='component', key='whole/a', from_intent_id=intent))])
        row, = self.rows()
        self.store.release_work_intent(self.task['task_id'], intent)
        with self.store._connection() as db, self.assertRaisesRegex(ValueError, 'source'):
            promotion.require_ready(db, row['delegation_id'])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(self.rows()[0]['disposition'], 'unauthorized')
        self.assertTrue(all(r['status'] == 'released' for r in self.store.work_intents(row['child_task_id'])))

    def test_nested_inherited_planning_can_promote_without_replacement(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        root_task = self.task
        self.workspace_fixture()
        progress, = manager.materialize_delegations(root_task['task_id'])
        child_agent = progress['launch_agent_id']
        child_id = self.store.agent_task_ids(child_agent)[0]
        self.assertTrue(self.store.planning_enabled(child_agent))
        self.task = self.store.get_task(child_id)
        replacement, proc = child_agent, manager.agents[child_agent]
        source = self.store.work_intents(child_id)[0]['intent_id']
        message = self.message([selection(scope=dict(namespace='component', key='a/sub', from_intent_id=source))])
        self.stream(replacement, proc, [self.plan(), message])
        self.finish(replacement, proc)
        nested, = self.rows()
        self.assertEqual(nested['disposition'], 'ready', nested)
        grand, = manager.materialize_delegations(child_id)
        self.assertEqual(grand['phase'], 'started', grand)
        self.assertTrue(self.store.planning_enabled(grand['launch_agent_id']))
        count = len(self.commands)
        manager.materialize_delegations(child_id)
        self.assertEqual(len(self.commands), count)
        self.assertEqual(self.store.get_task(nested['child_task_id'])['parent_task_id'], child_id)
        self.assertNotEqual(self.store.get_task(root_task['task_id'])['status'], 'completed')

    def test_inheritance_requires_both_planning_and_recursive_authorization(self):
        from app import delegation_safety
        base = dict(agent_type='codex', planning_enabled=True,
                    delegation_protocol_enabled=True, child_materialization_enabled=True,
                    hybrid_delegation_enabled=True, recursive_delegation_enabled=True)
        self.assertTrue(delegation_safety.inherited(base)['planning_enabled'])
        for flag in ('planning_enabled', 'recursive_delegation_enabled'):
            with self.subTest(flag=flag):
                self.assertFalse(delegation_safety.inherited(base | {flag: False})['planning_enabled'])
        for flag in ('delegation_protocol_enabled', 'child_materialization_enabled', 'hybrid_delegation_enabled'):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                delegation_safety.inherited(base | {flag: False})
        with self.assertRaises(ValueError):
            delegation_safety.inherited(base | dict(agent_type='mock'))

    def test_nonrecursive_planning_parent_child_stays_disabled(self):
        self.accepted()
        self.workspace_fixture()
        child, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(child['phase'], 'started', child)
        self.assertFalse(self.store.planning_enabled(child['launch_agent_id']))
        with self.store._connection() as db:
            db.execute('UPDATE agents SET recursive_delegation_enabled=1 WHERE agent_id<>?', (child['launch_agent_id'],))
        reopened = AgentStore(self.store.path)
        self.assertFalse(reopened.planning_enabled(child['launch_agent_id']))

    def test_replacement_does_not_automatically_inherit_planning(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.workspace_fixture()
        progress, = manager.materialize_delegations(self.task['task_id'])
        child = progress['launch_agent_id']
        self.assertTrue(self.store.planning_enabled(child))
        task_id = self.store.agent_task_ids(child)[0]
        manager.stop_agent(child)
        replacement = manager.start_agent('', 'codex', task_id=task_id)
        self.assertFalse(self.store.planning_enabled(replacement))
        self.assertTrue(self.store.planning_enabled(child))

    def test_fresh_parent_capabilities_checked_at_popen_boundary(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.workspace_fixture()
        original = manager._spawn_with_resources
        def revoke_then_spawn(*args, **kwargs):
            with self.store._connection() as db:
                db.execute('UPDATE agents SET planning_enabled=0 WHERE agent_id=?', (key,))
            return original(*args, **kwargs)
        count = len(self.commands)
        with patch.object(manager, '_spawn_with_resources', side_effect=revoke_then_spawn):
            progress, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), count)
        self.assertIn('authorization', progress['last_error'])

    def test_permission_revoked_after_popen_is_compensated_before_prompt(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.workspace_fixture()
        original = manager._spawn_with_resources
        spawned = []
        def spawn_then_revoke(*args, **kwargs):
            child = original(*args, **kwargs)
            spawned.append(child)
            with self.store._connection() as db:
                db.execute('UPDATE agents SET planning_enabled=0 WHERE agent_id=?', (key,))
            return child
        with patch.object(manager, '_spawn_with_resources', side_effect=spawn_then_revoke):
            progress, = manager.materialize_delegations(self.task['task_id'])
        self.assertIn('last_error', progress)
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(spawned[0].stdin.getvalue(), '')

    def test_stale_parent_cannot_authorize_inherited_worker(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.workspace_fixture()
        with self.store._connection() as db:
            db.execute('UPDATE agents SET execution_generation=? WHERE agent_id=?', (str(uuid4()), key))
        count = len(self.commands)
        progress, = manager.materialize_delegations(self.task['task_id'])
        self.assertIn('last_error', progress)
        self.assertEqual(len(self.commands), count)

    def test_recursive_planning_cancellation_at_activation_boundary(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.workspace_fixture()
        original = manager._spawn_with_resources
        def cancel_then_spawn(*args, **kwargs):
            self.store.request_work_control(self.task['task_id'], 'canceled')
            return original(*args, **kwargs)
        count = len(self.commands)
        with patch.object(manager, '_spawn_with_resources', side_effect=cancel_then_spawn):
            progress, = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), count)
        self.assertIn('last_error', progress)

    def test_concurrent_distant_branches_share_project_scope_authority(self):
        key, process = self.launch(recursive_delegation_enabled=True)
        self.stream(key, process, [self.plan(), self.message([selection(), selection('b')])])
        self.finish(key, process)
        self.workspace_fixture()
        progress = manager.materialize_delegations(self.task['task_id'])
        children = [p['launch_agent_id'] for p in progress]
        task_ids = [self.store.agent_task_ids(c)[0] for c in children]
        for child in children:
            manager.stop_agent(child)
        processes, messages = [], []
        for task_id in task_ids:
            self.task = self.store.get_task(task_id)
            _, proc = self.active(recursive_delegation_enabled=True)
            processes.append(proc)
            messages.append(self.message([selection(scope=dict(namespace='component', key='shared', from_intent_id=None))]))
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        def submit(index):
            ctx = processes[index]._control_center_execution
            return stores[index].accept_promotion_message(ctx.origin, messages[index], session_id=ctx.events.session_id, message_id='choose')
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(submit, (0, 1)))
        rows = [self.store.list_promotions(task)[0] for task in task_ids]
        self.assertEqual(sorted(r['disposition'] for r in rows), ['conflict', 'ready'])


class PromotionIntegrationTests(unittest.TestCase):
    setUp = PromotionTests.setUp
    patch = PromotionTests.patch
    stream = PromotionTests.stream
    finish = PromotionTests.finish
    message = PromotionTests.message
    plan = PromotionTests.plan
    read = baseline.RuntimeTests.read

    def launch(self):
        return PromotionTests.launch(self, sandbox='workspace-write')

    def accepted(self):
        key, _ = PromotionTests.accepted(self)
        return key

    def test_real_workspaces_promoted_child_integration_ack_and_completion(self):
        hybrid.HybridRuntimeTests.test_full_result_integration_ack_and_parent_completion_with_mocked_cli(self)
        row, = self.store.list_promotions(self.task['task_id'])
        self.assertEqual(row['disposition'], 'acknowledged')
        self.assertEqual(len(self.store.list_integrations()), 1)

    def test_dependency_waits_for_integration_ack_and_rechecks_resource(self):
        from app.task_workspaces import provision_task_workspace
        from app.project_domain import canonical_path
        from app.workspace_freshness import ensure_workspace_current
        repository = self.root / 'repository'
        hybrid.make_repository(repository)
        path, canonical = canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?', (path, canonical, self.project['project_id']))
        self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        claim = dict(resource_type='generic', resource_key='build:slot', scope='global', mode='exclusive', recursive=False)
        key, _ = PromotionTests.accepted(self, [selection(), selection('b', resources=[claim])],
            self.plan([plans.item('a'), plans.item('b', depends_on=['a'])]))
        self.patch(patch.dict(manager.os.environ, CONTROL_CENTER_WORKSPACE_ROOT=str(self.root / 'workspaces')))
        provision_task_workspace(self.store, self.task['task_id'])
        self.patch(patch.object(manager, '_execution_workspace', side_effect=lambda task_id, origin='legacy_project_snapshot':
            Path(provision_task_workspace(self.store, task_id, origin_kind=origin)['workspace_path'])))
        self.patch(patch.object(manager, 'ensure_workspace_current', side_effect=ensure_workspace_current))
        progress = manager.materialize_delegations(self.task['task_id'])
        child = progress[0]['launch_agent_id']
        self.assertEqual(progress[0]['phase'], 'started', progress)
        self.assertNotIn('launch_agent_id', progress[1])
        self.read(child, manager.agents[child], 'Done')
        self.finish(child, manager.agents[child])
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 2)  # completion without acknowledgement is insufficient
        state = manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'], 'reviewing', state)
        with self.store._connection() as db:
            result, = delegation_orchestration.result_rows(db, self.task['task_id'])
        ack = json.dumps(dict(protocol='control-center.delegation.v2', action='acknowledge_results',
            delivery_id=state['delivery_id'], results=[dict(result_id=result['result_id'], decision='accept', reason='Reviewed')]))
        self.read(key, manager.agents[key], ack, session=manager.agent_sessions[key])
        self.finish(key, manager.agents[key])
        other = self.store.create_task('Resource holder', project_id=self.project['project_id'])
        claim_id = self.store.create_resource_claim(other['task_id'], **claim)
        manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands), 3)  # only Parent result-review resume
        self.store.release_resource_claim(other['task_id'], claim_id)
        progress = manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(progress[0]['phase'], 'started', progress)
        self.assertEqual(len(self.commands), 4)


if __name__ == '__main__':
    unittest.main()
