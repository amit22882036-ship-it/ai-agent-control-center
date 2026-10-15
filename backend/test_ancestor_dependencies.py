"""CB-12: a Task cannot depend on one of its own ancestors (that would deadlock the subtree)."""
import unittest

from app import agent_manager as manager
import test_dependencies


class AncestorDependencyTests(unittest.TestCase):
    setUp = test_dependencies.DependencyTests.setUp
    tearDown = test_dependencies.DependencyTests.tearDown
    create = test_dependencies.DependencyTests.create
    replacement = test_dependencies.DependencyTests.replacement
    task = test_dependencies.DependencyTests.task
    state = test_dependencies.DependencyTests.state
    edge = test_dependencies.DependencyTests.edge
    request = test_dependencies.DependencyTests.request

    def edges(self, task):
        return manager._store.list_dependencies(task['task_id'])

    def test_direct_and_indirect_ancestors_are_rejected(self):
        root = self.task()
        child = self.task(root)
        grandchild = self.task(child)
        for dependent, ancestor in ((child, root), (grandchild, child), (grandchild, root)):
            with self.subTest(dependent=dependent['task_id'], ancestor=ancestor['task_id']):
                with self.assertRaisesRegex(ValueError, 'ancestor'):
                    self.edge(dependent, ancestor)
                self.assertEqual(self.edges(dependent), [])
                self.assertNotEqual(self.state(dependent)['status'], 'blocked')

    def test_api_rejects_ancestor_dependency_without_effect(self):
        root = self.task()
        child = self.task(root)
        detail = self.request(f"/tasks/{child['task_id']}/dependencies", 'POST',
                              {'depends_on_task_id': root['task_id']}, expected=409)
        self.assertIn('ancestor', detail['detail'])
        self.assertEqual(self.edges(child), [])

    def test_valid_relationships_remain_allowed(self):
        root, other_root, third_root = self.task(), self.task(), self.task()
        a, b = self.task(root), self.task(root)
        uncle = self.task(root)
        nephew = self.task(a)
        self.edge(a, b)                # sibling
        self.edge(nephew, uncle)       # parent's sibling is not an ancestor
        self.edge(root, a)             # Parent waiting on its own Child (existing behaviour)
        self.edge(other_root, nephew)  # cross-root, same Project
        self.edge(nephew, third_root)  # descendant depending on another root
        self.assertEqual({e['depends_on_task_id'] for e in self.edges(nephew)}, {uncle['task_id'], third_root['task_id']})

    def test_existing_cycle_and_project_rules_still_apply(self):
        a, b = self.task(), self.task()
        self.edge(a, b)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.edge(b, a)
        with self.assertRaisesRegex(ValueError, 'cycle'):
            self.edge(a, a)

    def test_removing_an_edge_is_never_blocked_by_ancestry(self):
        root = self.task()
        child = self.task(root)
        # Simulate a legacy edge created before this validation existed.
        with manager._store._connection() as db:
            db.execute('INSERT INTO task_dependencies(task_id,depends_on_task_id) VALUES (?,?)',
                       (child['task_id'], root['task_id']))
        self.edge(child, root, remove=True)
        self.assertEqual(self.edges(child), [])


if __name__ == '__main__':
    unittest.main()
