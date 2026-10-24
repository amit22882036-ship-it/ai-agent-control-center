"""Stage 2F provider contract: adapters, capabilities, readiness and policy.

An adapter knows how to find, check, launch and interpret one agent CLI. It holds
no authority: Task lifecycle, admission, resources, supervision, persistence and
orchestration stay in the existing backend, and every launch still passes its
admission transaction. Adapters only use executable interfaces that the
provider documents (or that the installed CLI lists in its own help), and they
fail closed when something cannot be verified.

Readiness separates what the application supports, what is installed, what is
authenticated, what the user enabled, what the billing policy permits and what
is usable right now. Nothing here installs software, signs in, changes provider
configuration or makes a model request.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from threading import Lock
import time
import tempfile
import tomllib
from uuid import UUID

from . import managed_orchestration

logger = logging.getLogger(__name__)

IDS = ('mock', 'codex', 'claude', 'gemini')
REAL = ('codex', 'claude', 'gemini')
SANDBOXES = ('read-only', 'workspace-write')
LABELS = {'mock': 'Mock', 'codex': 'Codex', 'claude': 'Claude Code', 'gemini': 'Gemini CLI'}
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

# Readiness states (a provider is launchable only when READY).
READY = 'ready'
DISABLED = 'disabled'
NOT_INSTALLED = 'not_installed'
INVALID_INSTALLATION = 'invalid_installation'
UNSUPPORTED_VERSION = 'unsupported_version'
NOT_AUTHENTICATED = 'not_authenticated'
INCOMPATIBLE = 'incompatible'
USAGE_LIMITED = 'usage_limited'
UNAVAILABLE = 'temporarily_unavailable'
BILLING_BLOCKED = 'billing_blocked'
STATES = (READY, DISABLED, NOT_INSTALLED, INVALID_INSTALLATION, UNSUPPORTED_VERSION, NOT_AUTHENTICATED,
          INCOMPATIBLE, USAGE_LIMITED, UNAVAILABLE, BILLING_BLOCKED)

# Classified provider failures (Stage 2F error taxonomy).
TRANSIENT = 'transient'
PROVIDER_UNAVAILABLE = 'provider_unavailable'
AUTH_FAILED = 'auth_failed'
PERMISSION_FAILED = 'permission_failed'
QUOTA_EXHAUSTED = 'quota_exhausted'
RATE_LIMITED = 'rate_limited'
BILLING_ERROR = 'billing_error'
MODEL_UNAVAILABLE = 'model_unavailable'
SESSION_INVALID = 'session_invalid'
WORKER_CRASH = 'worker_crash'
UNKNOWN_FAILURE = 'unknown_failure'
CONDITIONS = (TRANSIENT, PROVIDER_UNAVAILABLE, AUTH_FAILED, PERMISSION_FAILED, QUOTA_EXHAUSTED, RATE_LIMITED,
              BILLING_ERROR, MODEL_UNAVAILABLE, SESSION_INVALID, WORKER_CRASH, UNKNOWN_FAILURE)
# Conditions that block new work on the provider until positive evidence or an
# explicit user decision. A reset time is stored only when the provider reports one.
BLOCKING = (QUOTA_EXHAUSTED, RATE_LIMITED, AUTH_FAILED, BILLING_ERROR)
# Conditions after which an ordinary execution keeps its Agent, Task, session and
# assignment and waits (instead of ending) so that it can continue later.
WAITING = {QUOTA_EXHAUSTED: 'usage_limited', RATE_LIMITED: 'usage_limited',
           AUTH_FAILED: 'auth_required', BILLING_ERROR: 'billing_blocked',
           PROVIDER_UNAVAILABLE: 'provider_unavailable'}
WAITING_LABELS = {'usage_limited': 'Waiting for provider usage', 'auth_required': 'Waiting for authentication',
                  'billing_blocked': 'Billing policy blocked', 'provider_unavailable': 'Provider unavailable',
                  'resume_ready': 'Ready to continue', 'model_not_enforced': 'Selected model not verified'}
UNCHECKED = 'unchecked'

CORE_CAPABILITIES = ('start', 'stop', 'structured_state', 'output_history', 'stable_identity',
                     'process_supervision', 'termination_verification', 'crash_recovery')
ADVANCED_CAPABILITIES = ('session_resume', 'reply', 'redirect', 'waiting_input', 'decisions', 'autonomous',
                         'delegation', 'hybrid', 'delegated_child', 'model_selection', 'model_change_keeps_session',
                         'permission_change')

MODEL_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
SESSION_RESUME_MARKER = '--- Provider session resumed ---'
READY_TTL = 120.0
NOT_READY_TTL = 10.0
PROBE_TIMEOUT = 20
DISCOVERY_TIMEOUT = 25
EFFORT_TOKEN = re.compile(r'[a-z][a-z_-]{0,23}')
# Documented version requirements (Agent SDK v0.3.N bundles Claude Code v2.1.N).
CLAUDE_USAGE_REPORT_VERSION = (2, 1, 273)   # SDKUsageReport (usage_report on /usage)
CLAUDE_RESOLVED_MODEL_VERSION = (2, 1, 197)  # ModelInfo.resolvedModel


class ProviderRefused(ValueError):
    """An actionable refusal. Never followed by a fallback to another provider or model."""

    def __init__(self, provider, code, message, guidance=None, docs_url=None):
        self.provider, self.code, self.guidance, self.docs_url = provider, code, guidance, docs_url
        text = f'{LABELS.get(provider, provider)}: {message}'
        if guidance:
            text += f' {guidance}'
        super().__init__(text)

    def as_dict(self):
        return {'provider': self.provider, 'code': self.code, 'message': str(self),
                'guidance': self.guidance, 'docs_url': self.docs_url}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


@dataclass
class Readiness:
    provider: str
    state: str
    reason: str | None = None
    guidance: str | None = None
    docs_url: str | None = None
    installed: bool | None = None
    version: str | None = None
    authenticated: bool | None = None
    auth_route: str | None = None
    billing_route: str | None = None
    checked_at: str = field(default_factory=_now)

    @property
    def ready(self):
        return self.state == READY

    def as_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    returncode: int | None
    stdout: str
    stderr: str = ''


# ---------------------------------------------------------------- environment

def _system_cmd():
    return Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'cmd.exe'


def _cmd_line(launcher, arguments):
    """A cmd.exe command line for an npm `.cmd` shim; every argument is a validated token."""
    inner = f'"{launcher}" ' + ' '.join(arguments)
    return f'"{_system_cmd()}" /d /s /v:off /c "{inner.strip()}"'


def _exe_line(executable, arguments):
    return f'"{executable}" ' + ' '.join(arguments)


def run_probe(command, *, env, timeout=PROBE_TIMEOUT):
    """Run a local, non-billable status command (version, help, sign-in status). Never a model request.

    A probe is not agent work: it never enters the execution journal or a worker
    Job. On a timeout its whole tree is ended (a shim may have started children).
    """
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                   encoding='utf-8', errors='replace', shell=False, env=env)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(command, **options)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10,
                           shell=False)
        process.kill()
        process.communicate()
        return ProbeResult(None, '', 'timed out')
    return ProbeResult(process.returncode, out or '', err or '')


def version_tuple(text):
    match = re.search(r'(\d+)\.(\d+)\.(\d+)', text or '')
    return tuple(int(part) for part in match.groups()) if match else None


def _kill_tree(process):
    """End a probe or discovery process and every descendant (a shim may start children)."""
    if os.name == 'nt':
        taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
        try:
            subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10,
                           shell=False)
        except (OSError, subprocess.SubprocessError):
            logger.exception('Could not end a provider discovery process tree')
    try:
        process.kill()
    except OSError:
        pass


def run_with_input(command, text, *, env, cwd=None, timeout=DISCOVERY_TIMEOUT, stop_when=None):
    """A non-inference CLI exchange: write `text`, read stdout lines, and never leave the tree running.

    `stop_when(line)` returning True ends the exchange early. On a timeout, an
    early stop, or a process that does not exit by itself, the whole process tree
    is ended before this returns.
    """
    from threading import Thread
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                   encoding='utf-8', errors='replace', shell=False, env=env, cwd=cwd)
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    process = subprocess.Popen(command, **options)
    lines, stopped = [], []

    def reader():
        try:
            for line in process.stdout:
                lines.append(line)
                if stop_when is not None and stop_when(line):
                    stopped.append(True)
                    return
        except (OSError, ValueError):
            pass
    thread = Thread(target=reader, daemon=True)
    thread.start()
    try:
        process.stdin.write(text)
        process.stdin.close()
    except (OSError, ValueError):
        pass
    thread.join(timeout)
    timed_out = thread.is_alive() and not stopped
    if process.poll() is None:
        try:
            process.wait(timeout=0.5 if (timed_out or stopped) else 5)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        _kill_tree(process)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.error('A provider discovery process did not end after it was terminated')
    thread.join(2)
    try:
        process.stdout.close()
    except (OSError, ValueError):
        pass
    return lines, timed_out


def _scrubbed(names):
    env = dict(os.environ)
    for name in names:
        env.pop(name, None)
    return env


def _first_line(text):
    for line in (text or '').splitlines():
        if line.strip():
            return line.strip()[:200]
    return ''


def validate_effort(effort):
    if effort is None:
        return None
    if not isinstance(effort, str) or not EFFORT_TOKEN.fullmatch(effort):
        raise ValueError('An effort level is a short lowercase identifier.')
    return effort


def validate_model(model):
    if model is None:
        return None
    if not isinstance(model, str) or not MODEL_TOKEN.fullmatch(model.strip()) or model != model.strip():
        raise ValueError("A model identifier may contain only letters, digits, '.', '_' or '-' (1-128 characters).")
    return model


# ---------------------------------------------------------------- adapters

class Adapter:
    """The provider contract. Subclasses describe verified interfaces only."""
    id = ''
    docs_url = None
    install_guidance = None
    auth_guidance = None
    billing_guidance = None
    # Environment variables that would silently switch the provider to a paid
    # API route. They are removed from every probe and launch environment.
    scrubbed_env = ()
    # Documented model aliases (from the provider's own documentation or help).
    documented_models = ()
    # name -> None when supported, or the precise reason it is not.
    capabilities = {}

    @property
    def label(self):
        return LABELS[self.id]

    def capability(self, name):
        if name in CORE_CAPABILITIES:
            return None
        return self.capabilities.get(name, 'Not supported by this provider adapter.')

    def supports(self, name):
        return self.capability(name) is None

    def environment(self):
        return _scrubbed(self.scrubbed_env)

    def locate(self):
        return None

    def probe(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def sandbox_arguments(self, sandbox, *, managed=False):  # pragma: no cover - overridden
        raise NotImplementedError

    def classify_model(self, model, catalog=()):
        """default | known | unknown (never advertised as verified)."""
        if model is None:
            return 'default'
        if model in self.documented_models or model in catalog:
            return 'known'
        return 'unknown'

    def describe(self):
        return {'provider': self.id, 'label': self.label, 'docs_url': self.docs_url,
                'install_guidance': self.install_guidance, 'auth_guidance': self.auth_guidance,
                'billing_guidance': self.billing_guidance,
                'documented_models': list(self.documented_models),
                'capabilities': {name: {'supported': self.capability(name) is None, 'reason': self.capability(name)}
                                 for name in CORE_CAPABILITIES + ADVANCED_CAPABILITIES}}


class MockAdapter(Adapter):
    id = 'mock'
    capabilities = {
        'session_resume': 'Mock agents only simulate work and have no provider session.',
        'reply': 'Mock agents never wait for input.',
        'redirect': 'Mock agents have no session to redirect.',
        'waiting_input': 'Mock agents never wait for input.',
        'decisions': 'Mock agents never wait for decisions.',
        'autonomous': 'Mock agents only simulate work.',
        'delegation': 'Mock agents only simulate work.',
        'hybrid': 'Mock agents only simulate work.',
        'delegated_child': None,
        'model_selection': 'Mock agents have no model.',
        'model_change_keeps_session': 'Mock agents have no session.',
        'permission_change': 'Mock agents have no access policy.',
    }

    def probe(self):
        return Readiness('mock', READY, installed=True, authenticated=True, auth_route='none', billing_route='none')

    def classify_model(self, model, catalog=()):
        if model is not None:
            raise ValueError('Mock agents do not accept a model.')
        return 'default'


class CodexAdapter(Adapter):
    id = 'codex'
    docs_url = 'https://developers.openai.com/codex/cli'
    install_guidance = 'Install it with `npm install -g @openai/codex`, then run `codex login`.'
    auth_guidance = 'Run `codex login` and sign in with your ChatGPT account, then choose Check again.'
    billing_guidance = ('Only the ChatGPT sign-in routed through the built-in openai model provider is allowed. '
                        'API keys, custom model providers and custom endpoints are refused because Stage 2F does '
                        'not authorize additional spending.')
    # codex exec reads CODEX_API_KEY; an API key would switch billing to the API.
    # OPENAI_BASE_URL would move the built-in provider to another endpoint.
    scrubbed_env = ('CODEX_API_KEY', 'OPENAI_API_KEY', 'OPENAI_BASE_URL')
    # Documented config keys that change routing or the login route (config reference).
    ROUTING_KEYS = ('model_provider', 'openai_base_url', 'chatgpt_base_url', 'forced_login_method')
    capabilities = {name: None for name in ('session_resume', 'reply', 'redirect', 'waiting_input', 'decisions',
                                            'autonomous', 'delegation', 'hybrid', 'delegated_child', 'model_selection')}
    capabilities['model_change_keeps_session'] = ('A different model starts a new Codex session: resuming a session '
                                                  'with another model is not a documented exec contract.')
    capabilities['permission_change'] = 'Only narrowing (Workspace write to Read only) is allowed, at a safe boundary.'

    def locate(self):
        if os.name != 'nt':
            return None
        appdata = os.environ.get('APPDATA')
        launcher = Path(appdata) / 'npm' / 'codex.cmd' if appdata else None
        return launcher if launcher is not None and launcher.is_file() else None

    def sandbox_arguments(self, sandbox, *, managed=False):
        if sandbox not in SANDBOXES:
            raise ValueError('Unknown Codex sandbox')
        return ['--sandbox', sandbox]

    def config_path(self):
        home = os.environ.get('CODEX_HOME') or str(Path(os.environ.get('USERPROFILE') or Path.home()) / '.codex')
        return Path(home) / 'config.toml'

    @staticmethod
    def _table_problems(table, where):
        problems = []
        if not isinstance(table, dict):
            return problems
        provider = table.get('model_provider')
        if provider not in (None, 'openai'):
            problems.append(f'{where} sets model_provider = {str(provider)[:40]!r} (only the built-in openai provider is allowed)')
        for key in ('openai_base_url', 'chatgpt_base_url'):
            if key in table:
                problems.append(f'{where} sets {key} (a custom endpoint)')
        if table.get('forced_login_method') not in (None, 'chatgpt'):
            problems.append(f'{where} forces the {str(table.get("forced_login_method"))[:20]!r} login method')
        return problems

    def route_problems(self, workspace=None):
        """Documented configuration that would route this execution away from ChatGPT on openai.

        The launch pins `-c model_provider=openai` (a CLI override), scrubs the
        variables above and never passes --profile, --oss or --local-provider. What
        remains to check is configuration a CLI override does not neutralize: a
        custom endpoint for the built-in provider, the login route, a legacy default
        profile, and (conservatively, although Codex ignores provider keys there)
        project files of the workspace.
        """
        problems = []
        path = self.config_path()
        try:
            data = tomllib.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        except FileNotFoundError:
            data = {}  # No user configuration: Codex defaults (the built-in openai provider).
        except (OSError, ValueError) as exc:
            return [f'{path} could not be read or parsed ({type(exc).__name__}), so the model route is unknown']
        problems += self._table_problems(data, str(path))
        profile = data.get('profile')
        if profile is not None:
            profiles = data.get('profiles') if isinstance(data.get('profiles'), dict) else {}
            problems += self._table_problems(profiles.get(profile), f'{path} default profile {str(profile)[:40]!r}')
        if workspace is not None:
            current = Path(workspace)
            for directory in (current, *current.parents):
                candidate = directory / '.codex' / 'config.toml'
                try:
                    if candidate.is_file():
                        problems += self._table_problems(tomllib.loads(candidate.read_text(encoding='utf-8')), str(candidate))
                except FileNotFoundError:
                    pass
                except (OSError, ValueError):
                    problems.append(f'{candidate} could not be read or parsed')
                if (directory / '.git').exists():
                    break
        return problems

    def config_fingerprint(self):
        path = self.config_path()
        try:
            stat = path.stat()
            return (str(path), stat.st_mtime_ns, stat.st_size)
        except OSError:
            return (str(path), None, None)

    def resolve_configuration(self, model=None, effort=None):
        """The model and effort an execution runs with: the selection, else the backward-compatible
        `CONTROL_CENTER_CODEX_MODEL` / `CONTROL_CENTER_CODEX_REASONING_EFFORT`, else Codex's own default
        (None). Admission and the command use exactly these values. Returns (model, effort, sources)."""
        env_model = os.environ.get('CONTROL_CENTER_CODEX_MODEL', '').strip()
        env_effort = os.environ.get('CONTROL_CENTER_CODEX_REASONING_EFFORT', '').strip()
        if env_model and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', env_model):
            raise ValueError("CONTROL_CENTER_CODEX_MODEL must be a model identifier containing only letters, digits, '.', '_' or '-'.")
        if env_effort and env_effort not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
            raise ValueError('CONTROL_CENTER_CODEX_REASONING_EFFORT must be none, minimal, low, medium, high or xhigh.')
        selected, chosen = validate_model(model), validate_effort(effort)
        sources = {'model': 'selection' if selected else ('environment' if env_model else 'provider_default'),
                   'effort': 'selection' if chosen else ('environment' if env_effort else 'provider_default')}
        return selected or env_model or None, chosen or env_effort or None, sources

    def command(self, sandbox='read-only', session_id=None, *, structured=False, model=None, effort=None):
        """`effort` is this execution's validated reasoning effort (override > Agent default);
        without one, the backward-compatible application default applies, then Codex's own."""
        if sandbox not in SANDBOXES:
            raise ValueError('Unknown Codex sandbox')
        if os.name != 'nt':
            raise RuntimeError('Codex agents currently require the Windows npm installation.')
        launcher = self.locate()
        if launcher is None:
            raise FileNotFoundError('npm Codex CLI not found at %APPDATA%\\npm\\codex.cmd.')
        cmd = Path(os.environ['SystemRoot']) / 'System32' / 'cmd.exe'
        # Only validated CLI options enter cmd.exe; the prompt is sent through stdin.
        prompt_args = f'resume {UUID(session_id)} -' if session_id else '-'
        # Restrict overrides to single CMD-safe tokens, including through npm's
        # second parsing pass. Never interpolate arbitrary environment text.
        selected, chosen_effort, _ = self.resolve_configuration(model, effort)
        # Stage 2F: the documented CLI override pins the built-in openai provider,
        # whatever the configuration or a profile selects.
        overrides = '-c model_provider=openai '
        overrides += f'--model {selected} ' if selected else ''
        if chosen_effort:
            # A per-invocation override of the documented model_reasoning_effort key:
            # the user's config.toml is never changed and concurrent Agents never share it.
            overrides += f'-c model_reasoning_effort={chosen_effort} '
        if structured:
            overrides += managed_orchestration.CLI_OVERRIDE
        command = (f'"{launcher}" exec {overrides}--sandbox {sandbox} --color never --skip-git-repo-check '
                   f'{"--json " if structured else ""}{prompt_args}')
        # cmd /s /c strips the outer quotes, preserving the quoted launcher path.
        return f'"{cmd}" /d /s /v:off /c "{command}"'

    def discover_models(self):
        """`codex debug models`: the CLI's own model catalog as JSON (a debug interface, not a stable contract).

        Local and non-billable (no inference). Returns (catalog dict, cli version) or raises.
        """
        launcher = self.locate()
        if launcher is None:
            raise ProviderRefused('codex', NOT_INSTALLED, 'The Codex CLI was not found.')
        env = self.environment()
        usage = run_probe(_cmd_line(launcher, ['debug', '--help']), env=env)
        if usage.returncode != 0 or 'models' not in usage.stdout:
            raise ProviderRefused('codex', 'discovery_unsupported', 'This Codex CLI version has no model catalog command.')
        result = run_probe(_cmd_line(launcher, ['debug', '-c', 'model_provider=openai', 'models']), env=env,
                           timeout=DISCOVERY_TIMEOUT)
        if result.returncode is None:
            raise ProviderRefused('codex', UNAVAILABLE, 'The Codex model catalog did not answer in time.')
        if result.returncode != 0:
            raise ProviderRefused('codex', 'discovery_failed', 'The Codex model catalog command failed.')
        start = result.stdout.find('{')
        data = json.loads(result.stdout[start:]) if start >= 0 else None
        if not isinstance(data, dict) or not isinstance(data.get('models'), list):
            raise ProviderRefused('codex', 'discovery_failed', 'The Codex model catalog was not recognized.')
        version = run_probe(_cmd_line(launcher, ['--version']), env=env)
        return data, _first_line(version.stdout)

    def probe(self):
        launcher = self.locate()
        if launcher is None:
            return Readiness('codex', NOT_INSTALLED, 'The Codex CLI was not found at %APPDATA%\\npm\\codex.cmd.',
                             self.install_guidance, self.docs_url, installed=False)
        env = self.environment()
        version = run_probe(_cmd_line(launcher, ['--version']), env=env)
        if version.returncode != 0 or not version.stdout.strip().lower().startswith('codex'):
            return Readiness('codex', INVALID_INSTALLATION, 'The Codex launcher did not report a version.',
                             'Reinstall it with `npm install -g @openai/codex`.', self.docs_url, installed=True)
        found = _first_line(version.stdout)
        status = run_probe(_cmd_line(launcher, ['login', 'status']), env=env)
        text = (status.stdout + '\n' + status.stderr).lower()
        base = dict(installed=True, version=found)
        if status.returncode is None:
            return Readiness('codex', UNAVAILABLE, 'The Codex sign-in check did not answer in time.',
                             'Choose Check again.', self.docs_url, **base)
        if 'logged in using chatgpt' in text:
            # ChatGPT authentication is not the model route: check the routing too.
            problems = self.route_problems()
            if problems:
                return Readiness('codex', BILLING_BLOCKED, 'Codex is signed in with ChatGPT, but its configuration '
                                 'changes the model route: ' + '; '.join(problems) + '.',
                                 self.billing_guidance + ' Remove these settings yourself; the Control Center never '
                                 'edits Codex configuration.', self.docs_url, authenticated=True, auth_route='chatgpt',
                                 billing_route='custom_route', **base)
            return Readiness('codex', READY, authenticated=True, auth_route='chatgpt',
                             billing_route='subscription (built-in openai provider)', docs_url=self.docs_url, **base)
        if 'api key' in text and 'logged in' in text:
            return Readiness('codex', BILLING_BLOCKED, 'Codex is signed in with an API key (pay-per-use billing).',
                             self.billing_guidance + ' Run `codex logout`, then `codex login` with ChatGPT.',
                             self.docs_url, authenticated=True, auth_route='api_key', billing_route='paid_api', **base)
        if 'not logged in' in text:
            return Readiness('codex', NOT_AUTHENTICATED, 'Codex is not signed in.', self.auth_guidance,
                             self.docs_url, authenticated=False, **base)
        return Readiness('codex', NOT_AUTHENTICATED, 'The Codex sign-in status was not recognized.',
                         self.auth_guidance, self.docs_url, authenticated=None, **base)


