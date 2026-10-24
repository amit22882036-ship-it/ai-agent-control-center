"""Stage 2G provider onboarding: installation discovery, approved setup operations, official
sign-in and sign-out, update checks and update notifications.

This is not a shell API. Every operation is defined here as a fixed argument list for one
provider, action and installation method; the UI chooses only (provider, action, channel)
and confirms a reviewed plan whose digest binds the approval to that exact operation.

- Executables come only from trusted locations: the provider adapters' pinned paths and the
  Node.js installer's npm (`%ProgramFiles%\\nodejs\\npm.cmd`). A CLI found elsewhere is
  reported, never executed. Remote install scripts (curl/irm piped to a shell) are never run;
  those paths are manual steps with the official documentation.
- Maintenance and execution exclude each other per provider: an operation starts only when no
  execution of the provider is open or uncertain (checked again in its own write transaction),
  and the final Agent admission refuses a launch while an operation is running or has an
  unverified end (`require_no_maintenance`).
- Each operation runs in its own Windows Job Object (process_supervision.spawn: created
  suspended, assigned, verified, resumed only after the containment is recorded), with kill on
  close and no breakaway. Timeout, cancellation and shutdown end the whole tree and wait for the
  Job to report no process. After a backend crash, Windows ends the tree when the Job's last
  handle closes; recovery accepts that only when the owning backend is verifiably gone and the
  Job no longer exists or is empty. Otherwise the operation stays `uncertain` and keeps blocking
  the provider until the user confirms it ended.
- npm runs with a minimal environment built for the operation (no NODE_OPTIONS, npm_config_*
  or other loader overrides), from an empty working directory, with the official registry,
  empty user and global config files, the pinned global prefix and no lifecycle scripts. The
  channel is resolved to an exact version before approval; that version is what runs and what
  the result is checked against. Success is reported only from the exit status and a fresh
  probe afterwards.
- Sign-in uses each CLI's official command in its own console window, so the user interacts
  with the provider directly; nothing reads, stores or forwards credentials. An exited login
  window is not success: readiness is probed again.
- Update checks read the npm registry's dist-tags (an allowlisted, machine-readable source)
  at most about once a day, coalesced and bounded, and never execute anything they return.
"""
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Event, Lock, Thread
import time
import urllib.request
from uuid import uuid4

from . import executions, model_catalog, process_supervision as ps, providers

logger = logging.getLogger(__name__)
NOW = providers.NOW
ACTIONS = ('install', 'update', 'login', 'logout')
CHANNELS = {'codex': ('latest',), 'claude': ('latest', 'stable'), 'gemini': ('latest', 'preview', 'nightly')}
PACKAGES = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code', 'gemini': '@google/gemini-cli'}
REGISTRY = 'https://registry.npmjs.org/-/package/{package}/dist-tags'
NPM_REGISTRY = 'https://registry.npmjs.org/'
EMPTY_CONFIG = '<empty-config>'  # Replaced at run time by an empty file in the operation's own folder.
BLOCKING = ('running', 'uncertain')  # Statuses that hold the provider's maintenance gate.
# The only variables an npm installation inherits (plus a PATH built here). Everything else,
# including NODE_OPTIONS, NODE_PATH, NODE_EXTRA_CA_CERTS and every npm_config_* override, is dropped.
INSTALLER_ENV = ('SystemRoot', 'SystemDrive', 'windir', 'ComSpec', 'PATHEXT', 'TEMP', 'TMP', 'USERPROFILE', 'HOMEDRIVE',
                 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432', 'ProgramData',
                 'CommonProgramFiles', 'CommonProgramFiles(x86)', 'CommonProgramW6432', 'ALLUSERSPROFILE', 'PUBLIC',
                 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
                 'PROCESSOR_IDENTIFIER', 'OS', 'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY')
# Removed from a provider CLI's own environment for sign-in, sign-out and its native updater.
LOADER_OVERRIDES = re.compile(r'(?i)^(NODE_OPTIONS|NODE_PATH|NODE_EXTRA_CA_CERTS|NODE_TLS_REJECT_UNAUTHORIZED|NODE_REPL_.*|'
                              r'NPM_CONFIG_.*|ELECTRON_RUN_AS_NODE)$')
SAFE_PATH = re.compile(r'[A-Za-z]:\\[^"%&|<>^!\r\n]*')  # Passed through cmd.exe: no metacharacters.
SOURCES = {
    'codex': 'https://developers.openai.com/codex/cli',
    'claude': 'https://code.claude.com/docs/en/setup',
    'gemini': 'https://github.com/google-gemini/gemini-cli',
}
AUTH_DOCS = {
    'codex': 'https://developers.openai.com/codex/auth',
    'claude': 'https://code.claude.com/docs/en/authentication',
    'gemini': 'https://geminicli.com/docs/get-started/authentication/',
}
PLAN_SECONDS = 600
OPERATION_TIMEOUT = {'install': 900, 'update': 900, 'login': 900, 'logout': 120}
OUTPUT_LIMIT = 32768
UPDATE_INTERVAL = 24 * 3600
RETRY_INTERVAL = 3600  # A failed release-metadata read is retried sooner than a successful one.
MANUAL_CHECK_INTERVAL = 60
FETCH_TIMEOUT = 10
FETCH_LIMIT = 65536
# A Semantic Versioning core with optional prerelease (and ignored build metadata), standing alone
# in a CLI's version text such as "codex-cli 0.154.0" or "2.1.198 (Claude Code)".
VERSION = re.compile(r'(?<![\w.])(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)'
                     r'(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?![\w.+-])')
EXACT_VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]{1,64})?')
# Sign-in windows are real consoles so the user interacts with the provider directly.
CONSOLE_FLAGS = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0)
SECRET = re.compile(r'(?i)(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|eyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9._-]+|'
                    r'(?:token|secret|password|api[_-]?key|authorization)\s*[:=]\s*\S+|https?://\S*[?&](?:code|token|state)=\S+)')


OPERATIONS_TABLE = '''CREATE TABLE IF NOT EXISTS provider_operations (
        operation_id TEXT PRIMARY KEY,
        provider TEXT NOT NULL CHECK(provider IN ('codex','claude','gemini')),
        action TEXT NOT NULL CHECK(action IN ('install','update','login','logout')),
        method TEXT NOT NULL, command TEXT NOT NULL, plan_digest TEXT NOT NULL, target_version TEXT,
        status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','canceled','interrupted','needs_attention',
                                               'uncertain')),
        controller_instance_id TEXT,
        containment TEXT NOT NULL DEFAULT 'pending' CHECK(containment IN ('pending','job_kill_on_close')),
        job_name TEXT, root_pid INTEGER, root_created_at TEXT, evidence TEXT NOT NULL DEFAULT '{}',
        before_version TEXT, after_version TEXT, exit_code INTEGER, output_tail TEXT, result TEXT,
        requested_by TEXT NOT NULL CHECK(requested_by='user'),
        started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), finished_at TEXT)'''


