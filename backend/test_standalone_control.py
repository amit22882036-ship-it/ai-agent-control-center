"""Regression tests from the dashboard autonomous-delegation E2E (Claude Code root, 2026-10-25).

1. The root emitted prose + planning JSON + promotion JSON in ONE assistant message. Nothing was
   recognized and the Task silently completed. Protocol objects are now accepted only as whole
   assistant messages, and a managed execution that combines one with other content stops with a
   visible reason instead of completing.
2. The root's Auto allocation policy was stored after the provider had already received its first
   prompt, so that prompt never offered Auto. It is now stored before the first prompt is built.

Only synthetic provider events and process doubles are used: no provider is invoked.
"""
import json
import unittest
from unittest.mock import patch

from app import agent_manager as manager, allocation, delegation_protocol, main, provider_events, provider_policy
from app.codex_events import CodexEvents, Execution
import test_planning as plans
import test_product_workspace as product
import test_promotion as promotions

PROSE = ("I've created the test suite (`test_calculator.py`) and README as the Parent's responsibility. "
         "Now recording the orchestration decision and delegation request.")


def duplicate_field(envelope):
    """Same object with its `protocol` field repeated: strict JSON decoding must reject it."""
    value = json.loads(envelope)
    return '{"protocol": %s, %s' % (json.dumps(value['protocol']), envelope[1:])


def combined(plan, promotion):
    """The exact shape observed in the failed run: prose, blank line, plan, blank line, promotion."""
    return f'{PROSE}\n\n{plan}\n\n{promotion}'