# Tools a managed Claude Code run may not use: provider-internal sub-agents
# would bypass Control Center admission ("Agent" in current releases, "Task" in
# earlier ones). A bare tool name in a deny rule removes the tool entirely.
CLAUDE_NATIVE_COLLABORATION_TOOLS = ('Agent', 'Task')
# Shell and network tools are not sandboxed by Claude Code on Windows, so neither
# access policy can enforce workspace or network limits for them: denied in both.
CLAUDE_ALWAYS_DENIED = ('Bash', 'PowerShell', 'WebFetch', 'WebSearch')
CLAUDE_WRITE_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
# Every flag the adapter uses must appear in the installed CLI's own help.
CLAUDE_REQUIRED_HELP = ('--print', '--output-format', 'stream-json', '--verbose', '--permission-mode',
                        'dontAsk', 'acceptEdits', '--disallowedTools', '--setting-sources',
                        '--strict-mcp-config', '--resume', '--model')


class ClaudeAdapter(Adapter):
    id = 'claude'
    usage_command_supported = False
    effort_flag_supported = False
    settings_flag_supported = False
    installed_version = None
    user_fallback_chain = None  # A fallbackModel chain in the user's or managed settings, if any.
    usage_refresh_disabled_for = None  # An installation whose /usage produced no report.
    docs_url = 'https://code.claude.com/docs/en/setup'
    install_guidance = 'Install Claude Code with its native installer, then run `claude auth login`.'
    auth_guidance = 'Run `claude auth login` and sign in with your Claude subscription, then choose Check again.'
    billing_guidance = ('Only a Claude subscription sign-in is allowed. API keys, Console keys, cloud providers '
                        'and gateways are refused because they bill per use. Confirm in the provider panel that '
                        'paid extra usage is not enabled on your account.')
    documented_models = ('fable', 'opus', 'sonnet')  # aliases named by `claude --help`
    # In -p mode an API key always wins over the subscription login; these would
    # silently route work to pay-per-use or third-party billing.
    scrubbed_env = ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK',
                    'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY', 'ANTHROPIC_PROFILE',
                    'ANTHROPIC_FEDERATION_RULE_ID', 'ANTHROPIC_ORGANIZATION_ID', 'ANTHROPIC_IDENTITY_TOKEN_FILE',
                    'CLAUDE_CODE_SIMPLE')
    capabilities = {name: None for name in ('session_resume', 'reply', 'redirect', 'waiting_input', 'decisions',
                                            'autonomous', 'delegation', 'hybrid', 'delegated_child',
                                            'model_selection', 'model_change_keeps_session')}
    capabilities['permission_change'] = 'Only narrowing (Workspace write to Read only) is allowed, at a safe boundary.'

    def locate(self):
        if os.name != 'nt':
            return None
        profile = os.environ.get('USERPROFILE')
        native = Path(profile) / '.local' / 'bin' / 'claude.exe' if profile else None
        if native is not None and native.is_file():
            return native
        appdata = os.environ.get('APPDATA')
        shim = Path(appdata) / 'npm' / 'claude.cmd' if appdata else None
        return shim if shim is not None and shim.is_file() else None

    def _line(self, executable, arguments):
        if executable.suffix.lower() == '.cmd':
            return _cmd_line(executable, arguments)
        return _exe_line(executable, arguments)

    def sandbox_arguments(self, sandbox, *, managed=False):
        denied = list(CLAUDE_ALWAYS_DENIED)
        if sandbox == 'read-only':
            mode = 'dontAsk'  # Denies every call that would otherwise prompt.
            denied += CLAUDE_WRITE_TOOLS
        elif sandbox == 'workspace-write':
            mode = 'acceptEdits'  # File edits inside the working directory only.
        else:
            raise ValueError('Unknown Claude Code access policy')
        if managed:
            denied += CLAUDE_NATIVE_COLLABORATION_TOOLS
        # Project and local settings (hooks, allow rules, MCP servers) of the
        # repository being worked on are not loaded: they could widen access.
        return ['--setting-sources', 'user', '--strict-mcp-config', '--permission-mode', mode,
                '--disallowedTools', ','.join(denied)]

    def command(self, sandbox='read-only', session_id=None, *, structured=False, model=None, effort=None):
        executable = self.locate()
        if executable is None:
            raise FileNotFoundError('Claude Code was not found at %USERPROFILE%\\.local\\bin\\claude.exe '
                                    'or %APPDATA%\\npm\\claude.cmd.')
        arguments = ['-p', '--output-format', 'stream-json', '--verbose',
                     *self.sandbox_arguments(sandbox, managed=structured)]
        if self.settings_flag_supported:
            arguments += ['--settings', f'"{self.policy_settings_path()}"']
        if validate_model(model):
            arguments += ['--model', model]
        if validate_effort(effort):
            if not self.effort_flag_supported:
                raise ProviderRefused('claude', UNSUPPORTED_VERSION, 'This Claude Code version has no --effort option.',
                                      'Update Claude Code.')
            arguments += ['--effort', effort]
        if session_id:
            arguments += ['--resume', str(UUID(session_id))]
        # The prompt is sent on stdin: -p reads it when no prompt argument is given.
        return self._line(executable, arguments)

    def policy_settings_path(self):
        """A Control Center-owned flag-settings file: no automatic model switching.

        `switchModelsOnFlag: false` makes a classifier-flagged request end with a
        refusal in print mode instead of re-running on another model, and an empty
        `fallbackModel` configures no availability fallback chain. The user's own
        settings files are never changed.
        """
        folder = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir()) / 'AI Agent Control Center'
        path = folder / 'claude-model-policy.json'
        content = json.dumps({'switchModelsOnFlag': False, 'fallbackModel': []})
        try:
            if not path.is_file() or path.read_text(encoding='utf-8') != content:
                folder.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding='utf-8')
        except OSError as exc:
            raise ProviderRefused('claude', UNAVAILABLE, f'The model policy file could not be written ({type(exc).__name__}).')
        return path

    def configured_fallback(self):
        """A non-empty `fallbackModel` in user or managed settings (documented keys and paths), or None."""
        home = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path(os.environ.get('USERPROFILE') or Path.home()) / '.claude')
        candidates = [home / 'settings.json',
                      Path(os.environ.get('ProgramFiles') or r'C:\Program Files') / 'ClaudeCode' / 'managed-settings.json']
        for path in candidates:
            try:
                data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
            except (OSError, ValueError):
                return f'{path} (unreadable)'
            chain = data.get('fallbackModel') if isinstance(data, dict) else None
            if chain:
                return str(path)
        return None

    def discover_models(self):
        """The models, effort levels and account the installed CLI reports at initialization.

        Sends only the SDK `initialize` control request in stream-json input mode and
        closes stdin without a user message, so no model turn can start (verified
        against the installed CLI). The wire format is the SDK's, not a documented
        CLI contract, so the result is labelled as CLI-reported and validated strictly.
        """
        executable = self.locate()
        if executable is None:
            raise ProviderRefused('claude', NOT_INSTALLED, 'Claude Code was not found.', self.install_guidance)
        version = version_tuple(_first_line(run_probe(self._line(executable, ['--version']), env=self.environment()).stdout))
        arguments = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose',
                     '--no-session-persistence', *self.sandbox_arguments('read-only', managed=True)]
        request = json.dumps({'type': 'control_request', 'request_id': 'control-center-discovery',
                              'request': {'subtype': 'initialize'}}) + '\n'

        def response(line):
            return '"control_response"' in line
        with tempfile.TemporaryDirectory(prefix='cc-models-') as directory:
            lines, timed_out = run_with_input(self._line(executable, arguments), request, env=self.environment(),
                                              cwd=directory, stop_when=response)
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get('type') == 'control_response':
                body = event.get('response') if isinstance(event.get('response'), dict) else {}
                if body.get('subtype') != 'success' or not isinstance(body.get('response'), dict):
                    raise ProviderRefused('claude', 'discovery_failed', 'Claude Code refused the discovery request.')
                return body['response'], version
        raise ProviderRefused('claude', UNAVAILABLE if timed_out else 'discovery_failed',
                              'Claude Code did not report its models' + (' in time.' if timed_out else '.'))

    def usage_report(self):
        """The documented `/usage` local command in print mode: `usage_report.rate_limits` rows.

        `/usage` is a local command (its output arrives as an assistant message
        carrying `usage_report`), not a model prompt. The SDK marks the report
        shape experimental, so it is parsed strictly. If a run produced anything
        other than that report (for example a model turn), refresh is disabled for
        this installation so it is never repeated.
        """
        executable = self.locate()
        if executable is None:
            raise ProviderRefused('claude', NOT_INSTALLED, 'Claude Code was not found.', self.install_guidance)
        fingerprint = str(executable), executable.stat().st_mtime_ns
        if self.installed_version is None or self.installed_version < CLAUDE_USAGE_REPORT_VERSION:
            # The structured usage_report exists from Claude Code 2.1.273 (Agent SDK
            # v0.3.273). An older CLI might treat /usage differently, so it is never sent.
            found = '.'.join(map(str, self.installed_version)) if self.installed_version else 'unknown'
            raise ProviderRefused('claude', 'usage_unavailable', f'Usage refresh needs Claude Code 2.1.273 or later '
                                  f'(installed: {found}); usage is shown as unavailable.', 'Update Claude Code.')
        if self.usage_refresh_disabled_for == fingerprint:
            raise ProviderRefused('claude', 'usage_unavailable', 'This Claude Code installation did not return a '
                                  'usage report earlier, so refresh is disabled until it is updated.')
        arguments = ['-p', '--output-format', 'stream-json', '--verbose', '--no-session-persistence',
                     *self.sandbox_arguments('read-only', managed=True)]
        with tempfile.TemporaryDirectory(prefix='cc-usage-') as directory:
            lines, timed_out = run_with_input(self._line(executable, arguments), '/usage\n', env=self.environment(),
                                              cwd=directory)
        if timed_out:
            raise ProviderRefused('claude', UNAVAILABLE, 'The usage report did not answer in time.')
        report, unexpected = None, False
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'assistant':
                message = event.get('message') if isinstance(event.get('message'), dict) else {}
                candidate = event.get('usage_report', message.get('usage_report'))
                if isinstance(candidate, dict):
                    report = candidate
                else:
                    unexpected = True
            if event.get('type') == 'result' and isinstance(event.get('num_turns'), int) and event['num_turns'] > 0 and report is None:
                unexpected = True
        if report is None:
            if unexpected:
                self.usage_refresh_disabled_for = fingerprint
            raise ProviderRefused('claude', 'usage_unavailable', 'Claude Code did not return a usage report.')
        return report

    @staticmethod
    def update_guidance(executable):
        """How to update the installation that was actually found: `claude update` belongs to the
        native installer; an npm installation is updated through npm (Update in AI Providers)."""
        if executable.suffix.lower() == '.exe':
            return 'Update Claude Code with `claude update`, or choose Update in AI Providers.'
        return 'Update Claude Code with Update in AI Providers (npm), or `npm install -g @anthropic-ai/claude-code`.'

    def probe(self):
        executable = self.locate()
        if executable is None:
            return Readiness('claude', NOT_INSTALLED, 'Claude Code was not found.', self.install_guidance,
                             self.docs_url, installed=False)
        env = self.environment()
        version = run_probe(self._line(executable, ['--version']), env=env)
        if version.returncode != 0 or 'claude code' not in version.stdout.lower():
            return Readiness('claude', INVALID_INSTALLATION, 'Claude Code did not report a version.',
                             'Reinstall Claude Code.', self.docs_url, installed=True)
        found = _first_line(version.stdout)
        base = dict(installed=True, version=found)
        usage = run_probe(self._line(executable, ['--help']), env=env)
        missing = [flag for flag in CLAUDE_REQUIRED_HELP if flag not in usage.stdout]
        if usage.returncode != 0 or missing:
            return Readiness('claude', UNSUPPORTED_VERSION, 'This Claude Code version lacks required options: '
                             + (', '.join(missing) or 'help unavailable') + '.',
                             self.update_guidance(executable), self.docs_url, **base)
        status = run_probe(self._line(executable, ['auth', 'status', '--json']), env=env)
        if status.returncode is None:
            return Readiness('claude', UNAVAILABLE, 'The Claude Code sign-in check did not answer in time.',
                             'Choose Check again.', self.docs_url, **base)
        try:
            info = json.loads(status.stdout)
        except ValueError:
            info = None
        if not isinstance(info, dict) or not isinstance(info.get('loggedIn'), bool):
            return Readiness('claude', NOT_AUTHENTICATED, 'The Claude Code sign-in status was not recognized.',
                             self.auth_guidance, self.docs_url, **base)
        if not info['loggedIn']:
            return Readiness('claude', NOT_AUTHENTICATED, 'Claude Code is not signed in.', self.auth_guidance,
                             self.docs_url, authenticated=False, **base)
        method, api = info.get('authMethod'), info.get('apiProvider')
        self.installed_version = version_tuple(found)
        self.effort_flag_supported = '--effort' in usage.stdout
        self.settings_flag_supported = '--settings' in usage.stdout
        self.usage_command_supported = ('--no-session-persistence' in usage.stdout and self.installed_version is not None
                                        and self.installed_version >= CLAUDE_USAGE_REPORT_VERSION)
        self.user_fallback_chain = self.configured_fallback()
        # The field values are not a documented contract: they may only block.
        # A route is accepted solely when it is recognizably a first-party
        # subscription sign-in; anything else fails closed.
        if method in ('claude.ai', 'oauth_token') and api == 'firstParty':
            return Readiness('claude', READY, authenticated=True, auth_route=f'subscription ({method})',
                             billing_route='subscription', docs_url=self.docs_url, **base)
        return Readiness('claude', BILLING_BLOCKED, 'The Claude Code sign-in is not a recognized subscription route.',
                         self.billing_guidance, self.docs_url, authenticated=True,
                         auth_route=str(method)[:40] if method else 'unknown', billing_route='not_established', **base)