def migrate_v37(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(provider_operations)')}
    if columns and 'containment' not in columns:
        # An earlier draft of this table: rebuild it with the supervision columns, keeping every row.
        db.execute('DROP INDEX IF EXISTS provider_operation_running')
        db.execute('ALTER TABLE provider_operations RENAME TO provider_operations_draft')
        db.execute(OPERATIONS_TABLE)
        keep = ('operation_id,provider,action,method,command,plan_digest,status,before_version,after_version,exit_code,'
                'output_tail,result,requested_by,started_at,finished_at')
        db.execute(f'INSERT INTO provider_operations({keep}) SELECT {keep} FROM provider_operations_draft')
        db.execute('DROP TABLE provider_operations_draft')
    db.execute(OPERATIONS_TABLE)
    # One operation per provider holds the maintenance gate (running, or ended without verification).
    db.execute("DROP INDEX IF EXISTS provider_operation_running")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS provider_operation_blocking ON provider_operations(provider)
        WHERE status IN ('running','uncertain')""")
    db.execute('''CREATE TABLE IF NOT EXISTS provider_update_checks (
        provider TEXT PRIMARY KEY CHECK(provider IN ('codex','claude','gemini')),
        status TEXT NOT NULL, installed_version TEXT, channel TEXT, latest_version TEXT, method TEXT,
        source TEXT, error TEXT, checked_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS provider_notification_state (
        provider TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('dismissed','snoozed')), until TEXT,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY(provider, kind))''')


def recover(db):
    """Startup: decide what an operation left running by a previous backend means.

    Its tree ended only when that backend is verifiably gone and its kill-on-close Job no longer
    exists or is empty; the operation is then `interrupted` (outcome unknown, tree ended).
    Anything else is `uncertain`: it keeps blocking the provider until the user confirms.
    """
    if not providers._has(db, 'provider_operations'):
        return
    for row in db.execute("SELECT * FROM provider_operations WHERE status='running'").fetchall():
        ended, evidence = _tree_ended(db, dict(row))
        if ended is None:
            continue  # Its backend is still running: that backend owns it.
        status = 'interrupted' if ended else 'uncertain'
        result = ('The backend stopped while this operation was running. Windows ended its process tree; its outcome '
                  'is unknown, so check the provider again.' if ended else
                  'The backend stopped while this operation was running and the end of its processes could not be '
                  'verified. New work and setup for this provider stay blocked until you confirm the setup window '
                  'and installer have ended.')
        db.execute(f"""UPDATE provider_operations SET status=?,result=?,evidence=?,finished_at={NOW}
            WHERE operation_id=? AND status='running'""",
            (status, result, json.dumps({**_evidence(row), 'recovery': evidence}), row['operation_id']))


def _evidence(row):
    try:
        return json.loads(row['evidence'] or '{}')
    except ValueError:
        return {}


def _tree_ended(db, row):
    """(True: verified ended | False: unknown | None: still owned by a live backend, evidence)."""
    evidence = {'checked_at': providers._now()}
    controller = executions.controller(db, row['controller_instance_id']) if row.get('controller_instance_id') else None
    owner = ps.process_status(controller['pid'], controller['created_at']) if controller else 'unknown'
    evidence['owner'] = owner
    if owner == 'alive':
        return None, evidence
    if owner != 'gone' or not row.get('job_name'):
        return False, evidence
    try:
        job = ps.Job.open_existing(row['job_name'], terminate=False)
    except OSError as exc:
        evidence['job'] = f'unreadable: {str(exc)[:200]}'
        return False, evidence
    if job is None:
        evidence['job'] = 'gone'  # No handle and no process remains: kill on close ended the tree.
        return True, evidence
    try:
        active = job.active_processes()
    except OSError as exc:
        evidence['job'] = f'unreadable: {str(exc)[:200]}'
        return False, evidence
    finally:
        job.close()  # Query access only: closing it terminates nothing.
    evidence['job'] = f'{active} active process(es)'
    return active == 0, evidence


def resolve(connect, operation_id, *, confirm_ended):
    """The user's decision on an `uncertain` operation. The Job is checked first: a tree that
    is verifiably still running is never released, whatever is confirmed."""
    if confirm_ended is not True:
        raise ValueError('Confirm that the setup window and installer have ended.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM provider_operations WHERE operation_id=?', (operation_id,)).fetchone()
        if row is None:
            raise LookupError('Operation not found')
        if row['status'] != 'uncertain':
            raise ValueError('Only an operation whose end could not be verified needs this confirmation.')
        job = None
        try:
            job = ps.Job.open_existing(row['job_name'], terminate=False) if row['job_name'] else None
            active = job.active_processes() if job is not None else 0
        except OSError:
            active = None  # Unreadable: the user's confirmation decides.
        finally:
            if job is not None:
                job.close()
        if active:
            raise ValueError(f'The operation still has {active} running process(es) in its Job; close the setup window '
                             'or wait for it, then confirm again.')
        db.execute(f"""UPDATE provider_operations SET status='interrupted',evidence=?,finished_at=COALESCE(finished_at,{NOW}),
            result=? WHERE operation_id=? AND status='uncertain'""",
            (json.dumps({**_evidence(row), 'resolution': {'by': 'user', 'job_active_processes': active,
                                                          'at': providers._now()}}),
             'You confirmed the operation ended. Its outcome is unknown, so check the provider again.', operation_id))
    providers.invalidate(row['provider'])
    return operation(connect, operation_id)


def blocking_operation(db, provider):
    """The operation holding this provider's maintenance gate, if any."""
    if db is None or not providers._has(db, 'provider_operations'):
        return None
    marks = ','.join('?' for _ in BLOCKING)
    row = db.execute(f'SELECT * FROM provider_operations WHERE provider=? AND status IN ({marks})',
                     (provider, *BLOCKING)).fetchone()
    return dict(row) if row else None


def require_no_maintenance(db, provider):
    """Execution side of the mutual exclusion (inside the caller's admission transaction)."""
    if provider not in providers.REAL:
        return
    current = blocking_operation(db, provider)
    if current is None:
        return
    label = providers.LABELS[provider]
    if current['status'] == 'running':
        reason = (f'A {label} {current["action"]} is running in AI Providers. New {label} work starts after it finishes; '
                  'nothing was started.')
    else:
        reason = (f'A {label} {current["action"]} ended without verification. Confirm in AI Providers that it ended '
                  'before new work of this provider starts; nothing was started.')
    raise providers.ProviderRefused(provider, 'maintenance_in_progress', reason)


# ---------------------------------------------------------------- discovery (never executes)

def _clause(found):
    """A probe's reason for use inside a sentence (its own final period dropped)."""
    return (found.reason or found.state).rstrip('.')


def _env(name):
    value = os.environ.get(name)
    return Path(value) if value else None


def npm_path():
    """npm from the official Node.js installer location only (never a PATH lookup)."""
    for base in (_env('ProgramFiles'), _env('ProgramFiles(x86)')):
        if base is not None and (base / 'nodejs' / 'npm.cmd').is_file():
            return base / 'nodejs' / 'npm.cmd'
    return None


def method_for(provider, location):
    if location is None:
        return None
    appdata = _env('APPDATA')
    if appdata is not None and location.parent == appdata / 'npm':
        return 'npm'
    profile = _env('USERPROFILE')
    if provider == 'claude' and profile is not None and location == profile / '.local' / 'bin' / 'claude.exe':
        return 'native'
    return 'unknown'


def _elsewhere(provider, trusted):
    """Installations outside the trusted locations: reported, never executed or trusted."""
    names = {'codex': ('codex',), 'claude': ('claude',), 'gemini': ('gemini',)}[provider]
    found = set()
    for name in names:
        hit = shutil.which(name)
        if hit:
            found.add(str(Path(hit)))
    local = _env('LOCALAPPDATA')
    if provider == 'claude' and local is not None and (local / 'Microsoft' / 'WinGet' / 'Links' / 'claude.exe').is_file():
        found.add(str(local / 'Microsoft' / 'WinGet' / 'Links' / 'claude.exe'))
    return sorted(p for p in found if trusted is None or Path(p) != trusted)


def discover(provider):
    item = providers.adapter(provider)
    location = item.locate()
    cached = providers.cached(provider)
    elsewhere = _elsewhere(provider, location) if os.name == 'nt' else []
    if location is not None:
        state = 'installed'
    elif elsewhere:
        state = 'unsupported_location'
    else:
        state = 'not_installed'
    return dict(provider=provider, state=state, location=str(location) if location else None,
                method=method_for(provider, location), version=cached.version if cached else None,
                elsewhere=elsewhere, prerequisites=dict(npm=str(npm_path()) if npm_path() else None),
                os_supported=os.name == 'nt')


# ---------------------------------------------------------------- operation plans

@dataclass
class Plan:
    provider: str
    action: str
    method: str
    executable: str | None
    arguments: list
    current_version: str | None
    target: str | None
    console: bool = False
    target_version: str | None = None  # The exact version an npm installation is bound to.
    manual: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    changes: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    blockers: list = field(default_factory=list)

    def digest(self):
        return sha256(json.dumps([self.provider, self.action, self.method, self.executable, self.arguments,
                                  self.current_version, self.target, self.target_version], default=str).encode()).hexdigest()

    def display(self):
        if not self.executable:
            return None
        return [Path(self.executable).name, *self.arguments]


_plans: dict[str, tuple] = {}  # plan_id -> (digest, expires, provider, action, channel); used once.
_plans_lock = Lock()


def _version_text(provider):
    found = providers.cached(provider)
    return found.version if found else None


def _channel(provider, version_text, method):
    if provider == 'gemini':
        if version_text and 'nightly' in version_text:
            return 'nightly'
        if version_text and 'preview' in version_text:
            return 'preview'
        return 'latest'
    if provider == 'claude' and method == 'native':
        folder = os.environ.get('CLAUDE_CONFIG_DIR')
        path = Path(folder) / 'settings.json' if folder else (_env('USERPROFILE') or Path.home()) / '.claude' / 'settings.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        except (OSError, ValueError):
            data = {}
        channel = data.get('autoUpdatesChannel') if isinstance(data, dict) else None
        return channel if channel in ('latest', 'stable') else 'latest'
    return 'latest'


def active_work(db, provider):
    """Open or uncertain executions of this provider (authoritative journal, not UI state)."""
    if db is None or not providers._has(db, 'executions'):
        return []
    marks = ','.join('?' for _ in executions.OPEN)
    rows = db.execute(f'''SELECT e.execution_id,e.agent_id,e.state,g.display_name FROM executions e
        LEFT JOIN agents g USING(agent_id) WHERE e.provider=? AND e.state IN ({marks}) ORDER BY e.rowid''',
        (provider, *executions.OPEN)).fetchall()
    return [dict(execution_id=r['execution_id'], agent_id=r['agent_id'], state=r['state'],
                 name=r['display_name'] or r['agent_id']) for r in rows]


def npm_arguments(provider, version):
    """A global npm installation bound to one exact version, the official registry, empty user
    and global configuration, the pinned prefix and no lifecycle scripts. Command-line options
    take precedence over every npmrc file and environment setting."""
    prefix = (_env('APPDATA') or Path()) / 'npm'
    return ['install', '--global', f'{PACKAGES[provider]}@{version}', f'--prefix={prefix}', f'--registry={NPM_REGISTRY}',
            f'--userconfig={EMPTY_CONFIG}', f'--globalconfig={EMPTY_CONFIG}', '--ignore-scripts', '--strict-ssl=true',
            '--no-audit', '--no-fund', '--update-notifier=false']


def _resolve(plan, provider, channel, resolved):
    """The exact version an npm install or update will install (or a blocker)."""
    if resolved is not None:
        return resolved  # The version the user reviewed and approved.
    try:
        version = fetch_dist_tags(provider).get(channel)
    except (OSError, ValueError) as exc:
        plan.blockers.append(f'The "{channel}" release of {PACKAGES[provider]} could not be resolved from the npm registry '
                             f'({type(exc).__name__}), so the exact version cannot be shown for approval. Check the '
                             'connection and review again, or install manually from the official page.')
        return None
    if not isinstance(version, str) or not EXACT_VERSION.fullmatch(version):
        plan.blockers.append(f'The npm registry lists no "{channel}" release of {PACKAGES[provider]}.')
        return None
    return version


def build_plan(db, provider, action, channel=None, resolved=None):
    """The exact operation an approval would authorize (validated, nothing runs). `resolved` is
    the exact version approved with a plan, so a rebuild checks the same operation."""
    if provider not in providers.REAL:
        raise ValueError('Setup operations exist only for Codex, Claude Code and Gemini CLI.')
    if action not in ACTIONS:
        raise ValueError('Unknown setup operation')
    if channel is not None and channel not in CHANNELS[provider]:
        raise ValueError(f'Unknown release channel for {providers.LABELS[provider]}: use ' + ', '.join(CHANNELS[provider]))
    found = discover(provider)
    location = Path(found['location']) if found['location'] else None
    method = found['method'] or 'npm'
    version = _version_text(provider)
    npm = npm_path()
    label = providers.LABELS[provider]
    plan = Plan(provider, action, method, None, [], version, None)
    if action == 'install':
        if location is not None:
            plan.blockers.append(f'{label} is already installed; use Update instead.')
        if found['state'] == 'unsupported_location':
            plan.blockers.append(f'{label} is installed at {", ".join(found["elsewhere"])}, which the Control Center does '
                                 'not run. Reinstall it with npm (below), or follow the manual steps.')
        target = channel or 'latest'
        plan.method, plan.target = 'npm', target
        plan.dependencies = ['Node.js with npm (official installer location)']
        plan.changes = [f'Installs the {PACKAGES[provider]} npm package globally for your Windows user '
                        '(%APPDATA%\\npm) from the official npm registry, without running package scripts. No '
                        'administrator rights are requested.']
        if npm is None:
            plan.blockers.append('npm was not found at the Node.js installer location (%ProgramFiles%\\nodejs). Install '
                                 'Node.js from nodejs.org, then check again, or install the provider manually.')
        elif not plan.blockers:
            plan.target_version = _resolve(plan, provider, target, resolved)
            if plan.target_version:
                plan.executable, plan.arguments = str(npm), npm_arguments(provider, plan.target_version)
        if provider == 'claude':
            plan.manual = ['Claude Code also offers a native installer (recommended by Anthropic) and WinGet. Their '
                           'commands download and run an installer script, which the Control Center never runs for you: '
                           'follow the official setup page in a terminal, then choose Check again.']
            plan.risks = ['Claude Code requires a Pro, Max, Team, Enterprise or Console account.']
        if provider == 'gemini':
            plan.risks = ['Gemini CLI for unpaid and Google One accounts was replaced by Antigravity CLI on 2026-06-18; '
                          'only paid Code Assist sign-ins can use it.']
    elif action == 'update':
        if location is None:
            plan.blockers.append(f'{label} is not installed; use Install.')
        target = channel or _channel(provider, version, method)
        plan.target = target
        if method == 'npm':
            plan.dependencies = ['Node.js with npm (official installer location)']
            plan.changes = [f'Replaces the globally installed {PACKAGES[provider]} package with its "{target}" release from '
                            'the official npm registry, without running package scripts.']
            if npm is None:
                plan.blockers.append('npm was not found at the Node.js installer location; update manually.')
            elif location is not None:
                plan.target_version = _resolve(plan, provider, target, resolved)
                if plan.target_version:
                    plan.executable, plan.arguments = str(npm), npm_arguments(provider, plan.target_version)
        elif method == 'native' and provider == 'claude':
            plan.changes = ['Runs `claude update`, Claude Code\'s own updater for native installations, on your '
                            'configured release channel. That updater chooses and verifies the version.']
            plan.executable, plan.arguments = str(location), ['update']
            plan.risks = ['Native installations also update themselves in the background; this only applies an '
                          'update now.']
        else:
            plan.blockers.append(f'The installation method of {label} is not recognized; update it manually.')
        if provider == 'gemini' and channel is None and target != 'latest':
            plan.risks.append(f'This installation follows the {target} channel; it stays on it.')
    elif action == 'login':
        if location is None:
            plan.blockers.append(f'{label} is not installed.')
        if provider == 'codex':
            plan.executable, plan.arguments, plan.console = str(location) if location else None, ['login'], True
            plan.changes = ['Opens Codex\'s own sign-in in a new window and your browser (Sign in with ChatGPT).']
        elif provider == 'claude':
            plan.executable, plan.arguments, plan.console = str(location) if location else None, ['auth', 'login', '--claudeai'], True
            plan.changes = ['Opens Claude Code\'s own sign-in for your Claude subscription in a new window and your browser.']
        else:
            plan.manual = ['Gemini CLI has no separate sign-in command. Open a terminal, run `gemini`, choose Sign in with '
                           'Google and finish in your browser, then exit Gemini CLI with /quit and choose Check again. '
                           'Do not choose API key or Vertex AI: those routes are not approved.']
        plan.method, plan.target = 'official_cli', None
        plan.risks = ['You sign in directly with the provider; the Control Center never sees or stores credentials.',
                      'Use your subscription sign-in; API-key and paid-API routes are refused afterwards.']
        known = providers.cached(provider)
        if known is not None and known.authenticated:
            plan.risks.append(f'{label} is already signed in. Signing in again may switch to another account: the model '
                              'list and usage are then treated as unverified until checked for the new account, and '
                              'waiting agents resume only when you choose.')
    else:
        if location is None:
            plan.blockers.append(f'{label} is not installed.')
        if provider == 'codex':
            plan.executable, plan.arguments = str(location) if location else None, ['logout']
        elif provider == 'claude':
            plan.executable, plan.arguments = str(location) if location else None, ['auth', 'logout']
        else:
            plan.manual = ['Gemini CLI has no separate sign-out command: use /auth inside Gemini CLI to change the '
                           'sign-in, then choose Check again.']
        plan.method, plan.target = 'official_cli', None
        plan.changes = [f'Signs {label} out on this computer. New agents of this provider cannot start until you sign '
                        'in again; waiting agents keep their state and resume only when you choose.']
    busy = active_work(db, provider)  # Every action, installation included.
    if busy:
        plan.blockers.append(f'{len(busy)} {label} execution(s) are running or have an unverified end: ' +
                             ', '.join(b['name'] for b in busy[:5]) + '. Wait for them to finish or stop them with '
                             'the agent controls; nothing is stopped automatically.')
    current = blocking_operation(db, provider)
    if current and current['status'] == 'running':
        plan.blockers.append(f'Another {label} setup operation is already running.')
    elif current:
        plan.blockers.append(f'An earlier {label} {current["action"]} ended without verification. Confirm that it ended '
                             'before starting another.')
    for value in (npm, _env('APPDATA'), Path(tempfile.gettempdir())):
        if plan.executable and value is not None and not SAFE_PATH.fullmatch(str(value)):
            plan.blockers.append(f'The path {value} contains characters that cannot be passed safely; install manually.')
    if not found['os_supported']:
        plan.blockers.append('Provider setup is supported on Windows only.')
    if not plan.executable and not plan.blockers and not plan.manual:
        plan.blockers.append('This operation has no supported automated path; follow the manual steps.')
    return plan


def plan_view(plan, plan_id=None):
    return dict(plan_id=plan_id, provider=plan.provider, action=plan.action, method=plan.method,
                command=plan.display(), current_version=plan.current_version, target=plan.target,
                target_version=plan.target_version, package=PACKAGES[plan.provider] if plan.method == 'npm' else None,
                registry=NPM_REGISTRY if plan.method == 'npm' else None,
                source=SOURCES[plan.provider] if plan.action in ('install', 'update') else AUTH_DOCS[plan.provider],
                dependencies=plan.dependencies, changes=plan.changes, risks=plan.risks, blockers=plan.blockers,
                manual=plan.manual, console=plan.console, digest=plan.digest() if plan_id else None,
                executable=bool(plan.executable), expires_in=PLAN_SECONDS if plan_id else None)


def propose(db, provider, action, channel=None):
    """A reviewed plan with a one-time identity. Approval must quote its digest."""
    plan = build_plan(db, provider, action, channel)
    plan_id = None
    if plan.executable and not plan.blockers:
        plan_id = str(uuid4())
        with _plans_lock:
            now = time.monotonic()
            for key in [k for k, entry in _plans.items() if entry[1] < now]:
                _plans.pop(key, None)
            _plans[plan_id] = (plan.digest(), now + PLAN_SECONDS, provider, action, channel, plan.target_version)
    return plan_view(plan, plan_id)


# ---------------------------------------------------------------- running an approved operation

_running: dict = {}  # operation_id -> {'launch', 'canceled', 'done'}
_running_lock = Lock()
_stopping = Event()
_default_controller = None


def redact(text):
    return SECRET.sub('[redacted]', text or '')


def _controller(controller):
    global _default_controller
    if controller is not None:
        return controller
    if _default_controller is None:
        _default_controller = {**ps.controller_identity(), 'instance_id': str(uuid4())}
    return _default_controller


def start(connect, provider, plan_id, digest, publish=lambda: None, controller=None):
    """Run the approved plan if it still describes exactly the same operation."""
    with _plans_lock:
        entry = _plans.pop(plan_id, None) if isinstance(plan_id, str) else None
    if entry is None or entry[2] != provider:
        raise ValueError('This approval is unknown, expired or already used; review the operation again.')
    expected, expires, _, action, channel, resolved = entry
    if time.monotonic() > expires:
        raise ValueError('This approval expired; review the operation again.')
    if digest != expected:
        raise ValueError('The approval does not match the reviewed operation; review it again.')
    if _stopping.is_set():
        raise ValueError('The backend is stopping; no setup operation starts now.')
    with connect() as db:
        plan = build_plan(db, provider, action, channel, resolved)
    if plan.digest() != expected or plan.blockers or not plan.executable:
        reasons = '; '.join(line.rstrip('.') for line in plan.blockers) or 'the operation changed since it was reviewed'
        raise ValueError(f'The operation can no longer run as approved ({reasons}). Review it again.')
    operation_id, job_name, owner = str(uuid4()), ps.new_job_name(), _controller(controller)
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # Maintenance side of the mutual exclusion, in this write transaction: no concurrent or
        # unverified operation, and no execution of the provider open or uncertain (a launch
        # reservation commits before its process exists, so a starting Agent counts too).
        if blocking_operation(db, provider):
            raise ValueError(f'Another {providers.LABELS[provider]} setup operation is running or unverified.')
        if active_work(db, provider):
            raise ValueError(f'{providers.LABELS[provider]} executions started meanwhile; the operation did not run.')
        executions.register_controller(db, owner)
        db.execute('''INSERT INTO provider_operations(operation_id,provider,action,method,command,plan_digest,target_version,
            status,controller_instance_id,job_name,before_version,requested_by) VALUES (?,?,?,?,?,?,?,'running',?,?,?,'user')''',
            (operation_id, provider, action, plan.method, json.dumps(plan.display()), expected, plan.target_version,
             owner['instance_id'], job_name, plan.current_version))
    publish()
    state = {'launch': None, 'canceled': False, 'stopped': False, 'done': Event()}
    with _running_lock:
        _running[operation_id] = state
    Thread(target=_run, args=(connect, operation_id, plan, publish, job_name, state), daemon=True,
           name=f'provider-{action}-{provider}').start()
    return operation(connect, operation_id)


def command_for(plan, empty_config=None):
    """The process command for a plan: `.cmd` shims (npm, npm-installed CLIs) run through
    cmd.exe with fixed tokens, an executable runs directly; never shell=True."""
    arguments = [argument.replace(EMPTY_CONFIG, str(empty_config)) if empty_config else argument for argument in plan.arguments]
    if Path(plan.executable).suffix.lower() == '.cmd':
        return providers._cmd_line(Path(plan.executable), [f'"{a}"' if ' ' in a else a for a in arguments])
    return [plan.executable, *arguments]


def environment_for(plan):
    """npm gets a minimal environment built here; a provider CLI gets its own scrubbed
    environment without loader overrides. Neither is the model-execution environment."""
    if plan.method == 'npm':
        env = {name: os.environ[name] for name in INSTALLER_ENV if os.environ.get(name)}
        system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        env['PATH'] = os.pathsep.join([str(Path(plan.executable).parent), str(system / 'System32'), str(system),
                                       str(system / 'System32' / 'Wbem')])
        return env
    env = providers.adapter(plan.provider).environment()
    return {name: value for name, value in env.items() if not LOADER_OVERRIDES.match(name)}


def _run(connect, operation_id, plan, publish, job_name, state):
    timeout = OPERATION_TIMEOUT[plan.action]
    output, exit_code, error, evidence, verified = '', None, None, {}, False
    folder = Path(tempfile.mkdtemp(prefix='cc-provider-setup-'))  # Empty: no project npmrc or package.json.
    empty = folder / 'empty-npmrc'
    empty.write_text('', encoding='utf-8')
    launch = None
    try:
        options = dict(env=environment_for(plan), cwd=str(folder), shell=False)
        if plan.console:
            flags = CONSOLE_FLAGS
        else:
            flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            options.update(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           encoding='utf-8', errors='replace')
        try:
            launch = ps.spawn(command_for(plan, empty), job_name=job_name, creationflags=flags, **options)
        except ps.TerminationUnverified as exc:
            evidence, error = getattr(exc, 'evidence', {}), 'The operation could not be contained and its end is unverified.'
            raise
        evidence['contained'] = launch.evidence
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')  # The containment is durable before any installer code runs.
            db.execute('''UPDATE provider_operations SET containment='job_kill_on_close',root_pid=?,root_created_at=?,evidence=?
                WHERE operation_id=? AND status='running' ''',
                (launch.process.pid, launch.root_created_at, json.dumps(evidence), operation_id))
        with _running_lock:
            state['launch'] = launch
            if _stopping.is_set():
                state['stopped'] = True
            canceled = state['canceled'] or state['stopped']
        if canceled:
            status_, abort = launch.abort()
            evidence['abort'], verified = abort, status_ != 'uncertain'
            return
        launch.resume()
        process = launch.process
        chunks = []
        reader = None
        if not plan.console:
            def read():
                size = 0
                for line in process.stdout:
                    chunks.append(line)
                    size += len(line)
                    while size > OUTPUT_LIMIT and len(chunks) > 1:
                        size -= len(chunks.pop(0))
            reader = Thread(target=read, daemon=True)
            reader.start()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            evidence['timed_out'] = True
        verified = _end_tree(launch, plan, evidence, interactive_done=plan.console and not state['canceled']
                             and not _stopping.is_set())
        if state.get('termination'):
            evidence['termination'] = state['termination']  # Cancellation or shutdown: what the Job reported.
        if reader is not None:
            reader.join(5)
            with suppress(OSError):
                process.stdout.close()
            output = ''.join(chunks)[-OUTPUT_LIMIT:]
    except ps.TerminationUnverified:
        verified = False
    except OSError as exc:
        error = f'The operation could not start ({type(exc).__name__}).'
        verified = launch is None  # Nothing was created (spawn discards a process it could not contain).
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        _finish(connect, operation_id, plan, exit_code, redact(output), error, state, evidence, verified)
        with _running_lock:
            _running.pop(operation_id, None)
        state['done'].set()
        publish()


def _end_tree(launch, plan, evidence, interactive_done=False):
    """After the root exits (or on timeout, cancellation or shutdown): True once no process of
    the operation remains. Leftovers of an installer are ended. When an interactive sign-in
    finished by itself, programs it opened for the user (a browser) are released, not ended."""
    job = launch.job
    try:
        finished = launch.process.poll() is not None and not evidence.get('timed_out')
        active = job.active_processes()
        if finished and active == 0:
            evidence['ended'] = {'verified': True, 'active_processes': 0, 'at': providers._now()}
            return True
        if finished and interactive_done:
            try:
                job.release()  # The CLI exited; what it opened belongs to the user now.
                evidence['ended'] = {'verified': True, 'released_processes': active, 'at': providers._now()}
                return True
            except OSError as exc:
                evidence['release_error'] = str(exc)[:300]
        evidence['ended'] = {'verified': True, 'terminated': job.terminate(timeout=10)}
        return True
    except OSError as exc:
        evidence['ended'] = {'verified': False, 'error': str(exc)[:300]}
        return False
    finally:
        with suppress(OSError, subprocess.TimeoutExpired):
            launch.process.wait(timeout=5)
        job.close()


def _finish(connect, operation_id, plan, exit_code, output, error, state, evidence, verified):
    providers.invalidate(plan.provider)
    label = providers.LABELS[plan.provider]
    canceled = state['canceled']
    if not verified:
        status = 'uncertain'
        result = (f'The end of this operation\'s processes could not be verified. New {label} work and setup stay '
                  'blocked until you confirm in AI Providers that the setup window and installer have ended.')
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(f'''UPDATE provider_operations SET status=?,exit_code=?,output_tail=?,result=?,evidence=?,finished_at={NOW}
                WHERE operation_id=? AND status='running' ''', (status, exit_code, output or None, result, json.dumps(evidence),
                                                               operation_id))
        return
    found = providers.probe(plan.provider, force=True)
    if plan.provider in providers.REAL:
        model_catalog.mark_stale(connect, plan.provider)  # Version or account may have changed.
        try:  # The fresh probe is new version evidence for the recorded update comparison.
            reevaluate_updates(connect, [plan.provider])
        except Exception:
            logger.exception('Update comparison for %s was not refreshed', plan.provider)
    installed = found.state not in (providers.NOT_INSTALLED, providers.INVALID_INSTALLATION)
    bound = parse_version(plan.target_version) if plan.target_version else None
    reported = parse_version(found.version)
    if state['stopped'] and not canceled:
        status, result = 'interrupted', f'The backend stopped; the operation was ended. {label} was checked again: {_clause(found)}.'
    elif canceled:
        status, result = 'canceled', f'Canceled; its processes ended. {label} was checked again: {_clause(found)}.'
    elif error:
        status, result = 'failed', error
    elif evidence.get('timed_out'):
        status, result = 'failed', 'The operation did not finish in time; its processes were ended. Check the provider again.'
    elif plan.action in ('install', 'update'):
        if exit_code == 0 and installed and plan.target_version and not (bound and reported):
            # A pinned npm version is verified only by a version the CLI reports and that can be compared.
            status = 'needs_attention'
            result = (f'The {plan.action} exited successfully, but {label} reports no comparable version '
                      f'({found.version or "none"}), so the approved {plan.target_version} could not be verified. '
                      'Check again, or review the installation before relying on it.')
        elif exit_code == 0 and installed and plan.target_version and _precedence(reported) != _precedence(bound):
            status = 'needs_attention'
            result = (f'{label} reports {found.version}, not the approved {plan.target_version}. Review the installation '
                      'before relying on it.')
        elif exit_code == 0 and installed and found.version:
            status = 'succeeded'
            result = (f'{label} {found.version} is installed.' if plan.action == 'install' or found.version != plan.current_version
                      else f'{label} is already at {found.version}.')
        else:
            status = 'failed'
            result = (f'The {plan.action} exited with code {exit_code}. {label} now reports: '
                      f'{_clause(found)}. Nothing was claimed as installed.')
    elif plan.action == 'login':
        if found.authenticated and found.ready:
            status, result = 'succeeded', f'{label} is signed in on an approved subscription route.'
        elif found.authenticated:
            status, result = 'needs_attention', f'{label} is signed in, but not ready: {_clause(found)}.'
        else:
            status = 'needs_attention'
            result = (f'The sign-in window closed (exit code {exit_code}), but {label} does not report a sign-in: '
                      f'{_clause(found)}. Try again or sign in manually.')
    else:
        if exit_code == 0 and not found.authenticated:
            status, result = 'succeeded', f'{label} is signed out on this computer.'
        else:
            status, result = 'failed', f'{label} still reports: {_clause(found)}.'
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f'''UPDATE provider_operations SET status=?,exit_code=?,after_version=?,output_tail=?,result=?,evidence=?,
            finished_at={NOW} WHERE operation_id=? AND status='running' ''',
            (status, exit_code, found.version, output or None, result[:1000], json.dumps(evidence), operation_id))
        if plan.action in ('install', 'update'):
            db.execute('DELETE FROM provider_update_checks WHERE provider=?', (plan.provider,))  # Re-evaluated next check.


