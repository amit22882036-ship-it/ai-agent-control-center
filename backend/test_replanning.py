"""Synthetic final-message ingress and real existing control/promotion paths."""
import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, replanning, promotion, delegation_orchestration as hybrid
from app.persistence import AgentStore
import test_promotion as existing
from test_promotion import selection


class ReplanningTests(unittest.TestCase):
    patch=existing.PromotionTests.patch
    stream=existing.PromotionTests.stream
    finish=existing.PromotionTests.finish
    launch=existing.PromotionTests.launch
    plan=existing.PromotionTests.plan
    message=existing.PromotionTests.message
    rows=existing.PromotionTests.rows
    workspace_fixture=existing.PromotionTests.workspace_fixture
    accepted=existing.PromotionTests.accepted

    def setUp(self):
        existing.PromotionTests.setUp(self)
        self.intent,_ = self.store.create_work_intent(self.task['task_id'],namespace='component',key='parent',mode='single_owner')
        self.key,_ = self.accepted([selection(),selection('b')],continuation=dict(instruction='Independent parent work',intent_ids=[self.intent]))
        self.targets=self.rows()
        self.workspace_fixture()
        manager.advance_delegations(self.task['task_id'])
        self.process=manager.agents[self.key]

    def change(self,index=0,**options):
        d=self.targets[index]['delegation_id']
        with self.store._connection() as db:
            token=replanning.state_token(db,d)
        return dict(action='retire',delegation_id=d,expected=token,reason='Obsolete responsibility',replacement=None,depends_on_task_ids=[],reuse_result_id=None)|options

    def decision(self,**options):
        return json.dumps(dict(protocol=replanning.PROTOCOL,task_id=self.task['task_id'],decision_key='adapt',revision=1,
            changes=[self.change()],withdraw=[],promote=[],continuation=None)|options)

    def submit(self,message=None,origin=None):
        ctx=self.process._control_center_execution
        return self.store.accept_replanning_message(origin or ctx.origin,message or self.decision(),session_id=manager.agent_sessions[self.key],message_id='replan')

    def transitions(self):
        with self.store._connection() as db:
            return replanning.inspect(db,self.task['task_id'])

    def apply(self):
        return manager.advance_replanning(self.task['task_id'])

    def test_valid_retirement_uses_existing_control_and_preserves_sibling(self):
        self.submit()
        result,=self.apply()
        self.assertEqual(result['phase'],'applied',result)
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['status'],'canceled')
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['control_intent'],'active')
        self.assertEqual(len(self.store.list_tasks()),3)
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'],'completed')

    def test_exact_replay_returns_same_transition(self):
        message=self.decision()
        first=self.submit(message)
        self.assertEqual(first,self.submit(message))
        self.apply();self.apply()
        self.assertEqual(len(self.transitions()),1)

    def test_conflicting_replay_rejected(self):
        message=self.decision();self.submit(message)
        with self.assertRaises(ValueError):
            self.submit(self.decision(changes=[self.change(action='retain')]))

    def test_malformed_payload_has_no_effects(self):
        original=json.loads(self.decision())
        variants=[original|{'revision':True},original|{'protocol':'control-center.replanning.v2'},original|{'extra':0},
                  original|{'changes':[self.change(action='steal')]},original|{'changes':[self.change(expected='bad')]},
                  original|{'changes':[self.change(),self.change()]},original|{'changes':[self.change(reuse_result_id=str(uuid4()))]}]
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):self.submit(json.dumps(value))
        with self.assertRaises(ValueError):self.submit(self.decision()[:-1]+',"revision":1}')
        self.assertEqual(self.transitions(),[])

    def test_stale_revision_and_execution_rejected(self):
        with self.assertRaises(ValueError):self.submit(self.decision(revision=2))
        with self.assertRaises(ValueError):self.submit(origin=replace(self.process._control_center_execution.origin,generation=str(uuid4())))
        self.assertEqual(self.transitions(),[])

    def test_stale_target_state_rejected(self):
        message=self.decision()
        self.store.create_work_intent(self.targets[0]['child_task_id'],namespace='component',key='new-scope',mode='single_owner')
        with self.assertRaises(ValueError):self.submit(message)

    def test_unknown_target_and_project_authority_rejected(self):
        with self.assertRaises(LookupError):self.submit(self.decision(changes=[self.change(delegation_id=str(uuid4()))]))
        with self.assertRaises(ValueError):self.submit(self.decision(task_id=self.targets[0]['child_task_id']))

    def test_retaining_work_does_not_cancel_or_release(self):
        self.submit(self.decision(changes=[self.change(action='retain')]))
        result,=self.apply()
        self.assertEqual(result['phase'],'applied')
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['control_intent'],'active')

    def test_active_descendant_blocks_retirement(self):
        child=self.store.create_task('Useful descendant',parent_task_id=self.targets[0]['child_task_id'],project_id=self.project['project_id'])
        self.submit()
        result,=self.apply()
        self.assertEqual(result['phase'],'blocked')
        self.assertEqual(self.store.get_task(child['task_id'])['control_intent'],'active')

    def test_completed_consumer_blocks_retirement(self):
        other=self.store.create_task('Consumer',project_id=self.project['project_id'])
        self.store.change_dependency(other['task_id'],self.targets[0]['child_task_id'])
        with self.store._connection() as db:db.execute("UPDATE tasks SET status='completed' WHERE task_id=?",(other['task_id'],))
        self.submit();result,=self.apply()
        self.assertEqual(result['phase'],'blocked')
        self.assertIn('consumer',result['reason'])

    def test_unexecuted_dependency_remains_blocked_after_retirement(self):
        self.store.change_dependency(self.targets[1]['child_task_id'],self.targets[0]['child_task_id'])
        self.submit();self.apply()
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['status'],'blocked')

    def test_unknown_termination_retains_scopes_and_transition(self):
        self.submit()
        with patch.object(manager,'_settle_tasks',side_effect=RuntimeError('unknown tree')):
            result,=self.apply()
        self.assertEqual(result['phase'],'recovery_required')
        scopes=self.store.work_intents(self.targets[0]['child_task_id'])
        self.assertTrue(any(r['holds_authority'] for r in scopes))
        self.store.reconcile_task_recovery()
        self.assertTrue(any(r['holds_authority'] for r in self.store.work_intents(self.targets[0]['child_task_id'])))
        self.assertEqual(self.transitions()[0]['phase'],'recovery_required')

    def test_new_dependency_cycle_rolls_back_all_changes(self):
        a,b=[t['child_task_id'] for t in self.targets]
        message=self.decision(changes=[self.change(0,action='depend',depends_on_task_ids=[b]),self.change(1,action='depend',depends_on_task_ids=[a])])
        with self.assertRaises(ValueError):self.submit(message)
        self.assertEqual(self.store.list_dependencies(a),[])
        self.assertEqual(self.transitions(),[])

    def test_new_dependency_uses_existing_graph(self):
        self.submit(self.decision(changes=[self.change(1,action='depend',depends_on_task_ids=[self.targets[0]['child_task_id']])]))
        result,=self.apply()
        self.assertEqual(result['phase'],'applied',result)
        self.assertEqual(len(self.store.list_dependencies(self.targets[1]['child_task_id'])),1)

    def test_pending_transition_prevents_parent_completion(self):
        self.submit()
        with self.store._connection() as db:self.assertTrue(hybrid.outstanding(db,self.task['task_id']))

    def test_successful_final_provider_message_applies_control(self):
        self.stream(self.key,self.process,[self.decision()])
        self.finish(self.key,self.process)
        self.assertEqual(len(self.transitions()),1)
        result,=self.apply()
        self.assertEqual(result['phase'],'applied',result)
        self.assertEqual(manager.agent_statuses[self.key],'stopped')

    def test_failed_turn_cannot_replan(self):
        self.stream(self.key,self.process,[self.decision()],ending='turn.failed')
        self.finish(self.key,self.process)
        self.assertEqual(self.transitions(),[])

    def test_tool_and_stderr_cannot_replan(self):
        message=self.decision()
        self.stream(self.key,self.process,['Ordinary response'],tool=message,stderr=message)
        self.finish(self.key,self.process)
        self.assertEqual(self.transitions(),[])

    def test_nonfinal_replanning_is_rejected(self):
        self.stream(self.key,self.process,[self.decision(),'Another response'])
        self.finish(self.key,self.process)
        self.assertEqual(self.transitions(),[])

    def test_running_child_is_stopped_without_affecting_sibling(self):
        progress=manager.materialize_delegations(self.task['task_id'])
        children=[r['launch_agent_id'] for r in progress]
        for child in children:
            # Empty read stream drains normally; process still running until Stop.
            manager.agents[child].stdout.close()
        self.stream(self.key,self.process,[self.decision()])
        self.finish(self.key,self.process)
        result,=self.apply()
        self.assertEqual(result['phase'],'applied',result)
        self.assertIsNotNone(manager.agents[children[0]].poll())
        self.assertIsNone(manager.agents[children[1]].poll())
        self.assertEqual(len(self.store.list_task_assignments(self.targets[0]['child_task_id'])),1)

    def test_replacement_waits_for_retirement_then_materializes_once(self):
        import test_planning as plans
        plan=self.plan([plans.item('a'),plans.item('b'),plans.item('replacement')],decision_key='revision2',base_revision=1)
        message=self.decision(revision=2,changes=[self.change(replacement='replacement')],promote=[selection('replacement',scope=dict(namespace='component',key='a',from_intent_id=None))])
        self.stream(self.key,self.process,[plan,message]);self.finish(self.key,self.process)
        self.assertEqual(len(self.transitions()),1)
        rows=self.rows();replacement=next(r for r in rows if r['proposal_id']=='replacement')
        with self.store._connection() as db,self.assertRaises(ValueError):promotion.require_ready(db,replacement['delegation_id'])
        self.apply()
        manager.materialize_delegations(self.task['task_id'])
        replacement=next(r for r in self.rows() if r['proposal_id']=='replacement')
        self.assertEqual(replacement['materialization']['phase'],'started',replacement)
        count=len(self.commands)
        self.apply();manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands),count)

    def test_cancel_after_acceptance_prevents_transition_and_spawn(self):
        self.submit();count=len(self.commands)
        self.store.request_work_control(self.task['task_id'],'canceled')
        result,=self.apply()
        self.assertEqual(result['phase'],'blocked')
        self.assertEqual(len(self.commands),count)

    def test_child_failure_records_review_but_success_does_not(self):
        children=manager.materialize_delegations(self.task['task_id'])
        child=children[0]['launch_agent_id']
        self.stream(child,manager.agents[child],['Failure'],ending='turn.failed')
        self.finish(child,manager.agents[child])
        with self.store._connection() as db:
            replanning.collect_signals(db);replanning.collect_signals(db)
            signals=list(db.execute("SELECT * FROM replan_signals WHERE event_key LIKE 'result:%'"))
        self.assertEqual(len(signals),1)
        other=children[1]['launch_agent_id']
        self.stream(other,manager.agents[other],['Completed'])
        self.finish(other,manager.agents[other])
        with self.store._connection() as db:
            replanning.collect_signals(db)
            self.assertEqual(db.execute("SELECT count(*) FROM replan_signals WHERE event_key LIKE 'result:%'").fetchone()[0],1)

    def test_related_review_consumed_once_at_existing_quiet_boundary(self):
        self.submit(self.decision(changes=[self.change(action='retain')]))
        with self.store._connection() as db:
            replanning.signal(db,self.task['task_id'],'failure:one','Review material failure')
            first=replanning.review_prompt(db,self.task['task_id'])
            second=replanning.review_prompt(db,self.task['task_id'])
            replanning.signal(db,self.task['task_id'],'failure:two','New evidence')
            third=replanning.review_prompt(db,self.task['task_id'])
        self.assertIsNotNone(first);self.assertIsNone(second);self.assertIsNotNone(third)

    def test_concurrent_replay_creates_one_durable_transition(self):
        from concurrent.futures import ThreadPoolExecutor
        message=self.decision()
        with ThreadPoolExecutor(2) as pool:
            receipts=list(pool.map(lambda _:self.submit(message),range(2)))
        self.assertEqual(receipts[0],receipts[1])
        self.assertEqual(len(self.transitions()),1)

    def test_failure_during_acceptance_rolls_back_decision(self):
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_replan BEFORE INSERT ON replan_transitions BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.Error):self.submit()
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM replan_decisions').fetchone()[0],0)
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['control_intent'],'active')

    def test_unknown_stop_keeps_global_resource_claim(self):
        child=self.targets[0]['child_task_id']
        claim=self.store.create_resource_claim(child,resource_type='generic',resource_key='test:shared-slot',scope='global',mode='exclusive')
        self.submit()
        with patch.object(manager,'_settle_tasks',side_effect=RuntimeError('unknown')):self.apply()
        self.store.reconcile_task_recovery()
        row=next(r for r in self.store.resource_claims(child) if r['claim_id']==claim)
        self.assertEqual(row['status'],'active')

    def test_crash_after_stop_before_apply_retains_barrier(self):
        self.submit()
        def fail_quiet(_):raise sqlite3.OperationalError('crash after termination')
        service=replanning.TransitionService(self.store,lambda key:manager._settle_tasks([key],cascade=False),fail_quiet,lambda _:None)
        with self.assertRaises(sqlite3.Error):service.run(self.task['task_id'])
        self.assertEqual(self.transitions()[0]['phase'],'stopping')
        self.store.reconcile_task_recovery()
        self.assertEqual(self.transitions()[0]['phase'],'recovery_required')
        self.assertTrue(any(r['holds_authority'] for r in self.store.work_intents(self.targets[0]['child_task_id'])))

    def test_stale_owner_after_acceptance_cannot_apply(self):
        self.submit()
        with self.store._connection() as db:
            db.execute('UPDATE agents SET execution_generation=? WHERE agent_id=?',(str(uuid4()),self.key))
        result,=self.apply()
        self.assertEqual(result['phase'],'blocked')
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['control_intent'],'active')

    def test_revoked_planning_permission_cannot_apply(self):
        self.submit()
        with self.store._connection() as db:db.execute('UPDATE agents SET planning_enabled=0 WHERE agent_id=?',(self.key,))
        result,=self.apply()
        self.assertEqual(result['phase'],'blocked')

    def test_repeated_different_key_without_evidence_conflicts(self):
        message=self.decision(changes=[self.change(action='retain')]);self.submit(message)
        # Return the same authorized session to an independent synchronization turn.
        manager.agent_statuses[self.key]='stopped';self.process.poll.return_value=0;self.process.stdout.close()
        with self.store._connection() as db:
            db.execute("UPDATE agents SET status='running' WHERE agent_id=?",(self.key,))
            db.execute("UPDATE delegation_continuations SET phase='independent' WHERE task_id=?",(self.task['task_id'],))
            db.execute("UPDATE tasks SET status='in_progress' WHERE task_id=?",(self.task['task_id'],))
            db.execute("UPDATE task_blockers SET active=0 WHERE task_id=? AND blocker_type='orchestration'",(self.task['task_id'],))
        second=json.loads(message);second['decision_key']='different-key'
        with self.assertRaisesRegex(ValueError,'without new evidence'):self.submit(json.dumps(second))

    def test_retire_unmaterialized_request_keeps_history(self):
        # Existing independent round may contain a later scope-blocked request.
        with self.store._connection() as db:
            d=promotion.delegations.create(db,self.task['task_id'],project_id=self.project['project_id'],
                requested_by_agent_id=self.key,requested_by_assignment_id=self.process._control_center_execution.origin.assignment_id,
                request_key='unmaterialized',instruction='obsolete')
            token=replanning.state_token(db,d['delegation_id'])
        self.submit(self.decision(changes=[self.change(delegation_id=d['delegation_id'],expected=token)]))
        result,=self.apply()
        self.assertEqual(result['phase'],'applied')
        self.assertEqual(self.store.get_delegation(d['delegation_id'])['status'],'canceled')
        self.assertEqual(len(self.store.list_tasks()),3)

    def test_independent_transition_can_apply_while_other_is_blocked(self):
        self.store.create_task('Preserved descendant',parent_task_id=self.targets[0]['child_task_id'],project_id=self.project['project_id'])
        self.submit(self.decision(changes=[self.change(),self.change(1,action='retain')]))
        results=self.apply()
        self.assertEqual([r['phase'] for r in results],['blocked','applied'])

    def test_accepted_transition_never_launches_on_database_open(self):
        self.submit();count=len(self.commands)
        reopened=AgentStore(self.store.path)
        with reopened._connection() as db:self.assertEqual(len(replanning.inspect(db,self.task['task_id'])),1)
        self.assertEqual(len(self.commands),count)

    def test_real_task_worktree_partial_changes_survive_retirement(self):
        from pathlib import Path
        from app.task_workspaces import provision_task_workspace
        from app.project_domain import canonical_path
        from app.workspace_freshness import ensure_workspace_current
        from workspace_test_support import make_repository
        repository=self.root/'repository'
        make_repository(repository)
        path,canonical=canonical_path(repository)
        with self.store._connection() as db:
            db.execute('DELETE FROM task_workspaces')
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?',(path,canonical,self.project['project_id']))
        self.patch(patch.dict(manager.os.environ,CONTROL_CENTER_WORKSPACE_ROOT=str(self.root/'isolated-workspaces')))
        provision_task_workspace(self.store,self.task['task_id'])
        self.patch(patch.object(manager,'_execution_workspace',side_effect=lambda task_id,origin='legacy_project_snapshot':
            Path(provision_task_workspace(self.store,task_id,origin_kind=origin)['workspace_path'])))
        self.patch(patch.object(manager,'ensure_workspace_current',side_effect=ensure_workspace_current))
        children=manager.materialize_delegations(self.task['task_id'])
        child=children[0]['launch_agent_id']
        workspace=self.store.get_task_workspace(self.targets[0]['child_task_id'])
        partial=Path(workspace['workspace_path'])/'unfinished.txt'
        partial.write_text('Useful partial progress',encoding='utf-8')
        manager.agents[child].stdout.close()
        self.stream(self.key,self.process,[self.decision()]);self.finish(self.key,self.process)
        result,=self.apply()
        self.assertEqual(result['phase'],'applied',result)
        self.assertEqual(partial.read_text(encoding='utf-8'),'Useful partial progress')
        self.assertFalse((repository/'unfinished.txt').exists())
        self.assertEqual(self.store.get_task_workspace(self.targets[0]['child_task_id']),workspace)
        self.assertEqual(len(self.store.list_task_assignments(self.targets[0]['child_task_id'])),1)

    def test_foreign_root_delegation_is_not_authorized(self):
        other=self.store.create_task('Other root',project_id=self.project['project_id'])
        other_agent=manager.start_agent('Other root','codex',task_id=other['task_id'])
        assignment=self.store.get_active_assignment_for_agent(other_agent)
        record=self.store.create_delegation(other['task_id'],project_id=self.project['project_id'],
            requested_by_agent_id=other_agent,requested_by_assignment_id=assignment['assignment_id'],
            request_key='other',instruction='Unrelated work')
        with self.store._connection() as db:token=replanning.state_token(db,record['delegation_id'])
        with self.assertRaisesRegex(ValueError,'directly owned'):
            self.submit(self.decision(changes=[self.change(delegation_id=record['delegation_id'],expected=token)]))
        self.assertEqual(self.transitions(),[])
        self.assertEqual(self.store.get_task(other['task_id'])['control_intent'],'active')

    def test_competing_final_decisions_cannot_overwrite(self):
        from concurrent.futures import ThreadPoolExecutor
        messages=[self.decision(decision_key='one'),self.decision(decision_key='two',changes=[self.change(1)])]
        def submit(message):
            try:self.submit(message);return 'accepted'
            except ValueError:return 'conflict'
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(submit,messages))
        self.assertEqual(sorted(results),['accepted','conflict'])
        self.assertEqual(len(self.transitions()),1)

    def test_parent_finishing_requires_all_retirements_settled(self):
        self.stream(self.key,self.process,[self.decision(changes=[self.change(),self.change(1)])])
        self.finish(self.key,self.process)
        self.apply()
        self.assertTrue(all(r['phase']=='applied' for r in self.transitions()))
        state=manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'],'finishing',state)
        self.stream(self.key,manager.agents[self.key],['Overall task verified'])
        self.finish(self.key,manager.agents[self.key])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'],'completed')

    def test_unknown_launch_cannot_be_retired_or_released(self):
        with self.store._connection() as db:
            db.execute("UPDATE delegation_materializations SET phase='recovery_required' WHERE delegation_id=?",(self.targets[0]['delegation_id'],))
        self.submit();result,=self.apply()
        self.assertEqual(result['phase'],'blocked')
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['control_intent'],'active')

    def test_unintegrated_completed_result_remains_reuse_candidate(self):
        children=manager.materialize_delegations(self.task['task_id'])
        child=children[0]['launch_agent_id']
        self.stream(child,manager.agents[child],['Completed work'])
        self.finish(child,manager.agents[child])
        with self.store._connection() as db:
            result=db.execute('SELECT result_id FROM delegation_results WHERE agent_id=?',(child,)).fetchone()[0]
        self.submit(self.decision(changes=[self.change(1,action='reuse',reuse_result_id=result)]))
        transition,=self.apply()
        self.assertEqual(transition['phase'],'blocked')
        self.assertIn('integration evidence',transition['reason'])
        self.assertNotEqual(self.store.get_task(self.targets[1]['child_task_id'])['status'],'completed')

    def test_schema_v26_and_migration_rollback(self):
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],26)
            db.execute('DROP TABLE replan_transitions');db.execute('DROP TABLE replan_decisions');db.execute('DROP TABLE replan_signals')
            db.execute('PRAGMA user_version=25')
        original=replanning.migrate
        def fail(db):original(db);raise sqlite3.OperationalError('injected migration failure')
        with patch.object(replanning,'migrate',side_effect=fail),self.assertRaises(RuntimeError):AgentStore(self.store.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],25)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='replan_decisions'").fetchone())
        AgentStore(self.store.path)
        with self.store._connection() as db:self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_review_signals_coalesce_without_launch(self):
        count=len(self.commands)
        with self.store._connection() as db:
            replanning.signal(db,self.task['task_id'],'evidence:1','A material failure')
            replanning.signal(db,self.task['task_id'],'evidence:1','A material failure')
            self.assertEqual(db.execute('SELECT count(*) FROM replan_signals').fetchone()[0],1)
        self.assertEqual(len(self.commands),count)

    def test_restart_never_advances_accepted_transition(self):
        self.submit();count=len(self.commands)
        self.store.reconcile_task_recovery()
        self.assertEqual(len(self.commands),count)
        self.assertEqual(self.transitions()[0]['phase'],'validated')

    def test_context_is_valid_bounded_json(self):
        with self.store._connection() as db:
            context=replanning.context(db,self.task['task_id'])
        self.assertLessEqual(len(context.encode()),16384)
        self.assertEqual(len(json.loads(context)['targets']),2)