class StandaloneControlRuntimeTests(unittest.TestCase):
    """Managed (structured) executions through the real runtime and transactional storage."""
    setUp = promotions.PromotionTests.setUp
    patch = promotions.PromotionTests.patch
    stream = promotions.PromotionTests.stream
    finish = promotions.PromotionTests.finish
    launch = promotions.PromotionTests.launch
    plan = promotions.PromotionTests.plan
    message = promotions.PromotionTests.message
    rows = promotions.PromotionTests.rows

    def state(self):
        task_id = self.task['task_id']
        with self.store._connection() as db:
            return dict(
                plans=db.execute('SELECT COUNT(*) FROM planning_revisions WHERE task_id=?', (task_id,)).fetchone()[0],
                promotions=db.execute('SELECT COUNT(*) FROM promotion_decisions WHERE task_id=?', (task_id,)).fetchone()[0],
                items=db.execute('SELECT COUNT(*) FROM promotion_items WHERE task_id=?', (task_id,)).fetchone()[0],
                delegations=db.execute('SELECT COUNT(*) FROM delegations WHERE parent_task_id=?', (task_id,)).fetchone()[0],
                children=db.execute('SELECT COUNT(*) FROM tasks WHERE parent_task_id=?', (task_id,)).fetchone()[0])

    def output(self, key):
        return '\n'.join(self.store.full_output(key))

    def assert_stopped_with_nothing_accepted(self, key, reason=delegation_protocol.STANDALONE_REQUIRED):
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(self.state(), dict(plans=0, promotions=0, items=0, delegations=0, children=0))
        text = self.output(key)
        self.assertIn('--- Structured execution stopped; inspect output before retrying ---', text)
        self.assertIn(reason, text)
        self.assertEqual(len(self.commands), 1)  # No retry or other model call.

    # ---------------------------------------------------------------- standalone messages still work

    def test_standalone_plan_then_ordinary_completion_is_accepted(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), 'Work completed and checked.'])
        self.finish(key, process)
        self.assertEqual(self.state()['plans'], 1)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertNotIn(delegation_protocol.STANDALONE_REQUIRED, self.output(key))

    def test_standalone_promotion_after_an_accepted_standalone_plan_creates_the_child(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        row, = self.rows()
        self.assertEqual(row['disposition'], 'ready')
        self.assertIsNotNone(row['child_task_id'])
        self.assertEqual(self.state()['children'], 1)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIn('--- Delegation requests recorded; waiting for orchestration ---', self.output(key))

    # ---------------------------------------------------------------- the observed failure

    def test_prose_plan_and_promotion_in_one_message_stop_instead_of_completing(self):
        key, process = self.launch()
        self.stream(key, process, [combined(self.plan(), self.message())])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key)

    def test_a_combined_intermediate_message_is_not_partially_accepted(self):
        # The plan arrives with prose in an earlier message; a later standalone promotion cannot use it.
        key, process = self.launch()
        self.stream(key, process, [f'{PROSE}\n\n{self.plan()}', self.message()])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key)

    def test_pretty_printed_protocol_object_after_prose_is_also_refused(self):
        key, process = self.launch()
        self.stream(key, process, [PROSE + '\n' + json.dumps(json.loads(self.plan()), indent=2)])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key)

    def test_plan_and_promotion_without_prose_in_one_message_still_fail_closed(self):
        # Pre-existing rule: object-shaped but malformed (two objects) messages are rejected.
        key, process = self.launch()
        self.stream(key, process, [self.plan() + '\n\n' + self.message()])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertEqual(self.state(), dict(plans=0, promotions=0, items=0, delegations=0, children=0))
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')

    # ---------------------------------------------------------------- what must not be affected

    def test_quoted_examples_and_mentions_complete_normally(self):
        plan = self.plan()
        for final in (f'Done. The planning shape looks like:\n```json\n{plan}\n```',
                      f'Done. I did not delegate; an example is `{plan}`.',
                      f'Done.\n> {plan}',
                      'Done. No control-center.planning.v1 or control-center.promotion.v1 message was needed.',
                      'Done.\n{"protocol": "other.protocol", "note": "unrelated JSON"}'):
            with self.subTest(final=final[:40]):
                self.setUp()
                key, process = self.launch()
                self.stream(key, process, [final])
                self.finish(key, process)
                self.assertEqual(manager.agent_statuses[key], 'finished')
                self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
                self.assertNotIn(delegation_protocol.STANDALONE_REQUIRED, self.output(key))
                self.assertNotIn(delegation_protocol.MALFORMED_EMBEDDED, self.output(key))

    def test_tool_output_and_echoed_input_never_count_as_control(self):
        key, process = self.launch()
        echoed = f'{PROSE}\n\n{self.plan()}\n\n{self.message()}'  # e.g. a file or log containing protocol JSON
        self.stream(key, process, ['Checked the logs; nothing to delegate.'], tool=echoed)
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.state(), dict(plans=0, promotions=0, items=0, delegations=0, children=0))

    # ---------------------------------------------------------------- P2 A: malformed embedded protocol JSON

    def test_duplicate_key_plan_after_prose_stops_with_a_malformed_diagnostic(self):
        key, process = self.launch()
        self.stream(key, process, [f'{PROSE}\n\n{duplicate_field(self.plan())}'])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key, delegation_protocol.MALFORMED_EMBEDDED)
        self.assertNotIn(delegation_protocol.STANDALONE_REQUIRED, self.output(key))

    def test_duplicate_key_promotion_after_a_standalone_plan_creates_no_child(self):
        key, process = self.launch()
        self.stream(key, process, [self.plan(), f'{PROSE}\n{duplicate_field(self.message())}'])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        state = self.state()
        self.assertEqual((state['promotions'], state['items'], state['delegations'], state['children']), (0, 0, 0, 0))
        self.assertIn(delegation_protocol.MALFORMED_EMBEDDED, self.output(key))
        self.assertEqual(len(self.commands), 1)

    def test_duplicate_key_delegation_request_after_prose_is_refused(self):
        request = json.dumps(dict(protocol=delegation_protocol.PROTOCOL, action='request_delegations',
                                  requests=[dict(request_key='docs', instruction='Write the docs.')]))
        key, process = self.launch()
        self.stream(key, process, [f'{PROSE}\n{duplicate_field(request)}'])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key, delegation_protocol.MALFORMED_EMBEDDED)

    def test_the_combined_message_with_one_malformed_object_is_refused(self):
        key, process = self.launch()
        self.stream(key, process, [combined(self.plan(), self.message()[:-1])])  # Truncated promotion.
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key)  # The valid plan is found first.
        self.setUp()
        key, process = self.launch()
        self.stream(key, process, [combined(duplicate_field(self.plan()), self.message())])
        self.finish(key, process)
        self.assert_stopped_with_nothing_accepted(key, delegation_protocol.MALFORMED_EMBEDDED)

    def test_invalid_json_protocol_object_after_prose_is_refused(self):
        broken = self.plan().replace('"decision":', '"decision"', 1)
        self.assertNotEqual(broken, self.plan())
        for message in (f'{PROSE}\n{broken}', f'{PROSE}\n{self.plan()[:40]}',
                        PROSE + '\n{"decision_key": "x", "protocol": "control-center.planning.v1", items: []}'):
            with self.subTest(message=message[-50:]):
                self.setUp()
                key, process = self.launch()
                self.stream(key, process, [message])
                self.finish(key, process)
                self.assert_stopped_with_nothing_accepted(key, delegation_protocol.MALFORMED_EMBEDDED)

    # ---------------------------------------------------------------- P2 B: Markdown fences

    def test_protocol_json_inside_longer_or_nested_fences_completes_normally(self):
        plan = self.plan()
        bad = duplicate_field(plan)
        for final in (f'Done. Example:\n````markdown\n```json\n{plan}\n```\n{plan}\n````',
                      f'Done.\n`````\n````\n```\n{bad}\n```\n````\n`````',
                      f'Done.\n~~~~\n~~~\n{plan}\n~~~~',
                      f'Done.\n```\n~~~\n{plan}\n```',
                      f'Done.\n   ```json  \n{bad}\n   ```   \n',
                      f'Done.\n```\n{plan}\n'):  # An unclosed fence runs to the end of the message.
            with self.subTest(final=final[:30]):
                self.setUp()
                key, process = self.launch()
                self.stream(key, process, [final])
                self.finish(key, process)
                self.assertEqual(manager.agent_statuses[key], 'finished')
                self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
                self.assertEqual(self.state()['plans'], 0)
                self.assertNotIn('Structured execution stopped', self.output(key))

    def test_protocol_json_after_a_properly_closed_fence_is_still_detected(self):
        plan = self.plan()
        for message, reason in ((f'Example:\n````\n```\n{plan}\n````\n{plan}', delegation_protocol.STANDALONE_REQUIRED),
                                (f'Example:\n~~~\nx\n~~~~\n{duplicate_field(plan)}', delegation_protocol.MALFORMED_EMBEDDED),
                                (f'Example:\n```\nx\n```\n\n{plan}', delegation_protocol.STANDALONE_REQUIRED)):
            with self.subTest(message=message[:30]):
                self.setUp()
                key, process = self.launch()
                self.stream(key, process, [message])
                self.finish(key, process)
                self.assert_stopped_with_nothing_accepted(key, reason)

    def test_ordinary_completion_and_waiting_are_unchanged(self):
        key, process = self.launch()
        self.stream(key, process, ['All requested work is complete.'])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.setUp()
        key, process = self.launch()
        self.stream(key, process, ['I need one decision.\nCONTROL_CENTER_WAITING: Which module should the child own?'])
        self.finish(key, process)
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(manager.agent_waiting_questions[key], 'Which module should the child own?')


