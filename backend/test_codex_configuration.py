"""Configuration regression tests: no provider process is executed."""
import os
import subprocess
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager
from app.codex_events import StructuredCommand
from execution_test_support import use_attested_test_doubles


class CodexConfigurationTests(unittest.TestCase):
    def setUp(self):
        use_attested_test_doubles(self)  # explicit test-only evidence for test doubles
        environment = dict(os.environ, APPDATA=r'C:\npm fixture', SystemRoot=r'C:\Windows',
                           CODEX_HOME=r'C:\codex home fixture that does not exist')
        for key in ('CONTROL_CENTER_CODEX_MODEL', 'CONTROL_CENTER_CODEX_REASONING_EFFORT'):
            environment.pop(key, None)
        self.enterContext(patch.dict(os.environ, environment, clear=True))
        self.enterContext(patch.object(manager.os, 'name', 'nt'))
        self.enterContext(patch.object(manager.Path, 'is_file', return_value=True))
        # Command construction only: readiness probing is covered by test_providers.
        self.enterContext(patch.object(manager, '_require_launchable'))

    def test_unconfigured_command_is_unchanged(self):
        self.assertEqual(manager._codex_command(),
            '"C:\\Windows\\System32\\cmd.exe" /d /s /v:off /c '
            '""C:\\npm fixture\\npm\\codex.cmd" exec -c model_provider=openai --sandbox read-only '
            '--color never --skip-git-repo-check -"')
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='  ',
                        CONTROL_CENTER_CODEX_REASONING_EFFORT=''):
            self.assertNotIn('--model', manager._codex_command())
            self.assertNotIn('model_reasoning_effort', manager._codex_command())

    def test_initial_structured_and_resumed_fake_processes_share_overrides(self):
        session = str(uuid4())
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='gpt-6.1-sol',
                        CONTROL_CENTER_CODEX_REASONING_EFFORT='low'):
            for structured in (False, True):
                for session_id in (None, session):
                    for sandbox in ('read-only', 'workspace-write'):
                        with self.subTest(structured=structured, session=session_id, sandbox=sandbox):
                            command = manager._codex_command(sandbox, session_id, structured=structured)
                            with patch.object(manager.subprocess, 'Popen') as spawn:
                                process = manager._spawn_process(command, 'codex', cwd='isolated-workspace')
                            self.assertIs(process, spawn.return_value)
                            self.assertEqual(spawn.call_args.args, (command,))
                            self.assertFalse(spawn.call_args.kwargs['shell'])
                            self.assertEqual(spawn.call_args.kwargs['stdin'], subprocess.PIPE)
                            self.assertEqual(spawn.call_args.kwargs['cwd'], 'isolated-workspace')
                            self.assertEqual(isinstance(command, StructuredCommand), structured)
                            self.assertIn('exec -c model_provider=openai --model gpt-6.1-sol -c model_reasoning_effort=low ', command)
                            self.assertIn(f'--sandbox {sandbox} --color never --skip-git-repo-check ', command)
                            self.assertEqual('--json' in command, structured)
                            self.assertTrue(command.endswith(f'resume {session} -"' if session_id else ' -"'))

    def test_overrides_are_independent(self):
        with patch.dict(os.environ, CONTROL_CENTER_CODEX_MODEL='model_1.preview'):
            self.assertIn('--model model_1.preview ', manager._codex_command())
            self.assertNotIn('model_reasoning_effort', manager._codex_command())
        for effort in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
            with patch.dict(os.environ, CONTROL_CENTER_CODEX_REASONING_EFFORT=effort):
                self.assertIn(f'-c model_reasoning_effort={effort} ', manager._codex_command())
                self.assertNotIn('--model', manager._codex_command())

    def test_unsafe_configuration_is_rejected_before_spawn(self):
        for key in ('CONTROL_CENTER_CODEX_MODEL', 'CONTROL_CENTER_CODEX_REASONING_EFFORT'):
            for value in ('x & echo injected', 'x|echo', '%PATH%', '!PATH!', 'x^y',
                          'x"y', 'x\ny', 'x y', '--other-option', 'x>file', 'x(y)'):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}), \
                        patch.object(manager.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(ValueError, key):
                        command = manager._codex_command()
                        manager._spawn_process(command, 'codex', cwd='fixture')
                    spawn.assert_not_called()
