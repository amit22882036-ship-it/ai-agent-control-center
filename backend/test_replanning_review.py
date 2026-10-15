"""Replanning review at a Parent synchronization point (CB-5).

A pending review signal selects an 'independent' review continuation. It must
not depend on the original plan: there may be none, or it may already have
been consumed, in which case its intent IDs are stale.
"""
import unittest
from unittest.mock import patch

from app import agent_manager as manager, delegation_orchestration as domain, replanning
import test_delegation_hybrid
import test_replanning

REVIEW = 'Review meaningful new evidence'


class ReviewContinuationTests(unittest.TestCase):
    setUp = test_delegation_hybrid.HybridDomainTests.setUp
    agent = test_delegation_hybrid.HybridDomainTests.agent
    task = test_delegation_hybrid.HybridDomainTests.task
    worker = test_delegation_hybrid.HybridDomainTests.worker
    origin_context = test_delegation_hybrid.HybridDomainTests.origin_context
    accept = test_delegation_hybrid.HybridDomainTests.accept
    snapshot = test_delegation_hybrid.HybridDomainTests.snapshot
    attach = test_delegation_hybrid.HybridDomainTests.attach
    ready = test_delegation_hybrid.HybridDomainTests.ready
    admit = test_delegation_hybrid.HybridDomainTests.admit
    enable = test_delegation_hybrid.HybridDomainTests.enable
    current = test_delegation_hybrid.HybridDomainTests.current
    save_status = test_delegation_hybrid.HybridDomainTests.save_status
    prepare = test_delegation_hybrid.HybridDomainTests.prepare
    launch = test_delegation_hybrid.HybridDomainTests.launch

    def reviewable(self, planned):
        """A waiting hybrid Parent with authoritative, non-overlapping Parent and Child scopes."""
        self.parent_intent = self.store.create_work_intent(
            self.parent['task_id'], namespace='component', key='parent', mode='single_owner')[0]
        plan = dict(instruction='Independent work', intent_ids=[self.parent_intent]) if planned else None
        identifier, = self.enable(plan)
        child = self.ready(identifier)
        self.store.create_work_intent(child, namespace='component', key='child', mode='single_owner')
        with self.store._connection() as db:
            db.execute('UPDATE agents SET planning_enabled=1 WHERE agent_id=?', (self.origin['agent_id'],))
        return identifier

    def signal(self, key='failure:one'):
        with self.store._connection() as db:
            replanning.signal(db, self.parent['task_id'], key, 'Child execution requires review: failed')

    def undelivered(self):
        with self.store._connection() as db:
            return db.execute('SELECT count(*) FROM replan_signals WHERE task_id=? AND delivered=0',
                              (self.parent['task_id'],)).fetchone()[0]

    def test_review_without_plan_launches_review_continuation(self):
        self.reviewable(planned=False)
        self.assertIsNone(self.current()['plan'])
        self.signal()
        launch = self.prepare()  # Before the fix: TypeError ('NoneType' object is not subscriptable).
        self.assertEqual(launch['mode'], 'independent')
        self.assertTrue(launch['prompt'].startswith(REVIEW))
        self.assertIsNone(launch['delivery_id'])
        self.assertEqual(self.current()['phase'], 'launching')
        self.assertEqual(self.undelivered(), 0)  # Consumed once, with the launch journal.

    def test_review_after_consumed_plan_does_not_require_its_stale_intents(self):
        self.reviewable(planned=True)
        first, _ = self.launch()
        self.assertEqual(first['mode'], 'independent')
        self.assertTrue(first['prompt'].endswith('Independent work'))
        self.assertEqual(self.current()['plan_consumed'], 1)
        # Back at a synchronization point; the consumed plan's intent was released.
        self.store.release_work_intent(self.parent['task_id'], self.parent_intent)
        self.store.create_work_intent(self.parent['task_id'], namespace='component', key='parent-next', mode='single_owner')
        with self.store._connection() as db:
            db.execute("UPDATE delegation_continuations SET phase='waiting' WHERE task_id=?", (self.parent['task_id'],))
            db.execute("UPDATE agents SET status='stopped' WHERE agent_id=?", (self.origin['agent_id'],))
        self.signal()
        launch = self.prepare()  # Before the fix: stale plan intents re-required -> ValueError.
        self.assertEqual(launch['mode'], 'independent')
        self.assertTrue(launch['prompt'].startswith(REVIEW))

    def test_unconsumed_plan_still_requires_its_own_intents(self):
        self.reviewable(planned=True)
        self.store.release_work_intent(self.parent['task_id'], self.parent_intent)
        self.signal()
        with self.assertRaisesRegex(ValueError, 'no longer authoritative'):
            self.prepare()
        self.assertEqual(self.current()['phase'], 'waiting')
        self.assertEqual(self.undelivered(), 1)  # Rolled back: nothing consumed.

    def test_unconsumed_plan_takes_precedence_over_review(self):
        self.reviewable(planned=True)
        self.signal()
        launch = self.prepare()
        self.assertTrue(launch['prompt'].endswith('Independent work'))
        self.assertEqual(self.undelivered(), 1)  # Review waits for a later synchronization point.

    def test_review_still_enforces_scope_admission(self):
        self.reviewable(planned=False)
        # Remove the Parent's only authoritative scope: review cannot run beside the pending Child.
        self.store.release_work_intent(self.parent['task_id'], self.parent_intent)
        self.signal()
        with self.assertRaisesRegex(ValueError, 'non-overlapping scopes'):
            self.prepare()
        self.assertEqual(self.undelivered(), 1)