class ClaudeMessageCompositionTests(unittest.TestCase):
    """Claude Code stream-json: each assistant event is one accepted message; text joined within it."""
    plan = staticmethod(lambda: plans.decision(decision='propose', items=[plans.item('a')]))

    @staticmethod
    def promotion():
        return json.dumps(dict(protocol='control-center.promotion.v1', task_id='3f2a8d43-2b8c-4fd1-9f3e-1d2c3b4a5e6f',
                               revision=1, decision_key='choose', items=[promotions.selection('a')], continuation=None))

    @staticmethod
    def feed(events, managed=True):
        parser = provider_events.parser('claude')
        if managed:
            Execution(events=parser)  # Managed executions bind their parser in an Execution.
            parser.planning_enabled = True
        session = '0e7a2868-a041-4bde-a3d3-1d82addab8c8'
        lines = [dict(type='system', subtype='init', session_id=session, model='claude-fake')] + events
        for event in lines:
            parser.feed(json.dumps(event))
        return parser

    @staticmethod
    def assistant(uuid, *blocks):
        return dict(type='assistant', uuid=uuid, parent_tool_use_id=None,
                    message=dict(id='msg_' + uuid, content=list(blocks)))

    @staticmethod
    def text(value):
        return dict(type='text', text=value)

    @staticmethod
    def result(text):
        return dict(type='result', subtype='success', is_error=False, result=text,
                    session_id='0e7a2868-a041-4bde-a3d3-1d82addab8c8', uuid='result-1')

    def test_combined_text_in_one_assistant_message_fails_closed(self):
        message = combined(self.plan(), self.promotion())
        parser = self.feed([self.assistant('a1', self.text(message)), self.result(message)])
        self.assertEqual(parser.error, delegation_protocol.STANDALONE_REQUIRED)
        self.assertEqual(parser.plans, [])
        with self.assertRaisesRegex(ValueError, 'must be standalone JSON assistant messages'):
            parser.finish(0)

    def test_combined_text_blocks_in_one_assistant_event_fail_closed(self):
        parser = self.feed([self.assistant('a1', self.text(PROSE), self.text(self.plan()), self.text(self.promotion())),
                            self.result('\n'.join((PROSE, self.plan(), self.promotion())))])
        self.assertEqual(parser.error, delegation_protocol.STANDALONE_REQUIRED)

    def test_duplicate_key_plan_in_a_claude_message_fails_closed(self):
        message = f'{PROSE}\n\n{duplicate_field(self.plan())}'
        parser = self.feed([self.assistant('a1', self.text(message)), self.result(message)])
        self.assertEqual(parser.error, delegation_protocol.MALFORMED_EMBEDDED)
        self.assertEqual(parser.plans, [])
        with self.assertRaisesRegex(ValueError, 'malformed Control Center protocol object'):
            parser.finish(0)

    def test_plan_before_a_tool_call_and_a_final_promotion_are_separate_messages(self):
        tool = dict(type='tool_use', id='toolu_1', name='Read', input={'file_path': 'README.md'})
        parser = self.feed([self.assistant('a1', self.text(self.plan())), self.assistant('a2', tool),
                            dict(type='user', parent_tool_use_id=None,
                                 message=dict(content=[dict(type='tool_result', content=combined(self.plan(), self.promotion()))])),
                            self.assistant('a3', self.text(self.promotion())), self.result(self.promotion())])
        self.assertIsNone(parser.error)
        self.assertEqual([identifier for identifier, _ in parser.plans], ['a1'])
        self.assertEqual(parser.promotion_message, 'a3')
        self.assertEqual(parser.finish(0), self.promotion())

    def test_ordinary_unmanaged_claude_runs_are_not_affected(self):
        message = combined(self.plan(), self.promotion())
        parser = self.feed([self.assistant('a1', self.text(message)), self.result(message)], managed=False)
        self.assertIsNone(parser.error)
        self.assertFalse(parser.standalone_control)

    def test_every_managed_parser_enforces_the_rule(self):
        self.assertTrue(Execution().events.standalone_control)  # Codex default
        self.assertFalse(CodexEvents().standalone_control)
        self.assertTrue(Execution(events=provider_events.parser('gemini')).events.standalone_control)