def cancel(connect, operation_id, wait=30):
    """End the operation's whole process tree and wait for the verified result."""
    with _running_lock:
        state = _running.get(operation_id)
        if state is not None:
            state['canceled'] = True
            launch = state['launch']
    if state is None:
        raise ValueError('This operation is not running in this backend.')
    if launch is not None:
        _terminate(state, launch)  # The run thread verifies the end again and records both.
    state['done'].wait(wait)
    return operation(connect, operation_id)


def _terminate(state, launch):
    try:
        state['termination'] = launch.job.terminate(timeout=10)
    except OSError as exc:
        state['termination'] = {'verified': False, 'error': str(exc)[:300]}


def accept_operations():
    """A backend that initialized (again) accepts setup operations."""
    _stopping.clear()


def shutdown(wait=20):
    """Application shutdown: end every running operation and record it, before the database closes."""
    _stopping.set()
    with _running_lock:
        states = list(_running.values())
        for state in states:
            state['stopped'] = True
    for state in states:
        launch = state['launch']
        if launch is not None:
            _terminate(state, launch)
    for state in states:
        state['done'].wait(wait)


def operation(connect, operation_id):
    with connect() as db:
        row = db.execute('SELECT * FROM provider_operations WHERE operation_id=?', (operation_id,)).fetchone()
    if row is None:
        raise LookupError('Operation not found')
    value = dict(row)
    value['command'] = json.loads(value['command'])
    value['evidence'] = _evidence(row)
    return value


