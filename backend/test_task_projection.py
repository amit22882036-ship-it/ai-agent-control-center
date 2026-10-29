"""Atlas B1: the read-only task-centric projection (GET /workspace/tasks)."""
from contextlib import closing
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import main, task_projection
from app.persistence import AgentStore
from execution_test_support import use_attested_test_doubles
from test_product_workspace import TestClient
import task_projection_scenarios as scenarios

FIXTURE = Path(__file__).resolve().parents[1] / 'frontend' / 'tests' / 'fixtures' / 'workspace-tasks.contract.json'
FORBIDDEN_KEYS = {'description', 'instruction', 'summary', 'session_id', 'root_path', 'changed_paths', 'conflict_paths',
                  'failure_reason', 'last_error', 'waiting_question', 'task', 'plan', 'output', 'reason', 'workspace_path'}


def keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys(item)


class TaskProjectionTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'fixture.sqlite3'
        self.store = AgentStore(self.path)
        patcher = patch.object(main.manager, '_task_store', return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(main.app)

    def get(self, **params):
        query = '&'.join(f'{k}={v}' for k, v in params.items() if v is not None)
        response = self.client.get('/workspace/tasks' + ('?' + query if query else ''))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def pages(self, **params):
        result, after = [], None
        while True:
            page = self.get(after=after, **params)
            result.append(page)
            after = page['next_after']
            if after is None:
                return result

    def by_id(self, page):
        return {node['task_id']: node for node in page['tasks'] + page['ancestors']}

    # Semantics -----------------------------------------------------------------------------

    def test_contract_scenario_keeps_every_concern_separate(self):
        scenarios.contract(self.path)
        nodes = self.by_id(self.get(limit=500))
        lead = nodes['release']
        self.assertEqual((lead['parent_task_id'], lead['root_task_id'], lead['depth'], lead['ancestry']), (None, 'release', 0, 'complete'))
        self.assertEqual(lead['child_count'], 2)
        self.assertEqual(lead['assignment']['current']['agent_id'], 'lead')
        self.assertEqual(lead['agent']['role'], 'current')
        self.assertTrue(lead['agent']['autonomous'])
        self.assertIsNone(lead['waiting_context'])  # running
        self.assertEqual(lead['continuation'], {'phase': 'reviewing', 'mode': 'reviewing', 'current_assignment': True})
        self.assertEqual(lead['delegations'], {'requested': 2, 'materialized': 1, 'result_ready': 1, 'acknowledged': 0, 'canceled': 0})
        # Proposed responsibility is counted, never shown as a Task.
        self.assertEqual(lead['planned'], {'unmaterialized': 2, 'by_disposition': {'blocked': 1, 'direct': 1}})
        self.assertEqual((lead['plan_revision'], lead['rounds']), (2, 2))
        self.assertNotIn('d-plan', nodes)

        docs = nodes['docs']
        self.assertEqual(docs['status'], 'completed')
        self.assertIsNone(docs['assignment']['current'])
        self.assertEqual(docs['assignment']['count'], 2)
        self.assertEqual(docs['assignment']['last_ended']['ended_reason'], 'completed')
        self.assertEqual(docs['assignment']['previous_agent_ids'], ['docs-new', 'docs-old'])
        self.assertEqual((docs['agent']['agent_id'], docs['agent']['role'], docs['agent']['removed']), ('docs-new', 'last', False))
        self.assertEqual(docs['delegation']['status'], 'result_ready')
        self.assertEqual(docs['delegation']['result']['outcome'], 'completed')
        self.assertEqual(docs['delegation']['result']['phase'], 'delivered')
        self.assertEqual(docs['delegation']['result']['integration']['status'], 'applied')
        self.assertEqual(docs['integration']['destination_kind'], 'task')
        self.assertIsNone(docs['waiting_context'])

        tests = nodes['tests']
        self.assertEqual(tests['waiting_context'], {'kind': 'user', 'label': 'Waiting for you'})
        self.assertTrue(tests['agent']['asks_user'])
        self.assertEqual(tests['delegation']['status'], 'materialized')
        self.assertIsNone(tests['delegation']['result'])

        flaky = nodes['flaky']
        self.assertEqual((flaky['parent_task_id'], flaky['root_task_id'], flaky['depth']), ('tests', 'release', 2))
        self.assertIsNone(flaky['agent'])
        self.assertEqual(flaky['assignment']['count'], 0)
        self.assertIsNone(flaky['delegation'])  # a plain child Task, not a delegation

        solo = nodes['solo']
        self.assertEqual(solo['integration']['destination_kind'], 'project')
        self.assertEqual(solo['integration']['status'], 'applied')
        self.assertEqual(solo['integration']['validation_status'], 'not_run')

        orphan = nodes['orphaned-work']
        self.assertTrue(orphan['responsibility_awaiting'])
        self.assertTrue(orphan['agent']['removed'])
        self.assertEqual(orphan['agent']['removal']['state'], 'removed')
        self.assertIsNone(orphan['assignment']['current'])

        self.assertEqual(nodes['limited']['waiting_context']['kind'], 'provider')
        self.assertEqual(nodes['limited']['waiting_context']['condition'], 'usage_limited')
        self.assertEqual(nodes['paused']['control_intent'], 'paused')
        self.assertIsNone(nodes['paused']['waiting_context'])
        self.assertEqual(nodes['blocked']['waiting_context']['kind'], 'coordination')
        self.assertEqual(nodes['blocked']['blockers'], ['resource_conflict'])
        self.assertEqual(nodes['stray']['ancestry'], 'missing_parent')
        self.assertEqual({nodes['loop-a']['ancestry'], nodes['loop-b']['ancestry']}, {'cycle'})

    def test_agent_hierarchy_fields_never_create_task_edges(self):
        scenarios.contract(self.path)
        with closing(sqlite3.connect(self.path)) as db, db:  # a misleading Agent parent and display name
            db.execute("UPDATE agents SET parent_id='lead' WHERE agent_id='solo-agent'")
            db.execute("UPDATE agents SET display_name='Lead' WHERE agent_id='limited-agent'")
        nodes = self.by_id(self.get(limit=500))
        self.assertIsNone(nodes['solo']['parent_task_id'])
        self.assertEqual(nodes['solo']['root_task_id'], 'solo')
        self.assertEqual(nodes['release']['child_count'], 2)

    def test_reassignment_history_is_bounded_but_counted(self):
        s = scenarios.Seeder(self.path)
        s.project()
        s.task('busy', status='in_progress')
        for i in range(30):
            s.agent(f'a{i}', 'stopped' if i < 29 else 'running')
            s.assign(f'as{i}', 'busy', f'a{i}', ended='reassigned' if i < 29 else None)
        for i in range(20):
            s.blocker('busy', f'dependency_{i}', 'dependency')
        s.task('long', title='x' * 5000)
        s.close()
        nodes = self.by_id(self.get())
        busy = nodes['busy']
        self.assertEqual(busy['assignment']['count'], 30)
        self.assertEqual(busy['assignment']['current']['agent_id'], 'a29')
        self.assertEqual(busy['assignment']['last_ended']['agent_id'], 'a28')
        self.assertEqual(len(busy['assignment']['previous_agent_ids']), task_projection.MAX_PREVIOUS_AGENTS)
        self.assertEqual(len(busy['blockers']), task_projection.MAX_BLOCKERS)
        self.assertEqual(busy['blockers_more'], 4)
        self.assertEqual(len(nodes['long']['title']), task_projection.TITLE_LIMIT)

    def test_removal_in_progress_is_not_reported_as_removed(self):
        scenarios.contract(self.path)
        with patch('app.agent_removal.agent_states', return_value={'tester': {
                'state': 'blocked', 'removal_id': 'rm', 'mode': 'single', 'target_agent_id': 'tester',
                'failure': 'process_unconfirmed', 'removed_at': None}}):
            tests = self.by_id(self.get(limit=500))['tests']
        self.assertFalse(tests['agent']['removed'])
        self.assertEqual(tests['agent']['removal']['state'], 'blocked')
        self.assertEqual(tests['waiting_context'], {'kind': 'recovery', 'label': 'Removal blocked'})

    # Exposure ------------------------------------------------------------------------------

    def test_no_secret_path_output_or_unbounded_field_is_exposed(self):
        scenarios.contract(self.path)
        for page in self.pages(limit=3) + self.pages(limit=2, root='release'):
            text = json.dumps(page)
            self.assertNotIn(scenarios.SECRET, text)
            self.assertNotIn(scenarios.SECRET.lower(), text)
            self.assertFalse(FORBIDDEN_KEYS & set(keys(page)), FORBIDDEN_KEYS & set(keys(page)))

    def test_reads_do_not_mutate(self):
        scenarios.contract(self.path)
        with self.store._connection() as db:
            before = list(db.iterdump())
        self.pages(limit=2)
        self.pages(limit=1, root='release')
        with self.store._connection() as db:
            self.assertEqual(before, list(db.iterdump()))

    # Pagination ----------------------------------------------------------------------------

    def test_every_page_size_returns_each_task_once_in_stable_order_with_placeable_parents(self):
        scenarios.contract(self.path)
        everything = [n['task_id'] for n in self.get(limit=500)['tasks']]
        self.assertEqual(len(everything), 12)
        for limit in (1, 2, 3, 5, 11, 12, 13):
            with self.subTest(limit=limit):
                pages = self.pages(limit=limit)
                seen = [n['task_id'] for page in pages for n in page['tasks']]
                self.assertEqual(seen, everything)
                for page in pages:
                    self.assertEqual(page['total'], 12)
                    placed = {n['task_id'] for n in page['tasks'] + page['ancestors']}
                    for node in page['tasks'] + page['ancestors']:
                        if node['ancestry'] == 'complete' and node['parent_task_id']:
                            self.assertIn(node['parent_task_id'], placed)
                    self.assertFalse({n['task_id'] for n in page['tasks']} & {n['task_id'] for n in page['ancestors']})

    def test_root_family_filter_pages_only_that_subtree(self):
        scenarios.contract(self.path)
        for limit in (1, 2, 4, 10):
            pages = self.pages(limit=limit, root='release')
            seen = [n['task_id'] for page in pages for n in page['tasks']]
            self.assertEqual(sorted(seen), ['docs', 'flaky', 'release', 'tests'])
            self.assertEqual(len(seen), len(set(seen)))
            self.assertTrue(all(page['total'] == 4 and page['root'] == 'release' for page in pages))
        flaky_page = next(p for p in self.pages(limit=1, root='release') if p['tasks'][0]['task_id'] == 'flaky')
        self.assertEqual({n['task_id'] for n in flaky_page['ancestors']}, {'tests', 'release'})
        subtree = self.pages(limit=500, root='tests')[0]
        self.assertEqual(sorted(n['task_id'] for n in subtree['tasks']), ['flaky', 'tests'])
        self.assertEqual([n['task_id'] for n in subtree['ancestors']], ['release'])
        cyclic = self.pages(limit=500, root='loop-a')[0]  # terminates; includes both members once
        self.assertEqual(sorted(n['task_id'] for n in cyclic['tasks']), ['loop-a', 'loop-b'])

    def test_identical_timestamps_use_the_task_id_tie_breaker(self):
        s = scenarios.Seeder(self.path)
        s.project()
        for i in range(40):
            s.task(f't{i:02d}', created=scenarios.ts(1))
        s.close()
        seen = [n['task_id'] for page in self.pages(limit=7) for n in page['tasks']]
        self.assertEqual(seen, sorted((f't{i:02d}' for i in range(40)), reverse=True))

    def test_new_tasks_between_pages_are_not_duplicated_or_skipped(self):
        scenarios.contract(self.path)
        first = self.get(limit=4)
        s = scenarios.Seeder(self.path)
        s.clock = 500
        s.task('newer', status='pending')
        s.close()
        seen = [n['task_id'] for n in first['tasks']]
        after = first['next_after']
        while after:
            page = self.get(limit=4, after=after)
            seen += [n['task_id'] for n in page['tasks']]
            after = page['next_after']
        self.assertEqual(len(seen), 12)
        self.assertNotIn('newer', seen)  # appears on the next refresh from the top
        self.assertEqual(self.get(limit=1)['tasks'][0]['task_id'], 'newer')

    def test_invalid_or_foreign_cursors_and_unknown_roots_are_rejected(self):
        scenarios.contract(self.path)
        cursor = self.get(limit=1)['next_after']
        for bad in ('not-base64!', 'e30', 'W10', 'x' * 1100):
            self.assertIn(self.client.get(f'/workspace/tasks?after={bad}').status_code, (400, 422), bad)
        self.assertEqual(self.client.get(f'/workspace/tasks?after={cursor}&root=release').status_code, 400)
        self.assertEqual(self.client.get('/workspace/tasks?root=missing').status_code, 404)
        self.assertEqual(self.client.get('/workspace/tasks?limit=0').status_code, 422)
        self.assertEqual(self.client.get('/workspace/tasks?limit=501').status_code, 422)

    def chain(self, length):
        s = scenarios.Seeder(self.path)
        s.project()
        for i in range(length):
            s.task(f'c{i:02d}', f'c{i - 1:02d}' if i else None)
        s.close()
        return f'c{length - 1:02d}'

    def test_a_cut_ancestry_walk_is_reported_and_never_guessed(self):
        leaf = self.chain(10)
        # The walk stops after 1 + 3 + 0 rows, so three ancestors fit the cap but c05 and above are unknown.
        with patch.object(task_projection, 'MAX_ANCESTORS', 3), patch.object(task_projection, 'MAX_DEPTH', 0):
            page = self.get(limit=1)
        self.assertEqual([n['task_id'] for n in page['tasks']], [leaf])  # still returned
        self.assertEqual((page['tasks'][0]['ancestry'], page['tasks'][0]['root_task_id']), ('truncated', None))
        self.assertTrue(page['ancestors_truncated'])
        for node in page['ancestors']:  # only real parents, nearest first
            self.assertEqual(node['parent_task_id'], f"c{int(node['task_id'][1:]) - 1:02d}")
        self.assertEqual([n['task_id'] for n in page['ancestors']], ['c08', 'c07', 'c06'])
        self.assertEqual({n['ancestry'] for n in page['ancestors']}, {'truncated'})

    def test_capped_ancestors_keep_the_nearest_and_say_so(self):
        leaf = self.chain(6)
        with patch.object(task_projection, 'MAX_ANCESTORS', 2):
            page = self.get(limit=1)
        self.assertEqual(page['tasks'][0]['task_id'], leaf)
        self.assertEqual((page['tasks'][0]['ancestry'], page['tasks'][0]['root_task_id'], page['tasks'][0]['depth']), ('complete', 'c00', 5))
        self.assertEqual([n['task_id'] for n in page['ancestors']], ['c04', 'c03'])
        self.assertTrue(page['ancestors_truncated'])

    def test_empty_database(self):
        self.assertEqual(self.get(), {'tasks': [], 'ancestors': [], 'ancestors_truncated': False,
                                      'next_after': None, 'total': 0, 'root': None})

    # Scale ---------------------------------------------------------------------------------

    def test_large_synthetic_workspace_stays_bounded_and_fast(self):
        s = scenarios.Seeder(self.path)
        s.project()
        n = 0
        def make(parent=None):
            nonlocal n
            n += 1
            task_id = f'task-{n:05d}'
            s.task(task_id, parent, created=scenarios.ts(n))
            return task_id
        # 60 families of 100 Tasks (wide), one 300-deep chain and 2,000 siblings under one lead.
        for _ in range(60):
            root = make()
            for _ in range(99):
                make(root)
        chain = make()
        deep = [chain]
        for _ in range(299):
            deep.append(make(deep[-1]))
        wide = make()
        for _ in range(2000):
            make(wide)
        for i in range(1, n + 1, 3):
            s.agent(f'agent-{i}', 'running' if i % 2 else 'finished')
            s.assign(f'as-{i}', f'task-{i:05d}', f'agent-{i}', ended=None if i % 2 else 'completed')
        s.close()
        total = n
        timings, seen, payload = [], 0, 0
        after = None
        while True:
            started = time.perf_counter()
            response = self.client.get('/workspace/tasks?limit=500' + (f'&after={after}' if after else ''))
            timings.append(time.perf_counter() - started)
            page = response.json()
            payload = max(payload, len(response.text.encode()))
            seen += len(page['tasks'])
            self.assertLessEqual(len(page['ancestors']), task_projection.MAX_ANCESTORS)
            after = page['next_after']
            if not after:
                break
        self.assertEqual(seen, total)
        nodes = self.by_id(self.get(limit=500, root=wide))
        self.assertEqual(nodes[wide]['child_count'], 2000)
        deepest = self.by_id(self.get(limit=1, root=deep[-1]))
        self.assertEqual(deepest[deep[-1]]['ancestry'], 'truncated')  # deeper than MAX_DEPTH
        self.assertEqual(self.by_id(self.get(limit=1, root=deep[200]))[deep[200]]['depth'], 200)
        report = {'tasks': total, 'pages': len(timings), 'worst_page_s': round(max(timings), 3),
                  'all_pages_s': round(sum(timings), 3), 'largest_page_bytes': payload}
        print('\nworkspace/tasks scale:', report)
        self.assertLess(max(timings), 5.0, report)  # generous: CI machines vary; the report gives the number

    # Frontend contract ---------------------------------------------------------------------

    def test_frontend_contract_fixture_matches_the_real_endpoint(self):
        scenarios.contract(self.path)
        recorded = {'all': self.get(limit=500), 'paged': self.pages(limit=3), 'root': self.pages(limit=2, root='release')}
        if os.environ.get('ATLAS_WRITE_FIXTURES') == '1':
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(recorded, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
        self.assertEqual(json.loads(FIXTURE.read_text(encoding='utf-8')), recorded,
                         'Regenerate with ATLAS_WRITE_FIXTURES=1 after an intentional projection change')


if __name__ == '__main__':
    unittest.main()