class EmbeddedControlDetectorTests(unittest.TestCase):
    """The detector alone: a diagnostic string or None, never a parsed or executed object."""
    plan = ClaudeMessageCompositionTests.__dict__['plan']
    promotion = ClaudeMessageCompositionTests.__dict__['promotion']

    def detect(self, text):
        return delegation_protocol.embedded_control(text)

    def test_valid_and_malformed_protocol_objects_outside_fences(self):
        plan = self.plan()
        self.assertEqual(self.detect(f'{PROSE}\n{plan}'), delegation_protocol.STANDALONE_REQUIRED)
        for message in (f'{PROSE}\n{duplicate_field(plan)}', f'{PROSE}\n{duplicate_field(self.promotion())}',
                        f'{PROSE}\n  {plan[:-1]}', f'{PROSE}\n' + plan.replace(':', '', 1),
                        PROSE + '\n{"protocol": "control-center.delegation.v3", "protocol": "control-center.delegation.v3"}'):
            with self.subTest(message=message[-40:]):
                self.assertEqual(self.detect(message), delegation_protocol.MALFORMED_EMBEDDED)

    def test_nested_duplicate_fields_make_the_object_malformed(self):
        value = json.dumps(json.loads(self.plan()))
        nested = value.replace('"proposal_id": "a"', '"proposal_id": "a", "proposal_id": "a"', 1)
        self.assertNotEqual(nested, value)
        self.assertEqual(self.detect(f'Note:\n{nested}'), delegation_protocol.MALFORMED_EMBEDDED)

    def test_ordinary_content_is_not_a_control_request(self):
        plan = self.plan()
        for message in ('All done.', None, plan, duplicate_field(plan),  # Whole objects keep their own validation.
                        'Done.\n{"protocol": "other.v1", "protocol": "other.v1"}',
                        'Done.\n{"name": "x", "name": "y"}', 'Done.\n{not json at all}',
                        'Done.\n{"protocol": "other.v1", broken}', 'Done.\n{"see": "control-center.planning.v1", broken}',
                        f'Done; `{duplicate_field(plan)}` is wrong.', f'Done.\n> {duplicate_field(plan)}',
                        'The protocol field must be "control-center.planning.v1".'):
            with self.subTest(message=str(message)[:40]):
                self.assertIsNone(self.detect(message))

    def test_fences_close_only_with_the_same_character_and_enough_length(self):
        plan = self.plan()
        fenced = (('````', '```', '````'), ('~~~', '```', '~~~'), ('```', '~~~', '```'), ('~~~~~', '~~~~', '~~~~~'),
                  ('```', '```js', '```'))  # A closing fence never carries an info string.
        for opener, inner, closer in fenced:
            with self.subTest(opener=opener, inner=inner):
                self.assertIsNone(self.detect(f'Example:\n{opener}\n{inner}\n{plan}\n{closer}'))
                self.assertEqual(self.detect(f'Example:\n{opener}\n{inner}\n{plan}\n{closer}\n{plan}'),
                                 delegation_protocol.STANDALONE_REQUIRED)

    def test_fence_indentation_and_info_strings_follow_commonmark(self):
        plan = self.plan()
        self.assertIsNone(self.detect(f'x\n   ```json\n{plan}\n   ```  \t\n'))
        self.assertEqual(self.detect(f'x\n```\n{plan}\n```\n{plan}'), delegation_protocol.STANDALONE_REQUIRED)
        # Four spaces is indented code, not a fence; a backtick info string may not contain backticks.
        self.assertEqual(self.detect(f'x\n    ```\n{plan}'), delegation_protocol.STANDALONE_REQUIRED)
        self.assertEqual(self.detect(f'x\n``` a`b\n{plan}'), delegation_protocol.STANDALONE_REQUIRED)
        self.assertIsNone(self.detect(f'x\n~~~ a`b\n{plan}\n~~~'))
        # Trailing text after a would-be closer keeps the fence open.
        self.assertIsNone(self.detect(f'x\n```\n``` not a closer\n{plan}'))


