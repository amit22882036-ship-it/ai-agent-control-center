"""Multi-round authority, migration and bounded admission regressions."""
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from uuid import uuid4

from app import delegation_rounds as rounds, delegation_orchestration as domain
from app import delegation_materialization as materialization
from app.persistence import AgentStore
import test_delegation_hybrid as hybrid
from test_delegation_hybrid import request


class RoundTests(unittest.TestCase):
    setUp = hybrid.HybridDomainTests.setUp
    agent = hybrid.HybridDomainTests.agent
    task = hybrid.HybridDomainTests.task
    worker = hybrid.HybridDomainTests.worker
    origin_context = hybrid.HybridDomainTests.origin_context
    accept = hybrid.HybridDomainTests.accept
    snapshot = hybrid.HybridDomainTests.snapshot
    attach = hybrid.HybridDomainTests.attach
    admit = hybrid.HybridDomainTests.admit
    enable = hybrid.HybridDomainTests.enable
    current = hybrid.HybridDomainTests.current
    rows = hybrid.HybridDomainTests.rows
    save_status = hybrid.HybridDomainTests.save_status
    complete_child = hybrid.HybridDomainTests.complete_child
    make_deliverable = hybrid.HybridDomainTests.make_deliverable
    prepare = hybrid.HybridDomainTests.prepare
    launch = hybrid.HybridDomainTests.launch
    acknowledgement = hybrid.HybridDomainTests.acknowledgement
    ack = hybrid.HybridDomainTests.ack
    planned = hybrid.HybridDomainTests.planned

    def ready(self, identifier):
        record = self.store.get_delegation(identifier)
        if record['child_task_id']:
            with self.store._connection() as db:
                if db.execute('SELECT 1 FROM task_workspaces WHERE task_id=?',(record['child_task_id'],)).fetchone():
                    return record['child_task_id']
        return hybrid.HybridDomainTests.ready(self,identifier)

    def history(self):
        with self.store._connection() as db:
            return rounds.history(db, self.parent['task_id'])

    def new_round(self, origin, key='b', plan=None):
        return self.accept(origin, request(plan, key))

    def independent(self):
        identifier, child = self.planned()
        launch, origin = self.launch()
        return identifier, child, origin

    def test_multiple_rounds_preserve_old_pending_child_and_history(self):
        first, child, origin = self.independent()
        history = self.history()
        second = self.new_round(origin)
        self.assertEqual(len(self.history()), 2)
        self.assertEqual(self.history()[0], history[0])
        self.assertNotEqual(self.history()[0]['round_id'], self.history()[1]['round_id'])
        self.assertEqual(self.attach(first)['child_task_id'], child)
        self.assertIsNotNone(self.attach(second['delegation_ids'][0])['child_task_id'])
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']),self.origin)

    def test_new_round_after_acknowledgement_and_finish_continuation(self):
        first, = self.enable()
        self.make_deliverable(first)
        delivery, origin = self.launch()
        self.ack(origin, self.acknowledgement(delivery))
        _, origin = self.launch()
        second = self.new_round(origin)
        self.assertEqual(len(self.history()),2)
        self.assertEqual(self.rows()[0]['phase'],'accepted')
        self.assertIsNotNone(self.attach(second['delegation_ids'][0]))

    def test_new_round_while_old_child_running_does_not_replace_worker(self):
        first, child, origin = self.independent()
        key, _ = self.admit(first)
        record = next(r for r in self.store.load_agents() if r['agent_id']==origin.agent_id)
        record.update(agent_id=key,parent_id=origin.agent_id,status='running',task=self.store.get_task(child)['description'])
        self.store.save_agent(record,assignment_task_id=child)
        assignment = self.store.get_active_assignment_for_task(child)
        self.new_round(origin)
        self.assertEqual(self.store.get_active_assignment_for_task(child),assignment)
        self.assertEqual(next(r for r in self.store.load_agents() if r['agent_id']==key)['status'],'running')
        with self.assertRaises(ValueError):
            self.admit(first)

    def test_multiple_plans_use_current_scopes_without_rewriting_previous_plan(self):
        first, child, origin = self.independent()
        old = self.history()[0]
        plan = old['receipt']['continuation']
        second = self.new_round(origin, plan={**plan,'instruction':'Another explicit independent step'})
        child2 = self.ready(second['delegation_ids'][0])
        self.store.create_work_intent(child2,namespace='component',key='child2',mode='single_owner')
        launch, _ = self.launch()
        self.assertEqual(launch['mode'],'independent')
        self.assertEqual(self.history()[0],old)
        self.assertIn('Another explicit independent step',launch['prompt'])

    def test_exact_message_retry_is_read_only_and_no_duplicate_round(self):
        _, _, origin = self.independent()
        receipt = self.new_round(origin)
        before = self.snapshot()
        self.assertEqual(self.new_round(origin),receipt)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.history()),2)

    def test_replacement_generation_logical_retry_does_not_reset_history_or_resume(self):
        first, child, origin = self.independent()
        old = self.history()[0]
        replay = self.accept(origin,request(old['receipt']['continuation'],'a'))
        self.assertEqual(replay,old['receipt'])
        self.assertEqual(self.history(),[old])
        self.assertEqual(self.current()['phase'],'stopped')
        with self.assertRaises(ValueError):
            self.prepare()

    def test_conflicting_key_atomically_rejects_entire_new_batch(self):
        _, _, origin = self.independent()
        payload = json.loads(request(None,'new','a'))
        payload['requests'][1]['instruction']='Changed work'
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError,'different delegation instruction'):
            self.accept(origin,json.dumps(payload))
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(len(self.history()),1)

    def test_stale_generation_cannot_create_round(self):
        _, _, origin = self.independent()
        self.store.begin_delegation_execution(origin.agent_id,str(uuid4()))
        with self.assertRaisesRegex(ValueError,'Stale'):
            self.new_round(origin)
        self.assertEqual(len(self.history()),1)

    def test_concurrent_retries_create_one_round_and_one_child(self):
        _, _, origin = self.independent()
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(lambda _:self.new_round(origin), range(2)))
        self.assertEqual(responses[0],responses[1])
        with ThreadPoolExecutor(2) as pool:
            children = list(pool.map(self.attach,[responses[0]['delegation_ids'][0]]*2))
        self.assertEqual(children[0]['child_task_id'],children[1]['child_task_id'])
        self.assertEqual(len(self.history()),2)

    def test_conflicting_concurrent_messages_cannot_both_commit(self):
        _, _, origin = self.independent()
        def attempt(key):
            try:
                return self.new_round(origin,key)
            except ValueError:
                return None
        with ThreadPoolExecutor(2) as pool:
            responses = list(pool.map(attempt,['b','c']))
        self.assertEqual(sum(r is not None for r in responses),1)
        self.assertEqual(len(self.history()),2)

    def test_stop_pause_cancel_block_new_round_and_old_materialization(self):
        for control in ('stop','paused','canceled'):
            with self.subTest(control=control):
                self.setUp()
                first, _, origin = self.independent()
                with self.store._connection() as db:
                    if control=='stop':
                        domain.revoke(db,self.parent['task_id'])
                    else:
                        db.execute('UPDATE tasks SET control_intent=? WHERE task_id=?',(control,self.parent['task_id']))
                with self.assertRaises(ValueError):
                    self.new_round(origin)
                with self.assertRaises(ValueError):
                    self.attach(first)

    def test_new_round_cannot_orphan_unacknowledged_delivery(self):
        first, = self.enable()
        self.make_deliverable(first)
        delivery, origin = self.launch()
        with self.assertRaisesRegex(ValueError,'acknowledge'):
            self.new_round(origin)
        self.assertEqual(len(self.history()),1)
        self.ack(origin,self.acknowledgement(delivery))
        self.assertEqual(self.rows()[0]['phase'],'accepted')

    def test_results_from_different_rounds_can_be_acknowledged_incrementally(self):
        first, child, origin = self.independent()
        second = self.new_round(origin)['delegation_ids'][0]
        # Older result first, while newer work remains pending.
        self.make_deliverable(first)
        delivery, origin = self.launch()
        self.ack(origin,self.acknowledgement(delivery))
        self.assertIsNone(self.prepare())
        self.make_deliverable(second)
        delivery, origin = self.launch()
        self.ack(origin,self.acknowledgement(delivery))
        self.assertEqual([r['phase'] for r in self.rows()],['accepted','accepted'])
        launch, _ = self.launch()
        self.assertEqual(launch['mode'],'finishing')

    def test_unresolved_older_round_prevents_completion(self):
        first, child, origin = self.independent()
        second = self.new_round(origin)['delegation_ids'][0]
        self.make_deliverable(second)
        # Parent review must prove nonoverlap with the older pending child.
        delivery, origin = self.launch()
        self.ack(origin,self.acknowledgement(delivery))
        self.save_status(self.origin['agent_id'],'finished')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'blocked')
        self.assertIsNone(self.prepare())

    def test_restart_retains_all_receipts_and_results_without_launch(self):
        _, _, origin = self.independent()
        self.new_round(origin)
        before = self.history()
        self.store = AgentStore(self.path)
        self.store.reconcile_task_recovery()
        self.assertEqual(self.history(),before)
        self.assertEqual(len(self.store.list_tasks()),2)

    def test_more_than_sixteen_historical_children_do_not_exhaust_capacity(self):
        # Exercise real round/ack/finish admission repeatedly, with elapsed time.
        with patch.object(rounds,'now',return_value=1000) as clock:
            identifier, = self.enable()
            for index in range(18):
                self.make_deliverable(identifier)
                delivery, origin = self.launch()
                self.ack(origin,self.acknowledgement(delivery))
                _, origin = self.launch()
                clock.return_value += 61
                identifier = self.new_round(origin,str(index))['delegation_ids'][0]
            self.assertIsNotNone(self.attach(identifier)['child_task_id'])
        self.assertEqual(len(self.history()),19)

    def test_burst_retains_round_and_defers_child_then_allows_later_admission(self):
        with patch.object(rounds,'now',return_value=1000) as clock:
            identifier, = self.enable()
            for index in range(4):
                self.make_deliverable(identifier)
                delivery, origin = self.launch()
                self.ack(origin,self.acknowledgement(delivery))
                _, origin = self.launch()
                identifier = self.new_round(origin,str(index))['delegation_ids'][0]
            self.assertEqual(len(self.history()),5)
            with self.assertRaisesRegex(ValueError,'rate capacity'):
                self.attach(identifier)
            self.assertEqual(self.store.get_delegation(identifier)['status'],'requested')
            clock.return_value = 1061
            self.assertIsNotNone(self.attach(identifier)['child_task_id'])

    def test_direct_result_transition_stays_forbidden_with_accurate_error(self):
        identifier, = self.enable()
        with self.assertRaisesRegex(ValueError,'authorized orchestration services'):
            self.store.transition_delegation(identifier,'result_ready')

    def test_results_from_two_rounds_share_one_delivery_without_losing_provenance(self):
        first, _, origin = self.independent()
        second = self.new_round(origin)['delegation_ids'][0]
        self.make_deliverable(second)
        self.make_deliverable(first)
        delivery, origin = self.launch()
        self.assertEqual({r['delegation_id'] for r in self.rows()},{first,second})
        self.ack(origin,self.acknowledgement(delivery))
        self.ack(origin,self.acknowledgement(delivery) | {'results':[
            json.loads(r['acknowledgement']) for r in self.rows()]})
        self.assertTrue(all(r['phase']=='accepted' for r in self.rows()))

    def test_new_round_record_failure_rolls_back_delegations_and_current_receipt(self):
        _, _, origin = self.independent()
        before=self.snapshot()
        old=self.history()
        actual=rounds.record
        def broken(*args,**kwargs):
            actual(*args,**kwargs)
            raise sqlite3.OperationalError('injected after round insert')
        with patch.object(rounds,'record',side_effect=broken),self.assertRaises(sqlite3.OperationalError):
            self.new_round(origin)
        self.assertEqual(self.history(),old)
        self.assertEqual(self.snapshot(),before)

    def test_canceled_old_delegation_is_not_revived_by_new_round(self):
        first, _, origin=self.independent()
        self.store.transition_delegation(first,'canceled')
        self.new_round(origin)
        with self.assertRaisesRegex(ValueError,'canceled'):
            self.attach(first)

    def test_concurrent_worker_limit_counts_children_from_older_rounds(self):
        first, _, origin=self.independent()
        key,_=self.admit(first)
        second=self.new_round(origin)['delegation_ids'][0]
        self.ready(second)
        with self.assertRaisesRegex(ValueError,'concurrency capacity'):
            self.admit(second,materialization.Limits(concurrent_children=1))
        self.assertEqual(self.store.get_delegation(first)['materialization']['launch_agent_id'],key)
        self.assertEqual(self.store.get_delegation(second)['materialization']['phase'],'workspace_ready')

    def test_v20_nonhybrid_receipt_is_archived_without_activation(self):
        receipt=self.accept()
        with self.store._connection() as db:
            for table in ('delegation_round_requests','delegation_rounds','delegation_deliveries',
                          'delegation_results','delegation_continuations'):
                db.execute('DROP TABLE '+table)
            db.execute('ALTER TABLE agents DROP COLUMN hybrid_delegation_enabled')
            db.execute('PRAGMA user_version=20')
        self.store=AgentStore(self.path)
        self.assertEqual(self.history()[0]['receipt'],receipt)
        self.assertIsNone(self.history()[0]['accepted_at'])
        self.assertIsNone(self.current())
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT hybrid_delegation_enabled FROM agents').fetchone()[0],0)
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_restart_with_unacknowledged_cross_round_delivery_preserves_evidence(self):
        first, _, origin=self.independent()
        second=self.new_round(origin)['delegation_ids'][0]
        self.make_deliverable(first)
        self.make_deliverable(second)
        delivery, origin=self.launch()
        results=self.rows()
        history=self.history()
        self.store=AgentStore(self.path)
        self.store.reconcile_task_recovery()
        self.assertEqual(self.rows(),results)
        self.assertEqual(self.history(),history)
        self.assertEqual(self.current()['phase'],'recovery_required')
        with self.assertRaises(ValueError):
            self.prepare()
        with self.assertRaises(ValueError):
            self.ack(origin,self.acknowledgement(delivery))

    def test_v21_migration_keeps_cursor_receipt_and_rolls_back_on_failure(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        old_cursor = self.current()
        old_results = self.rows()
        receipt = self.history()[0]['receipt']
        with self.store._connection() as db:
            db.execute('DROP TABLE delegation_round_requests')
            db.execute('DROP TABLE delegation_rounds')
            db.execute('PRAGMA user_version=21')
        real = rounds.record
        def fail(*args,**kwargs):
            real(*args,**kwargs)
            raise sqlite3.IntegrityError('injected migration failure')
        with patch.object(rounds,'record',side_effect=fail),self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],21)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_rounds'").fetchone())
        self.store = AgentStore(self.path)
        self.assertEqual(self.current(),old_cursor)
        self.assertEqual(self.rows(),old_results)
        self.assertEqual(self.history()[0]['receipt'],receipt)
        self.assertEqual(self.history()[0]['round_id'],old_cursor['batch_id'])
        with self.store._connection() as db:
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())


if __name__=='__main__':
    unittest.main()
