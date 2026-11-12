"""control-center.orchestration.v1: one final delegation decision; structured failure diagnostics.

Runtime tests drive the real managed finalizer with synthetic Codex JSONL; parser tests feed synthetic
Claude Code / Gemini CLI stream-json. Temporary databases and workspaces only; no provider is invoked.
"""
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import patch

from app import agent_manager as manager, allocation, delegation_protocol, orchestration, presentation
from app import provider_events, provider_policy, work_control
from app.codex_events import CodexEvents, Execution
from app.delegation_protocol import ControlRejected
from app.persistence import AgentStore
import test_planning as plans
import test_promotion as promotions

STOPPED = '--- Structured execution stopped; inspect output before retrying ---'
RECORDED = '--- Delegation requests recorded; waiting for orchestration ---'


def entry(proposal_id='a', **changes):
    return dict(proposal_id=proposal_id, deliverable=f'Deliver {proposal_id} with tests',
                responsibility=f'Edit only the {proposal_id} component.',
                scope=dict(namespace='component', key=f'csv/{proposal_id}')) | changes


def decision(items=None, key='split', **changes):
    return json.dumps(dict(protocol=orchestration.PROTOCOL, decision_key=key,
                           rationale='Separable components with a stable interface.',
                           delegate=[entry()] if items is None else items) | changes)


