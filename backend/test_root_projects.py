"""Explicit root Project selection; real temporary Git workspaces, fake providers."""
from pathlib import Path
from unittest.mock import patch
import unittest
from app import agent_manager as manager
from app import workspace_git
from workspace_test_support import make_repository
import test_task_lifecycle


class RootProjectTests(unittest.TestCase):
    setUp = test_task_lifecycle.TaskLifecycleTests.setUp
    tearDown = test_task_lifecycle.TaskLifecycleTests.tearDown
    request = test_task_lifecycle.TaskLifecycleTests.request
    replacement = test_task_lifecycle.TaskLifecycleTests.replacement
    task_for = test_task_lifecycle.TaskLifecycleTests.task_for

    def project(self, name='Selected repository with spaces'):
        root = make_repository(Path(self.temp.name) / name)
        (root / 'selected.txt').write_text(name, encoding='utf-8')
        return self.request('/projects', {'name': name, 'root_path': str(root)}), root

    def start(self, body, path='/agents/start', expected=200):
        process = self.replacement()
        with patch.object(manager, '_spawn_process', return_value=process) as spawn, patch.object(manager, 'Thread') as thread, patch.object(manager, '_codex_command', return_value='synthetic'):
            thread.return_value.is_alive.return_value = False
            result = self.request(path, {'task':'Scoped work'} | body, expected)
        return result, spawn

    def test_selected_root_task_and_execution_workspace_are_same_project(self):
        project, root = self.project()
        before = workspace_git.git(manager._project_root, 'status', '--porcelain')
        result, spawn = self.start({'project_id': project['project_id'], 'agent_type':'codex', 'autonomous':True})
        task = self.task_for(result['agent_id'])
        self.assertEqual(task['project_id'], project['project_id'])
        workspace = manager._store.get_task_workspace(task['task_id'])
        cwd = Path(spawn.call_args.kwargs['cwd'])
        self.assertEqual(cwd, Path(workspace['workspace_path']))
        self.assertNotEqual(cwd, root)
        self.assertEqual((cwd / 'selected.txt').read_text(), project['name'])
        self.assertEqual(workspace_git.repository(cwd), workspace_git.repository(root))
        self.assertNotEqual(workspace_git.repository(cwd), workspace_git.repository(manager._project_root))
        self.assertEqual(workspace_git.git(manager._project_root, 'status', '--porcelain'),before)

    def test_children_and_grandchildren_inherit_selected_project(self):
        project, _ = self.project()
        root, _ = self.start({'project_id':project['project_id']})
        child, _ = self.start({}, f"/agents/{root['agent_id']}/children/start")
        grandchild, _ = self.start({}, f"/agents/{child['agent_id']}/children/start")
        tasks = [self.task_for(item['agent_id']) for item in [root,child,grandchild]]
        self.assertEqual({t['project_id'] for t in tasks},{project['project_id']})
        self.assertEqual(tasks[1]['parent_task_id'],tasks[0]['task_id'])
        self.assertEqual(tasks[2]['parent_task_id'],tasks[1]['task_id'])
        for task in tasks:
            self.assertTrue((Path(manager._store.get_task_workspace(task['task_id'])['workspace_path']) / 'selected.txt').exists())

    def test_unrelated_projects_have_distinct_sources_and_workspaces(self):
        paths = []
        for name in ['First repository','Second repository']:
            project, _ = self.project(name)
            result, _ = self.start({'project_id':project['project_id']})
            task = self.task_for(result['agent_id'])
            cwd = Path(manager._store.get_task_workspace(task['task_id'])['workspace_path'])
            self.assertEqual((cwd/'selected.txt').read_text(),name)
            paths.append(cwd)
        self.assertNotEqual(*paths)

    def test_invalid_explicit_selection_never_falls_back(self):
        for value, status in [('missing',404),('',422),(None,422)]:
            _, spawn = self.start({'project_id':value},expected=status)
            spawn.assert_not_called()
        self.assertEqual(manager._store.list_tasks(),[])
        self.assertEqual(manager.agents,{})

    def test_missing_or_non_git_registered_root_fails_without_agent(self):
        root = Path(self.temp.name)/'plain'; root.mkdir()
        project = manager._store.create_project('Legacy directory',root)
        _, spawn = self.start({'project_id':project['project_id']},expected=422)
        spawn.assert_not_called()
        root.rename(root.with_name('moved'))
        _, spawn = self.start({'project_id':project['project_id']},expected=422)
        spawn.assert_not_called()
        self.assertEqual(manager._store.list_tasks(),[])

    def test_registration_validates_git_and_prevents_duplicates(self):
        root = Path(self.temp.name)/'not git';root.mkdir()
        self.request('/projects',{'name':'Invalid','root_path':str(root)},409)
        project, root = self.project()
        self.request('/projects',{'name':'Duplicate','root_path':str(root / '.')},409)
        self.assertEqual(self.request('/projects')['projects'],[project])

    def test_legacy_root_remains_supported_but_child_override_is_rejected(self):
        root, _ = self.start({})
        task = self.task_for(root['agent_id'])
        self.assertEqual(manager._store.get_project(task['project_id'])['root_path'],str(manager._project_root))
        project, _ = self.project()
        _, spawn = self.start({'project_id':project['project_id']},f"/agents/{root['agent_id']}/children/start",422)
        spawn.assert_not_called()

    def test_provisioning_failure_leaves_pending_task_without_assignment(self):
        project, _ = self.project()
        with patch.object(manager,'_execution_workspace',side_effect=RuntimeError('Workspace provisioning failed')):
            _, spawn = self.start({'project_id':project['project_id']},expected=503)
        spawn.assert_not_called()
        task, = manager._store.list_tasks()
        self.assertEqual(task['project_id'],project['project_id'])
        self.assertEqual(task['status'],'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))
        self.assertFalse(manager.agents)

    def test_spawn_failure_never_reports_running(self):
        project, _ = self.project()
        with patch.object(manager,'_spawn_process',side_effect=OSError('Provider unavailable')):
            self.request('/agents/start',{'task':'Work','project_id':project['project_id']},503)
        self.assertFalse(manager.agents)
        task, = manager._store.list_tasks()
        self.assertEqual(task['status'],'pending')
        self.assertIsNone(manager._store.get_active_assignment_for_task(task['task_id']))