if __name__=='__main__':unittest.main()


class VerifiedReuseTests(unittest.TestCase):
    setUp = ReplanningTests.setUp
    patch = ReplanningTests.patch
    stream = ReplanningTests.stream
    finish = ReplanningTests.finish
    launch = ReplanningTests.launch
    plan = ReplanningTests.plan
    message = ReplanningTests.message
    rows = ReplanningTests.rows
    workspace_fixture = ReplanningTests.workspace_fixture
    accepted = ReplanningTests.accepted
    change = ReplanningTests.change
    decision = ReplanningTests.decision
    submit = ReplanningTests.submit
    transitions = ReplanningTests.transitions
    apply = ReplanningTests.apply

    def fixture(self, attested=True, integration_status='noop'):
        from app import integrations, workspace_git
        from workspace_test_support import make_repository
        source_id,target_id = [t['child_task_id'] for t in self.targets]
        self.store.change_dependency(target_id,source_id)
        children=manager.materialize_delegations(self.task['task_id'])
        child=next(c['launch_agent_id'] for c in children if c['delegation_id']==self.targets[0]['delegation_id'])
        self.stream(child,manager.agents[child],['Completed work'])
        self.finish(child,manager.agents[child])
        self.source_path=make_repository(self.root/'reuse-source')
        self.destination_path=make_repository(self.root/'reuse-destination')
        source_snapshot=workspace_git.snapshot_source_tree(self.source_path)
        destination_snapshot=workspace_git.snapshot_source_tree(self.destination_path)
        self.integration=self.store.claim_integration(dict(integration_id=str(uuid4()),source_task_id=source_id,
            destination_kind='task',destination_id=self.task['task_id'],destination_key='task:'+self.task['task_id'],
            base_snapshot=source_snapshot,source_snapshot=source_snapshot,destination_before_snapshot=destination_snapshot,status='preparing'))
        self.store.update_integration(self.integration['integration_id'],integration_status,result_snapshot=destination_snapshot)
        with self.store._connection() as db:
            self.result=db.execute('SELECT result_id FROM delegation_results WHERE agent_id=?',(child,)).fetchone()[0]
            # Fixture of an already delivered/explicitly accepted result. Real Hybrid
            # acknowledgement/integration validation is exercised by its own suite.
            db.execute("UPDATE delegation_results SET phase='accepted',integration_id=?,source_snapshot=?,acknowledgement=? WHERE result_id=?",
                (self.integration['integration_id'],source_snapshot,json.dumps(dict(decision='accept',reason='Reviewed interface')),self.result))
            db.execute("UPDATE delegations SET status='acknowledged',closed_at='fixture' WHERE delegation_id=?",(self.targets[0]['delegation_id'],))
            a=dict(protocol='control-center.reuse-attestation.v1',source_delegation_id=self.targets[0]['delegation_id'],
                source_task_id=source_id,source_expected=replanning.state_token(db,self.targets[0]['delegation_id']),
                integration_id=self.integration['integration_id'],target_task_id=target_id,
                responsibility=replanning.responsibility(db,self.targets[1]['delegation_id']),
                required_deliverable='The existing parser interface with the same input and output contract',
                explanation='The accepted source implements the same parser interface and validation rules required by this responsibility.')
        self.patch(patch.object(integrations,'resolve',return_value=({},self.source_path,self.destination_path,'task',self.task['task_id'])))
        self.patch(patch('app.workspace_freshness.evaluate_workspace_freshness',return_value={'freshness':'fresh'}))
        item=self.change(1,action='reuse',reuse_result_id=self.result)
        if attested:item['compatibility']=a
        self.payload=self.decision(changes=[item])
        return json.loads(self.payload)

    def apply_payload(self, value=None):
        self.submit(json.dumps(value) if value is not None else self.payload)
        self.process.poll.return_value=0
        self.process.stdout.close()
        return self.apply()[0]

    def test_authorized_reuse_retires_only_target_durably_and_idempotently(self):
        self.fixture()
        with self.store._connection() as db:
            original=dict(db.execute('SELECT * FROM delegation_results WHERE result_id=?',(self.result,)).fetchone())
        commands=len(self.commands)
        row=self.apply_payload()
        self.assertEqual(row['phase'],'applied',row)
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['status'],'canceled')
        self.assertEqual(self.store.get_task(self.targets[0]['child_task_id'])['status'],'completed')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'],'completed')
        self.assertEqual(self.apply()[0],row)
        self.assertEqual(len(self.commands),commands)
        with self.store._connection() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM delegation_results WHERE result_id=?',(self.result,)).fetchone()),original)
            self.assertEqual(db.execute('SELECT count(*) FROM delegation_results').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM integrations').fetchone()[0],1)
            self.assertFalse(hybrid.outstanding(db,self.task['task_id']))
        self.assertEqual(AgentStore(self.store.path).get_task(self.targets[1]['child_task_id'])['status'],'canceled')

    def test_old_fresh_candidate_without_attestation_stays_blocked(self):
        self.fixture(attested=False)
        row=self.apply_payload()
        self.assertEqual(row['phase'],'blocked')
        self.assertIn('attestation required',row['reason'])

    def test_applied_integration_is_supported(self):
        self.fixture(integration_status='applied')
        self.assertEqual(self.apply_payload()['phase'],'applied')

    def test_invalid_attestation_shapes_and_generic_approval_rejected(self):
        value=self.fixture()
        a=value['changes'][0]['compatibility']
        for bad in (None,{},a|{'explanation':'reuse this'},a|{'extra':1},a|{'protocol':'v2'},
                    a|{'source_expected':'bad'},a|{'explanation':'x'*2001},a|{'target_task_id':'bad'}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                self.submit(json.dumps(value|{'changes':[value['changes'][0]|{'compatibility':bad}]}))
        self.assertEqual(self.transitions(),[])

    def test_incorrect_declared_source_target_or_scope_stays_blocked(self):
        value=self.fixture()
        value['changes'][0]['compatibility']['target_task_id']=self.task['task_id']
        row=self.apply_payload(value)
        self.assertEqual(row['phase'],'blocked')
        self.assertIn('provenance',row['reason'])

    def test_stale_source_token_stays_blocked(self):
        value=self.fixture()
        value['changes'][0]['compatibility']['source_expected']='0'*64
        self.assertEqual(self.apply_payload(value)['phase'],'blocked')

    def test_stale_target_token_rejected_before_handoff(self):
        value=self.fixture();value['changes'][0]['expected']='0'*64
        with self.assertRaises(ValueError):self.submit(json.dumps(value))
        self.assertEqual(self.transitions(),[])

    def test_unknown_result_rejected(self):
        value=self.fixture();value['changes'][0]['reuse_result_id']=str(uuid4())
        with self.assertRaises(ValueError):self.submit(json.dumps(value))

    def test_unacknowledged_result_blocked(self):
        self.fixture()
        with self.store._connection() as db:db.execute("UPDATE delegation_results SET phase='ready' WHERE result_id=?",(self.result,))
        self.assertEqual(self.apply_payload()['phase'],'blocked')

    def test_failed_integration_blocked(self):
        self.fixture(integration_status='failed')
        self.assertEqual(self.apply_payload()['phase'],'blocked')

    def test_changed_source_snapshot_blocked(self):
        self.fixture();(self.source_path/'source.txt').write_text('changed')
        self.assertIn('source changed',self.apply_payload()['reason'])

    def test_changed_destination_snapshot_blocked(self):
        self.fixture();(self.destination_path/'source.txt').write_text('changed')
        self.assertIn('destination changed',self.apply_payload()['reason'])

    def test_stale_parent_workspace_blocked(self):
        self.fixture()
        with patch('app.workspace_freshness.evaluate_workspace_freshness',return_value={'freshness':'stale'}):
            self.assertIn('fresh Parent',self.apply_payload()['reason'])

    def test_stale_generation_cannot_accept(self):
        self.fixture()
        with self.assertRaises(ValueError):self.submit(self.payload,replace(self.process._control_center_execution.origin,generation=str(uuid4())))

    def test_permission_revocation_blocks_application(self):
        self.fixture();self.submit(self.payload)
        with self.store._connection() as db:db.execute('UPDATE agents SET planning_enabled=0 WHERE agent_id=?',(self.key,))
        self.assertEqual(self.apply()[0]['phase'],'blocked')

    def test_target_descendant_blocks_without_canceling_anyone(self):
        value=self.fixture()
        descendant=self.store.create_task('Active descendant',parent_task_id=self.targets[1]['child_task_id'],project_id=self.project['project_id'])
        value['changes'][0]['expected']=self.change(1)['expected']
        row=self.apply_payload(value)
        self.assertIn('descendants',row['reason'])
        self.assertEqual(self.store.get_task(descendant['task_id'])['control_intent'],'active')

    def test_downstream_consumer_blocks_without_rewiring(self):
        value=self.fixture()
        consumer=self.store.create_task('Consumer',project_id=self.project['project_id'])
        self.store.change_dependency(consumer['task_id'],self.targets[1]['child_task_id'])
        value['changes'][0]['expected']=self.change(1)['expected']
        self.assertIn('consumer',self.apply_payload(value)['reason'])
        self.assertEqual(len(self.store.list_dependencies(consumer['task_id'])),1)

    def test_uncertain_target_ownership_blocked(self):
        value=self.fixture()
        with self.store._connection() as db:db.execute("UPDATE delegation_materializations SET phase='recovery_required' WHERE delegation_id=?",(self.targets[1]['delegation_id'],))
        value['changes'][0]['expected']=self.change(1)['expected']
        self.assertIn('ownership',self.apply_payload(value)['reason'])

    def test_resource_conflict_blocks_before_release(self):
        self.fixture()
        # State token is unchanged; inject a conflict at final safety validation only.
        service=replanning.TransitionService(self.store,lambda _:None,lambda _:None,lambda _:None)
        self.submit(self.payload)
        with self.store._connection() as db:
            target=replanning.delegations.get(db,self.targets[1]['delegation_id'])
            with patch.object(replanning.resources,'inspect',return_value=[dict(current_ownership=None,conflicting_claim_ids=['other'],blocked_by=[])]),self.assertRaisesRegex(ValueError,'resource'):
                service._reuse_target(db,self.task,target)
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['control_intent'],'active')

    def test_exact_and_conflicting_replays(self):
        value=self.fixture();first=self.submit(self.payload)
        self.assertEqual(self.submit(self.payload),first)
        value['changes'][0]['compatibility']['explanation']+=' Changed.'
        with self.assertRaises(ValueError):self.submit(json.dumps(value))
        self.assertEqual(len(self.transitions()),1)

    def test_concurrent_identical_confirmations_create_one_transition(self):
        from concurrent.futures import ThreadPoolExecutor
        self.fixture()
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.submit(self.payload),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(self.transitions()),1)

    def test_crash_before_commit_rolls_back_retirement_and_retry_applies(self):
        self.fixture();self.submit(self.payload)
        self.process.poll.return_value=0;self.process.stdout.close()
        original=replanning.update
        def fail(db,key,phase,reason):
            original(db,key,phase,reason)
            if phase=='applied':raise sqlite3.OperationalError('injected failure')
        with patch.object(replanning,'update',side_effect=fail),self.assertRaises(sqlite3.OperationalError):self.apply()
        self.assertEqual(self.transitions()[0]['phase'],'validated')
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['control_intent'],'active')
        self.assertEqual(self.apply()[0]['phase'],'applied')

    def test_restart_pending_never_starts_worker_or_applies_reuse(self):
        self.fixture();self.submit(self.payload)
        commands=len(self.commands)
        AgentStore(self.store.path).reconcile_task_recovery()
        self.assertEqual(self.transitions()[0]['phase'],'validated')
        self.assertEqual(len(self.commands),commands)
        with self.store._connection() as db:self.assertTrue(replanning.pending(db,self.task['task_id']))

    def test_post_commit_publication_failure_does_not_repeat_side_effects(self):
        self.fixture();self.submit(self.payload)
        service=replanning.TransitionService(self.store,lambda _:None,lambda _:None,lambda _:(_ for _ in ()).throw(OSError('publish failed')))
        with self.assertRaises(OSError):service.run(self.task['task_id'])
        self.assertEqual(self.transitions()[0]['phase'],'applied')
        service.publish=lambda _:None
        service.run(self.task['task_id'])
        with self.store._connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM task_control_operations').fetchone()[0],1)


    def test_active_target_worker_is_not_stopped_or_released(self):
        value=self.fixture()
        target=self.targets[1]['child_task_id']
        self.store.change_dependency(target,self.targets[0]['child_task_id'],remove=True)
        manager.materialize_delegations(self.task['task_id'])
        value['changes'][0]['expected']=self.change(1)['expected']
        with self.store._connection() as db:
            value['changes'][0]['compatibility']['source_expected']=replanning.state_token(db,self.targets[0]['delegation_id'])
        row=self.apply_payload(value)
        self.assertEqual(row['phase'],'blocked')
        self.assertIn('ownership',row['reason'])
        assignment=self.store.get_active_assignment_for_task(target)
        self.assertIsNotNone(assignment)
        self.assertIsNone(manager.agents[assignment['agent_id']].poll())

    def test_foreign_project_source_is_not_adopted(self):
        self.fixture()
        import tempfile
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        foreign=self.store.create_project('Foreign',directory.name)
        with self.store._connection() as db:
            db.execute('UPDATE tasks SET project_id=? WHERE task_id=?',(foreign['project_id'],self.targets[0]['child_task_id']))
        self.assertEqual(self.apply_payload()['phase'],'blocked')

    def test_wrong_integration_destination_is_blocked(self):
        self.fixture()
        with self.store._connection() as db:db.execute('UPDATE integrations SET destination_id=? WHERE integration_id=?',(self.targets[1]['child_task_id'],self.integration['integration_id']))
        self.assertIn('integration evidence',self.apply_payload()['reason'])

    def test_stale_generation_after_acceptance_blocks_application(self):
        self.fixture();self.submit(self.payload)
        with self.store._connection() as db:db.execute('UPDATE agents SET execution_generation=? WHERE agent_id=?',(str(uuid4()),self.key))
        self.assertEqual(self.apply()[0]['phase'],'blocked')

    def test_source_changes_between_inspection_and_commit_fail_safe(self):
        self.fixture()
        original=replanning.TransitionService._reuse_evidence
        calls=[]
        def changed(service,db,row):
            calls.append(1)
            if len(calls)==2:(self.source_path/'source.txt').write_text('changed during inspection')
            return original(service,db,row)
        with patch.object(replanning.TransitionService,'_reuse_evidence',changed):
            row=self.apply_payload()
        self.assertEqual(row['phase'],'blocked')
        self.assertIn('workspace changed',row['reason'])
        self.assertEqual(self.store.get_task(self.targets[1]['child_task_id'])['control_intent'],'active')

    def test_reuse_and_retirement_cannot_both_own_target(self):
        self.fixture();self.submit(self.payload)
        retire=self.decision(decision_key='competing',changes=[self.change(1)])
        # A second execution cannot seize the pending transition even if it is
        # represented as current; the original target journal owns settlement.
        with self.store._connection() as db:
            db.execute("UPDATE agents SET status='running' WHERE agent_id=?",(self.key,))
            db.execute("UPDATE delegation_continuations SET phase='independent' WHERE task_id=?",(self.task['task_id'],))
        with self.assertRaises(ValueError):
            self.store.accept_replanning_message(self.process._control_center_execution.origin,retire,
                session_id=manager.agent_sessions[self.key],message_id='competing')
        self.assertEqual(len(self.transitions()),1)

    def test_unresolved_integration_journal_blocks_reuse(self):
        self.fixture()
        self.store.claim_integration(dict(integration_id=str(uuid4()),source_task_id=self.targets[0]['child_task_id'],
            destination_kind='task',destination_id=self.task['task_id'],destination_key='task:'+self.task['task_id'],
            base_snapshot='new-base',source_snapshot='new-source',destination_before_snapshot='new-before',status='preparing'))
        self.assertIn('integration ownership',self.apply_payload()['reason'])

    def test_successful_provider_ingress_retains_attestation(self):
        self.fixture()
        self.stream(self.key,self.process,[self.payload])
        self.finish(self.key,self.process)
        row=self.apply()[0]
        self.assertEqual(row['phase'],'applied',row)
        self.assertEqual(json.loads(row['content'])['compatibility'],json.loads(self.payload)['changes'][0]['compatibility'])


    def test_parent_completes_only_through_normal_finishing_after_reuse(self):
        self.fixture()
        self.stream(self.key,self.process,[self.payload])
        self.finish(self.key,self.process)
        with self.store._connection() as db:self.assertTrue(hybrid.outstanding(db,self.task['task_id']))
        self.assertEqual(self.apply()[0]['phase'],'applied')
        state=manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'],'finishing',state)
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'],'completed')
        self.stream(self.key,manager.agents[self.key],['Overall task verified'])
        self.finish(self.key,manager.agents[self.key])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'],'completed')