class ProtocolInstructionTests(unittest.TestCase):
    def test_legacy_instructions_still_describe_two_separate_messages(self):
        # Legacy planning/promotion stays accepted; its texts remain accurate where still used.
        from app import managed_orchestration, planning, promotion
        example = managed_orchestration.legacy_example('3f2a8d43-2b8c-4fd1-9f3e-1d2c3b4a5e6f', 0)
        self.assertIn('It is TWO separate assistant messages, never one.', example)
        self.assertLess(example.index('Message 1, the plan'), example.index('Message 2, the promotion'))
        planning_text = planning.instruction(0)
        self.assertIn('The planning message contains only that\nJSON object: no prose and no other JSON object.', planning_text)
        self.assertIn('Text that merely quotes or explains JSON is not a control request.', planning_text)
        promotion_text = promotion.instruction('task')
        self.assertIn('never send the plan and the promotion in the same message', promotion_text)
        lines = example.splitlines()
        plan_line = next(line for line in lines if line.startswith('{"protocol":"control-center.planning.v1"'))
        promote_line = next(line for line in lines if line.startswith('{"protocol":"control-center.promotion.v1"'))
        self.assertEqual(planning.validate(plan_line)['decision_key'], 'split-cli')
        promotion.validate(promote_line)


class AutoPolicyBeforeFirstPromptTests(unittest.TestCase):
    """Root creation through the real HTTP route, with process doubles."""
    setUp = product.WorkspaceTests.setUp
    patch = product.WorkspaceTests.patch
    stream = product.WorkspaceTests.stream
    finish = product.WorkspaceTests.finish

    def start(self, expected=200, **options):
        prompts = []
        real = manager._execution_prompt

        def recording(agent_id, text, process):
            task_id = manager._agent_task_id(agent_id)
            with self.store._connection() as db:
                prompts.append(dict(policy=allocation.policy_for(db, task_id), prompt=real(agent_id, text, process)))
            return prompts[-1]['prompt']
        self.patch(patch.object(manager, '_project_root', self.root))
        with patch.object(manager, '_execution_prompt', side_effect=recording):
            response = product.TestClient(main.app).post('/agents/start', json=dict(
                task='Build a calculator; delegate the module to one Child.', agent_type='codex') | options)
        self.assertEqual(response.status_code, expected, response.text)
        return response, prompts

    def tasks(self):
        with self.store._connection() as db:
            return db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def test_the_policy_exists_and_the_first_prompt_carries_the_auto_contract(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            provider_policy.set_global(db, allowed=['codex', 'claude'])
        response, prompts = self.start(autonomous=True, auto_allocation_providers=['claude', 'codex'])
        first, = prompts  # Exactly one launch.
        self.assertIsNotNone(first['policy'])
        self.assertTrue(first['policy']['enabled'])
        self.assertEqual(first['policy']['providers'], ['claude', 'codex'])  # The user's selection, as given.
        self.assertIn('Auto allocation is authorized by the user for this Task tree.', first['prompt'])
        self.assertIn('"allocation": {"mode":"auto"', first['prompt'])
        # The single delegation decision is the default contract; Auto is stated once, after it.
        self.assertEqual(first['prompt'].count('Auto allocation is authorized by the user'), 1)
        self.assertLess(first['prompt'].index('Delegation decision (control-center.orchestration.v1).'),
                        first['prompt'].index('Auto allocation is authorized by the user'))
        key = response.json()['agent_id']
        task_id = self.store.agent_task_ids(key)[0]
        view = manager.auto_allocation_view(task_id)
        self.assertEqual((view['policy']['task_id'], view['own_revision'], view['inherited']), (task_id, 1, False))
        self.assertEqual(len(self.commands), 1)

    def test_roots_without_auto_are_unchanged(self):
        response, prompts = self.start(autonomous=True)
        first, = prompts
        self.assertIsNone(first['policy'])
        self.assertNotIn('Auto allocation is authorized', first['prompt'])
        self.start(autonomous=False)
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_allocation_policies').fetchone()[0], 0)

    def test_invalid_auto_requests_create_nothing_and_launch_nothing(self):
        before = self.tasks()
        for options in (dict(autonomous=True, auto_allocation_providers=[]),
                        dict(autonomous=True, auto_allocation_providers=['other']),
                        dict(autonomous=False, auto_allocation_providers=['codex'])):
            with self.subTest(options=options):
                _, prompts = self.start(expected=422, **options)
                self.assertEqual(prompts, [])
        self.assertEqual(self.tasks(), before)
        self.assertEqual(self.commands, [])
        with self.assertRaisesRegex(ValueError, 'new autonomous root agent'):
            manager.start_agent('Child', 'codex', task_id=self.task['task_id'], planning_enabled=True,
                                delegation_protocol_enabled=True, child_materialization_enabled=True,
                                auto_allocation_providers=['codex'])
        self.assertEqual(self.commands, [])

    def test_a_root_provider_outside_the_global_permission_is_refused_before_any_policy_or_task(self):
        before = self.tasks()
        response, prompts = self.start(expected=409, agent_type='claude', autonomous=True,
                                       auto_allocation_providers=['codex'])
        self.assertIn('not in your allowed providers', response.text)
        self.assertEqual((prompts, self.commands, self.tasks()), ([], [], before))
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auto_allocation_policies').fetchone()[0], 0)

    def test_a_policy_write_failure_launches_nothing(self):
        with patch.object(allocation, 'set_policy', side_effect=ValueError('policy write refused')):
            _, prompts = self.start(expected=422, autonomous=True, auto_allocation_providers=['codex'])
        self.assertEqual((prompts, self.commands), ([], []))


if __name__ == '__main__':
    unittest.main()
