import json
import io
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from app import delegation_continuation as protocol, delegation_orchestration as domain
from app import delegation_materialization as materialization, agent_manager as manager
from app.delegation_delivery import DeliveryService
from app.persistence import AgentStore
from app.codex_events import CodexEvents
from app.task_workspaces import provision_task_workspace
from app import integrations
import test_delegation_protocol as v1
import test_delegation_materialization as materialization_tests
from workspace_test_support import make_repository


def request(plan=None, *keys):
    value = json.loads(v1.envelope(*(keys or ('a',))))
    return json.dumps({**value, 'protocol': protocol.PROTOCOL, 'continuation': plan})


class HybridProtocolTests(unittest.TestCase):
    def test_v1_remains_closed(self):
        self.assertEqual(len(v1.protocol.validate(v1.envelope())), 1)
        with self.assertRaises(ValueError):
            v1.protocol.validate(request())

    def test_v2_optional_and_bounded_plan(self):
        self.assertIsNone(protocol.validate(request())['continuation'])
        plan = dict(instruction='Review independent documentation', intent_ids=[str(uuid4())])
        self.assertEqual(protocol.validate(request(plan))['continuation'], plan)
        for bad in ({}, {**plan, 'shell': 'execute'}, {**plan, 'instruction': ' '},
                    {**plan, 'instruction': 'x'*4097}, {**plan, 'intent_ids': []},
                    {**plan, 'intent_ids': plan['intent_ids']*2}, {**plan, 'intent_ids': ['bad']}):
            with self.subTest(plan=bad), self.assertRaises(ValueError):
                protocol.validate(request(bad))

    def test_ack_schema_rejects_spoofed_and_duplicate_fields(self):
        value = dict(protocol=protocol.PROTOCOL, action='acknowledge_results', delivery_id=str(uuid4()),
                     results=[dict(result_id=str(uuid4()), decision='accept', reason='Verified')])
        self.assertEqual(protocol.validate(json.dumps(value)), value)
        for bad in ({**value, 'agent_id':'spoof'}, {**value, 'results':value['results']*2},
                    {**value, 'results':[dict(result_id=str(uuid4()), decision='accept', reason='')]}):
            with self.assertRaises(ValueError):
                protocol.validate(json.dumps(bad))

    def test_tools_cannot_emit_continuation_or_acknowledgement(self):
        stream = v1.events('Normal response')
        stream.insert(2, dict(type='item.completed', item=dict(id='tool',type='command_execution',aggregated_output=request())))
        adapter = CodexEvents()
        v1.feed(adapter, stream)
        self.assertEqual(adapter.finish(0), 'Normal response')
        self.assertFalse(adapter.saw_request)