GEMINI_REQUIRED_HELP = ('--output-format', 'stream-json', '--approval-mode', 'auto_edit', '--model', '--resume')
# Settings `security.auth.selectedType` values that are pay-per-use routes.
GEMINI_PAID_AUTH = ('gemini-api-key', 'vertex-ai')
# The AuthType values of packages/core/src/core/contentGenerator.ts. Only the
# Google sign-in is approved; every other value, known or not, is refused.
GEMINI_APPROVED_AUTH = ('oauth-personal',)
GEMINI_KNOWN_AUTH = ('oauth-personal', 'gemini-api-key', 'vertex-ai', 'cloud-shell', 'compute-default-credentials',
                     'gateway')
GEMINI_ENV_KEYS = ('GEMINI_API_KEY', 'GOOGLE_API_KEY', 'GOOGLE_GENAI_USE_VERTEXAI', 'GOOGLE_APPLICATION_CREDENTIALS')


class GeminiAdapter(Adapter):
    id = 'gemini'
    docs_url = 'https://geminicli.com/docs/get-started/'
    install_guidance = 'Install it with `npm install -g @google/gemini-cli`, then run `gemini` once and sign in.'
    auth_guidance = 'Run `gemini` in a terminal and choose Sign in with Google, then choose Check again.'
    billing_guidance = ('Only a Google sign-in route is allowed; Gemini API keys and Vertex AI bill per use. Set '
                        '"billing": {"overageStrategy": "never"} in ~/.gemini/settings.json so that AI credits are '
                        'never spent automatically, and confirm the route in the provider panel. Unpaid and '
                        'Google One accounts moved to Antigravity CLI on 2026-06-18.')
    documented_models = ('auto', 'pro', 'flash', 'flash-lite')  # aliases in the CLI reference
    scrubbed_env = GEMINI_ENV_KEYS + ('GEMINI_MODEL', 'GEMINI_CLI_TRUST_WORKSPACE')
    capabilities = {name: None for name in ('session_resume', 'reply', 'redirect', 'waiting_input', 'decisions',
                                            'delegated_child', 'model_selection')}
    _no_managed = ('Gemini CLI cannot be launched with its provider-internal sub-agents verifiably disabled, '
                   'so it cannot coordinate Control Center Children. Use Codex or Claude Code as the Parent.')
    capabilities.update(autonomous=_no_managed, delegation=_no_managed, hybrid=_no_managed,
                        model_change_keeps_session='A different model starts a new Gemini CLI session.',
                        permission_change='Only narrowing (Workspace write to Read only) is allowed, at a safe boundary.')

    def locate(self):
        if os.name != 'nt':
            return None
        appdata = os.environ.get('APPDATA')
        shim = Path(appdata) / 'npm' / 'gemini.cmd' if appdata else None
        return shim if shim is not None and shim.is_file() else None

    def settings_path(self):
        home = os.environ.get('GEMINI_CLI_HOME') or os.environ.get('USERPROFILE') or str(Path.home())
        return Path(home) / '.gemini' / 'settings.json'

    def layers(self, workspace=None):
        """Settings files in documented precedence order, lowest first.

        Defaults < system defaults < user < project (workspace) < system settings;
        environment variables and CLI arguments rank above them, and the launch
        removes the routing variables and passes no auth arguments. The project
        layer is considered even though folder trust may ignore it (fail closed).
        """
        program_data = Path(os.environ.get('ProgramData') or r'C:\ProgramData') / 'gemini-cli'
        files = [('system defaults', Path(os.environ.get('GEMINI_CLI_SYSTEM_DEFAULTS_PATH') or program_data / 'system-defaults.json')),
                 ('user', self.settings_path())]
        if workspace is not None:
            files.append(('project', Path(workspace) / '.gemini' / 'settings.json'))
        files.append(('system', Path(os.environ.get('GEMINI_CLI_SYSTEM_SETTINGS_PATH') or program_data / 'settings.json')))
        return files

    def effective(self, workspace=None):
        """(auth type, overage strategy, unreadable files) after applying every layer."""
        auth = overage = None
        unreadable = []
        for name, path in self.layers(workspace):
            try:
                if not path.is_file():
                    continue
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                unreadable.append(f'{name} settings {path}')
                continue
            if not isinstance(data, dict):
                unreadable.append(f'{name} settings {path}')
                continue
            security = data.get('security') if isinstance(data.get('security'), dict) else {}
            selected = security.get('auth', {}).get('selectedType') if isinstance(security.get('auth'), dict) else None
            if selected is not None:
                auth = (selected, name)
            billing = data.get('billing') if isinstance(data.get('billing'), dict) else {}
            if 'overageStrategy' in billing:
                overage = (billing['overageStrategy'], name)
        return auth, overage, unreadable

    def route_refusal(self, workspace=None):
        """(state, reason) when the effective route for this workspace is not approved, else None."""
        auth, overage, unreadable = self.effective(workspace)
        if unreadable:
            return BILLING_BLOCKED, ('Gemini CLI settings could not be read, so the effective billing route is '
                                     'unknown: ' + ', '.join(unreadable) + '.')
        if auth is None:
            return NOT_AUTHENTICATED, 'Gemini CLI has no saved sign-in.'
        value, layer = auth
        if value not in GEMINI_KNOWN_AUTH:
            return BILLING_BLOCKED, f'The {layer} settings select an unknown authentication type ({str(value)[:40]!r}).'
        if value not in GEMINI_APPROVED_AUTH:
            return BILLING_BLOCKED, (f'The {layer} settings select {value}, which is not an approved route '
                                     '(only Sign in with Google is allowed).')
        if overage is None or overage[0] != 'never':
            where = f' in the {overage[1]} settings' if overage else ''
            return BILLING_BLOCKED, ('Gemini CLI may spend AI credits when quota is exhausted (effective '
                                     f'billing.overageStrategy is {overage[0] if overage else "unset"}{where}).')
        conflicts = self.env_file_conflicts(workspace)
        if conflicts:
            return BILLING_BLOCKED, ('A .env file Gemini CLI reads names an API key or Vertex AI: '
                                     + ', '.join(conflicts) + '.')
        return None

    def config_fingerprint(self):
        stamps = []
        for name, path in self.layers(None):
            try:
                stat = path.stat()
                stamps.append((name, stat.st_mtime_ns, stat.st_size))
            except OSError:
                stamps.append((name, None, None))
        return tuple(stamps)

    def sandbox_arguments(self, sandbox, *, managed=False):
        if managed:
            raise ProviderRefused('gemini', 'incompatible', self._no_managed)
        # In headless mode tools that need confirmation are not run: `default`
        # allows no edits or shell, `auto_edit` allows file edits only.
        if sandbox == 'read-only':
            return ['--approval-mode', 'default']
        if sandbox == 'workspace-write':
            return ['--approval-mode', 'auto_edit']
        raise ValueError('Unknown Gemini CLI access policy')

    def command(self, sandbox='read-only', session_id=None, *, structured=False, model=None, effort=None):
        launcher = self.locate()
        if launcher is None:
            raise FileNotFoundError('Gemini CLI was not found at %APPDATA%\\npm\\gemini.cmd.')
        if effort is not None:
            raise ProviderRefused('gemini', 'unsupported_effort', 'Gemini CLI has no documented command-line '
                                  'reasoning control; it runs with its provider default.')
        # Children run structured for completion proof without managed authority.
        arguments = ['--output-format', 'stream-json', *self.sandbox_arguments(sandbox)]
        if validate_model(model):
            arguments += ['--model', model]
        if session_id:
            arguments += ['--resume', str(UUID(session_id))]
        # Non-TTY stdin makes Gemini CLI headless; the prompt is sent on stdin.
        return _cmd_line(launcher, arguments)

    def env_file_conflicts(self, start=None):
        """`.env` files Gemini CLI would read that name a paid route (names only, values never read)."""
        home = Path(os.environ.get('USERPROFILE') or Path.home())
        candidates = []
        if start is not None:
            current = Path(start)
            for directory in (current, *current.parents):
                candidates += [directory / '.gemini' / '.env', directory / '.env']
                if (directory / '.git').exists() or directory == home:
                    break
        candidates += [home / '.gemini' / '.env', home / '.env']
        conflicts = []
        for path in dict.fromkeys(candidates):
            try:
                if not path.is_file():
                    continue
                names = {line.split('=', 1)[0].strip().removeprefix('export ').strip()
                         for line in path.read_text(encoding='utf-8', errors='replace').splitlines()
                         if '=' in line and not line.lstrip().startswith('#')}
            except OSError:
                conflicts.append(str(path))
                continue
            if names & set(GEMINI_ENV_KEYS):
                conflicts.append(str(path))
        return conflicts

    def probe(self):
        launcher = self.locate()
        if launcher is None:
            return Readiness('gemini', NOT_INSTALLED, 'Gemini CLI was not found.', self.install_guidance,
                             self.docs_url, installed=False)
        env = self.environment()
        version = run_probe(_cmd_line(launcher, ['--version']), env=env)
        if version.returncode != 0 or not re.search(r'\d+\.\d+', version.stdout):
            return Readiness('gemini', INVALID_INSTALLATION, 'Gemini CLI did not report a version.',
                             'Reinstall it with `npm install -g @google/gemini-cli`.', self.docs_url, installed=True)
        base = dict(installed=True, version=_first_line(version.stdout))
        usage = run_probe(_cmd_line(launcher, ['--help']), env=env)
        missing = [flag for flag in GEMINI_REQUIRED_HELP if flag not in usage.stdout]
        if usage.returncode != 0 or missing:
            return Readiness('gemini', UNSUPPORTED_VERSION, 'This Gemini CLI version lacks required options: '
                             + (', '.join(missing) or 'help unavailable') + '.',
                             'Update it with `npm install -g @google/gemini-cli`.', self.docs_url, **base)
        refusal = self.route_refusal(None)
        if refusal is not None:
            state, reason = refusal
            return Readiness('gemini', state, reason, self.auth_guidance if state == NOT_AUTHENTICATED
                             else self.billing_guidance, self.docs_url,
                             authenticated=False if state == NOT_AUTHENTICATED else None, **base)
        # The sign-in has no documented status command: the effective saved
        # selection is evidence of configuration, and the user confirms the route.
        # The execution workspace's own layer is checked again before each launch.
        return Readiness('gemini', READY, authenticated=None, auth_route='google (oauth-personal)',
                         billing_route='subscription_unverified', docs_url=self.docs_url, **base)


