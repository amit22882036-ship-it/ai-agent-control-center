"""Read-only family scoping, independent of the workspace's loaded pages."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from app.persistence import AgentStore
from app import presentation, main
from test_product_workspace import TestClient
from execution_test_support import use_attested_test_doubles


class HierarchyProjectionTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = AgentStore(Path(folder.name) / 'fixture.sqlite3')

    def seed(self, rows):
        with self.store._connection() as db:
            db.executemany('''INSERT INTO agents(agent_id,parent_id,task,agent_type,status,
                similar_decisions_enabled,similar_examples,always_decide_enabled,always_decide_configured)
                VALUES (?,?,'Synthetic work','mock','finished',0,'[]',0,0)''', rows)

    def test_root_child_grandchild_have_one_family_and_reads_do_not_mutate(self):
        self.seed([('root', None), ('child', 'root'), ('grandchild', 'child'), ('sibling', 'root'), ('other', None)])
        with self.store._connection() as db:
            before = list(db.iterdump())
        for selected in ('root', 'child', 'grandchild'):
            snapshot = presentation.hierarchy(self.store, selected)
            self.assertEqual({n['agent_id'] for n in snapshot['agents']}, {'root', 'child', 'grandchild', 'sibling'})
            self.assertFalse(snapshot['truncated'])
            self.assertFalse(snapshot['incomplete_ancestry'])
        with self.store._connection() as db:
            self.assertEqual(before, list(db.iterdump()))
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)

    def test_missing_agent_is_404_and_orphan_is_explicit(self):
        self.seed([('orphan', 'missing'), ('single', None)])
        with patch.object(main.manager, '_task_store', return_value=self.store):
            client = TestClient(main.app)
            self.assertEqual(client.get('/agents/missing/hierarchy').status_code, 404)
            self.assertTrue(client.get('/agents/orphan/hierarchy').json()['incomplete_ancestry'])
            self.assertEqual(len(client.get('/agents/single/hierarchy').json()['agents']), 1)

    def test_cycles_terminate_without_duplicate_or_unrelated_nodes(self):
        self.seed([('a', 'b'), ('b', 'a'), ('c', 'b'), ('other', None)])
        snapshot = presentation.hierarchy(self.store, 'c')
        self.assertEqual([n['agent_id'] for n in snapshot['agents']], ['a', 'b', 'c'])

    def test_family_is_bounded_and_selected_node_is_always_included(self):
        self.seed([('root', None)] + [(f'child-{i}', 'root') for i in range(510)])
        snapshot = presentation.hierarchy(self.store, 'child-509')
        self.assertEqual(len(snapshot['agents']), 500)
        self.assertTrue(snapshot['truncated'])
        self.assertIn('child-509', {n['agent_id'] for n in snapshot['agents']})

    def test_family_load_is_independent_of_workspace_page(self):
        self.seed([('root', None), ('child', 'root')] + [(f'unrelated-{i}', None) for i in range(505)])
        self.assertNotIn('child', {n['agent_id'] for n in presentation.workspace(self.store)['agents']})
        self.assertEqual({n['agent_id'] for n in presentation.hierarchy(self.store, 'child')['agents']}, {'root', 'child'})


if __name__ == '__main__':
    unittest.main()