class HybridDomainTests(unittest.TestCase):
    setUp = materialization_tests.MaterializationTests.setUp
    agent = materialization_tests.MaterializationTests.agent
    task = materialization_tests.MaterializationTests.task
    worker = materialization_tests.MaterializationTests.worker
    origin_context = materialization_tests.MaterializationTests.origin_context
    accept = materialization_tests.MaterializationTests.accept
    snapshot = materialization_tests.MaterializationTests.snapshot
    attach = materialization_tests.MaterializationTests.attach
    ready = materialization_tests.MaterializationTests.ready
    admit = materialization_tests.MaterializationTests.admit

    def enable(self, plan=None, keys=('a',), old=False):
        with self.store._connection() as db:
            db.execute('UPDATE agents SET child_materialization_enabled=1,hybrid_delegation_enabled=1 WHERE agent_id=?', (self.origin['agent_id'],))
        receipt = self.accept(message=v1.envelope(*keys) if old else request(plan, *keys))
        return receipt['delegation_ids']

    def current(self):
        with self.store._connection() as db:
            return domain.state(db, self.parent['task_id'])

    def rows(self):
        with self.store._connection() as db:
            return domain.result_rows(db, self.parent['task_id'])

    def save_status(self, key, status, **options):
        record = next(r for r in self.store.load_agents() if r['agent_id'] == key)
        record['status'] = status
        self.store.save_agent(record, **options)

    def complete_child(self, identifier, code=0, status='finished'):
        child = self.ready(identifier)
        key, _ = self.admit(identifier)
        record = next(r for r in self.store.load_agents() if r['agent_id'] == self.origin['agent_id'])
        record.update(agent_id=key,task=self.store.get_task(child)['description'],status='running',parent_id=self.origin['agent_id'])
        self.store.save_agent(record, assignment_task_id=child)
        with self.store._connection() as db:
            db.execute("UPDATE delegation_materializations SET phase='started' WHERE delegation_id=?", (identifier,))
        assignment = self.store.get_active_assignment_for_task(child)
        self.save_status(key, status, output_lines=['Child output'], completion_evidence=dict(
            assignment_id=assignment['assignment_id'],returncode=code,validated=code==0))
        return child, key, assignment

    def make_deliverable(self, identifier):
        child, key, assignment = self.complete_child(identifier)
        record = self.store.claim_integration(dict(integration_id=str(uuid4()),source_task_id=child,
            destination_kind='task',destination_id=self.parent['task_id'],destination_key='task:'+self.parent['task_id'],
            base_snapshot='base',source_snapshot='source',destination_before_snapshot='before',status='preparing'))
        self.store.update_integration(record['integration_id'],'noop',result_snapshot='before')
        with self.store._connection() as db:
            db.execute("UPDATE delegation_results SET phase='ready',integration_id=? WHERE delegation_id=?", (record['integration_id'],identifier))
            db.execute("UPDATE delegations SET status='result_ready' WHERE delegation_id=?", (identifier,))
        return child, key, assignment

    def prepare(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return domain.prepare_delivery(db, self.parent['task_id'])

    def launch(self):
        launch = self.prepare()
        self.save_status(self.origin['agent_id'], 'running')
        origin = self.store.begin_delegation_execution(self.origin['agent_id'],str(uuid4()))
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            domain.launched(db, self.parent['task_id'],launch['launch_id'])
        return launch, origin

    def acknowledgement(self, launch, decision='accept'):
        return dict(protocol=protocol.PROTOCOL,action='acknowledge_results',delivery_id=launch['delivery_id'],
            results=[dict(result_id=r['result_id'],decision=decision,reason='Inspected result') for r in self.rows() if r['phase']=='delivered'])

    def ack(self, origin, payload):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            domain.acknowledge(db,origin,payload)

    def test_no_plan_waits_and_v1_can_opt_into_results(self):
        self.enable(old=True)
        self.assertIsNone(self.prepare())
        self.assertEqual(self.current()['phase'],'waiting')
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']),self.origin)

    def test_v2_requires_separate_activation(self):
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError,'not enabled'):
            self.accept(message=request())
        self.assertEqual(self.store.list_delegations(self.parent['task_id']),[])
        self.assertEqual(len(self.snapshot()['tasks']),len(before['tasks']))

    def planned(self, child_key='child'):
        intent = self.store.create_work_intent(self.parent['task_id'],namespace='component',key='parent',mode='single_owner')
        identifier, = self.enable(dict(instruction='Independent work',intent_ids=[intent[0]]))
        child = self.ready(identifier)
        self.store.create_work_intent(child,namespace='component',key=child_key,mode='single_owner')
        return identifier, child

    def test_explicit_plan_preserves_identity_and_allows_pending_children(self):
        identifier, child = self.planned()
        launch, origin = self.launch()
        self.assertEqual(launch['mode'],'independent')
        self.assertEqual(origin.agent_id,self.origin['agent_id'])
        self.assertEqual(origin.assignment_id,self.origin['assignment_id'])
        self.assertEqual(origin.parent_task_id,self.parent['task_id'])
        with self.store._connection() as db:
            self.assertEqual(materialization.authorize(db,identifier)[0]['child_task_id'],child)
        self.assertIsNotNone(self.admit(identifier)[0])

    def test_overlapping_scopes_block_continuation(self):
        self.planned('parent/subpart')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.current()['phase'],'waiting')

    def test_unverifiable_scopes_block_continuation(self):
        intent = self.store.create_work_intent(self.parent['task_id'],namespace='component',key='parent',mode='single_owner')
        identifier, = self.enable(dict(instruction='Work',intent_ids=[intent[0]]))
        self.ready(identifier)
        with self.assertRaisesRegex(ValueError,'scopes'):
            self.prepare()

    def test_no_materialized_children_cannot_admit_plan(self):
        self.enable(dict(instruction='Work',intent_ids=[str(uuid4())]))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_stale_worker_and_duplicate_launch_blocked(self):
        self.planned()
        self.launch()
        with self.assertRaises(ValueError):
            self.prepare()
        with self.store._connection() as db:
            db.execute('UPDATE agents SET execution_generation=? WHERE agent_id=?',(str(uuid4()),self.origin['agent_id']))
        with self.assertRaises(ValueError):
            self.prepare()

    def test_dependency_prevents_continuation(self):
        self.planned()
        dependency = self.task()
        self.store.change_dependency(self.parent['task_id'],dependency['task_id'])
        with self.assertRaises(ValueError):
            self.prepare()

    def test_unrelated_root_without_scope_blocks_continuation(self):
        self.planned()
        self.worker(self.task())
        with self.assertRaises(ValueError):
            self.prepare()

    def test_pause_cancel_and_stop_prevent_continuation(self):
        self.planned()
        with self.store._connection() as db:
            domain.revoke(db,self.parent['task_id'])
        with self.assertRaises(ValueError):
            self.prepare()
        for intent in ('paused','canceled'):
            self.store.request_work_control(self.parent['task_id'],intent)
            with self.assertRaises(ValueError):
                self.prepare()

    def test_child_success_proof_output_reference_and_assignment(self):
        identifier, = self.enable()
        child, key, assignment = self.complete_child(identifier)
        result, = self.rows()
        self.assertEqual(result['child_task_id'],child)
        self.assertEqual(result['assignment_id'],assignment['assignment_id'])
        self.assertEqual(result['agent_id'],key)
        self.assertEqual(result['outcome'],'completed')
        self.assertEqual(result['phase'],'recorded')
        self.assertGreaterEqual(result['output_through'],0)
        self.assertIn('Untrusted output excerpt',result['summary'])
        self.assertLessEqual(len(result['summary']),2000)

    def test_failed_child_does_not_become_success(self):
        identifier, = self.enable()
        self.complete_child(identifier,code=1,status='stopped')
        self.assertEqual(self.rows()[0]['outcome'],'failed')
        self.assertNotEqual(self.store.get_delegation(identifier)['status'],'result_ready')

    def test_stopped_child_does_not_become_success(self):
        identifier, = self.enable()
        self.complete_child(identifier,status='stopped')
        self.assertEqual(self.rows()[0]['outcome'],'stopped')

    def test_canceled_child_records_failure_without_success(self):
        identifier,=self.enable()
        child=self.ready(identifier)
        key,_=self.admit(identifier)
        record=next(r for r in self.store.load_agents() if r['agent_id']==self.origin['agent_id'])
        record.update(agent_id=key,parent_id=self.origin['agent_id'],status='running',task=self.store.get_task(child)['description'])
        self.store.save_agent(record,assignment_task_id=child)
        assignment=self.store.get_active_assignment_for_task(child)
        self.store.request_work_control(child,'canceled')
        self.save_status(key,'stopped',completion_evidence=dict(assignment_id=assignment['assignment_id'],returncode=0,validated=True))
        self.assertEqual(self.rows()[0]['outcome'],'canceled')
        self.assertEqual(self.store.get_task(child)['status'],'canceled')

    def test_replacement_results_preserve_prior_failure_history(self):
        identifier,=self.enable()
        child,_,_=self.complete_child(identifier,status='stopped')
        first=self.rows()[0]
        replacement=self.worker(self.store.get_task(child))
        self.save_status(replacement['agent_id'],'finished',completion_evidence=dict(
            assignment_id=replacement['assignment_id'],returncode=0,validated=True))
        old,new=self.rows()
        self.assertEqual(old,first)
        self.assertEqual(new['outcome'],'completed')
        self.assertNotEqual(new['assignment_id'],old['assignment_id'])

    def test_stale_child_exit_cannot_publish_for_replacement(self):
        identifier,=self.enable()
        child,key,assignment=self.complete_child(identifier,status='stopped')
        self.worker(self.store.get_task(child))
        with self.store._connection() as db:
            db.execute('DELETE FROM delegation_results')
            domain.record_exit(db,key,dict(assignment_id=assignment['assignment_id'],returncode=0,validated=True))
        self.assertEqual(self.rows(),[])

    def test_global_resource_conflict_prevents_parent_resume(self):
        self.planned()
        other=self.task()
        self.store.create_resource_claim(other['task_id'],resource_type='generic',resource_key='build:cache',scope='global',mode='exclusive')
        self.store.create_resource_claim(self.parent['task_id'],resource_type='generic',resource_key='build:cache',scope='global',mode='exclusive')
        with self.assertRaises(ValueError):
            self.prepare()
        self.assertEqual(self.current()['phase'],'waiting')

    def test_incremental_delivery_with_running_sibling_and_verified_scopes(self):
        self.store.create_work_intent(self.parent['task_id'],namespace='component',key='parent',mode='single_owner')
        first,second=self.enable(keys=('a','b'))
        self.make_deliverable(first)
        child=self.ready(second)
        self.store.create_work_intent(child,namespace='component',key='second',mode='single_owner')
        key,_=self.admit(second)
        record=next(r for r in self.store.load_agents() if r['agent_id']==self.origin['agent_id'])
        record.update(agent_id=key,task=self.store.get_task(child)['description'],status='running')
        self.store.save_agent(record,assignment_task_id=child)
        with self.store._connection() as db:
            db.execute("UPDATE delegation_materializations SET phase='started' WHERE delegation_id=?",(second,))
        launch,_=self.launch()
        self.assertEqual(launch['mode'],'reviewing')
        self.assertEqual(self.store.get_task(child)['status'],'in_progress')

    def test_acknowledgement_after_cancel_is_rejected(self):
        identifier,=self.enable()
        self.make_deliverable(identifier)
        launch,origin=self.launch()
        payload=self.acknowledgement(launch)
        self.store.request_work_control(self.parent['task_id'],'canceled')
        with self.assertRaises(ValueError):
            self.ack(origin,payload)
        self.assertIsNone(self.rows()[0]['acknowledgement'])

    def test_explicit_pause_resume_releases_only_pause_gate(self):
        self.planned()
        self.store.request_work_control(self.parent['task_id'],'paused')
        self.store.finalize_work_control(self.parent['task_id'])
        self.assertEqual(self.current()['phase'],'stopped')
        self.store.request_work_control(self.parent['task_id'],'active')
        self.assertEqual(self.current()['phase'],'waiting')
        self.assertEqual(self.prepare()['mode'],'independent')

    def test_pause_resume_cannot_clear_unknown_launch(self):
        self.planned()
        self.prepare()
        self.store.request_work_control(self.parent['task_id'],'paused')
        self.store.finalize_work_control(self.parent['task_id'])
        self.store.request_work_control(self.parent['task_id'],'active')
        self.assertEqual(self.current()['phase'],'recovery_required')
        with self.assertRaises(ValueError):
            self.prepare()

    def test_genuine_user_wait_survives_pause_resume_and_restart(self):
        self.planned()
        self.launch()
        self.save_status(self.origin['agent_id'],'waiting')
        before=self.current()
        self.store.request_work_control(self.parent['task_id'],'paused')
        self.store.finalize_work_control(self.parent['task_id'])
        self.assertEqual(self.current()['phase'],before['phase'])
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'paused')
        with self.assertRaises(ValueError):
            self.prepare()
        self.store=AgentStore(self.path)
        self.store.reconcile_task_recovery()
        self.store.request_work_control(self.parent['task_id'],'active')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'waiting')
        self.assertEqual(self.current()['current_generation'],before['current_generation'])
        self.assertEqual(self.current()['phase'],before['phase'])
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']),self.origin)

    def test_completion_without_execution_proof_is_unverified(self):
        identifier, = self.enable()
        self.complete_child(identifier)
        with self.store._connection() as db:
            db.execute('DELETE FROM delegation_results')
            domain.collect(db,self.parent['task_id'])
        self.assertEqual(self.rows()[0]['outcome'],'unverified')

    def test_result_persistence_failure_rolls_back_completion(self):
        identifier, = self.enable()
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_result BEFORE INSERT ON delegation_results BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.complete_child(identifier)
        child = self.store.get_delegation(identifier)['child_task_id']
        self.assertEqual(self.store.get_task(child)['status'],'in_progress')
        self.assertIsNotNone(self.store.get_active_assignment_for_task(child))
        self.assertEqual(self.rows(),[])

    def test_incremental_results_do_not_wait_for_siblings(self):
        first, second = self.enable(keys=('a','b'))
        self.make_deliverable(first)
        launch, _ = self.launch()
        self.assertEqual(launch['mode'],'reviewing')
        self.assertEqual(self.store.get_delegation(second)['status'],'requested')
        self.assertEqual(len(self.rows()),1)

    def test_active_parent_defers_integration_and_result_delivery(self):
        self.planned()
        self.launch()
        integrate, resume = Mock(),Mock()
        service = DeliveryService(self.store,Mock(),integrate,resume,Mock())
        service.run(self.parent['task_id'])
        integrate.assert_not_called()
        resume.assert_not_called()

    def test_valid_acknowledgement_is_durable_and_idempotent(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        launch, origin = self.launch()
        payload = self.acknowledgement(launch)
        self.ack(origin,payload)
        snapshot = self.snapshot()
        self.ack(origin,payload)
        self.assertEqual(self.snapshot(),snapshot)
        self.assertEqual(self.store.get_delegation(identifier)['status'],'acknowledged')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'blocked')

    def test_conflicting_and_stale_acknowledgements_rejected(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        launch, origin = self.launch()
        payload = self.acknowledgement(launch)
        with self.assertRaises(ValueError):
            self.ack(replace(origin,generation=str(uuid4())),payload)
        self.ack(origin,payload)
        payload['results'][0]['decision']='reject'
        with self.assertRaises(ValueError):
            self.ack(origin,payload)

    def test_parent_rejection_preserves_artifacts_and_blocks_completion(self):
        identifier, = self.enable()
        child, _, _ = self.make_deliverable(identifier)
        launch, origin = self.launch()
        self.ack(origin,self.acknowledgement(launch,'reject'))
        self.assertEqual(self.rows()[0]['phase'],'rejected')
        self.assertEqual(self.store.get_task(child)['status'],'completed')
        self.assertIsNone(self.prepare())
        with self.assertRaises(ValueError):
            self.store.end_assignment(self.origin['assignment_id'],'completed')

    def test_ack_cannot_override_integration_failure(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        with self.store._connection() as db:
            db.execute("UPDATE integrations SET status='conflict'")
        launch, origin = self.launch()
        with self.assertRaises(ValueError):
            self.ack(origin,self.acknowledgement(launch))
        self.ack(origin,self.acknowledgement(launch,'failure'))
        self.assertEqual(self.rows()[0]['phase'],'failure')

    def test_ack_failure_rolls_back_every_record(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        launch, origin = self.launch()
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER fail_ack BEFORE UPDATE OF acknowledgement ON delegation_deliveries BEGIN SELECT RAISE(ABORT,'injected'); END")
        before=self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.ack(origin,self.acknowledgement(launch))
        self.assertEqual(self.snapshot(),before)

    def test_parent_independent_exit_is_not_task_completion(self):
        self.planned()
        self.launch()
        self.save_status(self.origin['agent_id'],'finished')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'blocked')
        self.assertIsNotNone(self.store.get_active_assignment_for_task(self.parent['task_id']))

    def test_parent_can_complete_after_results_and_final_work(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        launch, origin = self.launch()
        self.ack(origin,self.acknowledgement(launch))
        final, _ = self.launch()
        self.assertEqual(final['mode'],'finishing')
        self.save_status(self.origin['agent_id'],'finished')
        self.assertEqual(self.store.get_task(self.parent['task_id'])['status'],'completed')
        self.assertIsNone(self.store.get_active_assignment_for_task(self.parent['task_id']))

    def test_restart_never_launches_and_preserves_results(self):
        identifier, = self.enable()
        self.make_deliverable(identifier)
        self.launch()
        before=self.rows()
        self.store=AgentStore(self.path)
        with patch.object(manager,'_spawn_process') as spawn:
            self.store.reconcile_task_recovery()
        spawn.assert_not_called()
        self.assertEqual(self.current()['phase'],'recovery_required')
        self.assertEqual(self.rows(),before)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_crash_after_admission_does_not_retry_launch(self):
        self.planned()
        self.prepare()
        self.store.reconcile_task_recovery()
        self.assertEqual(self.current()['phase'],'recovery_required')
        with self.assertRaises(ValueError):
            self.prepare()

    def test_resume_failure_preserves_journal_and_identity(self):
        self.planned()
        service=DeliveryService(self.store,Mock(),Mock(),Mock(side_effect=OSError('spawn uncertain')),Mock())
        with self.assertRaises(OSError):
            service.run(self.parent['task_id'])
        self.assertEqual(self.current()['phase'],'recovery_required')
        self.assertEqual(self.store.get_active_assignment_for_task(self.parent['task_id']),self.origin)

    def test_v20_migration_rollback_and_default_off(self):
        with self.store._connection() as db:
            for table in ('delegation_deliveries','delegation_results','delegation_continuations'):
                db.execute('DROP TABLE '+table)
            db.execute('ALTER TABLE agents DROP COLUMN hybrid_delegation_enabled')
            db.execute('PRAGMA user_version=20')
        original=domain.migrate
        def interrupted(db):
            original(db)
            raise sqlite3.OperationalError('injected')
        with patch.object(domain,'migrate',side_effect=interrupted),self.assertRaises(RuntimeError):
            AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],20)
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_results'").fetchone())
        self.store=AgentStore(self.path)
        with self.store._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 36)
            self.assertEqual(db.execute('SELECT hybrid_delegation_enabled FROM agents').fetchone()[0],0)
            self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def real_workspaces(self, identifier, change=True, conflict=False):
        repository=Path(self.temp.name)/'source'
        make_repository(repository)
        from app.project_domain import canonical_path
        path,key=canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?',(path,key,self.project['project_id']))
            # Only discard the fake test fixture row, before provisioning real
            # temporary worktrees. Production code never replaces workspace rows.
            db.execute('DELETE FROM task_workspaces')
        child=self.store.get_delegation(identifier)['child_task_id']
        self.workspace_env=patch.dict(manager.os.environ,CONTROL_CENTER_WORKSPACE_ROOT=str(Path(self.temp.name)/'workspaces'))
        self.workspace_env.start()
        self.addCleanup(self.workspace_env.stop)
        parent_ws=provision_task_workspace(self.store,self.parent['task_id'])
        child_ws=provision_task_workspace(self.store,child,origin_kind='parent_task_snapshot')
        if change:
            (Path(child_ws['workspace_path'])/'source.txt').write_text('Child change\n',encoding='utf-8')
        if conflict:
            (Path(parent_ws['workspace_path'])/'source.txt').write_text('Parent conflict\n',encoding='utf-8')
        return Path(parent_ws['workspace_path']),Path(child_ws['workspace_path']),repository

    def service(self, integrate=None, resume=None):
        return DeliveryService(self.store,Mock(),
            integrate or (lambda task: integrations.integrate(self.store,task,lambda _:None)),
            resume or Mock(),Mock())

    def test_real_integration_preserves_canonical_and_records_result(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        parent,child,repository=self.real_workspaces(identifier)
        service=self.service()
        service.run(self.parent['task_id'])
        self.assertEqual((parent/'source.txt').read_text(),'Child change\n')
        self.assertEqual((repository/'source.txt').read_text(),'original\n')
        result,=self.rows()
        self.assertEqual(self.store.get_integration(result['integration_id'])['status'],'applied')
        self.assertEqual(self.store.get_delegation(identifier)['status'],'result_ready')
        self.assertTrue(child.exists())

    def test_no_change_result_uses_noop_without_filesystem_apply(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        self.real_workspaces(identifier,change=False)
        with patch.object(integrations.applygit,'apply_plan') as apply:
            self.service().run(self.parent['task_id'])
        apply.assert_not_called()
        self.assertEqual(self.store.get_integration(self.rows()[0]['integration_id'])['status'],'noop')

    def test_read_only_parent_cannot_integrate_file_changes(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        parent,_,_=self.real_workspaces(identifier)
        with self.store._connection() as db:
            db.execute("UPDATE agents SET sandbox='read-only' WHERE agent_id=?",(self.origin['agent_id'],))
        self.service().run(self.parent['task_id'])
        self.assertEqual((parent/'source.txt').read_text(),'original\n')
        self.assertIn('Read-only',self.rows()[0]['last_error'])
        self.assertEqual(self.store.get_delegation(identifier)['status'],'materialized')

    def test_integration_conflict_is_reported_not_accepted(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        parent,_,_=self.real_workspaces(identifier,conflict=True)
        self.service().run(self.parent['task_id'])
        result,=self.rows()
        self.assertIn('conflict',result['last_error'])
        self.assertEqual((parent/'source.txt').read_text(),'Parent conflict\n')
        self.assertEqual(self.store.get_delegation(identifier)['status'],'materialized')

    def test_crash_after_integration_does_not_apply_twice(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        self.real_workspaces(identifier)
        def crash(task):
            integrations.integrate(self.store,task,lambda _:None)
            raise sqlite3.OperationalError('lost result acknowledgement')
        with self.assertRaises(sqlite3.OperationalError):
            self.service(integrate=crash).run(self.parent['task_id'])
        self.assertEqual(self.rows()[0]['phase'],'recorded')
        self.store=AgentStore(self.path)
        with patch.object(integrations.applygit,'apply_plan') as apply:
            self.service().run(self.parent['task_id'])
        apply.assert_not_called()
        self.assertEqual(len(self.store.list_integrations()),1)

    def test_changed_child_snapshot_after_crash_requires_inspection(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        _,child,_=self.real_workspaces(identifier)
        with self.assertRaises(sqlite3.OperationalError):
            self.service(integrate=Mock(side_effect=sqlite3.OperationalError('crash'))).run(self.parent['task_id'])
        (child/'source.txt').write_text('Changed again\n',encoding='utf-8')
        integrate=Mock()
        self.service(integrate=integrate).run(self.parent['task_id'])
        integrate.assert_not_called()
        self.assertIn('source changed',self.rows()[0]['last_error'])

    def test_delivery_resume_failure_keeps_durable_result_and_integration(self):
        identifier,=self.enable()
        self.complete_child(identifier)
        self.real_workspaces(identifier)
        with self.assertRaises(OSError):
            self.service(resume=Mock(side_effect=OSError('spawn failed'))).run(self.parent['task_id'])
        self.assertEqual(self.current()['phase'],'recovery_required')
        self.assertEqual(self.rows()[0]['phase'],'ready')
        self.assertEqual(self.store.get_delegation(identifier)['status'],'result_ready')


class HybridRuntimeTests(unittest.TestCase):
    setUp=v1.RuntimeTests.setUp
    patch=v1.RuntimeTests.patch
    read=v1.RuntimeTests.read
    finish=v1.RuntimeTests.finish

    def accepted(self, plan=None):
        self.patch(patch.object(manager,'_schedule_hybrid_for_agent'))
        self.patch(patch.object(manager,'_notify_hybrid_child_end'))
        with patch.object(manager,'_schedule_materialization'):
            key=manager.start_agent('Parent work','codex','workspace-write',task_id=self.task['task_id'],
                delegation_protocol_enabled=True,child_materialization_enabled=True,hybrid_delegation_enabled=True)
            self.read(key,manager.agents[key],request(plan))
            self.finish(key,manager.agents[key])
        def workspace(task_id,origin='legacy_project_snapshot'):
            task=self.store.get_task(task_id)
            if self.store.get_task_workspace(task_id) is None:
                ws=str(uuid4())
                self.store.save_task_workspace(dict(workspace_id=ws,task_id=task_id,project_id=task['project_id'],
                    workspace_path=str(self.root.parent/ws),workspace_path_key=ws,base_snapshot='fixture',
                    origin_kind=origin,source_task_id=task['parent_task_id']))
            return self.root
        self.patch(patch.object(manager,'_execution_workspace',side_effect=workspace))
        return key

    def planned(self):
        intent,_=self.store.create_work_intent(self.task['task_id'],namespace='component',key='parent',mode='single_owner')
        key=self.accepted(dict(instruction='Independent review only',intent_ids=[intent]))
        record,=self.store.list_delegations(self.task['task_id'])
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            child=materialization.attach(db,record['delegation_id'])['child_task_id']
        self.store.create_work_intent(child,namespace='component',key='child',mode='single_owner')
        return key,child

    def test_runtime_resume_uses_same_session_and_assignment(self):
        key,_=self.planned()
        assignment=self.store.get_active_assignment_for_agent(key)
        session=manager.agent_sessions[key]
        old=manager.agents[key]
        result=manager.advance_delegations(self.task['task_id'])
        self.assertEqual(result['phase'],'independent')
        self.assertIsNot(manager.agents[key],old)
        self.assertIn('resume '+session,self.commands[-1])
        self.assertNotIn('Independent review',self.commands[-1])
        self.assertEqual(self.store.get_active_assignment_for_agent(key),assignment)
        self.assertEqual(manager.agent_sandboxes[key],'workspace-write')
        count=len(self.commands)
        manager.advance_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands),count)

    def test_resume_instruction_goes_only_through_stdin(self):
        key,_=self.planned()
        sent=[]
        class Capture(io.StringIO):
            def close(self):
                sent.append(self.getvalue())
                super().close()
        spawn=manager._spawn_process.side_effect
        def capture(*args,**kwargs):
            process=spawn(*args,**kwargs)
            process.stdin=Capture()
            return process
        with patch.object(manager,'_spawn_process',side_effect=capture):
            manager.advance_delegations(self.task['task_id'])
        self.assertIn('Independent review only',sent[-1])
        self.assertNotIn('Independent review only',self.commands[-1])

    def test_independent_completion_does_not_notify_finished(self):
        key,_=self.planned()
        manager.advance_delegations(self.task['task_id'])
        self.read(key,manager.agents[key],'Independent work done',session=manager.agent_sessions[key])
        self.finish(key,manager.agents[key])
        self.assertEqual(manager.agent_statuses[key],'stopped')
        self.assertEqual(manager.notifications.transition.call_args.args[1],'stopped')

    def test_failed_resume_persistence_never_sends_prompt(self):
        key,_=self.planned()
        sent=[]
        spawn=manager._spawn_process.side_effect
        class Capture(io.StringIO):
            def write(self,value):
                sent.append(value)
                return super().write(value)
        def capture(*args,**kwargs):
            process=spawn(*args,**kwargs)
            process.stdin=Capture()
            return process
        with patch.object(manager,'_spawn_process',side_effect=capture),patch.object(manager,'_save_agent',return_value=False):
            with self.assertRaisesRegex(OSError,'persist resumed'):
                manager.advance_delegations(self.task['task_id'])
        self.assertEqual(sent,[])
        self.assertEqual(self.store.get_task(self.task['task_id'])['delegation_continuation']['phase'],'recovery_required')

    def test_explicit_child_replacement_retains_structured_completion_validation(self):
        key,child=self.planned()
        result,=manager.materialize_delegations(self.task['task_id'])
        first=result['launch_agent_id']
        manager.stop_agent(first)
        replacement=manager.start_agent('', 'codex','workspace-write',task_id=child)
        self.assertNotEqual(replacement,first)
        self.assertIsNotNone(manager._structured(manager.agents[replacement]))
        with self.store._connection() as db:
            row=db.execute('SELECT delegation_protocol_enabled FROM agents WHERE agent_id=?',(replacement,)).fetchone()
        self.assertEqual(row[0],0)

    def test_failed_always_resume_restores_wait_and_generation(self):
        key,_=self.planned()
        manager.advance_delegations(self.task['task_id'])
        self.read(key,manager.agents[key],'CONTROL_CENTER_WAITING: Which option?',session=manager.agent_sessions[key])
        self.finish(key,manager.agents[key])
        before=self.store.get_task(self.task['task_id'])['delegation_continuation']
        spawn=manager._spawn_process.side_effect
        class Broken(io.StringIO):
            def write(self,value):
                raise OSError('stdin failed')
        def broken(*args,**kwargs):
            process=spawn(*args,**kwargs)
            process.stdin=Broken()
            manager.agent_readers[key]=Mock(is_alive=Mock(return_value=False))
            return process
        with patch.object(manager,'_spawn_process',side_effect=broken),patch.object(manager,'_resume_cwd',return_value=self.root):
            with self.assertRaises(OSError):
                manager.decide_always_agent(key)
        after=self.store.get_task(self.task['task_id'])['delegation_continuation']
        self.assertEqual(after['phase'],before['phase'])
        self.assertEqual(after['current_generation'],before['current_generation'])
        self.assertEqual(manager.agent_statuses[key],'waiting')

    def test_pending_child_can_launch_alongside_authorized_parent(self):
        key,_=self.planned()
        manager.advance_delegations(self.task['task_id'])
        result,=manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(result['phase'],'started',result)
        self.assertEqual(manager.agent_statuses[key],'running')
        self.assertEqual(manager.agent_statuses[result['launch_agent_id']],'running')
        with self.store._connection() as db:
            row=db.execute('SELECT * FROM agents WHERE agent_id=?',(result['launch_agent_id'],)).fetchone()
        self.assertEqual(row['delegation_protocol_enabled'],0)
        self.assertEqual(row['hybrid_delegation_enabled'],0)
        self.assertIsNotNone(manager._structured(manager.agents[result['launch_agent_id']]))

    def test_stop_prevents_queued_followup(self):
        key,_=self.planned()
        manager.stop_agent(key)
        count=len(self.commands)
        manager.advance_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands),count)
        self.assertEqual(self.store.get_task(self.task['task_id'])['delegation_continuation']['phase'],'stopped')

    def test_stop_branch_stops_hybrid_parent_and_child_without_continuation(self):
        key,_=self.planned()
        manager.advance_delegations(self.task['task_id'])
        result,=manager.materialize_delegations(self.task['task_id'])
        child=result['launch_agent_id']
        manager.stop_branch(key)
        self.assertEqual(manager.agent_statuses[key],'stopped')
        self.assertEqual(manager.agent_statuses[child],'stopped')
        count=len(self.commands)
        manager.advance_delegations(self.task['task_id'])
        self.assertEqual(len(self.commands),count)
        with self.store._connection() as db:
            self.assertEqual(domain.result_rows(db,self.task['task_id'])[0]['outcome'],'stopped')

    def test_waiting_reply_redirect_and_stop_preserve_hybrid_identity(self):
        key,_=self.planned()
        manager.advance_delegations(self.task['task_id'])
        assignment=self.store.get_active_assignment_for_agent(key)
        session=manager.agent_sessions[key]
        self.read(key,manager.agents[key],'CONTROL_CENTER_WAITING: Which option?',session=session)
        self.finish(key,manager.agents[key])
        self.assertEqual(manager.agent_statuses[key],'waiting')
        with patch.object(manager,'_resume_cwd',return_value=self.root):
            manager.reply_agent(key,'Option A')
        self.assertEqual(self.store.get_active_assignment_for_agent(key),assignment)
        with patch.object(manager,'_resume_cwd',return_value=self.root),patch.object(manager,'evaluate_workspace_freshness',return_value=None),patch.object(manager,'reject_divergence'):
            manager.redirect_agent(key,'Keep the scope')
        self.assertEqual(self.store.get_active_assignment_for_agent(key),assignment)
        manager.stop_agent(key)
        self.assertEqual(manager.agent_statuses[key],'stopped')

    def test_mock_default_has_no_hybrid_orchestration(self):
        key=manager.start_agent('Parent work','mock',task_id=self.task['task_id'])
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT hybrid_delegation_enabled FROM agents WHERE agent_id=?',(key,)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delegation_continuations').fetchone()[0],0)

    def test_stale_jsonl_ack_cannot_change_result_state(self):
        key,_=self.planned()
        previous=manager.agents[key]
        manager.advance_delegations(self.task['task_id'])
        with patch.object(domain,'acknowledge') as ack:
            manager._finalize_process(key,previous)
        ack.assert_not_called()

    def test_full_result_integration_ack_and_parent_completion_with_mocked_cli(self):
        repository=self.root/'repository'
        make_repository(repository)
        from app.project_domain import canonical_path
        path,canonical=canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?',(path,canonical,self.project['project_id']))
        key=self.accepted()
        self.patch(patch.object(manager,'_schedule_materialization'))
        self.patch(patch.dict(manager.os.environ,CONTROL_CENTER_WORKSPACE_ROOT=str(self.root/'workspaces')))
        parent=provision_task_workspace(self.store,self.task['task_id'])
        def workspace(task_id,origin='legacy_project_snapshot'):
            return Path(provision_task_workspace(self.store,task_id,origin_kind=origin)['workspace_path'])
        from app.workspace_freshness import ensure_workspace_current
        self.patch(patch.object(manager,'_execution_workspace',side_effect=workspace))
        self.patch(patch.object(manager,'ensure_workspace_current',side_effect=ensure_workspace_current))
        progress,=manager.materialize_delegations(self.task['task_id'])
        self.assertEqual(progress['phase'],'started',progress)
        child=progress['launch_agent_id']
        child_task=self.store.agent_task_ids(child)[0]
        child_ws=self.store.get_task_workspace(child_task)
        (Path(child_ws['workspace_path'])/'source.txt').write_text('Validated Child contribution\n',encoding='utf-8')
        self.read(child,manager.agents[child],'Implemented and checked the assigned change.')
        self.finish(child,manager.agents[child])
        self.assertEqual(self.store.get_task(child_task)['status'],'completed')
        assignment=self.store.get_active_assignment_for_agent(key)
        state=manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'],'reviewing',state)
        self.assertEqual((Path(parent['workspace_path'])/'source.txt').read_text(),'Validated Child contribution\n')
        self.assertEqual((repository/'source.txt').read_text(),'original\n')
        with self.store._connection() as db:
            result,=domain.result_rows(db,self.task['task_id'])
        ack=json.dumps(dict(protocol=protocol.PROTOCOL,action='acknowledge_results',delivery_id=state['delivery_id'],
            results=[dict(result_id=result['result_id'],decision='accept',reason='Reviewed the integrated contribution')]))
        self.read(key,manager.agents[key],ack,session=manager.agent_sessions[key])
        self.finish(key,manager.agents[key])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'],'blocked')
        self.assertEqual(self.store.get_active_assignment_for_agent(key),assignment)
        state=manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'],'finishing',state)
        self.read(key,manager.agents[key],'Overall work checked and complete.',session=manager.agent_sessions[key])
        self.finish(key,manager.agents[key])
        self.assertEqual(self.store.get_task(self.task['task_id'])['status'],'completed')
        self.assertEqual(manager.agent_statuses[key],'finished')
        self.assertIsNone(self.store.get_active_assignment_for_agent(key))

    def test_child_failed_jsonl_turn_cannot_complete_task(self):
        key=self.accepted()
        self.patch(patch.object(manager,'_schedule_materialization'))
        progress,=manager.materialize_delegations(self.task['task_id'])
        child=progress['launch_agent_id']
        self.read(child,manager.agents[child],'Could not complete',ending='turn.failed')
        self.finish(child,manager.agents[child])
        self.assertEqual(manager.agent_statuses[child],'stopped')
        self.assertEqual(self.store.get_task(self.store.agent_task_ids(child)[0])['status'],'pending')
        self.assertEqual(manager.agent_statuses[key],'stopped')


if __name__ == '__main__':
    unittest.main()