def latest_operations(db, provider, limit=3):
    if not providers._has(db, 'provider_operations'):
        return []
    rows = db.execute('SELECT * FROM provider_operations WHERE provider=? ORDER BY started_at DESC,rowid DESC LIMIT ?',
                      (provider, limit)).fetchall()
    return [dict(r, command=json.loads(r['command']), evidence=_evidence(r)) for r in rows]


# ---------------------------------------------------------------- update checks

def parse_version(text):
    """(major, minor, patch, prerelease) or None. A numeric prerelease identifier with a leading
    zero is malformed (Semantic Versioning 2.0.0, item 9), so the whole version is not used."""
    match = VERSION.search(text or '') if isinstance(text, str) else None
    if not match:
        return None
    prerelease = match[4] or ''
    if any(part.isdigit() and len(part) > 1 and part.startswith('0') for part in prerelease.split('.') if prerelease):
        return None
    return (int(match[1]), int(match[2]), int(match[3]), prerelease)


def _precedence(version):
    """Sort key for Semantic Versioning precedence: a release ranks above its prereleases;
    prerelease identifiers compare left to right, numeric ones numerically and below
    alphanumeric ones, and a longer list wins when the shorter one is its prefix."""
    major, minor, patch, prerelease = version
    if not prerelease:
        return (major, minor, patch, 1, ())
    return (major, minor, patch, 0, tuple((0, int(part), '') if part.isdigit() else (1, 0, part)
                                          for part in prerelease.split('.')))


