"""Disposable fake provider CLIs for Stage 2F tests. They never call a model.

Each fake is a Python script behind the same launcher a real installation uses
(`%APPDATA%\\npm\\<name>.cmd`). It answers `--version`, `--help` and the local
sign-in status commands, and for a run it reads the prompt from stdin and prints
the documented JSONL event shapes for a scenario chosen by environment variables.
Every invocation is appended to a log with its arguments, the names (never the
values) of credential-like environment variables it received, and the prompt.
"""
from pathlib import Path
import json
import os
import sys

FAKE_CLI = r'''
import json, os, sys, time, uuid
provider = sys.argv[1]
args = sys.argv[2:]
scenario = os.environ.get('FAKE_' + provider.upper() + '_SCENARIO') or os.environ.get('FAKE_CLI_SCENARIO', 'success')
log = os.environ.get('FAKE_CLI_LOG')


def record(kind, prompt=None):
    if not log:
        return
    names = sorted(k for k in os.environ if k.endswith('API_KEY') or k.startswith(('ANTHROPIC', 'GEMINI_API', 'GOOGLE_', 'CODEX_')))
    with open(log, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps({'provider': provider, 'kind': kind, 'args': args, 'env': names,
                                 'prompt': prompt, 'scenario': scenario}) + '\n')


def emit(event):
    print(json.dumps(event), flush=True)


HELP = {
    'claude': ' '.join(['--print', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'dontAsk',
                        'acceptEdits', '--disallowedTools', '--setting-sources', '--strict-mcp-config', '--resume',
                        '--model', '--no-session-persistence', '--effort', '--settings', '--input-format']),
    'gemini': ' '.join(['--output-format', 'stream-json', '--approval-mode', 'auto_edit', '--model', '--resume']),
    'codex': 'Usage: codex exec',
}
if '--version' in args:
    if os.environ.get('FAKE_' + provider.upper() + '_BROKEN'):
        sys.exit(3)
    print({'claude': os.environ.get('FAKE_CLAUDE_VERSION', '2.1.198') + ' (Claude Code)', 'gemini': '0.30.0',
           'codex': 'codex-cli 0.154.0'}[provider])
    sys.exit(0)
DEFAULT_CLAUDE_MODELS = json.loads(@@CLAUDE_MODELS@@)
DEFAULT_CODEX_MODELS = json.loads(@@CODEX_MODELS@@)
if provider == 'codex' and args[:1] == ['debug']:
    record('discovery')
    if '--help' in args:
        print('' if os.environ.get('FAKE_CODEX_NO_DISCOVERY') else 'Commands:\n  models  Render the raw model catalog as JSON')
        sys.exit(0)
    if os.environ.get('FAKE_CODEX_DISCOVERY_SLEEP'):
        time.sleep(float(os.environ['FAKE_CODEX_DISCOVERY_SLEEP']))
    print(os.environ.get('FAKE_CODEX_MODELS') or json.dumps(DEFAULT_CODEX_MODELS))
    sys.exit(0)
if '--help' in args:
    print('' if os.environ.get('FAKE_' + provider.upper() + '_OLD') else HELP[provider])
    sys.exit(0)
if provider == 'claude' and args[:2] == ['auth', 'status']:
    record('auth')
    route = os.environ.get('FAKE_CLAUDE_AUTH', 'subscription')
    print(json.dumps({'subscription': {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'},
                      'none': {'loggedIn': False, 'authMethod': 'none', 'apiProvider': 'firstParty'},
                      'apikey': {'loggedIn': True, 'authMethod': 'api_key', 'apiProvider': 'firstParty'}}[route]))
    sys.exit(0)
if provider == 'claude' and '--input-format' in args:
    # The SDK initialize control request: models and account, no model turn.
    record('discovery')
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get('type') == 'control_request' and message.get('request', {}).get('subtype') == 'initialize':
            models = json.loads(os.environ['FAKE_CLAUDE_MODELS']) if os.environ.get('FAKE_CLAUDE_MODELS') else DEFAULT_CLAUDE_MODELS
            account = json.loads(os.environ.get('FAKE_CLAUDE_ACCOUNT') or '{"email": "a@example.test", "subscriptionType": "max", "tokenSource": "claude.ai", "apiProvider": "firstParty"}')
            emit({'type': 'control_response', 'response': {'subtype': 'success', 'request_id': message.get('request_id'),
                  'response': {'commands': [], 'agents': [], 'models': models, 'account': account}}})
        elif message.get('type') == 'user':
            record('UNEXPECTED_USER_MESSAGE')
    sys.exit(0)
if provider == 'codex' and args[:2] == ['login', 'status']:
    route = os.environ.get('FAKE_CODEX_LOGIN', 'chatgpt')
    print({'chatgpt': 'Logged in using ChatGPT', 'apikey': 'Logged in using an API key - sk-****',
           'none': 'Not logged in'}[route])
    sys.exit(0)

prompt = sys.stdin.read()
if provider == 'claude' and prompt.strip() == '/usage':
    # The documented /usage local command: a usage_report on an assistant message, no model turn.
    record('usage', prompt)
    report = os.environ.get('FAKE_CLAUDE_USAGE')
    if report:
        emit({'type': 'assistant', 'uuid': str(uuid.uuid4()), 'parent_tool_use_id': None,
              'message': {'id': 'msg_usage', 'content': []}, 'usage_report': json.loads(report)})
    else:
        emit({'type': 'assistant', 'uuid': str(uuid.uuid4()), 'parent_tool_use_id': None,
              'message': {'id': 'msg_x', 'content': [{'type': 'text', 'text': 'a model answer'}]}})
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '', 'session_id': str(uuid.uuid4()),
          'num_turns': 0 if report else 1})
    sys.exit(0)
record('run', prompt)
if scenario == 'slow':
    time.sleep(float(os.environ.get('FAKE_SLOW_SECONDS', '30')))
if provider == 'codex':
    if scenario == 'waiting':
        # The text header carries the session id; the answer asks a question.
        print('OpenAI Codex (fake)', flush=True)
        print('session id: ' + (args[args.index('resume') + 1] if 'resume' in args else str(uuid.uuid4())), flush=True)
    print('codex', flush=True)
    print('CONTROL_CENTER_WAITING: Which module?' if scenario == 'waiting' else 'Fake Codex completed the work.', flush=True)
    sys.exit(0)
session = args[args.index('--resume') + 1] if '--resume' in args else str(uuid.uuid4())
if scenario == 'mismatch':
    session = str(uuid.uuid4())
if provider == 'claude':
    if scenario == 'limit_presession':
        # The provider reports the limit before any session exists.
        emit({'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour'},
              'uuid': str(uuid.uuid4()), 'session_id': None})
        emit({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'uuid': str(uuid.uuid4())})
        sys.exit(1)
    requested = args[args.index('--model') + 1] if '--model' in args else 'default'
    resolved = {'sonnet': 'claude-fake-sonnet', 'opus': 'claude-fake-opus', 'haiku': 'claude-fake-haiku'}.get(requested, 'claude-fake-opus')
    emit({'type': 'system', 'subtype': 'init', 'session_id': session,
          'model': os.environ.get('FAKE_CLAUDE_INIT_MODEL') or resolved, 'tools': []})
    def finish(text):
        emit({'type': 'assistant', 'uuid': str(uuid.uuid4()), 'parent_tool_use_id': None,
              'message': {'id': 'msg_' + uuid.uuid4().hex, 'content': [{'type': 'text', 'text': text}]}})
        emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text, 'session_id': session,
              'uuid': str(uuid.uuid4())})
    if scenario == 'limit':
        reset = os.environ.get('FAKE_RESET_AT')
        info = {'status': 'rejected'}
        if reset:
            info['resetsAt'] = int(reset)
        emit({'type': 'rate_limit_event', 'rate_limit_info': info, 'uuid': str(uuid.uuid4()), 'session_id': session})
        emit({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': session,
              'uuid': str(uuid.uuid4())})
        sys.exit(1)
    if scenario in ('success', 'slow', 'mismatch'):
        finish('Fake Claude completed the work.')
    elif scenario == 'waiting':
        finish('I need one decision.\nCONTROL_CENTER_WAITING: Which file should I change?')
    elif scenario == 'native':
        emit({'type': 'assistant', 'uuid': str(uuid.uuid4()), 'parent_tool_use_id': None,
              'message': {'id': 'msg_x', 'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'Agent',
                                                      'input': {'prompt': 'spawn'}}]}})
        time.sleep(5)
        finish('done')
    else:
        category = {'rate_limit': 'rate_limit', 'auth': 'authentication_failed', 'billing': 'billing_error',
                    'overloaded': 'overloaded', 'model': 'model_not_found'}[scenario]
        emit({'type': 'system', 'subtype': 'api_retry', 'attempt': 1, 'max_retries': 1, 'retry_delay_ms': 1,
              'error_status': 429, 'error': category, 'uuid': str(uuid.uuid4()), 'session_id': session})
        emit({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'session_id': session,
              'uuid': str(uuid.uuid4())})
        sys.exit(1)
    sys.exit(0)
if provider == 'gemini':
    # The init event carries the configured model (FAKE_GEMINI_INIT_MODEL, else the selection);
    # the result's usage statistics list FAKE_GEMINI_MODELS (e.g. after a fallback), or none
    # when FAKE_GEMINI_NO_STATS is set.
    selected = args[args.index('--model') + 1] if '--model' in args else 'gemini-fake'
    reported = [] if os.environ.get('FAKE_GEMINI_NO_STATS') else (
        [m for m in os.environ.get('FAKE_GEMINI_MODELS', '').split(',') if m] or [selected])
    emit({'type': 'init', 'timestamp': 't', 'session_id': session, 'model': os.environ.get('FAKE_GEMINI_INIT_MODEL') or selected})
    emit({'type': 'message', 'timestamp': 't', 'role': 'user', 'content': prompt[:20]})
    if scenario == 'error':
        emit({'type': 'error', 'timestamp': 't', 'severity': 'error', 'message': 'failure'})
        emit({'type': 'result', 'timestamp': 't', 'status': 'error', 'error': {'type': 'FatalError', 'message': 'x'}})
        sys.exit(1)
    text = 'Fake Gemini completed the work.' if scenario != 'waiting' else 'Question.\nCONTROL_CENTER_WAITING: Proceed?'
    for chunk in (text[:10], text[10:]):
        emit({'type': 'message', 'timestamp': 't', 'role': 'assistant', 'content': chunk, 'delta': True})
    emit({'type': 'result', 'timestamp': 't', 'status': 'success',
          'stats': {'total_tokens': 1, 'input_tokens': 1, 'output_tokens': 0, 'cached': 0, 'input': 1,
                    'duration_ms': 1, 'tool_calls': 0, 'models': {name: {} for name in reported}}})
    sys.exit(0)
'''


