"""Phase 1.6: environment-configured delegation limits (defaults unchanged; strict 1/1/1 supported)."""
import os
import unittest
from unittest.mock import patch

from app import delegation_materialization as materialization, delegation_safety as safety
import test_delegation_materialization

CHILDREN, CONCURRENT, DEPTH = ('CONTROL_CENTER_DELEGATION_CHILDREN_PER_PARENT',
                               'CONTROL_CENTER_DELEGATION_CONCURRENT_CHILDREN',
                               'CONTROL_CENTER_DELEGATION_DEPTH')


def environment(**values):
    cleared = {name: '' for name in (CHILDREN, CONCURRENT, DEPTH)}
    patcher = patch.dict(os.environ, cleared)
    patcher.start()
    for name in (CHILDREN, CONCURRENT, DEPTH):
        os.environ.pop(name)
    os.environ.update(values)
    return patcher


class ConfiguredLimitTests(unittest.TestCase):
    def test_defaults_are_unchanged(self):
        self.addCleanup(environment().stop)
        limits = materialization.configured_limits()
        self.assertEqual((limits.children_per_parent, limits.concurrent_children, limits.depth), (16, 4, 4))
        self.assertEqual(limits.preparation_attempts, materialization.DEFAULT_LIMITS.preparation_attempts)
        self.assertEqual(safety.maximum_concurrent_children(), 4)

    def test_strict_one_one_one(self):
        self.addCleanup(environment(**{CHILDREN: '1', CONCURRENT: '1', DEPTH: '1'}).stop)
        limits = materialization.configured_limits()
        self.assertEqual((limits.children_per_parent, limits.concurrent_children, limits.depth), (1, 1, 1))

    def test_each_limit_is_independent(self):
        self.addCleanup(environment(**{CHILDREN: '3', CONCURRENT: '2'}).stop)
        limits = materialization.configured_limits()
        self.assertEqual((limits.children_per_parent, limits.concurrent_children, limits.depth), (3, 2, 4))

    def test_invalid_values_fail_closed_and_name_the_variable(self):
        for name in (CHILDREN, CONCURRENT):
            for value in ('0', '-1', 'abc', '', ' ', '1.5', 'true'):
                with self.subTest(name=name, value=value):
                    patcher = environment(**{name: value})
                    try:
                        with self.assertRaisesRegex(ValueError, name + ' must be a positive integer'):
                            materialization.configured_limits()
                    finally:
                        patcher.stop()


class LimitEnforcementTests(unittest.TestCase):
    """The configured values reach every existing enforcement point."""
    setUp = test_delegation_materialization.MaterializationTests.setUp
    agent = test_delegation_materialization.MaterializationTests.agent
    task = test_delegation_materialization.MaterializationTests.task
    worker = test_delegation_materialization.MaterializationTests.worker
    origin_context = test_delegation_materialization.MaterializationTests.origin_context
    accept = test_delegation_materialization.MaterializationTests.accept
    accepted = test_delegation_materialization.MaterializationTests.accepted
    attach = test_delegation_materialization.MaterializationTests.attach
    ready = test_delegation_materialization.MaterializationTests.ready
    admit = test_delegation_materialization.MaterializationTests.admit
    snapshot = test_delegation_materialization.MaterializationTests.snapshot

    def test_children_per_parent_from_environment(self):
        self.addCleanup(environment(**{CHILDREN: '1'}).stop)
        a, b = self.accepted('a', 'b')
        self.attach(a, None)  # None: the configured limits, as in production
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.attach(b, None)
        self.assertEqual(self.store.get_delegation(b)['status'], 'requested')

    def test_concurrent_children_from_environment(self):
        self.addCleanup(environment(**{CONCURRENT: '1'}).stop)
        a, b = self.accepted('a', 'b')
        for identifier, key in ((a, 'api'), (b, 'docs')):
            task = self.ready(identifier)
            self.store.create_work_intent(task, namespace='component', key=key, mode='single_owner')
        self.admit(a, None)
        with self.assertRaisesRegex(ValueError, 'capacity'):
            self.admit(b, None)

    def test_worker_boundary_uses_configured_concurrency(self):
        a, b = self.accepted('a', 'b')
        for identifier, key in ((a, 'api'), (b, 'docs')):
            task = self.ready(identifier)
            self.store.create_work_intent(task, namespace='component', key=key, mode='single_owner')
        self.admit(a)
        second = self.store.get_delegation(b)['child_task_id']
        with self.store._connection() as db:
            self.addCleanup(environment().stop)
            safety.require_capacity(db, second)  # Default 4: one occupied Child still fits.
            patcher = environment(**{CONCURRENT: '1'})
            try:
                with self.assertRaisesRegex(ValueError, 'capacity'):
                    safety.require_capacity(db, second)
                with self.assertRaisesRegex(ValueError, 'capacity'):
                    safety.worker_boundary(db, second)  # Used, e.g., before a nested Parent resumes.
            finally:
                patcher.stop()


if __name__ == '__main__':
    unittest.main()