def _newer(a, b):
    """a has higher precedence than b."""
    return _precedence(a) > _precedence(b)


def fetch_dist_tags(provider):
    """The npm registry dist-tags of the provider's package (allowlisted URL; untrusted data)."""
    if os.environ.get('CONTROL_CENTER_DISABLE_UPDATE_CHECKS'):
        raise OSError('Update checks are disabled in this environment.')
    request = urllib.request.Request(REGISTRY.format(package=PACKAGES[provider].replace('/', '%2F')),
                                     headers={'Accept': 'application/json', 'User-Agent': 'ai-agent-control-center'})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        body = response.read(FETCH_LIMIT + 1)
    if len(body) > FETCH_LIMIT:
        raise ValueError('Release metadata is unexpectedly large')
    data = json.loads(body.decode('utf-8'))
    if not isinstance(data, dict):
        raise ValueError('Release metadata is not an object')
    tags = {}
    for tag, version in data.items():
        if isinstance(tag, str) and isinstance(version, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,31}', tag) \
                and re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]{1,64})?', version):
            tags[tag] = version
    return tags


def installed_version(provider, found):
    """The installed CLI's version as evidence for an update comparison: the cached readiness
    probe, else a fresh one. A probe runs only local, non-billable commands (`--version`, `--help`
    and the local sign-in status), is coalesced per provider and never makes a model request."""
    if found['state'] != 'installed':
        return None
    version = _version_text(provider)
    if version is None:
        try:
            version = providers.probe(provider).version
        except Exception as exc:  # No evidence: the comparison stays unresolved, never "up to date".
            logger.warning('Installed-version probe for %s failed (%s)', provider, type(exc).__name__)
            version = None
    return version


