"""Observable orchestration decisions; synthetic provider events, no real Codex processes."""
import io
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from app import agent_manager as manager, managed_orchestration as policy, planning, presentation, promotion
import test_planning as plans
import test_promotion as promotions

CAPABILITIES = ('planning_enabled', 'delegation_protocol_enabled', 'child_materialization_enabled',
                'hybrid_delegation_enabled', 'recursive_delegation_enabled')


def example_messages(task_id, revision=0):
    """The two JSON lines the managed prompt offers as its minimal example."""
    lines = [line for line in policy.example(task_id, revision).splitlines() if line.startswith('{')]
    assert len(lines) == 2
    return lines


class DecisionTests(unittest.TestCase):
    def setUp(self):
        promotions.PromotionTests.setUp(self)
        spawn = manager._spawn_process.side_effect

        class Prompt(io.StringIO):
            def close(self):
                pass  # keep the captured prompt readable after the launch closes stdin

        def capture(*args, **kwargs):
            process = spawn(*args, **kwargs)
            process.stdin = Prompt()
            return process

        self.patch(patch.object(manager, '_spawn_process', side_effect=capture))

    patch = promotions.PromotionTests.patch
    stream = promotions.PromotionTests.stream
    finish = promotions.PromotionTests.finish
    message = promotions.PromotionTests.message
    plan = promotions.PromotionTests.plan

    def launch(self, autonomous=True, **changes):
        options = {k: True for k in CAPABILITIES} if autonomous else {}
        options.update(changes)
        key = manager.start_agent('Build a CSV toolkit.', 'codex', 'workspace-write',
                                  task_id=self.task['task_id'], **options)
        return key, manager.agents[key]

    def decision(self, task_id=None):
        return manager.orchestration_status(task_id or self.task['task_id'])['decision']

    def snapshot(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return list(db.iterdump())

    def test_eligible_root_prompt_requests_one_decision_with_criteria_and_example(self):
        _, process = self.launch()
        prompt = process.stdin.getvalue()
        self.assertIn(policy.DECISION_LEAD, prompt)
        self.assertNotIn(planning.OPTIONAL_LEAD, prompt)
        self.assertIn('Planning metadata (control-center.planning.v1)', prompt)
        self.assertIn('Delegation criteria.', prompt)
        self.assertIn('It never authorizes bypassing admission, scope, resource', prompt)
        self.assertIn(policy.example(self.task['task_id'], 0), prompt)
        self.assertNotIn('independently authorized, select', prompt)
        self.assertEqual(self.decision()['state'], 'not_recorded')

    def test_example_validates_against_current_schemas(self):
        for revision in (0, 3):
            plan, promote = example_messages(self.task['task_id'], revision)
            self.assertEqual(planning.validate(plan)['base_revision'], revision)
            value = promotion.validate(promote)
            self.assertEqual(value['revision'], revision + 1)
            self.assertIsNotNone(value['items'][0]['scope'])

    def test_example_is_admitted_by_existing_backend_authority(self):
        key, process = self.launch()
        self.stream(key, process, example_messages(self.task['task_id']))
        self.finish(key, process)
        decision = self.decision()
        self.assertEqual(decision['state'], 'promoted')
        self.assertEqual(decision['revision'], 1)
        self.assertFalse(decision['promotion_rejected'])
        item, = decision['promotions']
        self.assertEqual(item['admission_disposition'], 'ready')
        self.assertIsNotNone(item['child_task_id'])
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(len(self.commands), 1)

    def test_independent_completion_records_rationale_without_children(self):
        key, process = self.launch()
        self.stream(key, process, [plans.decision(), 'Implemented and verified.'])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        decision = self.decision()
        self.assertEqual((decision['state'], decision['revision']), ('independent', 1))
        self.assertEqual(decision['rationale'], 'One small deliverable; coordination adds no benefit.')
        self.assertEqual(decision['promotions'], [])
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])
        self.assertEqual(len(self.commands), 1)

    def test_missing_decision_is_observable_nonfatal_and_costs_no_continuation(self):
        key, process = self.launch()
        self.stream(key, process, ['Implemented and verified.'])
        self.finish(key, process)
        for _ in range(3):
            manager.get_agent(key)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        decision = self.decision()
        self.assertEqual(decision['state'], 'not_recorded')
        self.assertIsNone(decision['rationale'])
        self.assertEqual(len(self.commands), 1)

    def test_proposal_without_promotion_stays_proposed(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), 'Done after all.'])
        self.finish(key, process)
        decision = self.decision()
        self.assertEqual(decision['state'], 'proposed')
        self.assertEqual(decision['promotions'], [])
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_null_scope_is_rejected_and_not_reported_as_promoted(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message([promotions.selection(scope=None)])])
        self.finish(key, process)
        decision = self.decision()
        self.assertEqual(decision['state'], 'proposed')
        self.assertTrue(decision['promotion_rejected'])
        item, = decision['promotions']
        self.assertEqual(item['admission_disposition'], 'conflict')
        self.assertIsNone(item['child_task_id'])

    def test_partially_admitted_promotion_is_promoted_with_visible_rejection(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message([promotions.selection(),
                                                               promotions.selection('b', scope=None)])])
        self.finish(key, process)
        decision = self.decision()
        self.assertEqual(decision['state'], 'promoted')
        self.assertFalse(decision['promotion_rejected'])
        self.assertEqual(sorted(i['admission_disposition'] for i in decision['promotions']), ['conflict', 'ready'])

    def test_malformed_promotion_rolls_back_and_is_not_recorded(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message([promotions.selection('missing')])])
        self.finish(key, process)
        decision = self.decision()
        self.assertEqual(decision['state'], 'not_recorded')
        self.assertEqual(decision['promotions'], [])
        self.assertEqual(self.store.list_delegations(self.task['task_id']), [])

    def test_resume_keeps_decision_and_does_not_request_another(self):
        key, process = self.launch()
        self.stream(key, process, [plans.decision(), 'CONTROL_CENTER_WAITING: Which delimiter?'])
        self.finish(key, process)
        self.assertEqual(self.decision()['state'], 'independent')
        with patch.object(manager, '_resume_cwd', return_value=self.root):
            manager.reply_agent(key, 'Comma')
        prompt = manager.agents[key].stdin.getvalue()
        self.assertNotIn(policy.DECISION_LEAD, prompt)
        self.assertIn(planning.OPTIONAL_LEAD, prompt)
        self.assertIn('current accepted revision: 1', prompt)
        self.assertIn(policy.example(self.task['task_id'], 1), prompt)
        # The superseded generation cannot write; the current one's identical
        # replay is a receipt, never another revision.
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.store.accept_planning_messages(manager._structured(process).origin,
                [('message_0', plans.decision())], session_id=manager.agent_sessions[key])
        replay = self.store.accept_planning_messages(manager._structured(manager.agents[key]).origin,
            [('replayed', plans.decision())], session_id=manager.agent_sessions[key])
        self.assertEqual(replay[0]['revision'], 1)
        self.assertEqual(len(self.store.planning_history(self.task['task_id'])), 1)
        self.assertEqual(self.decision()['state'], 'independent')

    def test_resume_without_decision_requests_it_in_the_existing_turn(self):
        key, process = self.launch()
        self.stream(key, process, ['CONTROL_CENTER_WAITING: Which delimiter?'])
        self.finish(key, process)
        with patch.object(manager, '_resume_cwd', return_value=self.root):
            manager.reply_agent(key, 'Comma')
        self.assertIn(policy.DECISION_LEAD, manager.agents[key].stdin.getvalue())
        self.assertEqual(len(self.commands), 2)  # the user's reply only

    def test_replacement_assignment_does_not_inherit_a_stale_decision(self):
        key, process = self.launch()
        self.stream(key, process, ['Work continues'])
        self.store.accept_planning_messages(manager._structured(process).origin,
            [('plan', plans.decision())], session_id=manager.agent_sessions[key])
        old = self.decision()
        self.assertEqual(old['state'], 'independent')
        manager.stop_agent(key)
        replacement, _ = self.launch()
        decision = self.decision()
        self.assertEqual(decision['state'], 'not_recorded')
        self.assertNotEqual(decision['assignment_id'], old['assignment_id'])
        self.assertEqual(decision['superseded'], dict(revision=1, assignment_id=old['assignment_id'],
                                                      decision='independent'))
        self.assertIn(policy.DECISION_LEAD, manager.agents[replacement].stdin.getvalue())

    def test_child_and_planning_only_executions_are_not_asked_for_root_decisions(self):
        key, process = self.launch()
        self.stream(key, process, example_messages(self.task['task_id']))
        self.finish(key, process)
        promotions.PromotionTests.workspace_fixture(self)
        progress, = manager.materialize_delegations(self.task['task_id'])
        child = progress['launch_agent_id']
        prompt = manager.agents[child].stdin.getvalue()
        self.assertIn(planning.OPTIONAL_LEAD, prompt)
        self.assertNotIn(policy.DECISION_LEAD, prompt)
        child_task = self.store.agent_task_ids(child)[0]
        self.assertEqual(self.decision(child_task)['state'], 'not_recorded')
        other = self.store.create_task('Planning only', project_id=self.project['project_id'])
        planner = manager.start_agent('Planning only', 'codex', task_id=other['task_id'], planning_enabled=True)
        prompt = manager.agents[planner].stdin.getvalue()
        self.assertIn(planning.OPTIONAL_LEAD, prompt)
        self.assertNotIn(policy.DECISION_LEAD, prompt)
        self.assertNotIn('Minimal valid delegation example', prompt)
        self.assertEqual(self.decision(other['task_id'])['state'], 'not_authorized')

    def test_non_autonomous_and_unassigned_tasks_are_not_authorized(self):
        self.assertEqual(self.decision()['state'], 'not_authorized')
        key, process = self.launch(False)
        self.assertEqual(process.stdin.getvalue(), manager._codex_prompt(manager.agent_tasks[key]))
        self.assertEqual(self.decision()['state'], 'not_authorized')

    def test_status_is_read_only(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message([promotions.selection(),
                                                               promotions.selection('b', scope=None)])])
        self.finish(key, process)
        before = self.snapshot()
        with patch.object(manager, '_orchestration_pass', side_effect=AssertionError('read must not wake')):
            for _ in range(2):
                self.decision()
        self.assertEqual(self.snapshot(), before)

    def test_activity_projects_decision_enum_but_not_rationale(self):
        key, process = self.launch()
        self.stream(key, process, [plans.decision(rationale='Private provider reasoning text.'), 'Done'])
        self.finish(key, process)
        labels = [e['label'] for e in presentation.activity(self.store, key)['events'] if e['kind'] == 'plan']
        self.assertEqual(labels, ['Plan revision 1 accepted: independent'])
        self.assertNotIn('Private provider reasoning', json.dumps(presentation.activity(self.store, key)))


if __name__ == '__main__':
    unittest.main()
