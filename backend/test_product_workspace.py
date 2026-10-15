"""Product HTTP boundaries and durable projections; every provider process is a shim."""
import json
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from app import main, agent_manager as manager, presentation
from app.persistence import AgentStore
import test_promotion as promotion_tests
import test_replanning as replan_tests
from api_test_support import request_headers


class TestClient:
    """Existing project's dependency-free ASGI request style; no server/lifespan."""
    def __init__(self, app):
        self.app = app

    def post(self, path, json):
        return self.request('POST', path, json)

    def get(self, path):
        return self.request('GET', path)

    def request(self, method, url, body=None):
        async def run():
            messages = []
            path, _, query = url.partition('?')
            async def receive():
                return {'type': 'http.request', 'body': json.dumps(body).encode() if body is not None else b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await self.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': method, 'scheme': 'http', 'path': path, 'raw_path': path.encode(),
                'query_string': query.encode(), 'headers': request_headers(method),
                'root_path': '', 'client': ('127.0.0.1', 1234), 'server': ('localhost', 8000)}, receive, send)
            text = b''.join(item.get('body', b'') for item in messages).decode()
            return SimpleNamespace(status_code=messages[0]['status'], text=text, json=lambda: json.loads(text))
        return asyncio.run(run())


class WorkspaceTests(unittest.TestCase):
    setUp = promotion_tests.PromotionTests.setUp
    patch = promotion_tests.PromotionTests.patch
    plan = promotion_tests.PromotionTests.plan
    message = promotion_tests.PromotionTests.message
    stream = promotion_tests.PromotionTests.stream
    finish = promotion_tests.PromotionTests.finish
    launch = promotion_tests.PromotionTests.launch
    accepted = promotion_tests.PromotionTests.accepted
    rows = promotion_tests.PromotionTests.rows

    def create(self, _root=None, **options):
        self.patch(patch.object(manager, '_project_root', _root or self.root))
        response = TestClient(main.app).post('/agents/start', json=dict(task='Review fixture', agent_type='codex', autonomous=True) | options)
        self.assertEqual(response.status_code, 200, response.text)
        key = response.json()['agent_id']
        self.task = self.store.get_task(self.store.agent_task_ids(key)[0])
        return key, manager.agents[key]

    def test_api_creation_maps_all_flags_and_retains_sandbox(self):
        key, _ = self.create(sandbox='workspace-write')
        with self.store._connection() as db:
            row = db.execute('SELECT * FROM agents WHERE agent_id=?', (key,)).fetchone()
            self.assertTrue(all(row[flag] for flag in presentation.CAPABILITIES))
            self.assertEqual(row['sandbox'], 'workspace-write')
        node = TestClient(main.app).get('/workspace').json()['agents'][0]
        self.assertTrue(node['autonomous'])
        self.assertEqual(node['agent_id'], key)

    def test_explicit_off_and_legacy_requests_remain_off(self):
        for payload in ({'autonomous': False}, {}):
            with self.subTest(payload=payload), patch.object(main, 'start_agent', return_value='id') as start:
                response = TestClient(main.app).post('/agents/start', json={'task': 'Work', 'agent_type': 'codex'} | payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(start.call_args.kwargs, {})

    def test_malformed_and_forged_permissions_never_spawn(self):
        for fields in ({'agent_type': 'mock', 'autonomous': True}, {'agent_type': 'other'},
                       {'autonomous': 'true'}, {'autonomous': 1}, {'autonomous': None},
                       *({flag: True} for flag in presentation.CAPABILITIES)):
            with self.subTest(fields=fields), patch.object(main, 'start_agent') as start:
                response = TestClient(main.app).post('/agents/start', json={'task': 'Work', 'agent_type': 'codex'} | fields)
                self.assertEqual(response.status_code, 422)
                start.assert_not_called()
        for path in ('/agents/parent/children/start', '/tasks/task/start-agent'):
            response = TestClient(main.app).post(path, json={'task': 'Work', 'agent_type': 'codex', 'autonomous': True})
            self.assertEqual(response.status_code, 422)

    def test_read_projection_does_not_enable_old_agents_or_launch(self):
        key, process = self.create(autonomous=False)
        before = len(self.commands)
        first = presentation.workspace(self.store)
        for _ in range(3):
            self.assertEqual(presentation.workspace(self.store), first)
            self.assertFalse(presentation.activity(self.store, key)['context']['autonomous'])
        reopened = AgentStore(self.store.path)
        self.assertEqual(presentation.workspace(reopened), first)
        self.assertEqual(len(self.commands), before)
        self.assertIs(manager.agents[key], process)

    def test_pagination_unique_and_counts_use_tasks_not_workers(self):
        key, process = self.create()
        self.stream(key, process, ['Done'])
        self.finish(key, process)
        second, _ = self.create(autonomous=False)
        page = presentation.workspace(self.store, limit=1)
        next_page = presentation.workspace(self.store, limit=1, after=page['next_after'])
        self.assertEqual({page['agents'][0]['agent_id'], next_page['agents'][0]['agent_id']}, {key, second})
        self.assertIsNone(next_page['next_after'])
        self.assertEqual(page['overview']['completed_tasks'], 1)
        self.assertEqual(page['overview']['active_agents'], 1)
        client = TestClient(main.app)
        self.assertEqual(client.get('/workspace?limit=501').status_code, 422)
        self.assertEqual(client.get('/agents/missing/activity').status_code, 404)

    def test_planning_promotion_activity_handoff_and_privacy(self):
        key, process = self.create()
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        view = TestClient(main.app).get('/agents/' + key + '/activity').json()
        self.assertEqual(view['context']['waiting_context']['kind'], 'delegation')
        self.assertEqual(view['context']['plan_revision'], 1)
        self.assertIn('plan', {e['kind'] for e in view['events']})
        self.assertIn('promotion', {e['kind'] for e in view['events']})
        self.assertEqual(len(view['delegations']), 1)
        self.assertNotIn(str(self.root), json.dumps(view))
        self.assertNotIn('session_id', json.dumps(view))
        self.assertNotIn('source_snapshot', json.dumps(view))
        self.assertEqual(presentation.workspace(self.store)['overview']['completed_tasks'], 0)
        before = len(self.commands)
        presentation.activity(self.store, key)
        self.assertEqual(len(self.commands), before)

    def test_waiting_classification_is_conservative_and_history_is_not_current(self):
        cases = [('dependency_incomplete', 'dependency'), ('resource_conflict', 'coordination'),
                 ('delegation_requested', 'delegation'), ('recovery_required', 'recovery')]
        for blocker, kind in cases:
            self.assertEqual(presentation.waiting_context('waiting', 'Question?', 'blocked', [blocker], None, True)['kind'], kind)
        self.assertEqual(presentation.waiting_context('waiting', None, 'waiting', [], None, True)['kind'], 'unknown')
        self.assertEqual(presentation.waiting_context('waiting', 'Question?', 'waiting', [], None, True)['kind'], 'user')
        self.assertIsNone(presentation.waiting_context('stopped', 'Old question', 'blocked', ['resource_conflict'], None, False))
        self.assertEqual(presentation.waiting_context('stopped', None, 'blocked', ['delegation_requested'], 'waiting', True, 0)['kind'], 'continuation')

    def test_core_details_survive_activity_projection_failure(self):
        key, _ = self.create()
        client = TestClient(main.app)
        with patch.object(presentation, 'activity', side_effect=OSError('Unavailable')):
            self.assertEqual(client.get('/agents/' + key + '/activity').status_code, 503)
            response = client.get('/agents/' + key + '?include_output=false')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['agent_id'], key)

    def test_paused_question_is_not_counted_as_input_request(self):
        key, process = self.create()
        self.stream(key, process, ['CONTROL_CENTER_WAITING: Which format?'])
        self.finish(key, process)
        self.assertEqual(presentation.workspace(self.store)['overview']['needs_input'], 1)
        with self.store._connection() as db:
            db.execute("UPDATE tasks SET status='paused' WHERE task_id=?", (self.task['task_id'],))
        snapshot = presentation.workspace(self.store)
        self.assertEqual(snapshot['overview']['needs_input'], 0)
        self.assertEqual(snapshot['agents'][0]['waiting_context']['kind'], 'control')

    def test_genuine_question_notifies_but_handoff_does_not(self):
        key, process = self.create()
        manager.notifications.transition.reset_mock()
        self.stream(key, process, ['CONTROL_CENTER_WAITING: Which format?'])
        self.finish(key, process)
        self.assertEqual(manager.get_agent(key, include_output=False)['waiting_context']['kind'], 'user')
        manager.notifications.transition.assert_called_once_with(key, 'waiting', 'Review fixture')
        key, process = self.create()
        manager.notifications.transition.reset_mock()
        self.stream(key, process, [self.plan(), self.message()])
        self.finish(key, process)
        self.assertEqual(manager.get_agent(key, include_output=False)['waiting_context']['kind'], 'delegation')
        self.assertFalse(any(call.args[1] == 'waiting' for call in manager.notifications.transition.call_args_list))

    def test_projection_read_is_bounded_and_unrelated_activity_is_excluded(self):
        key, process = self.create()
        self.stream(key, process, ['Work underway'])
        origin = process._control_center_execution.origin
        import test_planning as planning_tests
        for revision in range(35):
            self.store.accept_planning_messages(origin, [(str(revision), planning_tests.decision(
                decision_key='revision-' + str(revision), base_revision=revision))], session_id=manager.agent_sessions[key])
        self.assertEqual(len(presentation.activity(self.store, key)['events']), 30)
        before = presentation.activity(self.store, key)
        other = self.store.create_task('Unrelated private responsibility', project_id=self.project['project_id'])
        self.assertNotIn(other['description'], json.dumps(presentation.activity(self.store, key)))
        self.assertEqual(presentation.activity(self.store, key), before)

    def test_product_creation_plan_child_result_replan_completion_and_reconnect(self):
        from pathlib import Path
        from app.project_domain import canonical_path
        from app.task_workspaces import provision_task_workspace
        from app.workspace_freshness import ensure_workspace_current
        from app import replanning, delegation_orchestration
        import test_planning as planning_tests
        from workspace_test_support import make_repository
        repository = self.root / 'repository'
        make_repository(repository)
        path, canonical = canonical_path(repository)
        with self.store._connection() as db:
            db.execute('UPDATE projects SET root_path=?,root_path_key=? WHERE project_id=?', (path, canonical, self.project['project_id']))
        self.patch(patch.dict(manager.os.environ, CONTROL_CENTER_WORKSPACE_ROOT=str(self.root / 'workspaces')))
        self.patch(patch.object(manager, '_execution_workspace', side_effect=lambda task_id, origin='project_snapshot':
            Path(provision_task_workspace(self.store, task_id, origin_kind=origin)['workspace_path'])))
        self.patch(patch.object(manager, 'ensure_workspace_current', side_effect=ensure_workspace_current))
        # The backend default differs from the selected source; ownership comes from the request.
        key, process = self.create(project_id=self.project['project_id'], sandbox='workspace-write')
        self.assertEqual(self.task['project_id'], self.project['project_id'])
        client = TestClient(main.app)
        self.evidence = []
        def capture(stage):
            snapshot = client.get('/workspace').json()
            details = {node['agent_id']: client.get('/agents/' + node['agent_id'] + '?include_output=false').json() for node in snapshot['agents']}
            activities = {node['agent_id']: client.get('/agents/' + node['agent_id'] + '/activity').json() for node in snapshot['agents']}
            self.evidence.append(dict(stage=stage, workspace=snapshot, details=details, activities=activities))
            return snapshot
        capture('created')
        intent, _ = self.store.create_work_intent(self.task['task_id'], namespace='component', key='parent', mode='single_owner')
        self.stream(key, process, [self.plan([planning_tests.item('a'), planning_tests.item('b', depends_on=['a'])]), self.message([promotion_tests.selection(), promotion_tests.selection('b')],
            continuation=dict(instruction='Review the remaining scope', intent_ids=[intent]))])
        self.finish(key, process)
        capture('promoted')
        progress = manager.materialize_delegations(self.task['task_id'])
        child = next(row['launch_agent_id'] for row in progress if row.get('phase') == 'started')
        child_task = self.store.agent_task_ids(child)[0]
        self.assertEqual(self.store.get_task(child_task)['project_id'], self.project['project_id'])
        snapshot = capture('child_started')
        self.assertEqual(next(n for n in snapshot['agents'] if n['agent_id'] == child)['parent_id'], key)
        self.assertTrue(next(n for n in snapshot['agents'] if n['agent_id'] == child)['autonomous'])
        child_workspace = Path(self.store.get_task_workspace(child_task)['workspace_path'])
        (child_workspace / 'source.txt').write_text('Verified synthetic contribution\n', encoding='utf-8')
        self.stream(child, manager.agents[child], ['Implemented the scoped contribution.'])
        self.finish(child, manager.agents[child])
        capture('child_completed')
        state = manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'], 'reviewing', state)
        with self.store._connection() as db:
            result, = delegation_orchestration.result_rows(db, self.task['task_id'])
        capture('result_delivered')
        ack = json.dumps(dict(protocol='control-center.delegation.v2', action='acknowledge_results', delivery_id=state['delivery_id'],
            results=[dict(result_id=result['result_id'], decision='accept', reason='Reviewed the contribution')]))
        self.stream(key, manager.agents[key], [ack]); self.finish(key, manager.agents[key])
        state = manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'], 'independent', state)
        pending = next(row for row in self.rows() if row['proposal_id'] == 'b')
        with self.store._connection() as db:
            token = replanning.state_token(db, pending['delegation_id'])
        decision = json.dumps(dict(protocol=replanning.PROTOCOL, task_id=self.task['task_id'], decision_key='remove-obsolete', revision=1,
            changes=[dict(action='retire', delegation_id=pending['delegation_id'], expected=token,
                reason='The remaining responsibility is no longer required', replacement=None, depends_on_task_ids=[], reuse_result_id=None)],
            withdraw=[], promote=[], continuation=None))
        self.stream(key, manager.agents[key], [decision]); self.finish(key, manager.agents[key])
        manager.advance_replanning(self.task['task_id'])
        capture('replanned')
        self.assertNotEqual(self.store.get_task(self.task['task_id'])['status'], 'completed')
        state = manager.advance_delegations(self.task['task_id'])
        self.assertEqual(state['phase'], 'finishing', state)
        self.stream(key, manager.agents[key], ['Reviewed the integrated contribution; complete.'])
        self.finish(key, manager.agents[key])
        snapshot = capture('completed')
        self.assertEqual(snapshot['overview']['completed_tasks'], 2)
        self.assertEqual(snapshot['overview']['pending_responsibilities'], 0)
        self.assertEqual((repository / 'source.txt').read_text(), 'original\n')
        self.assertEqual(presentation.workspace(AgentStore(self.store.path)), snapshot)
        self.assertEqual(presentation.activity(AgentStore(self.store.path), key), self.evidence[-1]['activities'][key])


class ReplanProjectionTests(unittest.TestCase):
    setUp = replan_tests.ReplanningTests.setUp

    def test_retirement_is_not_completion_and_survives_reopen(self):
        self.submit()
        self.apply()
        view = presentation.activity(self.store, self.key)
        self.assertIn('replan', {e['kind'] for e in view['events']})
        self.assertTrue(any(e['label'] == 'Responsibility retire: applied' for e in view['events']))
        self.assertTrue(any(d['status'] == 'canceled' for d in view['delegations']))
        self.assertEqual(presentation.workspace(self.store)['overview']['completed_tasks'], 0)
        self.assertEqual(presentation.activity(AgentStore(self.store.path), self.key), view)


for _name in ('patch', 'stream', 'finish', 'launch', 'plan', 'message', 'rows', 'workspace_fixture',
              'accepted', 'change', 'decision', 'submit', 'transitions', 'apply'):
    setattr(ReplanProjectionTests, _name, getattr(replan_tests.ReplanningTests, _name))


if __name__ == '__main__':
    unittest.main()