def evaluate_update(provider, tags, found, version_text=None):
    if version_text is None:
        version_text = _version_text(provider)
    method = found['method']
    channel = _channel(provider, version_text, method)
    latest = tags.get(channel)
    installed = parse_version(version_text)
    published = parse_version(latest)
    if found['state'] != 'installed':
        status = 'not_installed'
    elif latest is None:
        status = 'unsupported_channel'
    elif installed is None or published is None:
        status = 'unknown'  # Never compared, so never shown as an upgrade.
    elif _newer(published, installed):
        status = 'update_available' if method in ('npm', 'native') else 'manual_required'
    elif _newer(installed, published):
        status = 'newer_than_channel'
    else:
        status = 'up_to_date'
    return dict(status=status, installed_version=version_text and '.'.join(map(str, installed[:3])) + (
        f'-{installed[3]}' if installed and installed[3] else '') if installed else version_text,
        channel=channel, latest_version=latest, method=method, source='npm_registry_dist_tags', error=None)


_check_lock = Lock()
_last_manual = 0.0


def check_updates(connect, *, force=False, trigger='manual'):
    """One coalesced check of every real provider; at most once a day unless asked."""
    global _last_manual
    if not _check_lock.acquire(timeout=FETCH_TIMEOUT * 4):
        return update_view(connect)
    try:
        with connect() as db:
            rows = {r['provider']: dict(r) for r in db.execute('SELECT * FROM provider_update_checks')}
        if force:
            if time.monotonic() - _last_manual < MANUAL_CHECK_INTERVAL:
                return update_view(connect)
            _last_manual = time.monotonic()
        for provider in providers.REAL:
            previous = rows.get(provider)
            if not force and previous and _age(previous['checked_at']) < _recheck_after(previous):
                # Recent release metadata: only re-compare it with newer version evidence (no fetch).
                _reevaluate(connect, provider, previous)
                continue
            found = discover(provider)
            # Version evidence first, so a cold start never records an unresolved comparison
            # merely because no readiness check has run yet.
            version = installed_version(provider, found)
            try:
                result = evaluate_update(provider, fetch_dist_tags(provider), found, version)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                result = dict(status='check_failed', installed_version=version,
                              channel=_channel(provider, version, found['method']), latest_version=None,
                              method=found['method'], source='npm_registry_dist_tags',
                              error=f'Release metadata could not be read ({type(exc).__name__}).')
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute(f'''INSERT INTO provider_update_checks(provider,status,installed_version,channel,latest_version,
                    method,source,error,checked_at) VALUES (?,?,?,?,?,?,?,?,{NOW})
                    ON CONFLICT(provider) DO UPDATE SET status=excluded.status,installed_version=excluded.installed_version,
                    channel=excluded.channel,latest_version=excluded.latest_version,method=excluded.method,
                    source=excluded.source,error=excluded.error,checked_at=excluded.checked_at''',
                    (provider, result['status'], result['installed_version'], result['channel'],
                     result['latest_version'], result['method'], result['source'], result['error']))
        return update_view(connect)
    finally:
        _check_lock.release()


