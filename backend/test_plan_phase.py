"""The plan phase: a read-only planning turn whose result is the plan, then execution by policy.

Synthetic provider events and fake processes only (mock CLI); no model runs."""
import json
import unittest
from unittest.mock import patch

from app import agent_manager as manager, plan_phase, planning
import test_orchestration_decision as decisions
import test_planning as plans


def plan_message(decision='propose', **changes):
    items = [plans.item('search-core', deliverable='Local search service'),
             plans.item('demo-ui', deliverable='Search interface')] if decision == 'propose' else []
    return plans.decision(decision=decision, decision_key='frameseek-plan', items=items,
                          rationale='Separable parts once docs/CONTRACT.md defines the search API.', **changes)


class PlanPhaseTests(unittest.TestCase):
    setUp = decisions.DecisionTests.setUp
    patch = decisions.DecisionTests.patch
    stream = decisions.DecisionTests.stream
    finish = decisions.DecisionTests.finish
    launch = decisions.DecisionTests.launch
    decision = decisions.DecisionTests.decision

    def start(self, policy='lead_decides'):
        return self.launch(split_policy=policy)

    def phase(self):
        return manager.orchestration_status(self.task['task_id'])['plan_phase']

    def prompt(self, key):
        return manager.agents[key].stdin.getvalue()

    def access(self):
        """The access level of each Codex command built so far (the fake builder records its calls)."""
        return [call.args[0] if call.args else call.kwargs.get('sandbox_level', 'read-only')
                for call in manager._codex_command.call_args_list]

    def test_the_first_turn_plans_read_only_with_the_request_first(self):
        key, process = self.start()
        prompt = process.stdin.getvalue()
        self.assertTrue(prompt.startswith('User request:\nParent work\n\nControl Center planning turn'))
        self.assertIn('Control Center planning turn (helpers are enabled; split policy: the lead decides).', prompt)
        self.assertIn('An empty or new project is not by itself a reason to work alone', prompt)
        self.assertIn('Control Center operating rule (local work only)', prompt)
        # No delegation, replanning or waiting protocols in the planning turn.
        for absent in (decisions.DECISION_HEADER, 'replanning', 'CONTROL_CENTER_WAITING', 'Delegation criteria.'):
            self.assertNotIn(absent, prompt)
        self.assertEqual(self.access()[-1], 'read-only')
        self.assertEqual(self.phase(), {'policy': 'lead_decides', 'phase': 'planning', 'retries': 0, 'plan_revision': None})

    def test_a_plan_only_turn_succeeds_and_the_lead_is_resumed_to_execute_it(self):
        key, process = self.start()
        self.stream(key, process, [plan_message()])
        self.finish(key, process)
        self.assertEqual(self.phase()['phase'], 'executing')
        self.assertEqual(self.phase()['plan_revision'], 1)
        decision = self.decision()
        self.assertEqual((decision['state'], decision['rationale']),
                         ('proposed', 'Separable parts once docs/CONTRACT.md defines the search API.'))
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.access()[-1], 'workspace-write')  # Execution has the lead's own access.
        resumed = self.prompt(key)
        self.assertIn('Control Center: your plan (propose) was recorded.', resumed)
        self.assertIn('end this turn with ONE delegation decision', resumed)
        self.assertIn(decisions.DECISION_HEADER, resumed)
        self.assertNotIn('Early planning decision (required', resumed)  # Already decided.
        self.assertEqual(manager.agent_statuses[key], 'running')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')

    def test_an_independent_plan_is_executed_by_the_lead_itself(self):
        key, process = self.start()
        self.stream(key, process, [plan_message('independent')])
        self.finish(key, process)
        self.assertEqual(self.decision()['state'], 'independent')
        self.assertIn('your plan (independent) was recorded', self.prompt(key))

    def test_an_invalid_plan_gets_one_correction_then_stops(self):
        key, process = self.start()
        self.stream(key, process, ['I will just build it myself.'])
        self.finish(key, process)
        self.assertEqual(len(self.commands), 2)
        self.assertEqual(self.access()[-1], 'read-only')  # Still the planning turn.
        retry = self.prompt(key)
        self.assertTrue(retry.startswith('Control Center: your planning message was not accepted:'))
        self.assertIn('exactly one control-center.planning.v1 JSON object', retry)
        self.assertEqual(self.phase()['retries'], 1)
        second = manager.agents[key]
        self.stream(key, second, ['Still prose.'])
        self.finish(key, second)
        self.assertEqual(len(self.commands), 2)  # No second correction.
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.phase()['phase'], 'planning')

    def test_a_corrected_plan_after_the_retry_is_accepted(self):
        key, process = self.start()
        self.stream(key, process, ['{"protocol":"control-center.planning.v1","decision":"propose"}'])
        self.finish(key, process)
        self.assertEqual(len(self.commands), 2)
        second = manager.agents[key]
        self.stream(key, second, [plan_message()])
        self.finish(key, second)
        self.assertEqual(self.phase()['phase'], 'executing')
        self.assertEqual(len(self.commands), 3)

    def test_approve_policy_waits_in_needs_you_and_the_reply_starts_execution(self):
        key, process = self.start('approve')
        self.assertIn('The person reviews this plan before any work starts', process.stdin.getvalue())
        self.stream(key, process, [plan_message()])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        question = manager.agent_waiting_questions[key]
        self.assertTrue(question.startswith('Plan ready for your approval. The lead proposes 2 part(s) for helpers:'))
        self.assertIn("Reply 'approve' to start", question)
        self.assertEqual(self.phase()['phase'], 'awaiting_approval')
        # The interface shows the plan part by part (name and deliverable) with Approve / changes / Work alone.
        proposal = self.phase()['proposal']
        self.assertEqual(proposal['decision'], 'propose')
        self.assertEqual(len(proposal['items']), 2)
        self.assertTrue(all(item['proposal_id'] and item['deliverable'] for item in proposal['items']))
        self.assertEqual(len(self.commands), 1)
        with patch.object(manager, '_resume_cwd', return_value=self.root):
            manager.reply_agent(key, 'approve')
        self.assertEqual(self.phase()['phase'], 'executing')
        self.assertNotIn('proposal', self.phase())  # Only while it waits for the person.
        resumed = self.prompt(key)
        self.assertIn('the person reviewed your plan (revision 1) and replied:\napprove', resumed)
        self.assertEqual(self.access()[-1], 'workspace-write')

    def test_always_policy_asks_to_split_whenever_possible(self):
        _, process = self.start('always')
        prompt = process.stdin.getvalue()
        self.assertIn('split policy: split whenever possible', prompt)
        self.assertIn('propose a split whenever two or more substantial parts can be separated', prompt)

    def test_delegating_straight_from_the_planning_turn_moves_on_to_execution(self):
        key, process = self.start()
        decision = json.dumps(dict(protocol='control-center.orchestration.v1', decision_key='split',
            rationale='Two separable parts.', delegate=[dict(proposal_id='a', deliverable='A', responsibility='Only a.',
                                                            scope=dict(namespace='component', key='csv/a'))]))
        self.stream(key, process, [decision])
        self.finish(key, process)
        self.assertEqual(self.decision()['state'], 'promoted')
        self.assertEqual(self.phase()['phase'], 'executing')

    def test_a_refused_decision_after_the_plan_gets_one_correction_with_the_reason(self):
        key, process = self.start()
        self.stream(key, process, [plan_message()])
        self.finish(key, process)
        executing = manager.agents[key]
        refused = json.dumps(dict(protocol='control-center.orchestration.v1', decision_key='frameseek-components',
            rationale='Contract written.', delegate=[dict(proposal_id='search-core', deliverable='Search',
                responsibility='Only backend/app/search.', scope=dict(namespace='component', key='frameseek/search-core'),
                provider='skynet')]))
        self.stream(key, executing, [refused])
        self.finish(key, executing)
        self.assertEqual(len(self.commands), 3)
        prompt = self.prompt(key)
        self.assertIn('Control Center: your delegation decision was not accepted: Unknown Child provider', prompt)
        self.assertIn('with a NEW decision_key', prompt)
        self.assertEqual(self.access()[-1], 'workspace-write')

    def test_a_split_policy_needs_a_root_lead_with_helpers(self):
        with self.assertRaisesRegex(ValueError, 'Split policy must be'):
            self.launch(split_policy='sometimes')
        with self.assertRaisesRegex(ValueError, 'root task whose lead may bring in helpers'):
            manager.start_agent('Parent', 'codex', 'read-only', task_id=self.task['task_id'], split_policy='approve')

    def test_without_a_split_policy_the_lead_keeps_the_single_turn_behaviour(self):
        _, process = self.launch()
        prompt = process.stdin.getvalue()
        self.assertIn(decisions.DECISION_HEADER, prompt)
        self.assertNotIn('Control Center planning turn', prompt)
        self.assertIsNone(self.phase())


class PlanPhaseUnitTests(unittest.TestCase):
    def test_policies_and_question(self):
        self.assertEqual(plan_phase.validate_policy('always'), 'always')
        with self.assertRaises(ValueError):
            plan_phase.validate_policy('never')
        question = plan_phase.approval_question({'decision': 'independent', 'items': [], 'rationale': 'Small.'})
        self.assertEqual(question, "Plan ready for your approval. The lead proposes to do the work itself. Reason: Small. "
                                   "Reply 'approve' to start, describe the changes you want, or reply 'reject' to have the lead work alone.")
        self.assertIn(planning.PROTOCOL, plan_phase.planning_prompt('lead_decides', 'Do it'))


if __name__ == '__main__':
    unittest.main()