ADAPTERS = {adapter.id: adapter for adapter in (MockAdapter(), CodexAdapter(), ClaudeAdapter(), GeminiAdapter())}
CODEX, CLAUDE, GEMINI = ADAPTERS['codex'], ADAPTERS['claude'], ADAPTERS['gemini']


def adapter(provider):
    try:
        return ADAPTERS[provider]
    except KeyError:
        raise ValueError('Unknown agent type') from None


def supports(provider, capability):
    return adapter(provider).supports(capability)


def require_capability(provider, capability, action=None):
    reason = adapter(provider).capability(capability)
    if reason is not None:
        raise ProviderRefused(provider, 'unsupported_capability',
                              f'{action or capability.replace("_", " ").capitalize()} is not supported. {reason}')


def managed_capable(provider):
    return all(supports(provider, name) for name in ('autonomous', 'delegation', 'hybrid'))


# ---------------------------------------------------------------- durable settings, acknowledgments and usage

DEFAULT_ENABLED = {'mock': 1, 'codex': 1, 'claude': 0, 'gemini': 0}
CONFIRMATION = ('I confirm that this provider uses my existing subscription sign-in and that paid overage, '
                'extra usage, credits and API billing are not enabled for the Control Center.')
CODEX_ACKNOWLEDGMENT = (
    'I understand that Codex runs through my ChatGPT subscription and that its usage limits apply. Existing '
    'credits or automatic credit purchases configured on my OpenAI account may still affect billing outside the '
    'Control Center. The Control Center never purchases credits, never enables a paid fallback and never uses an '
    'API key, but it cannot enforce account-side spending limits that OpenAI does not expose. This acknowledgment '
    'does not permit pay-per-use API routes.')