def _recheck_after(row):
    return RETRY_INTERVAL if row.get('status') == 'check_failed' else UPDATE_INTERVAL


def _reevaluate(connect, provider, row):
    """Compare recorded release metadata with the current version evidence, without reading the
    registry. Used when a version becomes known after the metadata was read (for example a
    check that ran before any readiness probe, or an update that just finished). The row keeps
    its checked_at: that is when the metadata was read. A changed channel needs fresh metadata,
    so it is left for the next check. Returns True when the stored result changed."""
    if not row or row.get('status') == 'check_failed' or not row.get('latest_version'):
        return False
    found = discover(provider)
    version = _version_text(provider) if found['state'] == 'installed' else None
    if found['state'] == 'installed' and version is None:
        return False  # No new evidence: never resolved by assumption.
    result = evaluate_update(provider, {row['channel']: row['latest_version']}, found, version)
    if found['state'] == 'installed' and result['channel'] != row['channel']:
        return False
    if all(result[key] == row.get(key) for key in ('status', 'installed_version', 'method')):
        return False
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT checked_at, latest_version, channel FROM provider_update_checks WHERE provider=?',
                             (provider,)).fetchone()
        if current is None or (current['checked_at'], current['latest_version'], current['channel']) != (
                row['checked_at'], row['latest_version'], row['channel']):
            return False  # A newer check wrote meanwhile; it wins.
        db.execute('UPDATE provider_update_checks SET status=?, installed_version=?, method=? WHERE provider=?',
                   (result['status'], result['installed_version'], result['method'], provider))
    return True


