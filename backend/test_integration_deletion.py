"""CB-11: a file that merely becomes ignored in a source workspace is not deleted from its destination."""
import unittest

from app import agent_manager as manager
import test_integrations


class IntegrationDeletionTests(unittest.TestCase):
    setUp = test_integrations.IntegrationTests.setUp
    tearDown = test_integrations.IntegrationTests.tearDown
    task = test_integrations.IntegrationTests.task
    workspace = test_integrations.IntegrationTests.workspace
    git_state = test_integrations.IntegrationTests.git_state
    edit = test_integrations.IntegrationTests.edit
    root = test_integrations.IntegrationTests.root
    child = test_integrations.IntegrationTests.child
    integrate = test_integrations.IntegrationTests.integrate
    seed = test_integrations.IntegrationTests.seed

    def test_newly_ignored_file_is_kept_in_canonical(self):
        self.seed()
        task, source = self.root()
        canonical = manager._project_root
        self.edit(source, '.gitignore', 'a.txt\n')  # The only change: stop tracking a.txt.
        result = self.integrate(task)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual((canonical / 'a.txt').read_text(), 'original A\n')
        self.assertEqual((canonical / '.gitignore').read_text(), 'a.txt\n')
        self.assertEqual((canonical / 'b.txt').read_text(), 'original B\n')
        self.assertEqual((source / 'a.txt').read_text(), 'original A\n')

    def test_newly_ignored_file_with_other_work_is_kept(self):
        self.seed()
        task, source = self.root()
        canonical = manager._project_root
        self.edit(source, '.gitignore', 'a.txt\n')
        self.edit(source, 'a.txt', 'local-only edit of an ignored file')
        self.edit(source, 'b.txt', 'real work')
        self.assertEqual(self.integrate(task)['status'], 'applied')
        # Ignored content is neither deleted nor propagated; tracked work is.
        self.assertEqual((canonical / 'a.txt').read_text(), 'original A\n')
        self.assertEqual((canonical / 'b.txt').read_text(), 'real work')

    def test_intentional_deletion_still_deletes(self):
        self.seed()
        task, source = self.root()
        canonical = manager._project_root
        (source / 'a.txt').unlink()
        self.assertEqual(self.integrate(task)['status'], 'applied')
        self.assertFalse((canonical / 'a.txt').exists())
        self.assertEqual((canonical / 'b.txt').read_text(), 'original B\n')

    def test_ignored_and_deleted_is_an_intentional_deletion(self):
        self.seed()
        task, source = self.root()
        canonical = manager._project_root
        self.edit(source, '.gitignore', 'a.txt\n')
        (source / 'a.txt').unlink()
        self.assertEqual(self.integrate(task)['status'], 'applied')
        self.assertFalse((canonical / 'a.txt').exists())
        self.assertEqual((canonical / '.gitignore').read_text(), 'a.txt\n')

    def test_child_to_parent_integration_keeps_newly_ignored_file(self):
        self.seed()
        parent, p = self.root()
        child, c = self.child(parent)
        self.edit(c, '.gitignore', 'b.txt\n')
        self.edit(c, 'a.txt', 'child work')
        self.assertEqual(self.integrate(child)['status'], 'applied')
        self.assertEqual((p / 'b.txt').read_text(), 'original B\n')
        self.assertEqual((p / 'a.txt').read_text(), 'child work')
        self.assertEqual((p / '.gitignore').read_text(), 'b.txt\n')

    def test_child_intentional_deletion_reaches_parent(self):
        self.seed()
        parent, p = self.root()
        child, c = self.child(parent)
        (c / 'b.txt').unlink()
        self.assertEqual(self.integrate(child)['status'], 'applied')
        self.assertFalse((p / 'b.txt').exists())


if __name__ == '__main__':
    unittest.main()