ACKNOWLEDGMENTS = {'codex': CODEX_ACKNOWLEDGMENT, 'claude': CONFIRMATION, 'gemini': CONFIRMATION}
# A usage observation older than this is shown as stale and never confirms availability.
USAGE_FRESH_SECONDS = 900
# What each provider can tell the Control Center about usage (Stage 2F investigation).
USAGE_SOURCES = {
    'mock': ('not_applicable', 'Mock agents use no provider.'),
    'codex': ('exhaustion_signal', 'Codex exposes no documented usage interface to the Control Center. Managed '
              'runs report usage-limit errors; ordinary runs use text output and cannot be classified.'),
    'claude': ('exact_when_reported', 'Claude Code reports usage meters through its documented /usage command '
               '(an experimental report shape) and reports the active limit during runs.'),
    'gemini': ('none', 'Gemini CLI exposes no documented usage or quota interface; failures are not classified '
               'as quota.'),
}
WINDOW_STATUSES = ('available', 'exhausted', 'unknown')
CLAUDE_METER_LABELS = {'session': 'Session limit', 'weekly_all': 'Weekly limit (all models)',
                       'weekly_scoped': 'Weekly limit'}


def migrate(db):
    """v30 -> v31. Additive and idempotent."""
    columns = {r['name'] for r in db.execute('PRAGMA table_info(agents)')}
    if 'model' not in columns:
        db.execute('ALTER TABLE agents ADD COLUMN model TEXT')
    if 'provider_condition' not in columns:
        db.execute('ALTER TABLE agents ADD COLUMN provider_condition TEXT')
    if 'model' not in {r['name'] for r in db.execute('PRAGMA table_info(executions)')}:
        db.execute('ALTER TABLE executions ADD COLUMN model TEXT')
    delegation_columns = {r['name'] for r in db.execute('PRAGMA table_info(delegations)')}
    if 'child_provider' not in delegation_columns:
        db.execute("ALTER TABLE delegations ADD COLUMN child_provider TEXT CHECK(child_provider IS NULL OR child_provider IN ('mock','codex','claude','gemini'))")
    if 'child_model' not in delegation_columns:
        db.execute('ALTER TABLE delegations ADD COLUMN child_model TEXT')
    db.execute(f'''CREATE TABLE IF NOT EXISTS provider_settings (
        provider TEXT PRIMARY KEY CHECK(provider IN ('mock','codex','claude','gemini')),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),
        enabled_basis TEXT NOT NULL,
        billing_confirmed_at TEXT,
        billing_confirmation TEXT,
        models TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL DEFAULT ({NOW}))''')
    for provider, enabled in DEFAULT_ENABLED.items():
        basis = ('pre_2f_default' if provider in ('mock', 'codex') else 'disabled_by_default')
        db.execute('INSERT OR IGNORE INTO provider_settings(provider,enabled,enabled_basis) VALUES (?,?,?)',
                   (provider, enabled, basis))
    db.execute(f'''CREATE TABLE IF NOT EXISTS provider_usage_blocks (
        block_id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL CHECK(provider IN ('codex','claude','gemini')),
        model TEXT,
        condition TEXT NOT NULL CHECK(condition IN ('quota_exhausted','rate_limited','auth_failed','billing_error')),
        reason TEXT NOT NULL,
        reset_at TEXT,
        reset_source TEXT CHECK(reset_source IS NULL OR reset_source = 'provider_reported'),
        execution_id TEXT,
        agent_id TEXT,
        observed_at TEXT NOT NULL DEFAULT ({NOW}),
        cleared_at TEXT,
        cleared_by TEXT CHECK(cleared_by IS NULL OR cleared_by IN ('user','provider_signal')),
        CHECK((reset_at IS NULL) = (reset_source IS NULL)))''')
    db.execute('CREATE INDEX IF NOT EXISTS provider_usage_active ON provider_usage_blocks(provider) WHERE cleared_at IS NULL')
    db.execute(f'''CREATE TABLE IF NOT EXISTS agent_provider_changes (
        change_id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        assignment_id TEXT,
        from_provider TEXT NOT NULL, from_model TEXT, from_sandbox TEXT, from_session_id TEXT,
        to_provider TEXT NOT NULL CHECK(to_provider IN ('mock','codex','claude','gemini')),
        to_model TEXT, to_sandbox TEXT,
        session_retained INTEGER NOT NULL CHECK(session_retained IN (0,1)),
        boundary TEXT NOT NULL,
        handoff_pending INTEGER NOT NULL DEFAULT 0 CHECK(handoff_pending IN (0,1)),
        first_execution_id TEXT,
        changed_at TEXT NOT NULL DEFAULT ({NOW}))''')
    db.execute('CREATE INDEX IF NOT EXISTS agent_provider_changes_agent ON agent_provider_changes(agent_id)')
    # Responsibility transfers named only the providers known when they were added.
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='responsibility_transfers'").fetchone()
    if row and "agent_type IN ('mock','codex')" in row[0]:
        sql = row[0].replace("agent_type IN ('mock','codex')", "agent_type IN ('mock','codex','claude','gemini')")
        sql = sql.replace('CREATE TABLE responsibility_transfers', 'CREATE TABLE responsibility_transfers_v31', 1)
        indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                                            "AND tbl_name='responsibility_transfers' AND sql IS NOT NULL")]
        db.execute(sql)
        db.execute('INSERT INTO responsibility_transfers_v31 SELECT * FROM responsibility_transfers')
        db.execute('DROP TABLE responsibility_transfers')
        db.execute('ALTER TABLE responsibility_transfers_v31 RENAME TO responsibility_transfers')
        for statement in indexes:
            db.execute(statement)