def reevaluate_updates(connect, provider_ids=None):
    """After new version evidence (a readiness check or a finished setup operation): refresh the
    recorded comparisons without registry traffic. Returns whether anything changed."""
    with connect() as db:
        if not providers._has(db, 'provider_update_checks'):
            return False
        rows = {r['provider']: dict(r) for r in db.execute('SELECT * FROM provider_update_checks')}
    changed = False
    for provider in provider_ids or providers.REAL:
        if provider in rows:
            changed = _reevaluate(connect, provider, rows[provider]) or changed
    return changed


def _age(stamp):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    except (ValueError, AttributeError):
        return float('inf')


def update_view(connect):
    with connect() as db:
        rows = {r['provider']: dict(r) for r in db.execute('SELECT * FROM provider_update_checks')} \
            if providers._has(db, 'provider_update_checks') else {}
    result = {}
    for provider in providers.REAL:
        row = rows.get(provider)
        if row is None:
            result[provider] = dict(status='unknown', checked_at=None, stale=True)
            continue
        row['stale'] = _age(row['checked_at']) > UPDATE_INTERVAL
        result[provider] = row
    return result


class UpdateScheduler:
    """In-process only: a check shortly after startup if the last one is older than a day, then
    about once a day while the backend runs. No service, scheduled task or work while closed."""

    def __init__(self, connect, publish, first_delay=60):
        self.connect, self.publish, self.first_delay = connect, publish, first_delay
        self.stop_event = Event()
        self.thread = None

    def start(self):
        if os.environ.get('CONTROL_CENTER_DISABLE_UPDATE_CHECKS'):
            return self
        self.thread = Thread(target=self._loop, daemon=True, name='provider-update-checks')
        self.thread.start()
        return self

    def _loop(self):
        delay = self.first_delay
        while not self.stop_event.wait(delay):
            delay = UPDATE_INTERVAL
            try:
                view = check_updates(self.connect, trigger='scheduled') or {}
                self.publish()
                if any(row.get('status') == 'check_failed' for row in view.values()):
                    delay = RETRY_INTERVAL  # Unreadable metadata is retried within the hour, not tomorrow.
            except Exception:
                logger.exception('Provider update check failed')

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------- notifications

def notifications(connect):
    """Actionable update notices not dismissed for this version and not snoozed."""
    updates = update_view(connect)
    with connect() as db:
        states = {(r['provider'], r['kind']): dict(r) for r in db.execute('SELECT * FROM provider_notification_state')} \
            if providers._has(db, 'provider_notification_state') else {}
    result = []
    for provider, row in updates.items():
        if row.get('status') not in ('update_available', 'manual_required'):
            continue
        state = states.get((provider, 'update'))
        if state and state['version'] == row['latest_version']:
            if state['state'] == 'dismissed':
                continue
            if state['until'] and _age(state['until']) < 0:
                continue
        result.append(dict(id=f'update:{provider}:{row["latest_version"]}', provider=provider, kind='update',
                           label=providers.LABELS[provider], installed_version=row['installed_version'],
                           available_version=row['latest_version'], channel=row['channel'],
                           manual=row['status'] == 'manual_required', checked_at=row['checked_at'],
                           stale=row.get('stale', False)))
    return result


def dismiss(connect, provider, version, mode):
    if provider not in providers.REAL:
        raise ValueError('Unknown provider')
    if mode not in ('dismiss', 'remind_later'):
        raise ValueError('mode is dismiss or remind_later')
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]{1,64})?', version):
        raise ValueError('Invalid version')
    until = None
    if mode == 'remind_later':
        until = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute(f'''INSERT INTO provider_notification_state(provider,kind,version,state,until) VALUES (?,'update',?,?,?)
            ON CONFLICT(provider,kind) DO UPDATE SET version=excluded.version,state=excluded.state,until=excluded.until,
            updated_at={NOW}''', (provider, version, 'dismissed' if mode == 'dismiss' else 'snoozed', until))
    return notifications(connect)
