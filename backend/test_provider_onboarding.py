"""Stage 2G: global and Task provider permissions, onboarding operations, update checks and
update notices.

Only fake npm and fake provider CLIs run (provider_test_support): nothing is installed or
updated on this machine, no account is signed in or out, no release metadata is downloaded
and no model is called.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from app import agent_manager as manager, executions, process_supervision as ps
from app import provider_onboarding as onboarding, provider_policy as policy, providers
from app.persistence import AgentStore
import provider_test_support as fake
from test_api_security import call, headers
import test_providers as base


class Store:
    def make_store(self):
        self.temp = tempfile.TemporaryDirectory(prefix='onboarding ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AgentStore(self.root / 'data' / 'onboarding.sqlite3')
        return self.store

    def write(self, action, *args, **kwargs):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            return action(db, *args, **kwargs)

    def read(self, action, *args, **kwargs):
        with self.store._connection() as db:
            return action(db, *args, **kwargs)


class GlobalPermissionTests(Store, unittest.TestCase):
    def setUp(self):
        self.make_store()

    def test_migration_keeps_what_each_provider_could_already_do(self):
        enabled = [p for p in providers.REAL if self.read(providers.settings, p)['enabled']]
        current = self.read(policy.global_policy)
        self.assertEqual(current['allowed'], enabled)  # Nothing widened, nothing revoked.
        self.assertEqual(current['updated_by'], 'migration')
        # A v36 database where Claude Code was enabled keeps it allowed after the upgrade.
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('provider_global_policy', 'task_provider_restrictions', 'provider_policy_revisions',
                          'provider_operations', 'provider_update_checks', 'provider_notification_state'):
                db.execute(f'DROP TABLE {table}')
            providers.update_settings(db, 'claude', enabled=True, billing_confirmed=True)
            db.execute('PRAGMA user_version = 36')
        upgraded = AgentStore(self.store.path)
        with upgraded._connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 37)
            self.assertEqual(sorted(policy.global_policy(db)['allowed']), sorted(set(enabled) | {'claude'}))
            revision = policy.global_policy(db)['revision']
        with AgentStore(self.store.path)._connection() as db:  # Reopening changes nothing.
            self.assertEqual(policy.global_policy(db)['revision'], revision)

    def test_permission_is_separate_from_enablement_and_an_empty_set_allows_none(self):
        self.write(providers.update_settings, 'gemini', enabled=True, billing_confirmed=True)
        self.assertIn('not in your allowed providers', self.read(policy.denial, 'gemini'))  # Enabled, not allowed.
        self.write(policy.set_global, allowed=[])
        for provider in providers.REAL:
            with self.assertRaises(policy.ProviderNotPermitted) as caught:
                self.read(policy.require_allowed, provider)
            self.assertEqual(caught.exception.code, 'not_permitted')
            self.assertIn('No other provider is substituted', str(caught.exception))
        self.assertIsNone(self.read(policy.denial, 'mock'))  # Mock stays available for explicit use.
        self.assertTrue(self.read(providers.settings, 'gemini')['enabled'])  # Enablement untouched.
        self.write(policy.set_global, allowed=['gemini', 'codex', 'gemini'])
        self.assertEqual(self.read(policy.global_policy)['allowed'], ['gemini', 'codex'])
        with self.assertRaises(ValueError):
            self.write(policy.set_global, allowed=['mock'])
        with self.assertRaises(ValueError):
            self.write(policy.set_global, allowed=['codex', 'unknown'])

    def test_edits_require_the_current_revision_and_revisions_are_never_reused(self):
        first = self.read(policy.global_policy)['revision']
        second = self.write(policy.set_global, allowed=['codex'], expected_revision=first)['revision']
        self.assertGreater(second, first)
        with self.assertRaisesRegex(ValueError, 'changed since they were opened'):
            self.write(policy.set_global, allowed=['codex', 'claude'], expected_revision=first)
        self.assertEqual(self.read(policy.global_policy)['allowed'], ['codex'])
        self.write(lambda db: db.execute('DELETE FROM provider_policy_revisions'))
        third = self.write(policy.set_global, allowed=['codex'], expected_revision=second)['revision']
        self.assertGreater(third, second)  # AUTOINCREMENT: deleted revisions are not handed out again.

    def test_a_default_is_saved_only_when_valid_and_permitted_and_is_never_substituted(self):
        with self.assertRaisesRegex(ValueError, 'not in your allowed providers'):
            self.write(policy.set_global, allowed=['codex'], default_provider='claude')
        with self.assertRaisesRegex(ValueError, 'needs a default provider'):
            self.write(policy.set_global, allowed=['codex'], default_model='gpt-5')
        with self.assertRaisesRegex(ValueError, 'Mock has no model'):
            self.write(policy.set_global, allowed=['codex'], default_provider='mock', default_effort='high')
        saved = self.write(policy.set_global, allowed=['codex'], default_provider='codex')
        self.assertEqual(saved['default_provider'], 'codex')
        self.write(policy.set_global, allowed=[], default_provider='mock')
        # A default that became forbidden by an outside change is reported, never replaced.
        self.write(lambda db: db.execute("""UPDATE provider_global_policy SET default_provider='claude',
            allowed_providers='["codex"]'"""))
        current = self.read(policy.global_policy)
        problem = self.read(policy.default_problem, current, 'claude', None, None)
        self.assertIn('not in your allowed providers', problem)
        self.assertEqual(current['default_provider'], 'claude')

    def test_new_root_defaults_are_validated_against_the_model_catalog(self):
        with patch('app.model_catalog.admission', side_effect=providers.ProviderRefused(
                'codex', 'model_unavailable', 'Codex: gpt-unknown is not offered for this account.')):
            with self.assertRaisesRegex(ValueError, 'gpt-unknown is not offered'):
                self.write(policy.set_global, allowed=['codex'], default_provider='codex', default_model='gpt-unknown')
        with patch('app.model_catalog.admission', return_value=None):
            saved = self.write(policy.set_global, allowed=['codex'], default_provider='codex', default_model='gpt-5',
                               default_effort='high')
        self.assertEqual((saved['default_model'], saved['default_effort']), ('gpt-5', 'high'))


class TaskRestrictionTests(Store, unittest.TestCase):
    def setUp(self):
        self.make_store()
        self.write(policy.set_global, allowed=['codex', 'claude', 'gemini'])
        self.root_task = self.store.create_task('Root work', title='Root')['task_id']
        self.child = self.store.create_task('Child work', title='Child', parent_task_id=self.root_task)['task_id']
        self.leaf = self.store.create_task('Leaf work', title='Leaf', parent_task_id=self.child)['task_id']

    def effective(self, task_id):
        return self.read(policy.effective, task_id)[0]

    def test_restrictions_intersect_down_the_hierarchy_and_never_widen(self):
        self.write(policy.set_restriction, self.root_task, ['codex', 'claude'], expected_revision=None)
        self.assertEqual(self.effective(self.leaf), ['codex', 'claude'])
        with self.assertRaisesRegex(ValueError, 'only narrow inherited permissions.*Gemini CLI'):
            self.write(policy.set_restriction, self.child, ['claude', 'gemini'], expected_revision=None)
        self.write(policy.set_restriction, self.child, ['claude'], expected_revision=None)
        self.assertEqual(self.effective(self.leaf), ['claude'])
        self.assertEqual(self.effective(self.root_task), ['codex', 'claude'])  # An ancestor is unaffected.
        reason = self.read(policy.denial, 'codex', self.leaf)
        self.assertIn('"Child" excludes it', reason)
        self.assertIn('cannot widen', reason)
        # A narrower global permission applies below every restriction.
        self.write(policy.set_global, allowed=['codex'])
        self.assertEqual(self.effective(self.root_task), ['codex'])
        self.assertEqual(self.effective(self.leaf), [])
        self.assertIn('not in your allowed providers', self.read(policy.denial, 'claude', self.leaf))
        with self.assertRaisesRegex(ValueError, 'only narrow'):
            self.write(policy.set_restriction, self.leaf, ['claude'], expected_revision=None)

    def test_an_empty_restriction_allows_only_mock(self):
        self.write(policy.set_restriction, self.child, [], expected_revision=None)
        self.assertEqual(self.effective(self.leaf), [])
        self.assertIsNone(self.read(policy.denial, 'mock', self.leaf))
        self.assertIsNotNone(self.read(policy.denial, 'codex', self.leaf))

    def test_restriction_edits_require_their_revision(self):
        own = self.write(policy.set_restriction, self.child, ['codex'], expected_revision=None)
        with self.assertRaisesRegex(ValueError, 'changed since'):
            self.write(policy.set_restriction, self.child, ['claude'], expected_revision=None)
        own = self.write(policy.set_restriction, self.child, ['claude'], expected_revision=own['revision'])
        with self.assertRaisesRegex(ValueError, 'changed since'):
            self.write(policy.clear_restriction, self.child, expected_revision=own['revision'] - 1)
        self.write(policy.clear_restriction, self.child, expected_revision=own['revision'])
        self.assertIsNone(self.read(policy.own_restriction, self.child))
        self.assertEqual(self.effective(self.leaf), ['codex', 'claude', 'gemini'])
        with self.assertRaisesRegex(ValueError, 'no provider restriction'):
            self.write(policy.clear_restriction, self.child, expected_revision=None)

    def chain(self, top, length):
        """`length` nested Tasks below `top`; returns them nearest-to-top first."""
        tasks, parent = [], top
        for index in range(length):
            parent = self.store.create_task(f'Level {index}', title=f'Level {index}', parent_task_id=parent)['task_id']
            tasks.append(parent)
        return tasks

    def test_deep_hierarchies_keep_every_ancestor_restriction(self):
        # Review finding: a walk that stopped at 64 ancestors dropped the root's restriction.
        self.write(policy.set_global, allowed=['codex', 'claude'])
        self.write(policy.set_restriction, self.root_task, ['codex'], expected_revision=None)
        deep = self.chain(self.root_task, 200)
        for ancestors in (63, 64, 65, 66, 120, 200):  # The Task at deep[n - 1] has n ancestors.
            with self.subTest(ancestors=ancestors):
                self.assertEqual(self.effective(deep[ancestors - 1]), ['codex'])
                self.assertIn('excludes it', self.read(policy.denial, 'claude', deep[ancestors - 1]))
                self.assertIsNone(self.read(policy.denial, 'codex', deep[ancestors - 1]))
        self.write(policy.set_restriction, deep[99], [], expected_revision=None)  # A second restriction, far down.
        self.assertEqual(self.effective(deep[150]), [])
        self.assertEqual(self.effective(deep[98]), ['codex'])
        self.write(policy.set_global, allowed=['claude'])  # The global set still applies at any depth.
        self.assertEqual(self.effective(deep[50]), [])
        self.assertIn('not in your allowed providers', self.read(policy.denial, 'codex', deep[98]))

    def test_ancestry_beyond_the_bound_fails_closed(self):
        deep = self.chain(self.root_task, 12)
        with patch.object(policy, 'MAX_ANCESTRY', 10):
            self.assertEqual(self.effective(deep[9]), ['codex', 'claude', 'gemini'])  # Exactly 10 ancestors.
            self.assertEqual(self.effective(deep[10]), [])  # 11 ancestors: not verified, so nothing is permitted.
            reason = self.read(policy.denial, 'codex', deep[11])
            self.assertIn('could not be verified (it has more than 10 ancestors)', reason)
            self.assertIsNone(self.read(policy.denial, 'mock', deep[11]))

    def test_cyclic_missing_and_unknown_ancestry_fail_closed(self):
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (self.leaf, self.root_task))
        self.assertEqual(self.effective(self.leaf), [])
        self.assertIn('ancestry contains a cycle', self.read(policy.denial, 'codex', self.child))
        with self.assertRaisesRegex(ValueError, 'only narrow'):
            self.write(policy.set_restriction, self.leaf, ['codex'], expected_revision=None)
        with self.store._connection() as db:
            db.execute('PRAGMA foreign_keys = OFF')  # Corrupt data only this way: the schema forbids it.
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE tasks SET parent_task_id='missing' WHERE task_id=?", (self.root_task,))
        self.assertIn('an ancestor Task is missing', self.read(policy.denial, 'claude', self.leaf))
        self.assertIn('the Task was not found', self.read(policy.denial, 'claude', 'no-such-task'))
        self.assertEqual(self.read(policy.view, 'gemini', self.leaf)['effective'], [])

    def test_restrictions_are_separate_from_the_auto_policy(self):
        self.write(policy.set_restriction, self.root_task, ['claude'], expected_revision=None)
        tables = {row[0] for row in self.read(lambda db: db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall())}
        self.assertIn('task_provider_restrictions', tables)
        self.assertIsNone(self.read(lambda db: db.execute(
            'SELECT 1 FROM auto_allocation_policies WHERE task_id=?', (self.root_task,)).fetchone()))


TAGS = {'codex': {'latest': '0.154.0'}, 'claude': {'latest': '2.1.198', 'stable': '2.1.198'},
        'gemini': {'latest': '0.30.0', 'preview': '0.30.0', 'nightly': '0.30.0'}}


FAKE_NPM_CONTROLS = ('FAKE_CLI_LOG', 'FAKE_NPM_SLEEP', 'FAKE_NPM_EXIT', 'FAKE_NPM_NOOP', 'FAKE_NPM_CHILD_SLEEP')


def onboarding_environment(test, root):
    """Fake CLIs, a fake npm at the Node.js installer location and stubbed release metadata."""
    env, log = fake.isolated_environment(root)
    program_files = root / 'programfiles'
    env.update({'ProgramFiles': str(program_files), 'ProgramFiles(x86)': str(root / 'programfiles-x86'),
                'LOCALAPPDATA': str(root / 'localappdata'), 'FAKE_AUTH_STATE': str(root / 'auth.json'),
                'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': '1'})
    test.enterContext(patch.dict(os.environ, env))
    npm = fake.install_npm(program_files, Path(env['APPDATA']))
    test.enterContext(patch.object(onboarding, 'CONSOLE_FLAGS', 0))  # No real console window in tests.
    test.original_elsewhere = onboarding._elsewhere
    test.enterContext(patch.object(onboarding, '_elsewhere', return_value=[]))  # Never the developer's PATH.
    # The fake npm is steered by these test variables; the installer's allowlist passes nothing else.
    test.enterContext(patch.object(onboarding, 'INSTALLER_ENV', onboarding.INSTALLER_ENV + FAKE_NPM_CONTROLS))
    test.tags = {provider: dict(tags) for provider, tags in TAGS.items()}
    test.enterContext(patch.object(onboarding, 'fetch_dist_tags', side_effect=lambda provider: dict(test.tags[provider])))
    onboarding._plans.clear()
    test.addCleanup(onboarding._stopping.clear)
    return env, log, npm


def npm_args(provider, version, appdata):
    return ['install', '--global', f'{onboarding.PACKAGES[provider]}@{version}', f'--prefix={Path(appdata) / "npm"}',
            '--registry=https://registry.npmjs.org/', '--userconfig=<empty-config>', '--globalconfig=<empty-config>',
            '--ignore-scripts', '--strict-ssl=true', '--no-audit', '--no-fund', '--update-notifier=false']


def dead_controller(db):
    """The identity of a backend process that has exited (its PID and creation time)."""
    import subprocess
    import sys
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    identity = {**ps.controller_identity(), 'instance_id': str(uuid4()), 'pid': process.pid,
                'created_at': ps.process_created_at(int(process._handle))}
    process.kill()
    process.wait(10)
    executions.register_controller(db, identity)
    return identity


@unittest.skipUnless(os.name == 'nt', 'Windows provider installations')
class OnboardingOperationTests(Store, unittest.TestCase):
    """Fake npm at the Node.js installer location and fake provider launchers in %APPDATA%\\npm."""

    def setUp(self):
        self.make_store()
        env, self.log, self.npm = onboarding_environment(self, self.root)
        self.appdata = Path(env['APPDATA'])
        providers.invalidate()
        self.addCleanup(providers.invalidate)
        self.connect = self.store._connection
        self.published = []

    def uninstall(self, provider):
        (self.appdata / 'npm' / f'{provider}.cmd').unlink()
        providers.invalidate(provider)

    def propose(self, provider, action, channel=None):
        return self.read(onboarding.propose, provider, action, channel)

    def run_plan(self, plan, timeout=60):
        started = onboarding.start(self.connect, plan['provider'], plan['plan_id'], plan['digest'],
                                   publish=lambda: self.published.append(1))
        self.assertEqual(started['status'], 'running')
        return self.wait(started['operation_id'], timeout)

    def wait(self, operation_id, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = onboarding.operation(self.connect, operation_id)
            if value['status'] != 'running':
                return value
            time.sleep(0.1)
        self.fail('operation did not finish')

    def npm_entries(self):
        return [entry for entry in fake.read_log(self.log) if entry['provider'] == 'npm']

    def npm_calls(self):
        return [entry['args'] for entry in self.npm_entries()]

    def busy(self, provider):
        identity = {**ps.controller_identity(), 'instance_id': str(uuid4())}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return executions.reserve(db, identity, agent_id=str(uuid4()), task_id=None, provider=provider, containment='none')

    def test_discovery_reads_trusted_locations_and_executes_nothing(self):
        found = onboarding.discover('codex')
        self.assertEqual((found['state'], found['method']), ('installed', 'npm'))
        self.assertEqual(Path(found['location']), self.appdata / 'npm' / 'codex.cmd')
        self.assertEqual(Path(found['prerequisites']['npm']), self.npm)
        self.uninstall('codex')
        with patch.object(onboarding, '_elsewhere', return_value=[r'C:\Tools\codex.cmd']):
            found = onboarding.discover('codex')
            self.assertEqual((found['state'], found['elsewhere']), ('unsupported_location', [r'C:\Tools\codex.cmd']))
            plan = self.propose('codex', 'install')
        self.assertTrue(any('does not run' in line for line in plan['blockers']))
        self.assertIsNone(plan['plan_id'])
        self.assertEqual(fake.read_log(self.log), [])  # Discovery and planning ran nothing.

    def test_elsewhere_reports_path_and_winget_installations_without_trusting_them(self):
        winget = self.root / 'localappdata' / 'Microsoft' / 'WinGet' / 'Links'
        winget.mkdir(parents=True)
        (winget / 'claude.exe').write_bytes(b'')
        trusted = self.appdata / 'npm' / 'claude.cmd'
        hits = {'claude': str(trusted)}
        with patch('app.provider_onboarding.shutil.which', side_effect=lambda name: hits.get(name)):
            self.assertEqual(self.original_elsewhere('claude', trusted), [str(winget / 'claude.exe')])
            hits['claude'] = r'C:\Other\claude.cmd'
            self.assertEqual(self.original_elsewhere('claude', None), sorted([r'C:\Other\claude.cmd', str(winget / 'claude.exe')]))
        self.assertEqual(fake.read_log(self.log), [])  # Reported only: nothing found elsewhere was run.

    def test_install_runs_only_the_reviewed_exact_version_and_verifies_it(self):
        self.uninstall('codex')
        plan = self.propose('codex', 'install')
        self.assertEqual(plan['command'], ['npm.cmd', *npm_args('codex', '0.154.0', self.appdata)])
        self.assertEqual((plan['method'], plan['target'], plan['target_version'], plan['package'], plan['registry'],
                          plan['blockers']), ('npm', 'latest', '0.154.0', '@openai/codex', 'https://registry.npmjs.org/', []))
        self.assertTrue(plan['plan_id'] and len(plan['digest']) == 64)
        with self.assertRaisesRegex(ValueError, 'does not match the reviewed operation'):
            onboarding.start(self.connect, 'codex', plan['plan_id'], '0' * 64)
        with self.assertRaisesRegex(ValueError, 'unknown, expired or already used'):
            onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])  # One use, even when refused.
        self.assertEqual(self.npm_calls(), [])
        plan = self.propose('codex', 'install')
        with self.assertRaisesRegex(ValueError, 'unknown, expired or already used'):
            onboarding.start(self.connect, 'claude', plan['plan_id'], plan['digest'])  # Bound to its provider.
        plan = self.propose('codex', 'install')
        self.tags['codex']['latest'] = '0.155.0'  # A release after the review: the approved version still runs.
        result = self.run_plan(plan)
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual((result['after_version'], result['target_version']), ('codex-cli 0.154.0', '0.154.0'))
        entry, = self.npm_entries()
        self.assertEqual(entry['args'][:3], ['install', '--global', '@openai/codex@0.154.0'])
        self.assertNotIn('abcdef0123456789', result['output_tail'])
        self.assertNotIn('sk-test1234567890abcd', result['output_tail'])
        self.assertIn('[redacted]', result['output_tail'])
        self.assertFalse({'CODEX_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY'} & set(entry['env']))
        self.assertEqual(result['containment'], 'job_kill_on_close')
        self.assertTrue(result['evidence']['contained']['is_process_in_job'])
        self.assertTrue(result['evidence']['ended']['verified'])
        with self.assertRaisesRegex(ValueError, 'already used'):
            onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])
        self.assertTrue(self.published)

    def test_hostile_environment_and_npm_configuration_cannot_redirect_the_installation(self):
        self.uninstall('gemini')
        evil = self.root / 'evil'
        evil.mkdir()
        hostile = 'registry=http://evil.test/\nprefix=' + str(evil) + '\nignore-scripts=false\nscript-shell=' + str(evil / 'sh.exe') + '\n'
        (Path(os.environ['USERPROFILE']) / '.npmrc').write_text(hostile, encoding='utf-8')
        (self.appdata / 'npm' / 'etc').mkdir(parents=True)
        (self.appdata / 'npm' / 'etc' / 'npmrc').write_text(hostile, encoding='utf-8')
        with patch.dict(os.environ, {'NPM_CONFIG_REGISTRY': 'http://evil.test/', 'npm_config_prefix': str(evil),
                                     'NPM_CONFIG_USERCONFIG': str(evil / 'npmrc'), 'npm_config_script_shell': str(evil / 'sh.exe'),
                                     'NODE_OPTIONS': f'--require {evil / "inject.js"}', 'NODE_PATH': str(evil),
                                     'NODE_EXTRA_CA_CERTS': str(evil / 'ca.pem'), 'NODE_TLS_REJECT_UNAUTHORIZED': '0'}):
            result = self.run_plan(self.propose('gemini', 'install'))
        self.assertEqual(result['status'], 'succeeded', result)
        entry, = self.npm_entries()
        effective = entry['effective']
        self.assertEqual(effective['registry'], 'https://registry.npmjs.org/')
        self.assertEqual(Path(effective['prefix']), self.appdata / 'npm')
        self.assertEqual(effective['ignore-scripts'], 'true')
        self.assertIsNone(effective['script-shell'])
        self.assertEqual((Path(effective['userconfig']).name, Path(effective['globalconfig']).name), ('empty-npmrc', 'empty-npmrc'))
        self.assertEqual(entry['cwd_entries'], ['empty-npmrc'])  # A fresh folder: no project npmrc or package.json.
        names = {name.upper() for name in entry['env']}
        self.assertFalse({'NODE_OPTIONS', 'NODE_PATH', 'NODE_EXTRA_CA_CERTS', 'NODE_TLS_REJECT_UNAUTHORIZED'} & names)
        self.assertFalse([name for name in names if name.startswith('NPM_CONFIG_')])
        self.assertTrue((self.appdata / 'npm' / 'gemini.cmd').exists())
        self.assertEqual(list(evil.iterdir()), [])  # Nothing was installed or run from the hostile locations.

    def test_an_unresolvable_release_offers_the_manual_path_instead(self):
        self.uninstall('codex')
        with patch.object(onboarding, 'fetch_dist_tags', side_effect=OSError('offline')):
            plan = self.propose('codex', 'install')
        self.assertIsNone(plan['plan_id'])
        self.assertTrue(any('could not be resolved' in line and 'install manually' in line for line in plan['blockers']))
        self.tags['codex'] = {'beta': '1.0.0'}
        self.assertTrue(any('lists no "latest" release' in line for line in self.propose('codex', 'install')['blockers']))

    def test_a_different_installed_version_needs_attention(self):
        self.tags['claude']['latest'] = '2.2.0'  # The fake installation reports 2.1.198.
        result = self.run_plan(self.propose('claude', 'update'))
        self.assertEqual(result['status'], 'needs_attention')
        self.assertIn('not the approved 2.2.0', result['result'])

    def reported(self, version):
        """The fresh probe after the operation reports `version` (the real probe still runs)."""
        import dataclasses
        real = providers.probe
        return patch.object(providers, 'probe', side_effect=lambda provider, force=False: dataclasses.replace(
            real(provider, force=force), version=version))

    def test_a_pinned_npm_version_succeeds_only_when_the_reported_version_matches(self):
        # Second review: success needs an installed CLI, exit 0 and a parseable version equal to the approved one.
        cases = {'codex-cli 0.154.0': 'succeeded', 'codex-cli 0.155.0': 'needs_attention', None: 'needs_attention',
                 'codex': 'needs_attention', 'codex-cli 0.154': 'needs_attention', 'codex-cli 0.154.0-01': 'needs_attention'}
        for version, expected in cases.items():
            with self.subTest(version=version):
                plan = self.propose('codex', 'update')
                with self.reported(version):
                    result = self.run_plan(plan)
                self.assertEqual(result['status'], expected, result)
                if expected == 'needs_attention':
                    self.assertIn('0.154.0', result['result'])
        with patch.dict(os.environ, FAKE_NPM_EXIT='3'):
            result = self.run_plan(self.propose('codex', 'update'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('exited with code 3', result['result'])

    def test_the_native_claude_updater_is_not_bound_to_a_pinned_version(self):
        plan = onboarding.build_plan(None, 'claude', 'update')
        native = onboarding.Plan('claude', 'update', 'native', plan.executable, ['--version'], '2.1.190 (Claude Code)', 'latest')
        state = {'canceled': False, 'stopped': False}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("""INSERT INTO provider_operations(operation_id,provider,action,method,command,plan_digest,status,
                requested_by) VALUES ('native','claude','update','native','[]','d','running','user')""")
        onboarding._finish(self.connect, 'native', native, 0, '', None, state, {}, True)
        value = onboarding.operation(self.connect, 'native')
        self.assertEqual(value['status'], 'succeeded', value)  # Its own updater chose the version.
        self.assertIsNone(value['target_version'])

    def test_paths_that_cmd_exe_could_misread_are_refused(self):
        with patch.dict(os.environ, {'APPDATA': str(self.root / 'a&b')}):
            plan = self.propose('codex', 'install')  # Not installed under this profile: npm would run.
        self.assertIsNone(plan['plan_id'])
        self.assertTrue(any('cannot be passed safely' in line for line in plan['blockers']))

    def test_a_plan_that_changed_since_review_does_not_run(self):
        self.uninstall('codex')
        plan = self.propose('codex', 'install')
        fake.install(self.appdata, names=('codex',))  # Installed meanwhile: the reviewed install is obsolete.
        providers.invalidate('codex')
        with self.assertRaisesRegex(ValueError, 'can no longer run as approved.*already installed') as refused:
            onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])
        self.assertNotIn('.)', str(refused.exception))  # No doubled punctuation around the reason.
        self.assertEqual(self.npm_calls(), [])

    def test_a_failed_or_ineffective_install_is_never_reported_as_installed(self):
        self.uninstall('gemini')
        with patch.dict(os.environ, FAKE_NPM_EXIT='1'):
            result = self.run_plan(self.propose('gemini', 'install', 'preview'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('exited with code 1', result['result'])
        self.assertNotIn('..', result['result'])  # The provider's own reason is embedded without its period.
        self.assertEqual(self.npm_calls()[-1][:3], ['install', '--global', '@google/gemini-cli@0.30.0'])
        with patch.dict(os.environ, FAKE_NPM_NOOP='1'):
            result = self.run_plan(self.propose('gemini', 'install'))
        self.assertEqual(result['status'], 'failed')  # Exit 0, but nothing is installed.
        self.assertIn('Nothing was claimed as installed', result['result'])

    def test_every_action_waits_for_active_work_and_never_stops_it(self):
        execution_id = self.busy('codex')
        for action in ('update', 'login', 'logout'):
            plan = self.propose('codex', action)
            self.assertIsNone(plan['plan_id'])
            self.assertTrue(any('nothing is stopped automatically' in line for line in plan['blockers']), plan)
        self.uninstall('codex')
        self.assertTrue(any('nothing is stopped automatically' in line for line in self.propose('codex', 'install')['blockers']))
        plan = self.propose('claude', 'update')  # Another provider is unaffected.
        self.assertIsNotNone(plan['plan_id'])
        with self.connect() as db:
            self.assertEqual(executions.get(db, execution_id)['state'], 'launching')  # Untouched.

    def test_work_started_after_review_refuses_the_operation(self):
        plan = self.propose('codex', 'update')
        self.busy('codex')
        with self.assertRaisesRegex(ValueError, 'can no longer run as approved'):
            onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])
        self.assertEqual(self.npm_calls(), [])

    def test_one_blocking_operation_per_provider_and_cancel_waits_for_the_whole_tree(self):
        with patch.dict(os.environ, FAKE_NPM_SLEEP='30', FAKE_NPM_CHILD_SLEEP='60'):
            plan = self.propose('codex', 'update')
            started = onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])
            deadline = time.monotonic() + 20
            while not self.npm_calls() and time.monotonic() < deadline:
                time.sleep(0.1)
            second = self.propose('codex', 'update')
            self.assertTrue(any('already running' in line for line in second['blockers']))
            for status in ('running', 'uncertain'):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.write(lambda db: db.execute(f'''INSERT INTO provider_operations(operation_id,provider,action,method,
                        command,plan_digest,status,requested_by) VALUES ('{status}','codex','update','npm','[]','d','{status}','user')'''))
            began = time.monotonic()
            result = onboarding.cancel(self.connect, started['operation_id'])  # Returns once the end is recorded.
        self.assertEqual(result['status'], 'canceled', result)
        self.assertLess(time.monotonic() - began, 25)  # Ended, not waited out.
        self.assertTrue(result['evidence']['ended']['verified'])
        self.assertEqual(result['evidence']['termination']['active_processes'], 0)
        self.assertIsNone(ps.Job.open_existing(result['job_name'], terminate=False))  # The whole tree, child included.
        with self.assertRaisesRegex(ValueError, 'not running'):
            onboarding.cancel(self.connect, started['operation_id'])

    def test_installer_leftovers_are_ended_and_verified_after_a_normal_exit(self):
        with patch.dict(os.environ, FAKE_NPM_CHILD_SLEEP='60'):
            result = self.run_plan(self.propose('codex', 'update'))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['evidence']['ended']['terminated']['active_processes'], 0)
        self.assertIsNone(ps.Job.open_existing(result['job_name'], terminate=False))

    def test_an_operation_that_exceeds_its_time_limit_is_stopped(self):
        with patch.dict(os.environ, FAKE_NPM_SLEEP='30'), patch.dict(onboarding.OPERATION_TIMEOUT, update=1):
            result = self.run_plan(self.propose('claude', 'update', 'stable'))
        self.assertEqual(result['status'], 'failed')
        self.assertIn('did not finish in time', result['result'])
        self.assertTrue(result['evidence']['ended']['verified'])
        self.assertEqual(self.npm_calls()[-1][:3], ['install', '--global', '@anthropic-ai/claude-code@2.1.198'])

    def test_an_unverifiable_end_keeps_the_provider_blocked_until_the_user_confirms(self):
        with patch.dict(os.environ, FAKE_NPM_SLEEP='30'), patch.dict(onboarding.OPERATION_TIMEOUT, update=1), \
                patch.object(ps.Job, 'terminate', side_effect=ps.SupervisionError('could not confirm')):
            result = self.run_plan(self.propose('codex', 'update'))
        self.assertEqual(result['status'], 'uncertain')
        self.assertIn('could not be verified', result['result'])
        self.assertFalse(result['evidence']['ended']['verified'])
        blocked = self.propose('codex', 'update')
        self.assertTrue(any('ended without verification' in line for line in blocked['blockers']))
        with self.assertRaisesRegex(providers.ProviderRefused, 'Confirm in AI Providers'):
            self.read(onboarding.require_no_maintenance, 'codex')
        self.assertIsNotNone(self.propose('claude', 'update')['plan_id'])  # Other providers are unaffected.
        with self.assertRaisesRegex(ValueError, 'Confirm'):
            onboarding.resolve(self.connect, result['operation_id'], confirm_ended=False)
        resolved = onboarding.resolve(self.connect, result['operation_id'], confirm_ended=True)
        self.assertEqual(resolved['status'], 'interrupted')
        self.assertEqual(resolved['evidence']['resolution']['by'], 'user')
        self.assertIsNotNone(self.propose('codex', 'update')['plan_id'])

    def test_shutdown_ends_and_records_running_operations(self):
        with patch.dict(os.environ, FAKE_NPM_SLEEP='30'):
            plan = self.propose('gemini', 'update')
            started = onboarding.start(self.connect, 'gemini', plan['plan_id'], plan['digest'])
            deadline = time.monotonic() + 20
            while not self.npm_calls() and time.monotonic() < deadline:
                time.sleep(0.1)
            onboarding.shutdown()
        value = onboarding.operation(self.connect, started['operation_id'])
        self.assertEqual(value['status'], 'interrupted', value)
        self.assertTrue(value['evidence']['ended']['verified'])
        with self.assertRaisesRegex(ValueError, 'stopping'):
            plan = self.propose('codex', 'update')
            onboarding.start(self.connect, 'codex', plan['plan_id'], plan['digest'])
        onboarding.accept_operations()  # What initialize_persistence does when the backend runs again.
        self.assertEqual(self.run_plan(self.propose('codex', 'update'))['status'], 'succeeded')

    def test_sign_in_uses_the_official_command_and_is_verified_afterwards(self):
        Path(os.environ['FAKE_AUTH_STATE']).write_text(json.dumps({'codex': 'none'}), encoding='utf-8')
        self.assertFalse(providers.probe('codex', force=True).authenticated)
        plan = self.propose('codex', 'login')
        self.assertEqual((plan['command'], plan['console']), (['codex.cmd', 'login'], True))
        self.assertIn('never sees or stores credentials', ' '.join(plan['risks']))
        with patch.dict(os.environ, FAKE_LOGIN_ABANDON='1'):
            result = self.run_plan(plan)
        self.assertEqual(result['status'], 'needs_attention')  # The window closed; that is not a sign-in.
        self.assertIn('does not report a sign-in', result['result'])
        result = self.run_plan(self.propose('codex', 'login'))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(providers.probe('codex').authenticated)
        self.assertIsNone(result['output_tail'])  # The console belongs to the user; nothing is captured.
        providers.probe('claude', force=True)  # The view's last check: already signed in.
        claude = self.propose('claude', 'login')
        self.assertEqual(claude['command'], ['claude.cmd', 'auth', 'login', '--claudeai'])
        self.assertTrue(any('switch to another account' in line for line in claude['risks']))  # Already signed in.

    def test_a_program_a_finished_sign_in_opened_is_released_not_ended(self):
        Path(os.environ['FAKE_AUTH_STATE']).write_text(json.dumps({'codex': 'none'}), encoding='utf-8')
        with patch.dict(os.environ, FAKE_LOGIN_BROWSER_SLEEP='60'):
            result = self.run_plan(self.propose('codex', 'login'))
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertGreaterEqual(result['evidence']['ended']['released_processes'], 1)
        browser = next(entry for entry in fake.read_log(self.log) if entry['kind'] == 'browser')
        created = ps.created_at_of(browser['pid'])
        self.addCleanup(ps.terminate_exact, browser['pid'], created)
        self.assertEqual(ps.process_status(browser['pid'], created), 'alive')  # The user's browser keeps running.

    def test_sign_out_and_its_environment(self):
        plan = self.propose('codex', 'logout')
        self.assertEqual(plan['command'], ['codex.cmd', 'logout'])
        self.assertTrue(any('cannot start until you sign in again' in line for line in plan['changes']))
        with patch.dict(os.environ, NODE_OPTIONS='--require evil.js', NPM_CONFIG_REGISTRY='http://evil.test/'):
            env = onboarding.environment_for(onboarding.build_plan(None, 'codex', 'logout'))
            result = self.run_plan(plan)
        self.assertFalse({'NODE_OPTIONS', 'NPM_CONFIG_REGISTRY', 'OPENAI_API_KEY', 'CODEX_API_KEY'} & {k.upper() for k in env})
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertFalse(providers.probe('codex').authenticated)

    def test_gemini_sign_in_and_remote_installer_scripts_are_manual_only(self):
        for action in ('login', 'logout'):
            plan = self.propose('gemini', action)
            self.assertIsNone(plan['plan_id'])
            self.assertIsNone(plan['command'])
            self.assertTrue(plan['manual'])
        self.assertIn('Do not choose API key or Vertex AI', self.propose('gemini', 'login')['manual'][0])
        self.uninstall('claude')
        install = self.propose('claude', 'install')
        self.assertEqual(install['command'][:4], ['npm.cmd', 'install', '--global', '@anthropic-ai/claude-code@2.1.198'])
        self.assertIn('never runs for you', install['manual'][0])
        code = Path(onboarding.__file__).read_text(encoding='utf-8').split('"""', 2)[2]  # After the module docstring.
        self.assertIsNone(re.search(r'shell\s*=\s*True\s*[,)]', code))
        for forbidden in ('irm http', '| iex', 'Invoke-Expression', 'curl -', 'runas', 'Start-Process', 'powershell'):
            self.assertNotIn(forbidden, code.lower() if forbidden.islower() else code)

    def test_native_claude_updates_with_its_own_updater(self):
        native = self.root / 'home' / '.local' / 'bin'
        native.mkdir(parents=True)
        (native / 'claude.exe').write_bytes(b'')
        providers.invalidate('claude')
        found = onboarding.discover('claude')
        self.assertEqual((found['method'], Path(found['location'])), ('native', native / 'claude.exe'))
        plan = self.propose('claude', 'update')
        self.assertEqual((plan['command'], plan['method'], plan['target_version']), (['claude.exe', 'update'], 'native', None))
        self.assertEqual(onboarding.command_for(onboarding.build_plan(None, 'claude', 'update')),
                         [str(native / 'claude.exe'), 'update'])

    def test_cmd_shims_run_through_cmd_exe_with_fixed_quoted_tokens(self):
        plan = onboarding.build_plan(None, 'codex', 'update')
        command = onboarding.command_for(plan, self.root / 'with space' / 'empty-npmrc')
        self.assertIsInstance(command, str)
        self.assertIn('/d /s /v:off /c', command)
        self.assertIn('@openai/codex@0.154.0', command)
        self.assertIn(f'"--userconfig={self.root / "with space" / "empty-npmrc"}"', command)

    def test_requests_are_validated_in_the_backend(self):
        with self.assertRaisesRegex(ValueError, 'only for Codex'):
            self.propose('mock', 'install')
        with self.assertRaisesRegex(ValueError, 'Unknown setup operation'):
            self.propose('codex', 'uninstall')
        with self.assertRaisesRegex(ValueError, 'Unknown release channel'):
            self.propose('codex', 'install', 'nightly')
        with self.assertRaisesRegex(ValueError, 'Unknown release channel'):
            self.propose('claude', 'update', 'latest; calc.exe')


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class OperationRecoveryTests(Store, unittest.TestCase):
    """What a backend that stopped mid-operation leaves behind, with real Job Objects."""

    def setUp(self):
        self.make_store()
        self.connect = self.store._connection

    def insert(self, db, controller, job_name, containment='job_kill_on_close', provider='claude'):
        db.execute('''INSERT INTO provider_operations(operation_id,provider,action,method,command,plan_digest,status,
            controller_instance_id,containment,job_name,requested_by) VALUES (?,?,?,?,?,?,?,?,?,?,'user')''',
            (job_name[-8:], provider, 'update', 'npm', '[]', 'd', 'running', controller['instance_id'] if controller else None,
             containment, job_name))
        return job_name[-8:]

    def test_a_crashed_backend_leaves_no_installer_behind(self):
        # A separate "backend" contains a long-running installer in a kill-on-close Job, then dies abruptly.
        import subprocess
        import sys
        script = ('import json, os, subprocess, sys, time\n'
                  'from app import process_supervision as ps\n'
                  'name = ps.new_job_name()\n'
                  'launch = ps.spawn([sys.executable, "-c", "import time; time.sleep(120)"], job_name=name)\n'
                  'launch.resume()\n'
                  'print(json.dumps({"job": name, "pid": launch.process.pid}), flush=True)\n'
                  'time.sleep(1)\n'
                  'os._exit(9)\n')
        backend = subprocess.Popen([sys.executable, '-c', script], cwd=str(Path(__file__).parent), stdout=subprocess.PIPE,
                                   text=True)
        record = json.loads(backend.stdout.readline())
        identity = {**ps.controller_identity(), 'instance_id': str(uuid4()), 'pid': backend.pid,
                    'created_at': ps.created_at_of(backend.pid)}
        backend.wait(30)
        backend.stdout.close()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            executions.register_controller(db, identity)
            key = self.insert(db, identity, record['job'])
        deadline = time.monotonic() + 10
        while ps.Job.open_existing(record['job'], terminate=False) and time.monotonic() < deadline:
            time.sleep(0.1)  # Windows ends the Job's processes once its last handle closed.
        self.write(onboarding.recover)
        value = onboarding.operation(self.connect, key)
        self.assertEqual(value['status'], 'interrupted', value)
        self.assertEqual((value['evidence']['recovery']['owner'], value['evidence']['recovery']['job']), ('gone', 'gone'))
        self.assertIsNone(self.read(onboarding.blocking_operation, 'claude'))

    def test_a_job_that_still_runs_after_its_backend_died_stays_uncertain(self):
        import subprocess
        import sys
        name = ps.new_job_name()
        launch = ps.spawn([sys.executable, '-c', 'import time; time.sleep(120)'], job_name=name)
        launch.resume()  # This test holds the Job, standing in for whatever keeps it alive.
        self.addCleanup(launch.job.close)
        self.addCleanup(lambda: launch.job.terminate(timeout=10) if launch.job.handle else None)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            key = self.insert(db, dead_controller(db), name)
        self.write(onboarding.recover)
        value = onboarding.operation(self.connect, key)
        self.assertEqual(value['status'], 'uncertain', value)
        self.assertRegex(value['evidence']['recovery']['job'], r'^[1-9]\d* active process')
        with self.assertRaisesRegex(providers.ProviderRefused, 'ended without verification'):
            self.read(onboarding.require_no_maintenance, 'claude')
        self.assertIsNone(self.read(onboarding.require_no_maintenance, 'codex'))  # Other providers are unaffected.
        with self.assertRaisesRegex(ValueError, r'still has \d+ running process'):
            onboarding.resolve(self.connect, key, confirm_ended=True)  # Never released while it verifiably runs.
        launch.job.terminate(timeout=10)
        launch.job.close()
        self.assertEqual(onboarding.resolve(self.connect, key, confirm_ended=True)['status'], 'interrupted')

    def test_unknown_owners_and_never_contained_operations(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            unknown = self.insert(db, None, ps.new_job_name())  # No controller identity recorded.
            dead = dead_controller(db)
            pending = self.insert(db, dead, ps.new_job_name(), containment='pending', provider='codex')
            live = {**ps.controller_identity(), 'instance_id': str(uuid4())}  # This process: still alive.
            executions.register_controller(db, live)
            owned = self.insert(db, live, ps.new_job_name(), provider='gemini')
        self.write(onboarding.recover)
        self.assertEqual(onboarding.operation(self.connect, unknown)['status'], 'uncertain')
        self.assertEqual(onboarding.operation(self.connect, pending)['status'], 'interrupted')  # Its Job never existed.
        self.assertEqual(onboarding.operation(self.connect, owned)['status'], 'running')  # Its backend still owns it.

    def test_an_earlier_draft_of_the_operations_table_is_rebuilt_with_its_rows(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DROP TABLE provider_operations')
            db.execute('''CREATE TABLE provider_operations (operation_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                action TEXT NOT NULL, method TEXT NOT NULL, command TEXT NOT NULL, plan_digest TEXT NOT NULL,
                status TEXT NOT NULL, before_version TEXT, after_version TEXT, exit_code INTEGER, output_tail TEXT,
                result TEXT, requested_by TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT 'x', finished_at TEXT)''')
            db.execute("CREATE UNIQUE INDEX provider_operation_running ON provider_operations(provider) WHERE status='running'")
            db.execute('''INSERT INTO provider_operations(operation_id,provider,action,method,command,plan_digest,status,
                requested_by) VALUES ('old','codex','update','npm','[]','d','succeeded','user')''')
            onboarding.migrate_v37(db)
        value = onboarding.operation(self.connect, 'old')
        self.assertEqual((value['status'], value['containment']), ('succeeded', 'pending'))


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class MaintenanceExclusionTests(unittest.TestCase):
    """Maintenance and Agent launches of one provider exclude each other, in both race orders."""
    rows = base.SupervisedFakeProviderTests.rows
    wait_status = base.SupervisedFakeProviderTests.wait_status

    def setUp(self):
        base.SupervisedFakeProviderTests.setUp(self)
        program_files = Path(self.temp.name) / 'programfiles'
        self.enterContext(patch.dict(os.environ, {'ProgramFiles': str(program_files), 'FAKE_AUTH_STATE':
                                                  str(Path(self.temp.name) / 'auth.json')}))
        fake.install_npm(program_files, Path(os.environ['APPDATA']))
        self.enterContext(patch.object(onboarding, 'INSTALLER_ENV', onboarding.INSTALLER_ENV + FAKE_NPM_CONTROLS))
        self.enterContext(patch.object(onboarding, 'CONSOLE_FLAGS', 0))
        self.enterContext(patch.object(onboarding, '_elsewhere', return_value=[]))
        self.enterContext(patch.object(onboarding, 'fetch_dist_tags', side_effect=lambda provider: dict(TAGS[provider])))
        onboarding._plans.clear()

    def tearDown(self):
        onboarding.shutdown(wait=30)
        onboarding._stopping.clear()
        base.SupervisedFakeProviderTests.tearDown(self)

    def npm_calls(self):
        return [entry for entry in fake.read_log(self.log) if entry['provider'] == 'npm']

    def test_an_agent_admitted_first_refuses_the_operation_in_its_write_transaction(self):
        providers.probe('codex', force=True)  # The reviewed plan and the rebuilt one see the same version.
        plan = manager.plan_provider_operation('codex', 'update')
        planned, proceed, outcome = threading.Event(), threading.Event(), {}
        original = onboarding._controller

        def reviewed_before_the_agent(*args, **kwargs):
            planned.set()  # The approved plan was rebuilt and checked while no execution existed.
            proceed.wait(30)  # Outside any database connection: the Agent can be admitted meanwhile.
            return original(*args, **kwargs)

        def maintenance():
            try:
                outcome['value'] = manager.start_provider_operation('codex', plan['plan_id'], plan['digest'])
            except ValueError as exc:
                outcome['error'] = str(exc)
        with patch.object(onboarding, '_controller', side_effect=reviewed_before_the_agent):
            worker = threading.Thread(target=maintenance)
            worker.start()
            self.assertTrue(planned.wait(30))
            with patch.dict(os.environ, FAKE_CODEX_SCENARIO='slow', FAKE_SLOW_SECONDS='3'):
                agent_id = manager.start_agent('Work', 'codex', 'read-only')  # Reserved and started meanwhile.
            proceed.set()
            worker.join(30)
        self.assertIn('started meanwhile', outcome.get('error', ''), outcome)
        self.assertEqual(self.npm_calls(), [])  # The installer never ran.
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')

    def test_an_operation_started_first_refuses_the_agent_at_the_final_admission(self):
        providers.probe('codex', force=True)
        plan = manager.plan_provider_operation('codex', 'update')
        original = manager._spawn_with_resources
        started = {}

        def maintenance_begins_after_the_early_checks(*args, **kwargs):
            if not started:
                with patch.dict(os.environ, FAKE_NPM_SLEEP='3'):
                    started['op'] = manager.start_provider_operation('codex', plan['plan_id'], plan['digest'])
            return original(*args, **kwargs)
        tasks = len(self.rows('SELECT * FROM tasks'))
        with patch.object(manager, '_spawn_with_resources', side_effect=maintenance_begins_after_the_early_checks):
            with self.assertRaisesRegex(providers.ProviderRefused, 'update is running in AI Providers'):
                manager.start_agent('Work', 'codex', 'read-only')
        self.assertEqual(fake.runs(self.log, 'codex'), [])  # No Agent process was created.
        self.assertEqual(len(self.rows("SELECT * FROM executions WHERE provider='codex' AND state IN "
                                       "('launching','spawned','contained','running')")), 0)
        self.assertGreaterEqual(len(self.rows('SELECT * FROM tasks')), tasks)
        claude = manager.start_agent('Other provider', 'claude', 'read-only')  # Unrelated providers keep working.
        self.assertEqual(self.wait_status(claude, 'finished'), 'finished')
        deadline = time.monotonic() + 30
        while manager.provider_operation(started['op']['operation_id'])['status'] == 'running' and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(manager.provider_operation(started['op']['operation_id'])['status'], 'succeeded')
        codex = manager.start_agent('After the update', 'codex', 'read-only')  # The gate opens once it ended.
        self.assertEqual(self.wait_status(codex, 'finished'), 'finished')

    def test_an_unverified_operation_blocks_new_work_early(self):
        with manager._store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO provider_operations(operation_id,provider,action,method,command,plan_digest,status,
                requested_by) VALUES ('u','claude','login','official_cli','[]','d','uncertain','user')''')
        tasks = len(self.rows('SELECT * FROM tasks'))
        with self.assertRaisesRegex(providers.ProviderRefused, 'ended without verification'):
            manager.start_agent('Work', 'claude', 'read-only')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), tasks)  # Refused before any Task.
        self.assertEqual(fake.runs(self.log, 'claude'), [])


class UpdateCheckTests(Store, unittest.TestCase):
    def setUp(self):
        self.make_store()
        self.connect = self.store._connection
        onboarding._last_manual = 0.0
        self.addCleanup(setattr, onboarding, '_last_manual', 0.0)
        self.versions = {'codex': 'codex-cli 0.154.0', 'claude': '2.1.198 (Claude Code)', 'gemini': '0.30.0'}
        self.tags = {'codex': {'latest': '0.154.0'}, 'claude': {'latest': '2.1.198', 'stable': '2.1.190'},
                     'gemini': {'latest': '0.30.0', 'preview': '0.31.0-preview.1', 'nightly': '0.32.0-nightly.20261020'}}
        self.enterContext(patch.object(onboarding, '_version_text', side_effect=lambda p: self.versions.get(p)))
        self.enterContext(patch.object(onboarding, 'discover', side_effect=lambda p: dict(
            provider=p, state='installed' if self.versions.get(p) else 'not_installed', method='npm', location='x')))
        self.original_fetch = onboarding.fetch_dist_tags
        self.fetch = self.enterContext(patch.object(onboarding, 'fetch_dist_tags', side_effect=lambda p: dict(self.tags[p])))

    def test_versions_are_compared_on_the_installed_channel(self):
        self.tags['codex'] = {'latest': '0.155.0'}
        self.versions['gemini'] = '0.31.0-preview.0'
        self.versions['claude'] = '2.1.200 (Claude Code)'
        self.tags['claude'] = {'latest': '2.1.199'}
        view = onboarding.check_updates(self.connect)
        self.assertEqual((view['codex']['status'], view['codex']['latest_version']), ('update_available', '0.155.0'))
        self.assertEqual((view['gemini']['channel'], view['gemini']['status']), ('preview', 'update_available'))
        self.assertEqual(view['claude']['status'], 'newer_than_channel')
        self.assertEqual(onboarding.evaluate_update('codex', {}, dict(state='installed', method='npm'))['status'],
                         'unsupported_channel')
        self.assertEqual(onboarding.evaluate_update('codex', {'latest': '0.155.0'}, dict(state='installed', method='unknown'))['status'],
                         'manual_required')
        self.versions['codex'] = None
        self.assertEqual(onboarding.evaluate_update('codex', {'latest': '0.155.0'}, dict(state='not_installed', method=None))['status'],
                         'not_installed')

    def test_prerelease_precedence_follows_semantic_versioning(self):
        # Second review: numeric prerelease identifiers compare numerically.
        order = ['1.2.2', '1.2.3-alpha', '1.2.3-alpha.1', '1.2.3-alpha.beta', '1.2.3-nightly.20261023',
                 '1.2.3-nightly.20261024', '1.2.3-preview.2', '1.2.3-preview.9', '1.2.3-preview.10', '1.2.3-rc.2',
                 '1.2.3-rc.10', '1.2.3', '1.2.4-preview.1', '1.10.0']
        parsed = [onboarding.parse_version(v) for v in order]
        for low, high in zip(parsed, parsed[1:]):
            self.assertTrue(onboarding._newer(high, low), (high, low))
            self.assertFalse(onboarding._newer(low, high), (low, high))
        self.assertFalse(onboarding._newer(onboarding.parse_version('1.2.3+build.2'), onboarding.parse_version('1.2.3')))
        for malformed in ('codex', '1.2', '1.2.3-', '1.2.3-a..b', '1.2.3-01', '01.2.3', '1.2.3.4', ''):
            self.assertIsNone(onboarding.parse_version(malformed), malformed)
        for text, expected in (('codex-cli 0.154.0', (0, 154, 0, '')), ('2.1.198 (Claude Code)', (2, 1, 198, '')),
                               ('0.31.0-preview.10', (0, 31, 0, 'preview.10'))):
            self.assertEqual(onboarding.parse_version(text), expected)

    def test_a_newer_installed_prerelease_is_never_offered_as_an_update(self):
        self.versions['gemini'] = '1.2.3-preview.10'
        self.tags['gemini'] = {'latest': '1.2.2', 'preview': '1.2.3-preview.9'}
        self.assertEqual(onboarding.check_updates(self.connect)['gemini']['status'], 'newer_than_channel')
        self.tags['gemini']['preview'] = '1.2.3-preview.11'
        self.assertEqual(onboarding.check_updates(self.connect, force=True)['gemini']['status'], 'update_available')
        self.assertEqual(onboarding.evaluate_update('gemini', {'preview': '1.2.3-preview.01'},
                                                    dict(state='installed', method='npm'))['status'], 'unknown')

    def test_checks_are_coalesced_daily_and_manual_checks_are_rate_limited(self):
        onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 3)
        onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 3)  # Checked less than a day ago.
        onboarding.check_updates(self.connect, force=True)
        self.assertEqual(self.fetch.call_count, 6)
        onboarding.check_updates(self.connect, force=True)
        self.assertEqual(self.fetch.call_count, 6)  # A second manual check within a minute reads the last result.
        self.write(lambda db: db.execute("UPDATE provider_update_checks SET checked_at='2026-01-01T00:00:00.000Z'"))
        view = onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 9)
        self.assertFalse(view['codex']['stale'])

    def test_a_failed_check_is_recorded_and_never_shown_as_current(self):
        self.fetch.side_effect = OSError('offline')
        view = onboarding.check_updates(self.connect)
        self.assertEqual(view['codex']['status'], 'check_failed')
        self.assertIn('could not be read (OSError)', view['codex']['error'])
        self.assertIsNone(view['codex']['latest_version'])
        self.assertEqual(onboarding.notifications(self.connect), [])

    def test_release_metadata_is_bounded_validated_and_never_executed(self):
        requests = []

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return self.body[:limit]

        def opener(body):
            def open_(request, timeout):
                requests.append((request.full_url, timeout))
                return Response(body)
            return open_
        body = json.dumps({'latest': '0.155.0', 'beta': '1.0.0-beta.1', 'evil': '$(calc)', 'BAD TAG': '1.0.0',
                           'next': 7}).encode()
        with patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': ''}), \
                patch('app.provider_onboarding.urllib.request.urlopen', side_effect=opener(body)):
            self.assertEqual(self.original_fetch('codex'), {'latest': '0.155.0', 'beta': '1.0.0-beta.1'})
        self.assertEqual(requests, [('https://registry.npmjs.org/-/package/@openai%2Fcodex/dist-tags', onboarding.FETCH_TIMEOUT)])
        with patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': ''}), \
                patch('app.provider_onboarding.urllib.request.urlopen', side_effect=opener(b'{' + b' ' * onboarding.FETCH_LIMIT + b'}')):
            with self.assertRaisesRegex(ValueError, 'unexpectedly large'):
                self.original_fetch('codex')
        with patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': '1'}), \
                patch('app.provider_onboarding.urllib.request.urlopen') as network:
            with self.assertRaises(OSError):
                self.original_fetch('codex')
        network.assert_not_called()

    def test_notices_are_dismissed_or_snoozed_per_version(self):
        self.tags['codex'] = {'latest': '0.155.0'}
        onboarding.check_updates(self.connect)
        notices = onboarding.notifications(self.connect)
        self.assertEqual([(n['provider'], n['available_version'], n['installed_version']) for n in notices],
                         [('codex', '0.155.0', '0.154.0')])
        self.assertEqual(onboarding.dismiss(self.connect, 'codex', '0.155.0', 'dismiss'), [])
        self.tags['codex'] = {'latest': '0.156.0'}
        onboarding.check_updates(self.connect, force=True)
        self.assertEqual([n['available_version'] for n in onboarding.notifications(self.connect)], ['0.156.0'])  # A newer one shows.
        self.assertEqual(onboarding.dismiss(self.connect, 'codex', '0.156.0', 'remind_later'), [])
        self.write(lambda db: db.execute("UPDATE provider_notification_state SET until='2026-01-01T00:00:00.000Z'"))
        self.assertEqual(len(onboarding.notifications(self.connect)), 1)  # The reminder time has passed.
        for bad in (('mock', '1.0.0', 'dismiss'), ('codex', '1.0', 'dismiss'), ('codex', '1.0.0', 'forever')):
            with self.assertRaises(ValueError):
                onboarding.dismiss(self.connect, *bad)

    # Browser E2E finding P2-3: a cold start compared releases before any version was known.
    def probe_with(self, versions):
        calls = []

        def probe(provider, force=False):
            calls.append(provider)
            if isinstance(versions.get(provider), Exception):
                raise versions[provider]
            return providers.Readiness(provider, providers.READY, version=versions.get(provider))
        self.enterContext(patch.object(providers, 'probe', side_effect=probe))
        return calls

    def installed_everywhere(self):
        self.enterContext(patch.object(onboarding, 'discover', side_effect=lambda p: dict(
            provider=p, state='installed', method='npm', location='x')))

    def test_a_cold_start_check_obtains_version_evidence_before_comparing(self):
        cold = dict(self.versions)
        self.versions.update(codex=None, claude=None, gemini=None)  # Nothing probed yet (fresh backend).
        self.installed_everywhere()
        probes = self.probe_with(cold)
        self.tags['codex'] = {'latest': '0.155.0'}
        view = onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(sorted(probes), ['claude', 'codex', 'gemini'])  # Local, non-billable probes only.
        self.assertEqual((view['codex']['status'], view['codex']['installed_version']), ('update_available', '0.154.0'))
        self.assertEqual(view['claude']['status'], 'up_to_date')
        self.assertEqual([n['available_version'] for n in onboarding.notifications(self.connect)], ['0.155.0'])

    def test_an_unresolved_comparison_is_resolved_by_later_evidence_without_registry_traffic(self):
        self.versions.update(codex=None, claude=None, gemini=None)
        self.installed_everywhere()
        self.probe_with({'codex': OSError('probe failed'), 'claude': None, 'gemini': None})
        self.tags['codex'] = {'latest': '0.155.0'}
        view = onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(view['codex']['status'], 'unknown')  # Never "up to date" by assumption.
        self.assertEqual(view['codex']['latest_version'], '0.155.0')
        self.assertEqual(onboarding.notifications(self.connect), [])
        checked, fetches = view['codex']['checked_at'], self.fetch.call_count
        self.assertFalse(onboarding.reevaluate_updates(self.connect))  # Still no evidence: unchanged.
        self.versions['codex'] = 'codex-cli 0.154.0'  # A later readiness check found the version.
        self.assertTrue(onboarding.reevaluate_updates(self.connect, ['codex']))
        view = onboarding.update_view(self.connect)
        self.assertEqual((view['codex']['status'], view['codex']['installed_version']), ('update_available', '0.154.0'))
        self.assertEqual(view['codex']['checked_at'], checked)  # When the metadata was read, not now.
        self.assertEqual(self.fetch.call_count, fetches)
        self.assertEqual([n['available_version'] for n in onboarding.notifications(self.connect)], ['0.155.0'])
        # A restarted backend reads the resolved comparison; a recent scheduled pass does not refetch.
        restarted = AgentStore(self.root / 'data' / 'onboarding.sqlite3')._connection
        self.assertEqual(onboarding.update_view(restarted)['codex']['status'], 'update_available')
        onboarding.check_updates(restarted, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, fetches)

    def test_a_recent_scheduled_pass_re_compares_an_unknown_result_instead_of_freezing_it(self):
        self.versions['claude'] = None
        self.installed_everywhere()
        self.probe_with({'claude': None})
        self.tags['claude'] = {'latest': '2.1.199'}
        self.assertEqual(onboarding.check_updates(self.connect, trigger='scheduled')['claude']['status'], 'unknown')
        fetches = self.fetch.call_count
        self.versions['claude'] = '2.1.198 (Claude Code)'
        view = onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(view['claude']['status'], 'update_available')
        self.assertEqual(self.fetch.call_count, fetches)  # Within the day: no registry read.

    def test_re_evaluation_never_overrides_a_newer_check_or_a_changed_channel(self):
        onboarding.check_updates(self.connect)
        rows = self.read(lambda db: {r['provider']: dict(r) for r in db.execute('SELECT * FROM provider_update_checks')})
        self.write(lambda db: db.execute("UPDATE provider_update_checks SET checked_at='2026-10-24T00:00:00.000Z' "
                                         "WHERE provider='codex'"))
        self.versions['codex'] = 'codex-cli 0.150.0'
        self.assertFalse(onboarding._reevaluate(self.connect, 'codex', rows['codex']))  # Raced by a newer check.
        self.versions['gemini'] = '0.31.0-preview.2'  # Now on the preview channel: needs fresh metadata.
        self.assertFalse(onboarding._reevaluate(self.connect, 'gemini', rows['gemini']))
        self.assertEqual(onboarding.update_view(self.connect)['gemini']['status'], 'up_to_date')

    def test_a_failed_metadata_read_is_retried_within_the_hour_not_the_day(self):
        self.fetch.side_effect = OSError('offline')
        onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 3)
        onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 3)  # Not hammered.
        self.write(lambda db: db.execute(
            "UPDATE provider_update_checks SET checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now','-2 hours')"))
        self.fetch.side_effect = lambda p: dict(self.tags[p])
        view = onboarding.check_updates(self.connect, trigger='scheduled')
        self.assertEqual(self.fetch.call_count, 6)
        self.assertEqual(view['codex']['status'], 'up_to_date')
        checks = []

        def failing_check(*args, **kwargs):
            checks.append(kwargs)
            return {'codex': {'status': 'check_failed'}}
        with patch.object(onboarding, 'RETRY_INTERVAL', 0.05), patch.object(onboarding, 'UPDATE_INTERVAL', 30), \
                patch.object(onboarding, 'check_updates', side_effect=failing_check), \
                patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': ''}):
            scheduler = onboarding.UpdateScheduler(self.connect, lambda: None, first_delay=0.01).start()
            deadline = time.monotonic() + 5
            while len(checks) < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
            scheduler.stop()
            scheduler.thread.join(5)
        self.assertGreaterEqual(len(checks), 3)  # The retry interval, not the daily one.

    def test_the_scheduler_runs_in_process_only_and_stops(self):
        calls = []
        with patch.object(onboarding, 'check_updates', side_effect=lambda *a, **k: calls.append(k)):
            with patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': ''}):
                scheduler = onboarding.UpdateScheduler(self.connect, lambda: calls.append('published'), first_delay=0.05).start()
                deadline = time.monotonic() + 5
                while len(calls) < 2 and time.monotonic() < deadline:
                    time.sleep(0.02)
                scheduler.stop()
                scheduler.thread.join(5)
            self.assertFalse(scheduler.thread.is_alive())
            self.assertEqual(calls[:2], [{'trigger': 'scheduled'}, 'published'])
            with patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': '1'}):
                self.assertIsNone(onboarding.UpdateScheduler(self.connect, lambda: None).start().thread)
        source = Path(onboarding.__file__).read_text(encoding='utf-8')
        for forbidden in ('schtasks', 'New-Service', 'sc.exe', 'Register-ScheduledTask'):
            self.assertNotIn(forbidden, source)


@unittest.skipUnless(os.name == 'nt', 'Windows process supervision')
class LaunchPermissionTests(unittest.TestCase):
    """The hard permission on real supervised fake-CLI launches: root, reply, resume and provider change."""
    rows = base.SupervisedFakeProviderTests.rows
    wait_status = base.SupervisedFakeProviderTests.wait_status

    def setUp(self):
        base.SupervisedFakeProviderTests.setUp(self)

    def tearDown(self):
        base.SupervisedFakeProviderTests.tearDown(self)

    def allow(self, *allowed):
        manager.set_provider_policy(allowed=list(allowed), expected_revision=manager.provider_policy_view()['revision'])

    def test_a_root_launch_is_refused_before_any_task_exists(self):
        self.allow('codex')
        tasks = len(self.rows('SELECT * FROM tasks'))
        with self.assertRaisesRegex(policy.ProviderNotPermitted, 'Claude Code is not in your allowed providers'):
            manager.start_agent('Summarize', 'claude', 'read-only')
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), tasks)
        self.assertEqual(fake.runs(self.log, 'claude'), [])
        mock = manager.start_agent('Simulate', 'mock')  # Mock is never restricted or substituted.
        self.assertEqual(manager.agent_types[mock], 'mock')

    def test_replies_and_resumes_stay_within_the_task_restriction(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            agent_id = manager.start_agent('Change a file', 'claude', 'workspace-write')
            self.assertEqual(self.wait_status(agent_id, 'waiting'), 'waiting')
        task_id = manager._agent_task_id(agent_id)
        runs = len(fake.runs(self.log, 'claude'))
        manager.set_task_provider_restrictions(task_id, ['codex'], expected_revision=None)
        view = manager.get_agent(agent_id, include_output=False)
        self.assertFalse(view['provider_permission']['allowed'])
        self.assertIn('provider restriction on Task', view['provider_permission']['reason'])
        with self.assertRaisesRegex(policy.ProviderNotPermitted, 'not allowed for this Task'):
            manager.reply_agent(agent_id, 'source.txt')
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs)  # No process started.
        self.assertEqual(manager.agent_statuses[agent_id], 'waiting')  # Work and session kept.
        own = manager.task_provider_restrictions(task_id)['own']
        manager.clear_task_provider_restrictions(task_id, expected_revision=own['revision'])
        manager.reply_agent(agent_id, 'source.txt')
        self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')

    def test_a_provider_change_cannot_leave_the_permissions(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            agent_id = manager.start_agent('Document the parser', 'claude', 'workspace-write')
            self.wait_status(agent_id, 'waiting')
        self.allow('claude', 'codex')
        with self.assertRaisesRegex(policy.ProviderNotPermitted, 'Gemini CLI is not in your allowed providers'):
            manager.change_agent_provider(agent_id, provider='gemini', sandbox='read-only')
        manager.set_task_provider_restrictions(manager._agent_task_id(agent_id), ['claude'], expected_revision=None)
        with self.assertRaisesRegex(policy.ProviderNotPermitted, 'not allowed for this Task'):
            manager.change_agent_provider(agent_id, provider='codex', sandbox='read-only')
        self.assertEqual(manager.agent_types[agent_id], 'claude')  # Unchanged.

    def test_the_final_admission_enforces_restrictions_of_deep_ancestors(self):
        # Review finding: the authoritative check walks the whole ancestry (here 70 levels).
        self.allow('codex', 'claude')
        top = parent = manager.create_task('Top', 'Top work')['task_id']
        for index in range(70):
            parent = manager._store.create_task(f'Level {index}', title=f'Level {index}', parent_task_id=parent)['task_id']
        manager.set_task_provider_restrictions(top, ['codex'], expected_revision=None)
        original = manager._spawn_with_resources

        def placed_deep_after_the_early_checks(task_id, *args, **kwargs):
            with manager._store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE tasks SET parent_task_id=? WHERE task_id=?', (parent, task_id))
            return original(task_id, *args, **kwargs)
        with patch.object(manager, '_spawn_with_resources', side_effect=placed_deep_after_the_early_checks):
            with self.assertRaisesRegex(policy.ProviderNotPermitted, 'restriction on Task "Top" excludes it'):
                manager.start_agent('Work', 'claude', 'read-only')
        self.assertEqual(fake.runs(self.log, 'claude'), [])  # No process was created.

    def test_a_commands_task_scope_never_leaks_into_a_later_launch(self):
        agent_id = manager.start_agent('Simulate', 'mock')
        with patch.object(manager, '_agent_command_for', side_effect=RuntimeError('failed before any check')):
            with self.assertRaises(RuntimeError):
                manager._agent_command(agent_id)
        self.assertIsNone(getattr(manager._launch, 'policy_task_id', None))
        self.allow('codex', 'claude')
        claude = manager.start_agent('Root work', 'claude', 'read-only')  # Checked against no stale Task.
        self.assertEqual(self.wait_status(claude, 'finished'), 'finished')

    def test_a_child_is_refused_for_its_inherited_restriction_before_readiness_or_usage(self):
        # Browser E2E finding: a quota refusal was reported for a Child the Task restriction forbids.
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='waiting'):
            parent = manager.start_agent('Plan the parser', 'claude', 'workspace-write')
            self.assertEqual(self.wait_status(parent, 'waiting'), 'waiting')
        manager.set_task_provider_restrictions(manager._agent_task_id(parent), ['codex'], expected_revision=None)
        tasks, runs = len(self.rows('SELECT * FROM tasks')), len(fake.runs(self.log, 'claude'))
        limited = providers.ProviderRefused('claude', 'usage_limited', 'Session limit.')
        with patch.object(providers, 'require_launchable', side_effect=limited):
            with self.assertRaisesRegex(policy.ProviderNotPermitted, 'not allowed for this Task'):
                manager.start_agent('Child work', 'claude', 'read-only', parent_id=parent)
        self.assertEqual(len(self.rows('SELECT * FROM tasks')), tasks)  # No Child Task was created.
        self.assertEqual(len(fake.runs(self.log, 'claude')), runs)
        self.assertIsNone(getattr(manager._launch, 'policy_task_id', None))  # The scope never leaks.
        codex = manager.start_agent('Child work', 'codex', 'read-only', parent_id=parent)  # Permitted: unchanged.
        self.assertEqual(self.wait_status(codex, 'finished'), 'finished')

    def test_revoking_a_permission_never_stops_running_work(self):
        with patch.dict(os.environ, FAKE_CLAUDE_SCENARIO='slow', FAKE_SLOW_SECONDS='2'):
            agent_id = manager.start_agent('Long work', 'claude', 'read-only')
            self.allow('codex')
            self.assertEqual(self.wait_status(agent_id, 'finished'), 'finished')


class ApiTests(Store, unittest.TestCase):
    def setUp(self):
        self.make_store()
        self.enterContext(patch.object(manager, '_store', self.store))
        self.enterContext(patch.object(manager, '_emit_agent_change'))
        self.enterContext(patch.dict(os.environ, {'CONTROL_CENTER_DISABLE_UPDATE_CHECKS': '1'}))

    def request(self, method, path, body=None):
        status, _, payload = call(method, path, headers(content_type='application/json' if body is not None else None),
                                  json.dumps(body).encode() if body is not None else b'')
        return status, json.loads(payload) if payload else None

    def test_policy_routes_require_the_loaded_revision(self):
        status, current = self.request('GET', '/provider-policy')
        self.assertEqual(status, 200)
        status, _ = self.request('PUT', '/provider-policy', {'allowed': ['codex']})
        self.assertEqual(status, 422)  # expected_revision is required.
        status, saved = self.request('PUT', '/provider-policy', {'allowed': ['codex', 'claude'], 'expected_revision': current['revision']})
        self.assertEqual((status, saved['allowed']), (200, ['codex', 'claude']))
        status, body = self.request('PUT', '/provider-policy', {'allowed': [], 'expected_revision': current['revision']})
        self.assertEqual(status, 409)
        self.assertIn('changed since', body['detail'])
        status, _ = self.request('PUT', '/provider-policy', {'allowed': ['mock'], 'expected_revision': saved['revision']})
        self.assertEqual(status, 422)

    def test_task_restriction_routes(self):
        task = self.store.create_task('Work', title='Work')['task_id']
        status, _ = self.request('PUT', f'/tasks/{task}/provider-restrictions', {'providers': ['codex']})
        self.assertEqual(status, 428)
        status, view = self.request('PUT', f'/tasks/{task}/provider-restrictions', {'providers': ['codex'], 'expected_revision': None})
        self.assertEqual((status, view['effective']), (200, ['codex']))
        status, _ = self.request('DELETE', f'/tasks/{task}/provider-restrictions')
        self.assertEqual(status, 428)
        status, view = self.request('DELETE', f'/tasks/{task}/provider-restrictions?expected_revision={view["own"]["revision"]}')
        self.assertEqual((status, view['own']), (200, None))
        status, _ = self.request('GET', '/tasks/missing/provider-restrictions')
        self.assertEqual(status, 404)

    def test_operation_routes_accept_no_command_path_or_package(self):
        for body in ({'action': 'install', 'command': 'calc.exe'}, {'action': 'install', 'package': 'evil'},
                     {'action': 'install', 'executable': 'C:/x.exe'}, {'action': 'run'}, {'action': 'install', 'channel': 'beta'}):
            status, _ = self.request('POST', '/providers/codex/operations/plan', body)
            self.assertEqual(status, 422, body)
        status, _ = self.request('POST', '/providers/unknown/operations/plan', {'action': 'install'})
        self.assertEqual(status, 404)
        status, _ = self.request('POST', '/providers/codex/operations', {'plan_id': 'p', 'digest': 'd', 'approved': False})
        self.assertEqual(status, 422)
        status, _ = self.request('POST', '/providers/codex/operations', {'plan_id': 'p', 'digest': 'd', 'approved': 'yes'})
        self.assertEqual(status, 422)
        status, body = self.request('POST', '/providers/codex/operations', {'plan_id': 'p', 'digest': 'd', 'approved': True})
        self.assertEqual(status, 409)
        self.assertIn('unknown, expired or already used', body['detail'])
        status, _ = self.request('POST', '/providers/codex/notifications/dismiss', {'version': '1.0.0', 'mode': 'forever'})
        self.assertEqual(status, 422)
        status, _ = self.request('GET', '/provider-operations/missing')
        self.assertEqual(status, 404)

    def test_onboarding_view_probes_nothing(self):
        with patch.object(providers, 'probe', side_effect=AssertionError('probed')), \
                patch.object(providers, 'run_probe', side_effect=AssertionError('ran a command')):
            status, view = self.request('GET', '/providers/onboarding')
        self.assertEqual(status, 200)
        self.assertEqual([entry['provider'] for entry in view['providers']], list(providers.IDS))
        self.assertEqual(sorted(view['updates']), sorted(providers.REAL))
        self.assertIn('allowed', view['policy'])


if __name__ == '__main__':
    unittest.main()