def migrate_v32(db):
    """v31 -> v32. Additive and idempotent: usage windows, block provenance and resume intents."""
    block_columns = {r['name'] for r in db.execute('PRAGMA table_info(provider_usage_blocks)')}
    if 'window_id' not in block_columns:
        db.execute('ALTER TABLE provider_usage_blocks ADD COLUMN window_id TEXT')
    if 'clear_reason' not in block_columns:
        db.execute('ALTER TABLE provider_usage_blocks ADD COLUMN clear_reason TEXT')
    db.execute(f'''CREATE TABLE IF NOT EXISTS provider_usage_windows (
        provider TEXT NOT NULL CHECK(provider IN ('codex','claude','gemini')),
        window_id TEXT NOT NULL CHECK(length(window_id) BETWEEN 1 AND 80),
        scope_key TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL,
        kind TEXT,
        group_name TEXT,
        model_scope TEXT,
        entitlement_scope TEXT,
        duration_seconds INTEGER CHECK(duration_seconds IS NULL OR duration_seconds > 0),
        used_percent REAL CHECK(used_percent IS NULL OR used_percent BETWEEN 0 AND 100),
        remaining_percent REAL CHECK(remaining_percent IS NULL OR remaining_percent BETWEEN 0 AND 100),
        remaining_amount REAL CHECK(remaining_amount IS NULL OR remaining_amount >= 0),
        status TEXT NOT NULL CHECK(status IN ('available','exhausted','unknown')),
        reset_at TEXT,
        reset_source TEXT CHECK(reset_source IS NULL OR reset_source = 'provider_reported'),
        severity TEXT,
        observed_at TEXT NOT NULL DEFAULT ({NOW}),
        source TEXT NOT NULL,
        PRIMARY KEY(provider, window_id, scope_key),
        CHECK((reset_at IS NULL) = (reset_source IS NULL)))''')
    db.execute(f'''CREATE TABLE IF NOT EXISTS agent_resume_requests (
        request_id TEXT PRIMARY KEY,
        agent_id TEXT NOT NULL REFERENCES agents(agent_id),
        provider TEXT NOT NULL, model TEXT,
        mode TEXT NOT NULL CHECK(mode IN ('verified','explicit_retry')),
        overridden TEXT NOT NULL DEFAULT '[]',
        outcome TEXT NOT NULL CHECK(outcome IN ('pending','admitted','refused')),
        reason TEXT,
        execution_id TEXT,
        requested_at TEXT NOT NULL DEFAULT ({NOW}),
        completed_at TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS agent_resume_requests_agent ON agent_resume_requests(agent_id)')


def exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE name='provider_settings'").fetchone() is not None


def _has(db, table):
    return bool(db) and db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone() is not None


# Settings when the backend runs without persistence (direct manager use): never
# shared with a database and lost on restart.
_memory_settings: dict[str, dict] = {}


def settings(db, provider):
    adapter(provider)
    row = db.execute('SELECT * FROM provider_settings WHERE provider=?', (provider,)).fetchone() if db and exists(db) else None
    if row is None:
        memory = _memory_settings.get(provider, {})
        return {'provider': provider, 'enabled': memory.get('enabled', bool(DEFAULT_ENABLED[provider])),
                'enabled_basis': 'pre_2f_default' if DEFAULT_ENABLED[provider] else 'disabled_by_default',
                'billing_confirmed': memory.get('billing_confirmed_at') is not None,
                'billing_confirmed_at': memory.get('billing_confirmed_at'), 'models': []}
    return {'provider': provider, 'enabled': bool(row['enabled']), 'enabled_basis': row['enabled_basis'],
            'billing_confirmed': row['billing_confirmed_at'] is not None,
            'billing_confirmed_at': row['billing_confirmed_at'], 'models': json.loads(row['models'] or '[]')}


def acknowledge_without_persistence(provider, acknowledged=True):
    """No-persistence mode only (the database, when present, is authoritative)."""
    _memory_settings.setdefault(provider, {})['billing_confirmed_at'] = _now() if acknowledged else None


def update_settings(db, provider, *, enabled=None, billing_confirmed=None, models=None):
    """Inside BEGIN IMMEDIATE. Mock is the development default and cannot be disabled.

    The billing acknowledgment is the user's statement about their arrangement; it
    is stored separately from, and never replaces, the verified billing route.
    """
    adapter(provider)
    if provider == 'mock' and (enabled is False or billing_confirmed is not None or models is not None):
        raise ValueError('The Mock provider is the development default; it has no settings to change.')
    current = settings(db, provider)
    if models is not None:
        if not isinstance(models, list) or len(models) > 32:
            raise ValueError('A model catalog holds at most 32 model identifiers.')
        models = sorted({validate_model(m) for m in models})
    confirmed_at = current['billing_confirmed_at']
    if billing_confirmed is True and confirmed_at is None:
        confirmed_at = _now()
    elif billing_confirmed is False:
        confirmed_at = None
    if enabled and provider in ('claude', 'gemini') and confirmed_at is None:
        raise ProviderRefused(provider, 'billing_confirmation_required',
                              'Enabling requires your confirmation of the billing route.', adapter(provider).billing_guidance)
    new_enabled = current['enabled'] if enabled is None else bool(enabled)
    if provider in ('claude', 'gemini') and confirmed_at is None:
        new_enabled = False  # Withdrawing the confirmation disables new work.
    basis = 'user' if enabled is not None else current['enabled_basis']
    db.execute(f'''UPDATE provider_settings SET enabled=?,enabled_basis=?,billing_confirmed_at=?,
        billing_confirmation=?,models=?,updated_at={NOW} WHERE provider=?''',
        (int(new_enabled), basis, confirmed_at, ACKNOWLEDGMENTS.get(provider) if confirmed_at else None,
         json.dumps(models if models is not None else current['models']), provider))
    return settings(db, provider)


def active_blocks(db, provider, model=None):
    if provider not in REAL or not db or not exists(db):
        return []
    return [dict(r) for r in db.execute('''SELECT * FROM provider_usage_blocks WHERE provider=? AND cleared_at IS NULL
        AND (model IS NULL OR ? IS NULL OR model=?) ORDER BY block_id''', (provider, model, model))]


def _iso(value):
    """A provider-reported timestamp (ISO string or epoch seconds) as ISO UTC, or None. Never estimated."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timestamp without a time zone')
        return parsed.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    raise ValueError('invalid timestamp')


def record_block(db, provider, condition, reason, *, model=None, execution_id=None, agent_id=None, reset_at=None,
                 window_id=None):
    """A provider-reported limit. A reset time is stored only when the provider reported one."""
    if condition not in BLOCKING or provider not in REAL:
        return None
    if reset_at is not None:
        reset_at = _iso(reset_at)  # Never store an unparseable estimate.
    existing = db.execute('''SELECT block_id FROM provider_usage_blocks WHERE provider=? AND cleared_at IS NULL
        AND condition=? AND model IS ?''', (provider, condition, model)).fetchone()
    if existing:
        if reset_at is not None:
            db.execute("UPDATE provider_usage_blocks SET reset_at=?,reset_source='provider_reported' WHERE block_id=?",
                       (reset_at, existing[0]))
        return existing[0]
    cursor = db.execute('''INSERT INTO provider_usage_blocks(provider,model,condition,reason,reset_at,reset_source,
        execution_id,agent_id,window_id) VALUES (?,?,?,?,?,?,?,?,?)''',
        (provider, model, condition, reason[:500], reset_at, 'provider_reported' if reset_at else None,
         execution_id, agent_id, window_id))
    return cursor.lastrowid


def clear_blocks(db, provider, *, by='user', conditions=BLOCKING, reason=None):
    marks = ','.join('?' for _ in conditions)
    cursor = db.execute(f'''UPDATE provider_usage_blocks SET cleared_at={NOW},cleared_by=?,clear_reason=?
        WHERE provider=? AND cleared_at IS NULL AND condition IN ({marks})''', (by, reason, provider, *conditions))
    return cursor.rowcount


# ---------------------------------------------------------------- unpersisted blocks (current controller)

# A known limit whose durable write failed. In memory only, so it protects only
# while this backend runs (the Stage 2D.1b ownership-guard principle): it is
# added before the write is attempted, consulted by every admission inside its
# transaction, and removed only after a later write of the same block commits.
# A successful read never removes it. A crash before that write loses it.
_unpersisted: list[dict] = []
_unpersisted_lock = Lock()


def unpersisted(provider=None):
    with _unpersisted_lock:
        return [dict(entry) for entry in _unpersisted if provider is None or entry['provider'] == provider]


def note_block(connect, provider, condition, reason, **details):
    """Fail closed first, then persist. `connect` opens a store connection (or is None)."""
    if condition not in BLOCKING or provider not in REAL:
        return None
    entry = dict(provider=provider, condition=condition, reason=reason[:500], token=os.urandom(8).hex(),
                 observed_at=_now(), **details)
    with _unpersisted_lock:
        _unpersisted.append(entry)
    reconcile(connect)
    return entry


def reconcile(connect):
    """Write every unpersisted block durably; each leaves memory only after its own commit."""
    if connect is None:
        return 0
    written = 0
    for entry in unpersisted():
        details = {k: entry.get(k) for k in ('model', 'execution_id', 'agent_id', 'reset_at', 'window_id')}
        try:
            with connect() as db:
                db.execute('BEGIN IMMEDIATE')
                record_block(db, entry['provider'], entry['condition'], entry['reason'], **details)
                db.commit()
        except Exception:
            logger.exception('A provider usage block could not be persisted yet; it stays enforced in memory')
            continue
        with _unpersisted_lock:
            _unpersisted[:] = [item for item in _unpersisted if item['token'] != entry['token']]
        written += 1
    return written


# ---------------------------------------------------------------- usage windows

def record_windows(db, provider, windows, *, source):
    """Store provider-reported usage windows (latest observation per window). Never invents a value."""
    stored = []
    for window in windows:
        status = window.get('status')
        if status not in WINDOW_STATUSES:
            raise ValueError('invalid usage window status')
        values = dict(provider=provider, window_id=str(window['window_id'])[:80],
                      scope_key=str(window.get('scope_key') or '')[:120], label=str(window['label'])[:120],
                      kind=window.get('kind'), group_name=window.get('group_name'), model_scope=window.get('model_scope'),
                      entitlement_scope=window.get('entitlement_scope'), duration_seconds=window.get('duration_seconds'),
                      used_percent=window.get('used_percent'), remaining_percent=window.get('remaining_percent'),
                      remaining_amount=window.get('remaining_amount'), status=status,
                      reset_at=_iso(window.get('reset_at')),
                      severity=window.get('severity'), observed_at=window.get('observed_at') or _now(), source=source)
        values['reset_source'] = 'provider_reported' if values['reset_at'] else None
        db.execute('''INSERT OR REPLACE INTO provider_usage_windows(provider,window_id,scope_key,label,kind,group_name,
            model_scope,entitlement_scope,duration_seconds,used_percent,remaining_percent,remaining_amount,status,
            reset_at,reset_source,severity,observed_at,source) VALUES (:provider,:window_id,:scope_key,:label,:kind,
            :group_name,:model_scope,:entitlement_scope,:duration_seconds,:used_percent,:remaining_percent,
            :remaining_amount,:status,:reset_at,:reset_source,:severity,:observed_at,:source)''', values)
        stored.append(values)
    return stored


def _age_seconds(stamp):
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace('Z', '+00:00'))).total_seconds()
    except (ValueError, AttributeError):
        return None


def _in_future(stamp):
    age = _age_seconds(stamp) if stamp else None
    return age is not None and age < 0


def usage_windows(db, provider):
    if provider not in REAL or not _has(db, 'provider_usage_windows'):
        return []
    result = []
    for row in db.execute('SELECT * FROM provider_usage_windows WHERE provider=? ORDER BY group_name,window_id,scope_key',
                          (provider,)):
        window = dict(row)
        age = _age_seconds(window['observed_at'])
        window['fresh'] = age is not None and age <= USAGE_FRESH_SECONDS
        window['reset_passed'] = bool(window['reset_at']) and not _in_future(window['reset_at'])
        result.append(window)
    return result


def window_applies(window, model):
    """Uncertain scope applies (never assumed to affect no model). A named model scope
    applies unless the Agent's explicitly selected model is recognizably another one."""
    scope = (window.get('model_scope') or '').strip().lower()
    if not scope:
        return True
    if not model:
        return True
    tokens = [token for token in re.split(r'[^a-z0-9.]+', scope) if token and token not in ('claude', 'model', 'models')]
    return any(token in model.lower() for token in tokens) if tokens else True


def windows_from_claude_report(report):
    """`usage_report.rate_limits.limits` rows, verbatim: used percent and provider reset only."""
    rate = report.get('rate_limits') if isinstance(report, dict) else None
    limits = rate.get('limits') if isinstance(rate, dict) else None
    if not isinstance(limits, list):
        return None  # Claude Code had no rows to report: usage stays unknown.
    windows = []
    for row in limits:
        if not isinstance(row, dict) or not isinstance(row.get('kind'), str):
            raise ValueError('invalid usage row')
        percent = row.get('percent')
        if isinstance(percent, bool) or not isinstance(percent, (int, float)) or not 0 <= percent <= 100:
            raise ValueError('invalid usage percent')
        scope = row.get('scope') if isinstance(row.get('scope'), dict) else {}
        model = (scope.get('model') or {}).get('display_name') if isinstance(scope.get('model'), dict) else None
        surface = (scope.get('surface') or {}).get('display_name') if isinstance(scope.get('surface'), dict) else None
        label = CLAUDE_METER_LABELS.get(row['kind'], row['kind'].replace('_', ' ').capitalize())
        if model or surface:
            label += f' ({model or surface})'
        windows.append(dict(window_id=row['kind'], scope_key=model or surface or '', label=label, kind=row['kind'],
                            group_name=row.get('group') if isinstance(row.get('group'), str) else None,
                            model_scope=model, entitlement_scope=surface, used_percent=float(percent),
                            status='exhausted' if percent >= 100 else 'available',
                            reset_at=row.get('resets_at'), severity=row.get('severity') if isinstance(row.get('severity'), str) else None))
    return windows


LIMIT_TYPE = re.compile(r'[a-z][a-z0-9_]{0,39}')


def window_from_claude_event(info, observed_at=None):
    """A `rate_limit_event`: the limit's status and provider reset.

    The SDK documents status, resetsAt and the credits fields. When an event also
    carries `rateLimitType`, its raw identifier is kept verbatim as the window id;
    no meaning (such as a duration) is attached to it, and no percentage is derived.
    An event without it describes the active limit only.
    """
    status = info.get('status') if isinstance(info, dict) else None
    if status not in ('allowed', 'allowed_warning', 'rejected'):
        return None
    raw = info.get('rateLimitType')
    known = isinstance(raw, str) and LIMIT_TYPE.fullmatch(raw)
    return dict(window_id=f'limit:{raw}' if known else 'active_limit', kind=raw if known else None,
                label=(f'Claude Code limit "{raw}"' if known else 'Current Claude Code limit (window not reported)'),
                status='exhausted' if status == 'rejected' else 'available', reset_at=info.get('resetsAt'),
                severity='warning' if status == 'allowed_warning' else None, observed_at=observed_at)


def constraints(db, provider, model=None):
    """Every known limit that applies to a new execution of this provider and model.

    `hard` constraints cannot be bypassed even by an explicit user retry: a limit
    that is not yet durable, one with a provider-reported reset still in the
    future, or a fresh exhausted reading that reported no reset.
    """
    if provider not in REAL:
        return []
    result = []
    for entry in unpersisted(provider):
        result.append(dict(kind='unpersisted', id=f'memory:{entry["token"]}', label=entry['reason'],
                           condition=entry['condition'], reset_at=entry.get('reset_at'), hard=True))
    for block in active_blocks(db, provider, model):
        result.append(dict(kind='block', id=f'block:{block["block_id"]}', label=block['reason'],
                           condition=block['condition'], reset_at=block['reset_at'],
                           hard=_in_future(block['reset_at']), window_id=block.get('window_id')))
    for window in usage_windows(db, provider):
        if window['status'] != 'exhausted' or not window_applies(window, model):
            continue
        result.append(dict(kind='window', id=f'window:{window["window_id"]}:{window["scope_key"]}',
                           label=window['label'], condition=QUOTA_EXHAUSTED, reset_at=window['reset_at'],
                           # Confirmed: a provider reset still ahead, or a fresh reading with no reset
                           # reported. A reading whose reported reset has passed confirms nothing.
                           hard=_in_future(window['reset_at']) or (window['fresh'] and not window['reset_at']),
                           fresh=window['fresh'], model_scope=window['model_scope'],
                           observed_at=window['observed_at'], window_id=window['window_id']))
    return result


def describe_constraints(items):
    if not items:
        return ''
    labels = [item['label'] for item in items]
    resets = [item['reset_at'] for item in items if item.get('reset_at') and _in_future(item['reset_at'])]
    text = ('Usage limits reached: ' + '; '.join(labels[:4])) if len(items) > 1 else labels[0]
    if resets:
        text += f'. Provider-reported reset: {max(resets)} (not a guarantee of availability)'
    else:
        text += '. The reset time is unknown'
    return text + '.'


def clear_overridden(db, items, request_id):
    """After an explicitly retried launch commits: the overridden limits are superseded, not forgotten."""
    reason = f'explicit_retry:{request_id}'
    for item in items:
        if item['kind'] == 'block':
            db.execute(f'''UPDATE provider_usage_blocks SET cleared_at={NOW},cleared_by='user',clear_reason=?
                WHERE block_id=? AND cleared_at IS NULL''', (reason, int(item['id'].split(':', 1)[1])))
        elif item['kind'] == 'window':
            _, window_id, scope_key = item['id'].split(':', 2)
            db.execute(f'''UPDATE provider_usage_windows SET status='unknown',used_percent=NULL,observed_at={NOW},
                source='explicit_user_retry' WHERE window_id=? AND scope_key=? AND status='exhausted' ''',
                       (window_id, scope_key))


# ---------------------------------------------------------------- readiness cache and policy

_cache: dict[str, tuple[Readiness, float, tuple]] = {}
_cache_lock = Lock()
_probe_locks: dict[str, Lock] = {provider: Lock() for provider in IDS}


# ---------------------------------------------------------------- account identity evidence

def _home():
    return Path(os.environ.get('USERPROFILE') or Path.home())


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    except (OSError, ValueError):
        return None


def account_evidence(provider):
    """A hash of the signed-in account as the CLI records it locally, or None when not exposed.

    Only identifiers and plan-tier fields are read (Codex `auth.json` account id and
    mode, Claude Code `oauthAccount`, Gemini CLI's active Google account), never a
    token, and only their hash leaves this function. A different account, organization
    or plan tier gives a different value, so cached entitlements of the previous
    account are never reused. These files are not documented contracts: a missing or
    unrecognized file means the identity is not exposed.
    """
    from hashlib import sha256
    values = None
    if provider == 'codex':
        data = _read_json(Path(os.environ.get('CODEX_HOME') or _home() / '.codex') / 'auth.json')
        tokens = data.get('tokens') if isinstance(data, dict) and isinstance(data.get('tokens'), dict) else {}
        if isinstance(tokens.get('account_id'), str) and tokens['account_id']:
            values = ['codex', data.get('auth_mode'), tokens['account_id']]
    elif provider == 'claude':
        folder = os.environ.get('CLAUDE_CONFIG_DIR')
        data = _read_json(Path(folder) / '.claude.json' if folder else _home() / '.claude.json')
        account = data.get('oauthAccount') if isinstance(data, dict) else None
        if isinstance(account, dict) and isinstance(account.get('accountUuid'), str) and account['accountUuid']:
            values = ['claude', *(account.get(k) for k in ('accountUuid', 'organizationUuid', 'organizationType',
                                                          'seatTier', 'billingType', 'userRateLimitTier',
                                                          'organizationRateLimitTier'))]
    elif provider == 'gemini':
        home = Path(os.environ.get('GEMINI_CLI_HOME') or _home())
        data = _read_json(home / '.gemini' / 'google_accounts.json')
        if isinstance(data, dict) and isinstance(data.get('active'), str) and data['active']:
            values = ['gemini', data['active']]
    if values is None:
        return None
    return sha256(json.dumps(values, default=str).encode()).hexdigest()[:24]


# Gemini CLI model families, recognized from the provider-reported model names.
def gemini_family(name):
    name = (name or '').lower()
    if 'flash-lite' in name:
        return 'flash-lite'
    if 'flash' in name:
        return 'flash'
    if re.search(r'(^|-)pro($|-)', name):
        return 'pro'
    return None


def model_enforcement(provider, requested, observed, initial=None):
    """Evidence about the model that produced a Gemini CLI response.

    Returns (state, note) with state one of 'verified' (every model in the usage
    statistics is the selected one), 'routed' (Auto or the provider default: no pin),
    'alternatives' (the selected model and others were used; the primary one is not
    identified), 'mismatch' (the selected model is absent) or 'insufficient' (no usage
    statistics). `observed` holds only result usage statistics; the init event's
    configured model (`initial`) is never evidence of what produced the response.
    Explicit Gemini pins are refused before launch, so this is a defense in depth.
    """
    observed = sorted(set(observed or ()))
    configured = f' Configured at start: {initial}.' if initial else ''
    if provider != 'gemini':
        return 'verified', None
    listing = ', '.join(observed)
    if not requested or requested == 'auto':
        text = (f'Gemini CLI routed this run; models in its usage statistics: {listing}.' if observed
                else 'Gemini CLI routed this run and reported no usage statistics.')
        return 'routed', text + configured
    if not observed:
        return 'insufficient', (f'Gemini CLI reported no usage statistics, so it is not verified that {requested} '
                                'produced the response.' + configured)
    family = requested in ('pro', 'flash', 'flash-lite')
    matched = [m for m in observed if (gemini_family(m) == requested if family else m == requested)]
    if not matched:
        return 'mismatch', (f'Gemini CLI usage statistics report {listing}, not the selected {requested}; the selected '
                            'model was not enforced.' + configured)
    if len(matched) < len(observed):
        return 'alternatives', (f'Gemini CLI usage statistics report {listing}; which model produced the response is '
                                f'not identified, so {requested} is not verified.' + configured)
    return 'verified', None


def _fingerprint(provider):
    """What a cached result was observed for: the executable and the configuration the probe read.

    Installing, updating or removing a CLI, or editing the configuration a probe
    validated, changes it, so a cached result never describes something else.
    """
    item = adapter(provider)
    try:
        location = item.locate()
        stamp = location.stat().st_mtime_ns if location is not None else None
    except OSError:
        location, stamp = None, None
    extra = item.config_fingerprint() if hasattr(item, 'config_fingerprint') else None
    return (str(location) if location is not None else None, stamp, extra)


def invalidate(provider=None):
    with _cache_lock:
        if provider is None:
            _cache.clear()
        else:
            _cache.pop(provider, None)


def cached(provider):
    with _cache_lock:
        entry = _cache.get(provider)
    if not entry or entry[2] != _fingerprint(provider):
        return None
    return entry[0]


def probe(provider, *, force=False):
    """Installation, version, authentication and billing route (no settings, no usage)."""
    if provider == 'mock':
        return ADAPTERS['mock'].probe()
    with _probe_locks[provider]:
        fingerprint = _fingerprint(provider)
        with _cache_lock:
            entry = _cache.get(provider)
        if entry and not force and entry[2] == fingerprint:
            result, at, _ = entry
            if time.monotonic() - at < (READY_TTL if result.ready else NOT_READY_TTL):
                return result
        try:
            result = adapter(provider).probe()
        except Exception as exc:  # A failed check never counts as ready.
            result = Readiness(provider, UNAVAILABLE, f'The readiness check could not run ({type(exc).__name__}).',
                               'Choose Check again.', adapter(provider).docs_url)
        with _cache_lock:
            _cache[provider] = (result, time.monotonic(), fingerprint)
        return result


def policy_snapshot(provider):
    """Recorded with a validated command; the admission transaction compares it again."""
    return {'provider': provider, 'config': _fingerprint(provider)[2]}


def evaluate(db, provider, *, force=False, probe_installation=True, model=None):
    """The provider's effective state for new work: settings, acknowledgment, usage and probe combined."""
    item = adapter(provider)
    config = settings(db, provider)
    if not config['enabled']:
        guidance = ('Enable it in the provider panel after confirming its billing route.'
                    if provider in ('claude', 'gemini') else 'Enable it in the provider panel.')
        state = Readiness(provider, DISABLED, f'{item.label} is not enabled for new work.', guidance, item.docs_url)
        found = (probe(provider, force=force) if probe_installation else cached(provider)) if provider != 'mock' else None
        if found is not None:
            state.installed, state.version = found.installed, found.version
            state.authenticated, state.auth_route, state.billing_route = found.authenticated, found.auth_route, found.billing_route
        return state
    found = probe(provider, force=force) if probe_installation else (
        ADAPTERS['mock'].probe() if provider == 'mock' else cached(provider))
    if found is not None and not found.ready:
        return found
    known = dict(installed=found.installed, version=found.version, authenticated=found.authenticated,
                 auth_route=found.auth_route, billing_route=found.billing_route) if found else {}
    if provider in REAL and not config['billing_confirmed']:
        what = 'acknowledgment' if provider == 'codex' else 'confirmation'
        return Readiness(provider, BILLING_BLOCKED, f'The billing {what} is required before new executions.',
                         'Review and accept it in the provider panel.', item.docs_url, **known)
    limits = constraints(db, provider, model)
    if limits:
        # Recorded provider-reported limits are shown even before a new check.
        state = {AUTH_FAILED: NOT_AUTHENTICATED, BILLING_ERROR: BILLING_BLOCKED}.get(limits[0]['condition'], USAGE_LIMITED)
        return Readiness(provider, state, describe_constraints(limits),
                         'Refresh usage where supported, then resume a waiting agent explicitly.', item.docs_url, **known)
    if found is None:
        return Readiness(provider, UNCHECKED, 'Not checked yet.', 'Choose Check again.', item.docs_url)
    return found


def require_launchable(db, provider, *, model=None, sandbox='read-only', managed=False, child=False, override=(),
                       effort=None):
    """Before any reservation or process: refuse with an actionable reason."""
    item = adapter(provider)
    if provider == 'mock':
        item.classify_model(model)
        if effort is not None:
            raise ValueError('Mock agents do not accept an effort level.')
        return Readiness('mock', READY)
    validate_model(model)
    validate_effort(effort)
    if effort is not None and provider == 'gemini':
        raise ProviderRefused('gemini', 'unsupported_effort', 'Gemini CLI has no documented command-line reasoning '
                              'control; it runs with its provider default.')
    if model is not None:
        require_capability(provider, 'model_selection', 'Model selection')
    if managed:
        require_capability(provider, 'delegation', 'Managed orchestration')
    if child:
        require_capability(provider, 'delegated_child', 'Running as a delegated Child')
    item.sandbox_arguments(sandbox, managed=managed and provider != 'gemini')  # Unsupported access fails here.
    found = probe(provider)
    if not found.ready:
        raise ProviderRefused(provider, found.state, found.reason or found.state, found.guidance, found.docs_url)
    if provider == 'claude' and model is not None and CLAUDE.user_fallback_chain:
        # A configured fallback chain could run another model than the one selected.
        raise ProviderRefused('claude', INCOMPATIBLE, f'A fallbackModel chain is configured ({CLAUDE.user_fallback_chain}), '
                              'so an explicitly selected model cannot be guaranteed.',
                              'Remove fallbackModel from that settings file, or use the provider default model.')
    _require_policy(db, provider, model, override=override)
    return found


def _require_policy(db, provider, model, *, override=(), acknowledgment=True):
    item = adapter(provider)
    config = settings(db, provider)
    if not config['enabled']:
        raise ProviderRefused(provider, DISABLED, f'{item.label} is not enabled for new work.',
                              'Enable it in the provider panel.', item.docs_url)
    if acknowledgment and not config['billing_confirmed']:
        what = 'acknowledgment' if provider == 'codex' else 'confirmation'
        raise ProviderRefused(provider, BILLING_BLOCKED, f'The billing {what} is required before new executions.',
                              ACKNOWLEDGMENTS.get(provider), item.docs_url)
    allowed = set(override)
    limits = constraints(db, provider, model)
    blocking = [c for c in limits if c['hard'] or c['id'] not in allowed]
    if blocking:
        state = {AUTH_FAILED: NOT_AUTHENTICATED, BILLING_ERROR: BILLING_BLOCKED}.get(blocking[0]['condition'], USAGE_LIMITED)
        raise ProviderRefused(provider, state, describe_constraints(blocking),
                              'No new work is admitted while a known limit applies. Refresh usage where supported, '
                              'or resume a waiting agent explicitly once the limit may have renewed.', item.docs_url)
    return [c for c in limits if c['id'] in allowed]


def admission_check(db, provider, *, model=None, policy=None, override=()):
    """Inside the authoritative admission transaction, immediately before Popen.

    Rechecks enablement, the acknowledgment, every known limit (including limits
    that could not be persisted) and any negative readiness or configuration
    change observed since the launch was prepared. Never probes: no subprocess runs
    while the write lock is held. Returns the limits an explicit retry overrides.
    """
    if provider == 'mock':
        return []
    item = adapter(provider)
    config = settings(db, provider)
    if not config['enabled']:
        raise ProviderRefused(provider, DISABLED, f'{item.label} was disabled before the launch was admitted.',
                              'Enable it again in the provider panel.', item.docs_url)
    if (provider in ('claude', 'gemini') or policy is not None) and not config['billing_confirmed']:
        raise ProviderRefused(provider, BILLING_BLOCKED, 'The billing acknowledgment was withdrawn.',
                              ACKNOWLEDGMENTS.get(provider), item.docs_url)
    if policy is not None and policy.get('config') != _fingerprint(provider)[2]:
        raise ProviderRefused(provider, BILLING_BLOCKED, 'The provider configuration changed after it was validated.',
                              'Choose Check again so the new configuration is validated.', item.docs_url)
    # A command not built by the validating builder (direct test doubles) carries
    # no policy: its acknowledgment was not part of what was validated.
    overridden = _require_policy(db, provider, model, override=override,
                                 acknowledgment=policy is not None or provider in ('claude', 'gemini'))
    latest = cached(provider)
    if latest is not None and not latest.ready:
        raise ProviderRefused(provider, latest.state, latest.reason or latest.state, latest.guidance, item.docs_url)
    return overridden


def resume_assessment(db, provider, model=None):
    """Whether a waiting Agent can be resumed now, without probing. The resume itself rechecks everything."""
    if provider not in REAL:
        return {'available': False, 'mode': None, 'reason': 'This provider cannot resume work.', 'blocking': []}
    config = settings(db, provider)
    if not config['enabled']:
        return {'available': False, 'mode': None, 'reason': f'{LABELS[provider]} is disabled.', 'blocking': []}
    if not config['billing_confirmed']:
        return {'available': False, 'mode': None, 'reason': 'Billing acknowledgment required.', 'blocking': []}
    limits = constraints(db, provider, model)
    hard = [c for c in limits if c['hard']]
    latest = cached(provider)
    if latest is not None and not latest.ready:
        return {'available': False, 'mode': None, 'reason': latest.reason or latest.state, 'blocking': limits}
    if hard:
        return {'available': False, 'mode': None, 'reason': describe_constraints(hard), 'blocking': limits}
    if limits:
        return {'available': True, 'mode': 'explicit_retry', 'blocking': limits,
                'reason': (describe_constraints(limits) + ' Availability cannot be confirmed without a request; an '
                           'explicit retry uses the verified subscription route and may meet the limit again.')}
    return {'available': True, 'mode': 'verified', 'reason': 'Manual resume available.', 'blocking': []}


def usage_view(db, provider):
    source, explanation = USAGE_SOURCES[provider]
    windows = usage_windows(db, provider)
    for window in windows:
        window['applies_to'] = window['model_scope'] or 'Shared or scope not reported'
    exhausted = [w for w in windows if w['status'] == 'exhausted']
    blocks = [{k: b[k] for k in ('block_id', 'model', 'condition', 'reason', 'reset_at', 'reset_source', 'observed_at',
                                  'window_id')} for b in active_blocks(db, provider)]
    pending = unpersisted(provider)
    if provider == 'mock':
        overall = 'not_applicable'
    elif pending or blocks or exhausted:
        overall = 'limited'
    elif windows and all(w['status'] == 'available' and w['fresh'] for w in windows):
        overall = 'available'
    else:
        overall = 'unknown'
    observed = max((w['observed_at'] for w in windows), default=None)
    return {'source': source, 'explanation': explanation, 'overall': overall, 'windows': windows, 'blocks': blocks,
            'unpersisted': len(pending), 'last_observed_at': observed,
            'refresh_supported': provider == 'claude',
            # Never estimated: only provider-reported values appear in `windows`.
            'remaining': None, 'remaining_source': 'not_reported_by_provider'}


def status(db, provider, *, force=False, probe_installation=True):
    item = adapter(provider)
    state = evaluate(db, provider, force=force, probe_installation=probe_installation)
    config = settings(db, provider)
    return {**item.describe(), 'state': state.as_dict(), 'settings': config, 'usage': usage_view(db, provider),
            'confirmation_text': ACKNOWLEDGMENTS.get(provider),
            'acknowledgment_kind': 'acknowledgment' if provider == 'codex' else 'confirmation' if provider in REAL else None}


def condition_view(raw):
    """The persisted provider condition of a waiting Agent, as a closed enum view."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    kind = value.get('kind') if isinstance(value, dict) else None
    if kind not in WAITING_LABELS:
        return None
    blocking = [{k: item.get(k) for k in ('label', 'reset_at', 'window_id')}
                for item in value.get('blocking') or [] if isinstance(item, dict)][:8]
    return {'kind': kind, 'label': WAITING_LABELS[kind], 'provider': value.get('provider'),
            'model': value.get('model'), 'condition': value.get('condition') if value.get('condition') in CONDITIONS else None,
            'reset_at': value.get('reset_at'), 'resume_available': bool(value.get('resume_available')),
            'blocking': blocking, 'observed_at': value.get('observed_at'),
            'session_preserved': bool(value.get('session_preserved')),
            'detail': str(value['detail'])[:500] if value.get('detail') else None}