class ContinuationErrorTests(unittest.TestCase):
    """Unexpected continuation errors are recorded in last_error and still raised."""
    setUp = test_replanning.ReplanningTests.setUp
    tearDown = test_replanning.ReplanningTests.tearDown
    patch = test_replanning.ReplanningTests.patch
    stream = test_replanning.ReplanningTests.stream
    finish = test_replanning.ReplanningTests.finish
    launch = test_replanning.ReplanningTests.launch
    plan = test_replanning.ReplanningTests.plan
    message = test_replanning.ReplanningTests.message
    rows = test_replanning.ReplanningTests.rows
    workspace_fixture = test_replanning.ReplanningTests.workspace_fixture
    accepted = test_replanning.ReplanningTests.accepted

    def state(self):
        with self.store._connection() as db:
            return domain.state(db, self.task['task_id'])

    def waiting(self):
        with self.store._connection() as db:
            db.execute("UPDATE delegation_continuations SET phase='waiting',last_error=NULL WHERE task_id=?", (self.task['task_id'],))

    def test_unexpected_error_is_recorded_and_reraised(self):
        self.waiting()
        count = len(self.commands)
        with patch('app.agent_manager.DeliveryService.run', side_effect=TypeError("'NoneType' object is not subscriptable")):
            with self.assertRaises(TypeError):
                manager.advance_delegations(self.task['task_id'])
        state = self.state()
        self.assertEqual(state['phase'], 'waiting')
        self.assertEqual(state['last_error'],
                         "Unexpected continuation error (TypeError): 'NoneType' object is not subscriptable")
        self.assertEqual(len(self.commands), count)  # No retry and no additional launch.

    def test_expected_errors_keep_existing_handling(self):
        self.waiting()
        with patch('app.agent_manager.DeliveryService.run', side_effect=ValueError('Scopes overlap')):
            self.assertIsNone(manager.advance_delegations(self.task['task_id']))
        self.assertEqual(self.state()['last_error'], 'Scopes overlap')

    def test_unexpected_error_does_not_overwrite_an_inspection_reason(self):
        with self.store._connection() as db:
            db.execute("UPDATE delegation_continuations SET phase='recovery_required',last_error='Parent resume outcome requires inspection' WHERE task_id=?",
                       (self.task['task_id'],))
        with patch('app.agent_manager.DeliveryService.run', side_effect=AttributeError('missing')):
            with self.assertRaises(AttributeError):
                manager.advance_delegations(self.task['task_id'])
        self.assertEqual(self.state()['last_error'], 'Parent resume outcome requires inspection')


if __name__ == '__main__':
    unittest.main()
