"""CB-24: recovery-required transitions must not consume the bounded transition batch."""
import json
import sqlite3
import unittest
from uuid import uuid4

from app import replanning
import test_replanning

BATCH = 16


class TransitionBatchTests(unittest.TestCase):
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
    transitions = test_replanning.ReplanningTests.transitions
    apply = test_replanning.ReplanningTests.apply

    def accept(self, key, change):
        origin = self.process._control_center_execution.origin
        message = self.decision(decision_key=key, changes=[change])
        return self.store.accept_replanning_message(origin, message, session_id=test_replanning.manager.agent_sessions[self.key],
                                                    message_id='replan-' + key)

    def stuck(self, count, decision_id):
        """Inspection-only rows that the pass must skip (fixture inserted with FKs off)."""
        with sqlite3.connect(self.store.path) as db:
            db.execute('PRAGMA foreign_keys=OFF')
            for _ in range(count):
                db.execute('''INSERT INTO replan_transitions(transition_id,decision_id,delegation_id,task_id,action,
                    expected,content,phase,reason) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (str(uuid4()), decision_id, str(uuid4()), None, 'retain', 'stale', json.dumps({}),
                     'recovery_required', 'Interrupted stop; physical termination requires inspection'))
        db.close()

    def phases(self):
        return [t['phase'] for t in self.transitions()]

    def test_eligible_transition_after_a_full_batch_of_recovery_rows_is_processed(self):
        self.accept('first', self.change(1, action='retain'))
        first, = self.transitions()
        self.stuck(BATCH, first['decision_id'])
        # A second eligible retain of the other real target, journaled after the stuck rows
        # (one execution submits one decision, so it is added to the same decision).
        target = self.change(0, action='retain')
        with sqlite3.connect(self.store.path) as db:
            db.execute('''INSERT INTO replan_transitions(transition_id,decision_id,delegation_id,task_id,action,
                expected,content,phase,reason) VALUES (?,?,?,?,?,?,?,?,?)''',
                (str(uuid4()), first['decision_id'], target['delegation_id'], self.targets[0]['child_task_id'],
                 'retain', target['expected'], json.dumps(target), 'validated', 'Validated replanning transition'))
        db.close()
        before = self.phases()
        self.assertEqual(before.count('recovery_required'), BATCH)
        self.assertEqual((before[0], before[-1]), ('validated', 'validated'))  # Eligible rows around the stuck ones.
        result = self.apply()
        self.assertEqual(result[0]['phase'], 'applied', result[0])
        self.assertEqual(result[-1]['phase'], 'applied', result[-1])  # Before the fix: never reached.
        self.assertEqual(self.phases().count('recovery_required'), BATCH)  # Untouched, still inspectable.

    def test_batch_bound_still_applies_to_eligible_transitions(self):
        self.accept('first', self.change(1, action='retain'))
        first, = self.transitions()
        self.stuck(BATCH + 4, first['decision_id'])
        with sqlite3.connect(self.store.path) as db:
            db.execute("UPDATE replan_transitions SET phase='validated' WHERE phase='recovery_required'")
        db.close()
        processed = []
        original = replanning.TransitionService._advance
        replanning.TransitionService._advance = lambda service, row: processed.append(row['transition_id'])
        try:
            self.apply()
        finally:
            replanning.TransitionService._advance = original
        self.assertEqual(len(processed), BATCH)


if __name__ == '__main__':
    unittest.main()