# The catalogs the fake CLIs report by default (tests derive variants from them).
DEFAULT_CLAUDE_MODELS_JSON = [{'value': 'default', 'resolvedModel': 'claude-fake-opus', 'displayName': 'Default (recommended)', 'description': 'Default model', 'supportsEffort': True, 'supportedEffortLevels': ['low', 'medium', 'high']}, {'value': 'sonnet', 'resolvedModel': 'claude-fake-sonnet', 'displayName': 'Sonnet', 'description': 'Sonnet', 'supportsEffort': True, 'supportedEffortLevels': ['low', 'medium', 'high', 'max']}, {'value': 'opus', 'resolvedModel': 'claude-fake-opus', 'displayName': 'Opus', 'description': 'Opus', 'supportsEffort': True, 'supportedEffortLevels': ['low', 'medium', 'high', 'xhigh', 'max']}, {'value': 'haiku', 'resolvedModel': 'claude-fake-haiku', 'displayName': 'Haiku', 'description': 'Haiku'}]
DEFAULT_CODEX_MODELS_JSON = {'identity': 'fake-account-1', 'models': [{'slug': 'gpt-fake-1', 'display_name': 'GPT-Fake-1', 'visibility': 'list', 'default_reasoning_level': 'medium', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'medium'}, {'effort': 'high'}], 'supports_reasoning_effort_updates': True, 'upgrade': None}, {'slug': 'gpt-fake-2', 'display_name': 'GPT-Fake-2', 'visibility': 'list', 'default_reasoning_level': 'low', 'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'xhigh'}], 'upgrade': None}, {'slug': 'gpt-fake-locked', 'display_name': 'GPT-Fake-Locked', 'visibility': 'list', 'supported_reasoning_levels': [{'effort': 'low'}], 'upgrade': {'plan': 'x'}, 'availability_nux': {'message': 'Available on a higher ChatGPT plan'}}, {'slug': 'internal-review', 'display_name': 'Internal', 'visibility': 'hide', 'supported_reasoning_levels': []}]}
FAKE_CLI = FAKE_CLI.replace('@@CLAUDE_MODELS@@', repr(json.dumps(DEFAULT_CLAUDE_MODELS_JSON))).replace(
    '@@CODEX_MODELS@@', repr(json.dumps(DEFAULT_CODEX_MODELS_JSON)))

def install(appdata, names=('claude', 'gemini', 'codex')):
    """Write the fake script and npm-style launchers under `appdata\\npm`."""
    npm = Path(appdata) / 'npm'
    npm.mkdir(parents=True, exist_ok=True)
    script = npm / 'fake_provider_cli.py'
    script.write_text(FAKE_CLI, encoding='utf-8')
    for name in names:
        (npm / f'{name}.cmd').write_text(
            '@echo off\nsetlocal\n'
            f'set "_prog={sys.executable}"\n'
            'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & '
            f'"%_prog%" "%~dp0fake_provider_cli.py" {name} %*\n', encoding='utf-8')
    return npm


def gemini_settings(home, selected='oauth-personal', overage='never'):
    folder = Path(home) / '.gemini'
    folder.mkdir(parents=True, exist_ok=True)
    value = {}
    if selected is not None:
        value['security'] = {'auth': {'selectedType': selected}}
    if overage is not None:
        value['billing'] = {'overageStrategy': overage}
    (folder / 'settings.json').write_text(json.dumps(value), encoding='utf-8')


def read_log(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def runs(path, provider=None):
    return [entry for entry in read_log(path) if entry['kind'] == 'run'
            and (provider is None or entry['provider'] == provider)]


def acknowledge(test, *providers_to_acknowledge):
    """The one-time billing acknowledgment a test user gives (database when present, memory otherwise)."""
    from app import agent_manager as manager, providers
    for provider in providers_to_acknowledge:
        if manager._store is not None:
            with manager._store._connection() as db:
                db.execute('BEGIN IMMEDIATE')
                providers.update_settings(db, provider, billing_confirmed=True)
        else:
            providers.acknowledge_without_persistence(provider)
            test.addCleanup(providers._memory_settings.pop, provider, None)


def isolated_environment(root):
    """Environment for a provider test: fake installations only, never the developer's real CLIs."""
    root = Path(root)
    home, appdata = root / 'home', root / 'appdata'
    home.mkdir(parents=True, exist_ok=True)
    install(appdata)
    gemini_settings(home)
    return {'APPDATA': str(appdata), 'USERPROFILE': str(home), 'GEMINI_CLI_HOME': str(home),
            'CODEX_HOME': str(home / '.codex'), 'ProgramData': str(root / 'programdata'),
            'FAKE_CLI_LOG': str(root / 'cli.log'),
            # Paid-route credentials present in the parent: they must never reach a provider.
            'ANTHROPIC_API_KEY': 'test-not-a-key', 'GEMINI_API_KEY': 'test-not-a-key',
            'CODEX_API_KEY': 'test-not-a-key', 'OPENAI_API_KEY': 'test-not-a-key'}, os.path.join(root, 'cli.log')