class EnvelopeRuntimeTests(unittest.TestCase):
    setUp = promotions.PromotionTests.setUp
    patch = promotions.PromotionTests.patch
    stream = promotions.PromotionTests.stream
    finish = promotions.PromotionTests.finish
    launch = promotions.PromotionTests.launch
    plan = promotions.PromotionTests.plan
    message = promotions.PromotionTests.message
    rows = promotions.PromotionTests.rows

    # ---------------------------------------------------------------- helpers

    def run_final(self, *messages):
        key, process = self.launch()
        self.stream(key, process, list(messages))
        self.finish(key, process)
        return key, process

    def active(self):
        key, process = self.launch()
        self.stream(key, process, ['Work continues'])
        return key, process

    def submit(self, process, message=None, message_id='decision', origin=None, store=None):
        context = process._control_center_execution
        return (store or self.store).accept_orchestration_message(origin or context.origin, message or decision(),
            session_id=context.events.session_id, message_id=message_id)

    def counts(self):
        task_id = self.task['task_id']
        with self.store._connection() as db:
            one = lambda sql: db.execute(sql, (task_id,)).fetchone()[0]
            return dict(plans=one('SELECT COUNT(*) FROM planning_revisions WHERE task_id=?'),
                        promotions=one('SELECT COUNT(*) FROM promotion_decisions WHERE task_id=?'),
                        items=one('SELECT COUNT(*) FROM promotion_items WHERE task_id=?'),
                        decisions=one('SELECT COUNT(*) FROM orchestration_decisions WHERE task_id=?'),
                        delegations=one('SELECT COUNT(*) FROM delegations WHERE parent_task_id=?'),
                        children=one('SELECT COUNT(*) FROM tasks WHERE parent_task_id=?'))

    NOTHING = dict(plans=0, promotions=0, items=0, decisions=0, delegations=0, children=0)

    def output(self, key):
        return self.store.full_output(key)

    def assert_rejected(self, key, kind, reason_part):
        lines = self.output(key)
        self.assertIn(STOPPED, lines)
        line = next(l for l in lines if l.startswith('Structured control rejected ('))
        self.assertTrue(line.startswith(f'Structured control rejected ({kind}): '), line)
        self.assertIn(reason_part, line)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        rejection, = self.store.control_rejections(self.task['task_id'])
        self.assertEqual(rejection['kind'], kind)
        self.assertIn(reason_part, rejection['reason'])
        self.assertEqual(len(self.commands), 1)  # No retry and no other model call.
        return rejection

    # ---------------------------------------------------------------- acceptance

    def test_one_envelope_records_plan_promotion_and_exactly_one_child(self):
        key, process = self.run_final(decision())
        self.assertEqual(self.counts(), dict(plans=1, promotions=1, items=1, decisions=1, delegations=1, children=1))
        plan, = self.store.planning_history(self.task['task_id'])
        self.assertEqual((plan['revision'], plan['content']['decision'], plan['content']['base_revision']), (1, 'propose', 0))
        self.assertEqual(plan['content']['items'], [dict(proposal_id='a', deliverable='Deliver a with tests',
            responsibility='Edit only the a component.', depends_on=[], parallel=True)])
        row, = self.rows()
        self.assertEqual((row['proposal_id'], row['disposition'], row['revision']), ('a', 'ready', 1))
        self.assertIsNotNone(row['child_task_id'])
        record, = self.store.orchestration_history(self.task['task_id'])
        self.assertEqual((record['decision_key'], record['plan_revision']), ('split', 1))
        self.assertEqual(record['generation'], process._control_center_execution.origin.generation)
        self.assertEqual(manager.agent_statuses[key], 'stopped')
        self.assertIn(RECORDED, self.output(key))
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')  # Waiting, not completed.
        view = manager.orchestration_status(self.task['task_id'])['decision']
        self.assertEqual((view['state'], view['source'], view['decision_key'], view['revision']),
                         ('promoted', 'orchestration', 'split', 1))
        self.assertIsNone(view['last_rejection'])
        labels = [e['label'] for e in presentation.activity(self.store, key)['events']]
        self.assertIn('Delegation decision accepted: 1 responsibility promoted (plan revision 1)', labels)
        self.assertNotIn('Work promotion accepted', labels)
        self.assertEqual(len(self.commands), 1)

    def test_existing_revision_n_gives_n_plus_one_and_retained_proposals_stay_unpromoted(self):
        _, process = self.active()
        self.store.accept_planning_messages(process._control_center_execution.origin,
            [('p1', self.plan()), ('p2', self.plan(decision_key='second', base_revision=1))],
            session_id=process._control_center_execution.events.session_id)
        self.submit(process, decision([entry('c')]))
        history = self.store.planning_history(self.task['task_id'])
        self.assertEqual([h['revision'] for h in history], [1, 2, 3])
        self.assertEqual(history[-1]['content']['base_revision'], 2)
        self.assertEqual([i['proposal_id'] for i in history[-1]['content']['items']], ['a', 'b', 'c'])
        self.assertEqual([r['proposal_id'] for r in self.rows()], ['c'])  # a and b remain proposals only.
        self.assertEqual(self.store.orchestration_history(self.task['task_id'])[0]['plan_revision'], 3)

    def test_listing_an_existing_unpromoted_proposal_promotes_only_that_one(self):
        _, process = self.active()
        self.store.accept_planning_messages(process._control_center_execution.origin, [('p1', self.plan())],
                                            session_id=process._control_center_execution.events.session_id)
        self.submit(process, decision([entry('b')]))
        self.assertEqual([r['proposal_id'] for r in self.rows()], ['b'])
        current = self.store.current_plan(self.task['task_id'])
        self.assertEqual({i['proposal_id']: i['deliverable'] for i in current['content']['items']}['b'], 'Deliver b with tests')

    def test_intermediate_plan_then_envelope_in_one_execution_is_deterministic(self):
        key, _ = self.run_final(self.plan(), decision([entry('c')]))
        history = self.store.planning_history(self.task['task_id'])
        self.assertEqual([(h['revision'], h['message_id']) for h in history], [(1, 'message_0'), (2, 'message_1')])
        self.assertEqual([r['proposal_id'] for r in self.rows()], ['c'])
        self.assertEqual(self.rows()[0]['revision'], 2)
        self.assertEqual(manager.agent_statuses[key], 'stopped')

    def test_folder_and_file_resource_words_from_a_real_lead_are_accepted_as_file_paths(self):
        # A real Codex lead's early plan and decision (FrameSeek): folder and file claims written in its
        # own words, with a path as the scope and "write" access. Previously the whole decision was refused.
        plan = json.dumps(dict(protocol='control-center.planning.v1', decision_key='frameseek-initial-split',
            base_revision=0, decision='propose', rationale='Three separable deliverables with a shared contract.',
            items=[dict(proposal_id='search-core', deliverable='Local search service', responsibility='Backend files only',
                        depends_on=[], parallel=True),
                   dict(proposal_id='evaluation-docs', deliverable='Evaluation and docs', responsibility='Docs only',
                        depends_on=['search-core'], parallel=True)], references=[]))
        final = decision([
            entry('search-core', scope=dict(namespace='component', key='frameseek/search-core'), parallel=True,
                  resources=[{'resource_type': 'directory', 'resource_key': 'backend', 'scope': 'backend',
                              'mode': 'write', 'recursive': True}]),
            entry('evaluation-docs', scope=dict(namespace='component', key='frameseek/evaluation-docs'),
                  depends_on=['search-core'], parallel=True,
                  resources=[{'resource_type': 'directory', 'resource_key': 'evaluation', 'scope': 'evaluation',
                              'mode': 'write', 'recursive': True},
                             {'resource_type': 'file', 'resource_key': 'README.md', 'scope': 'README.md',
                              'mode': 'write', 'recursive': False}])], key='frameseek-initial-split')
        key, _ = self.run_final(plan, final)
        self.assertNotIn(STOPPED, self.output(key))
        self.assertEqual(manager.orchestration_status(self.task['task_id'])['decision']['state'], 'promoted')
        self.assertEqual(sorted(r['proposal_id'] for r in self.rows()), ['evaluation-docs', 'search-core'])
        record, = self.store.orchestration_history(self.task['task_id'])
        claims = {item['proposal_id']: item['resources'] for item in record['content']['delegate']}
        self.assertEqual(claims['search-core'], [dict(resource_type='file_path', resource_key='backend', scope='project',
                                                      mode='exclusive', recursive=True)])
        self.assertEqual(claims['evaluation-docs'][1], dict(resource_type='file_path', resource_key='README.md',
                                                            scope='project', mode='exclusive', recursive=False))

    def test_an_unknown_resource_type_is_refused_with_the_accepted_values(self):
        key, _ = self.run_final(decision([entry('a', resources=[{'resource_type': 'service', 'resource_key': 'api',
            'scope': 'project', 'mode': 'exclusive', 'recursive': False}])]))
        self.assertIn(STOPPED, self.output(key))
        rejection = manager.orchestration_status(self.task['task_id'])['decision']['last_rejection']
        self.assertIn("Invalid resource type 'service'", rejection['reason'])
        self.assertIn('file_path (files and folders', rejection['reason'])

    def test_the_prompt_states_the_accepted_resource_values(self):
        with self.store._connection() as db:
            text = orchestration.instruction(db, self.task['task_id'])
        self.assertIn('"resource_type":"file_path","resource_key":"backend","scope":"project"', text)
        self.assertIn('port, database, docker_resource or generic', text)
        self.assertIn('mode is advisory, shared or exclusive', text)

    def test_model_supplied_identity_or_revision_fields_are_rejected(self):
        for changes in (dict(task_id=self.task['task_id']), dict(revision=1), dict(base_revision=0),
                        dict(generation='g')):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'Invalid orchestration decision fields'):
                orchestration.validate(decision(**changes))

    # ---------------------------------------------------------------- replay and concurrency

    def test_identical_replay_returns_the_original_receipt_without_new_work(self):
        _, process = self.active()
        receipt = self.submit(process)
        before = self.counts()
        # A different provider message carrying the same decision is the same decision.
        self.assertEqual(self.submit(process, message_id='repeated'), receipt)
        self.assertEqual(self.submit(process), receipt)
        self.assertEqual(self.counts(), before)

    def test_conflicting_replay_fails_deterministically(self):
        _, process = self.active()
        self.submit(process)
        before = self.counts()
        for changed in (decision(rationale='Different reasons.'), decision([entry('b')]),
                        decision([entry(parallel=False)])):
            with self.subTest(changed=changed[-60:]), self.assertRaises(ControlRejected) as caught:
                self.submit(process, changed, message_id='other')
            self.assertEqual(caught.exception.kind, 'admission')
            self.assertIn('Conflicting replay of orchestration decision', str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_concurrent_identical_submissions_create_one_child(self):
        _, process = self.active()
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        with ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(lambda i: self.submit(process, store=stores[i], message_id=f'm{i}'), (0, 1)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.counts(), dict(plans=1, promotions=1, items=1, decisions=1, delegations=1, children=1))

    def test_restart_does_not_duplicate_accepted_work(self):
        key, _ = self.run_final(decision([entry(), entry('b', scope=dict(namespace='component', key='csv/a'))]))
        before, rows = self.counts(), self.rows()
        manager.agents[key] = None
        manager.initialize_persistence(self.store.path)
        self.assertEqual((self.counts(), self.rows()), (before, rows))
        self.assertEqual(len(self.commands), 1)

    def test_lost_response_after_commit_keeps_one_decision_and_is_visible_as_internal(self):
        key, process = self.launch()
        self.stream(key, process, [decision()])
        original = self.store.accept_orchestration_message

        def lost(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError('lost response')
        with patch.object(self.store, 'accept_orchestration_message', side_effect=lost):
            self.finish(key, process)
        before = self.counts()
        for _ in range(3):
            manager.get_agent(key)
        self.assertEqual(self.counts(), before)
        self.assertEqual(before['decisions'], 1)
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'blocked')
        lines = self.output(key)
        self.assertIn('Structured execution failed with an internal error (OperationalError) while processing its '
                      'control message; details are in the backend log.', lines)
        self.assertFalse(any('lost response' in line for line in lines))  # Internal details are not projected.
        self.assertEqual(self.store.control_rejections(self.task['task_id'])[0]['kind'], 'internal')
        self.assertEqual(len(self.commands), 1)

    # ---------------------------------------------------------------- refusals leave nothing behind

    def test_invalid_envelopes_create_no_durable_records(self):
        cases = ((decision([entry(depends_on=['missing'])]), 'format', 'Unknown proposal dependency'),
                 (decision([]), 'format', 'delegates 1-16 responsibilities'),
                 (decision([entry(scope=None)]), 'format', 'needs scope'),
                 (decision([entry(depends_on_task_ids=['3f2a8d43-2b8c-4fd1-9f3e-1d2c3b4a5e6f'])]), 'admission', 'Task'),
                 (decision().replace('"decision_key"', '"decision_key": "x", "decision_key"', 1), 'format', 'Duplicate'))
        for message, kind, reason in cases:
            with self.subTest(kind=kind, reason=reason):
                self.setUp()
                key, _ = self.run_final(message)
                self.assertEqual(self.counts(), self.NOTHING)
                self.assert_rejected(key, kind, reason)

    def test_prose_around_an_envelope_and_fenced_examples(self):
        key, _ = self.run_final('Delegating now.\n\n' + decision())
        self.assertEqual(self.counts(), self.NOTHING)
        self.assert_rejected(key, 'format', delegation_protocol.STANDALONE_REQUIRED)
        self.setUp()
        key, _ = self.run_final(f'Done myself. The decision format looks like:\n```json\n{decision()}\n```')
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.counts(), self.NOTHING)
        self.assertEqual(self.store.control_rejections(self.task['task_id']), [])

    def test_envelope_followed_by_another_message_is_refused(self):
        key, _ = self.run_final(decision(), 'One more thing.')
        self.assertEqual(self.counts(), self.NOTHING)
        self.assert_rejected(key, 'format', 'Promotion must be the final assistant message')

    # ---------------------------------------------------------------- M1 diagnostics for legacy messages

    def test_promotion_without_any_plan_is_a_durable_missing_step(self):
        # The second dashboard failure: a lone promotion inventing revision 1.
        key, _ = self.run_final(self.message())
        self.assertEqual(self.counts(), self.NOTHING)
        self.assert_rejected(key, 'missing_step', 'no planning revision has been accepted for this Task')
        view = manager.orchestration_status(self.task['task_id'])['decision']
        self.assertEqual(view['last_rejection']['kind'], 'missing_step')
        labels = [e['label'] for e in presentation.activity(self.store, key)['events'] if e['kind'] == 'rejection']
        self.assertTrue(labels[0].startswith('Control message rejected (missing step): Promotion must reference'))

    def test_plan_only_final_response_is_a_missing_step(self):
        key, _ = self.run_final(self.plan())
        self.assertEqual(self.counts(), self.NOTHING)
        self.assert_rejected(key, 'missing_step', 'Plan-only final response')

    def test_authority_failures_are_classified(self):
        _, process = self.active()
        origin = process._control_center_execution.origin
        for stale in (replace(origin, generation='0' * 32), replace(origin, assignment_id='x')):
            with self.subTest(stale=stale), self.assertRaises(ControlRejected) as caught:
                self.submit(process, origin=stale)
            self.assertEqual(caught.exception.kind, 'authority')
        self.store.request_work_control(self.task['task_id'], 'canceled')
        with self.assertRaises(ControlRejected) as caught:
            self.submit(process)
        self.assertEqual(caught.exception.kind, 'authority')
        self.assertEqual(self.counts(), self.NOTHING)

    def test_a_stopped_agent_cannot_record_a_decision(self):
        key, process = self.active()
        manager.stop_agent(key)
        with self.assertRaises(ControlRejected) as caught:
            self.submit(process)
        self.assertEqual(caught.exception.kind, 'authority')
        self.assertEqual(self.counts(), self.NOTHING)

    # ---------------------------------------------------------------- admission semantics preserved

    def test_provider_permission_revoked_before_admission_blocks_the_item(self):
        key, process = self.launch()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            provider_policy.set_restriction(db, self.task['task_id'], [])
        self.stream(key, process, [decision()])
        self.finish(key, process)
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertIn('not allowed for this Task', row['reason'])
        self.assertEqual(self.counts()['children'], 0)
        self.assertEqual(self.store.control_rejections(self.task['task_id']), [])  # A valid decision, held work.

    def test_auto_allocation_restrictions_hold_the_item(self):
        auto = dict(mode='auto', category='implementation', capabilities=[],
                    candidates=[dict(provider='claude', suitability='preferred')])
        self.run_final(decision([entry(allocation=auto)]))
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertIn('Auto allocation is not authorized', row['reason'])
        self.setUp()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            allocation.set_policy(db, self.task['task_id'], enabled=True, allowed=['codex'], expected_revision=None)
        self.run_final(decision([entry(allocation=auto)]))
        row, = self.rows()
        self.assertEqual(row['disposition'], 'blocked')
        self.assertTrue(row['reason'].startswith('Auto allocation held'))
        self.assertEqual(self.counts()['children'], 0)

    def test_previously_promoted_proposal_cannot_be_changed(self):
        key, process = self.active()
        self.submit(process)
        with self.store._connection() as db:  # The state a later Hybrid relaunch of this Parent leaves.
            db.execute("UPDATE agents SET status='running' WHERE agent_id=?", (key,))
            db.execute("UPDATE tasks SET status='pending' WHERE task_id=?", (self.task['task_id'],))
        before = self.counts()
        # The outstanding Child's Task gates are bypassed only to reach the durable-history rule.
        with patch.object(work_control, 'require_active'), self.assertRaises(ValueError) as caught:
            self.submit(process, decision([entry(responsibility='Something else entirely.')], key='again'), 'm2')
        self.assertIn('immutable', str(caught.exception))
        self.assertEqual(self.counts(), before)

    def test_multi_item_dependencies_and_partial_admission(self):
        self.run_final(decision([entry('b'), entry('a', depends_on=['b']),
                                 entry('c', scope=dict(namespace='component', key='csv/b'))]))
        rows = {r['proposal_id']: r for r in self.rows()}
        self.assertEqual(rows['b']['disposition'], 'ready')
        self.assertEqual(rows['a']['disposition'], 'blocked')  # Waits for b's integration and acknowledgement.
        self.assertEqual(rows['c']['disposition'], 'conflict')  # Same scope as b: existing scope rules apply.
        with self.store._connection() as db:
            edges = [tuple(r) for r in db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies')]
        self.assertIn((rows['a']['child_task_id'], rows['b']['child_task_id']), edges)
        self.assertEqual(self.store.orchestration_history(self.task['task_id'])[0]['plan_revision'], 1)

    def test_hybrid_handoff_is_recorded_for_the_parent(self):
        _, process = self.run_final(decision())
        with self.store._connection() as db:
            continuation = db.execute('SELECT * FROM delegation_continuations WHERE task_id=?',
                                      (self.task['task_id'],)).fetchone()
        self.assertEqual((continuation['assignment_id'], continuation['phase'], continuation['plan']),
                         (process._control_center_execution.origin.assignment_id, 'waiting', None))

    def test_ordinary_completion_and_waiting_are_unchanged(self):
        key, _ = self.run_final('All requested work is complete.')
        self.assertEqual(manager.agent_statuses[key], 'finished')
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        self.assertEqual(manager.orchestration_status(self.task['task_id'])['decision']['state'], 'not_recorded')
        self.setUp()
        key, _ = self.run_final('CONTROL_CENTER_WAITING: Which delimiter?')
        self.assertEqual(manager.agent_statuses[key], 'waiting')
        self.assertEqual(self.counts(), self.NOTHING)

    def test_legacy_planning_then_promotion_still_works(self):
        key, _ = self.run_final(self.plan(), self.message())
        self.assertEqual(self.counts(), dict(plans=1, promotions=1, items=1, decisions=0, delegations=1, children=1))
        view = manager.orchestration_status(self.task['task_id'])['decision']
        self.assertEqual((view['state'], view['source']), ('promoted', 'planning'))
        self.assertIn('Work promotion accepted', [e['label'] for e in presentation.activity(self.store, key)['events']])


class LaterRoundTests(unittest.TestCase):
    """Later delegation rounds of the same Parent: promoted history imposes no lifetime ceiling."""
    setUp = EnvelopeRuntimeTests.setUp
    patch = EnvelopeRuntimeTests.patch
    stream = EnvelopeRuntimeTests.stream
    launch = EnvelopeRuntimeTests.launch
    plan = EnvelopeRuntimeTests.plan
    message = EnvelopeRuntimeTests.message
    rows = EnvelopeRuntimeTests.rows
    active = EnvelopeRuntimeTests.active
    submit = EnvelopeRuntimeTests.submit
    counts = EnvelopeRuntimeTests.counts

    @staticmethod
    def many(prefix, count):
        return [entry(f'{prefix}{i}', scope=dict(namespace='component', key=f'csv/{prefix}{i}')) for i in range(count)]

    def later(self, key):
        """The state a Hybrid relaunch of this Parent leaves for its next round. Outstanding Child
        Task gates are bypassed by the caller only to reach the orchestration rules under test."""
        with self.store._connection() as db:
            db.execute("UPDATE agents SET status='running' WHERE agent_id=?", (key,))
            db.execute("UPDATE tasks SET status='pending' WHERE task_id=?", (self.task['task_id'],))
            # The Hybrid continuation is in its independent phase, where a new round is allowed.
            db.execute("UPDATE delegation_continuations SET phase='independent',mode='independent' WHERE task_id=?",
                       (self.task['task_id'],))

    def round(self, process, message, message_id, key, *, store=None):
        self.later(key)
        with patch.object(work_control, 'require_active'):
            return self.submit(process, message, message_id=message_id, store=store)

    def plan_ids(self):
        return [i['proposal_id'] for i in self.store.current_plan(self.task['task_id'])['content']['items']]

    def assert_no_duplicate_children(self):
        children = [r['child_task_id'] for r in self.rows() if r['child_task_id']]
        self.assertEqual(len(children), len(set(children)))
        self.assertEqual(self.counts()['children'], len(children))

    def test_a_later_round_delegates_a_17th_responsibility_after_16_promoted(self):
        key, process = self.active()
        self.submit(process, decision(self.many('h', 16), key='round-1'))
        self.assertEqual(self.counts()['items'], 16)
        self.round(process, decision([entry('n17')], key='round-2'), 'm2', key)
        self.assertEqual((self.counts()['items'], self.counts()['delegations']), (17, 17))
        self.assertEqual(self.plan_ids(), ['n17'])  # Promoted history is not re-proposed.
        self.assertEqual([h['plan_revision'] for h in self.store.orchestration_history(self.task['task_id'])], [1, 2])
        self.assert_no_duplicate_children()

    def test_a_single_decision_still_cannot_exceed_16_responsibilities(self):
        with self.assertRaisesRegex(ValueError, '1-16 responsibilities'):
            orchestration.validate(decision(self.many('x', 17)))
        key, process = self.active()
        self.submit(process, decision(self.many('h', 16), key='round-1'))
        before = self.counts()
        with self.assertRaises(ControlRejected) as caught:
            self.round(process, decision(self.many('y', 17), key='round-2'), 'm2', key)
        self.assertEqual(caught.exception.kind, 'format')
        self.assertEqual(self.counts(), before)

    def test_unpromoted_proposals_are_preserved_and_promoted_ones_are_not_re_proposed(self):
        key, process = self.active()
        context = process._control_center_execution
        self.store.accept_planning_messages(context.origin, [('p1', plans.decision(
            decision='propose', items=[plans.item('a'), plans.item('b'), plans.item('c')]))],
            session_id=context.events.session_id)
        self.submit(process, decision([entry('a')], key='round-1'))
        self.assertEqual(self.plan_ids(), ['b', 'c', 'a'])
        self.round(process, decision([entry('d')], key='round-2'), 'm2', key)
        self.assertEqual(self.plan_ids(), ['b', 'c', 'd'])  # b and c remain proposals, still unpromoted.
        self.assertEqual(sorted(r['proposal_id'] for r in self.rows()), ['a', 'd'])
        # Retained unpromoted proposals still count toward the per-revision limit of 16.
        before = self.counts()
        with self.assertRaises(ControlRejected) as caught:
            self.round(process, decision(self.many('z', 15), key='round-3'), 'm3', key)
        self.assertEqual(caught.exception.kind, 'admission')
        self.assertEqual(self.counts(), before)

    def test_cross_round_dependencies_carry_only_the_needed_promoted_snapshots(self):
        key, process = self.active()
        self.submit(process, decision([entry('a'), entry('x')], key='round-1'))
        frozen = {r['proposal_id']: (r['delegation_id'], r['child_task_id']) for r in self.rows()}
        self.round(process, decision([entry('b', depends_on=['a'])], key='round-2'), 'm2', key)
        self.round(process, decision([entry('c', depends_on=['b'])], key='round-3'), 'm3', key)
        self.assertEqual(sorted(self.plan_ids()), ['a', 'b', 'c'])  # a transitively; x is not needed.
        rows = {r['proposal_id']: r for r in self.rows()}
        for proposal_id, identity in frozen.items():
            self.assertEqual((rows[proposal_id]['delegation_id'], rows[proposal_id]['child_task_id']), identity)
        self.assertEqual(rows['a']['revision'], 1)  # Original provenance is unchanged.
        self.assertEqual(rows['b']['disposition'], 'blocked')  # Waits for a's integration and acknowledgement.
        with self.store._connection() as db:
            edges = {tuple(r) for r in db.execute('SELECT task_id,depends_on_task_id FROM task_dependencies')}
        self.assertIn((rows['b']['child_task_id'], rows['a']['child_task_id']), edges)
        self.assert_no_duplicate_children()

    def test_historical_promotion_identities_remain_immutable(self):
        key, process = self.active()
        self.submit(process, decision([entry('a')], key='round-1'))
        original, = self.rows()
        before = self.counts()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.round(process, decision([entry('a', responsibility='Changed.')], key='round-2'), 'm2', key)
        self.assertEqual(self.counts(), before)
        # Re-listing it unchanged alongside new work reuses its Delegation; no second Child.
        self.round(process, decision([entry('a'), entry('e')], key='round-3'), 'm3', key)
        rows = {r['proposal_id']: r for r in self.rows()}
        self.assertEqual((rows['a']['delegation_id'], rows['a']['child_task_id']),
                         (original['delegation_id'], original['child_task_id']))
        self.assertEqual(self.counts()['delegations'], 2)
        self.assert_no_duplicate_children()

    def test_invalid_later_rounds_roll_back_without_partial_effects(self):
        key, process = self.active()
        self.submit(process, decision(self.many('h', 16), key='round-1'))
        before, plan = self.counts(), self.store.current_plan(self.task['task_id'])
        for message in (decision([entry('n', depends_on=['unknown'])], key='bad-1'),
                        decision([entry('n', depends_on_task_ids=['3f2a8d43-2b8c-4fd1-9f3e-1d2c3b4a5e6f'])], key='bad-2')):
            with self.subTest(message=message[-80:]), self.assertRaises((ValueError, LookupError)):
                self.round(process, message, 'bad', key)
            self.assertEqual(self.counts(), before)
            self.assertEqual(self.store.current_plan(self.task['task_id']), plan)

    def test_later_round_replay_and_concurrent_acceptance_are_idempotent(self):
        key, process = self.active()
        self.submit(process, decision(self.many('h', 16), key='round-1'))
        receipt = self.round(process, decision([entry('n17')], key='round-2'), 'm2', key)
        after = self.counts()
        self.assertEqual(self.round(process, decision([entry('n17')], key='round-2'), 'replayed', key), receipt)
        self.assertEqual(self.counts(), after)
        stores = [AgentStore(self.store.path), AgentStore(self.store.path)]
        self.later(key)
        with patch.object(work_control, 'require_active'), ThreadPoolExecutor(2) as pool:
            receipts = list(pool.map(lambda i: self.submit(process, decision([entry('n18')], key='round-3'),
                                                           message_id=f'c{i}', store=stores[i]), (0, 1)))
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(self.counts()['items'], 18)
        self.assert_no_duplicate_children()

    def test_the_equivalent_legacy_round_still_accepts_a_17th_responsibility(self):
        key, process = self.active()
        self.submit(process, decision(self.many('h', 16), key='round-1'))
        context = process._control_center_execution
        self.later(key)
        with patch.object(work_control, 'require_active'):
            self.store.accept_promotion_message(context.origin, self.message(
                [promotions.selection('n17')], revision=2, decision_key='legacy-17'),
                session_id=context.events.session_id, message_id='legacy',
                plans=[('legacy-plan', plans.decision(decision='propose', items=[plans.item('n17')],
                                                      base_revision=1, decision_key='legacy-plan'))])
        self.assertEqual(self.counts()['items'], 17)
        self.assert_no_duplicate_children()


class EnvelopeParserTests(unittest.TestCase):
    """Each provider path recognizes the envelope only as a standalone final message."""
    session = '0e7a2868-a041-4bde-a3d3-1d82addab8c8'

    def stream_parser(self, provider, *texts):
        parser = provider_events.parser(provider)
        Execution(events=parser)
        parser.planning_enabled = True
        parser.feed(json.dumps(dict(type='system', subtype='init', session_id=self.session, model='fake')))
        for index, text in enumerate(texts):
            parser.feed(json.dumps(dict(type='assistant', uuid=f'a{index}', parent_tool_use_id=None,
                                        message=dict(id=f'msg_{index}', content=[dict(type='text', text=text)]))))
        parser.feed(json.dumps(dict(type='result', subtype='success', is_error=False, result=texts[-1],
                                    session_id=self.session, uuid='result-1')))
        return parser

    def test_claude_stream_json_recognizes_the_final_envelope(self):
        parser = self.stream_parser('claude', 'Reading files first.', decision())
        self.assertIsNone(parser.error)
        self.assertEqual(parser.promotion_message, 'a1')
        self.assertEqual(parser.finish(0), decision())

    def test_gemini_shares_the_stream_path(self):
        parser = provider_events.parser('gemini')
        Execution(events=parser)
        self.assertTrue(parser.standalone_control)
        self.assertIs(type(parser)._accept, provider_events.StreamEvents._accept)

    def test_stream_json_refusals_are_classified(self):
        parser = self.stream_parser('claude', decision(), 'Trailing text.')
        self.assertEqual(parser.rejection.kind, 'format')
        parser = self.stream_parser('claude', 'Delegating.\n' + decision())
        self.assertEqual((parser.rejection.kind, str(parser.rejection)), ('format', delegation_protocol.STANDALONE_REQUIRED))
        parser = self.stream_parser('claude', decision([]))
        self.assertEqual(parser.rejection.kind, 'format')
        with self.assertRaises(ValueError):
            parser.finish(0)

    def test_codex_jsonl_recognizes_the_final_envelope(self):
        parser = CodexEvents(planning_enabled=True, standalone_control=True)
        lines = [dict(type='thread.started', thread_id=self.session), dict(type='turn.started'),
                 dict(type='item.completed', item=dict(id='m1', type='agent_message', text=decision())),
                 dict(type='turn.completed', usage=dict(input_tokens=1, cached_input_tokens=0, output_tokens=1))]
        for line in lines:
            parser.feed(json.dumps(line))
        self.assertIsNone(parser.error)
        self.assertEqual(parser.promotion_message, 'm1')
        self.assertEqual(parser.finish(0), decision())


class EnvelopeSchemaTests(unittest.TestCase):
    def test_defaults_and_canonical_form(self):
        value = orchestration.validate(decision([entry(), entry('b', depends_on=['a'])]))
        a, b = value['delegate']
        self.assertEqual((a['depends_on'], a['parallel'], a['resources'], a['depends_on_task_ids'], a['reuse_task_id']),
                         ([], True, [], [], None))
        self.assertEqual((b['depends_on'], b['parallel']), (['a'], False))
        self.assertIsNone(value['continuation'])
        self.assertEqual(orchestration.validate(orchestration.encoded(value)), value)  # Canonical form is stable.

    def test_strict_fields_and_types(self):
        bad = (decision(protocol='control-center.orchestration.v2'), decision(extra=1),
               decision([entry(extra=1)]), decision([entry(), entry()]), decision([entry(parallel='yes')]),
               decision([entry(depends_on=['a'])]), decision([entry(provider='other')]),
               decision([entry(provider='codex', allocation=dict(mode='auto'))]), decision(key='bad key'),
               decision(rationale=''), decision([entry(scope=dict(namespace='component'))]), '[]', '{')
        for message in bad:
            with self.subTest(message=message[-70:]), self.assertRaises(ValueError):
                orchestration.validate(message)


if __name__ == '__main__':
    unittest.main()
