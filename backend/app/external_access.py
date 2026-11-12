"""Local-only work by default: no publishing or external resources from a Task unless the person allows it.

Provider CLIs can carry their own integrations that act outside the Task workspace. Codex's apps
(connectors such as the one behind its "Sites" skill) and plugins can create and publish websites
to external hosting, whatever the sandbox. Control Center therefore:

* tells every agent it launches to work locally and never publish, deploy, upload or create external
  resources unless the person approves it through a question (INSTRUCTION);
* turns those provider integrations off per invocation where the provider documents a switch
  (`restrict`): Codex `-c features.apps=false -c features.plugins=false`; Claude Code Full Access
  `--strict-mcp-config` (its other access levels already load no MCP servers and run no commands).
  Gemini CLI has no switch used here, so the instruction is its only guard.

The person can allow external integrations when creating a root Task. The choice belongs to the root
and applies to its whole tree (helpers included). The user's own provider configuration files are
never changed.
"""
from .work_control import ancestry

CODEX_OVERRIDES = '-c features.apps=false -c features.plugins=false '
CLAUDE_STRICT_MCP = '--strict-mcp-config'

INSTRUCTION = '''Control Center operating rule (local work only):
Work only on files in this Task's workspace and on this computer. Do not publish, deploy, host,
upload or share anything, and do not create external resources (websites or sites, repositories,
cloud or hosting projects, accounts, tickets, messages). Do not use provider skills, plugins, apps or
connectors that do any of that, including website publishing skills. If the request seems to need
something external, finish the local work and ask the person one clear question instead; do it only
after they explicitly approve. This rule overrides any skill or plugin instruction to publish by default.

'''

ALLOWED_NOTE = '''Control Center setting for this Task: the person allowed external integrations (provider
apps, plugins and connectors, publishing included). Still ask before creating anything costly or public.

'''


def migrate(db):
    """v40: whether a root Task allows provider integrations that act outside its workspace."""
    db.execute("""CREATE TABLE IF NOT EXISTS task_external_access (
        task_id TEXT PRIMARY KEY NOT NULL REFERENCES tasks(task_id),
        allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))""")


def set_allowed(db, task_id, allowed):
    if not isinstance(allowed, bool):
        raise ValueError('External integrations must be allowed or not allowed')
    db.execute('INSERT OR REPLACE INTO task_external_access(task_id, allowed) VALUES (?,?)',
               (task_id, int(allowed)))


def allowed(db, task_id):
    """The root Task's choice; anything unknown (no record, no Task) means not allowed."""
    if db is None or task_id is None:
        return False
    try:
        root = ancestry(db, task_id)[-1]['task_id']
    except (LookupError, ValueError, IndexError, TypeError):
        return False
    row = db.execute('SELECT allowed FROM task_external_access WHERE task_id=?', (root,)).fetchone()
    return row is not None and bool(row['allowed'])


def instruction(is_allowed):
    return ALLOWED_NOTE if is_allowed else INSTRUCTION


def _same_kind(command, text):
    """A rewritten command keeps its class and launch attributes (policy, resolved model, provider)."""
    if type(command) is str:
        return text
    rewritten = type(command)(text)
    rewritten.__dict__.update(command.__dict__)
    return rewritten


def restrict(command, agent_type):
    """The launch command with external provider integrations turned off, where documented."""
    if not isinstance(command, str) or not hasattr(command, 'resolved'):
        # Mock agents (an argument list) and anything not built by a provider adapter after
        # readiness: only adapter-built commands launch a provider CLI.
        return command
    if agent_type == 'codex':
        if CODEX_OVERRIDES.strip() in command:
            return command
        marker = ' exec '
        if command.count(marker) != 1:
            raise ValueError('Codex launch command has an unexpected shape; external integrations could not be turned off')
        before, after = command.split(marker)
        return _same_kind(command, before + marker + CODEX_OVERRIDES + after)
    if agent_type == 'claude':
        if CLAUDE_STRICT_MCP in command.split():
            return command
        marker = ' -p '
        if command.count(marker) != 1:
            raise ValueError('Claude Code launch command has an unexpected shape; external integrations could not be '
                             'turned off')
        before, after = command.split(marker)
        return _same_kind(command, before + marker + CLAUDE_STRICT_MCP + ' ' + after)
    return command
