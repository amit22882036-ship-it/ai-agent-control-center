"""Stage 2F stream parsers for Claude Code and Gemini CLI JSONL output.

They follow the same contract as the Codex parser (`codex_events.CodexEvents`):
`feed()` returns readable output only and `finish()` returns the final
assistant message or raises. Control state (waiting markers, planning,
promotion and delegation envelopes) is read only from completed assistant text
of the main conversation, never from tool output, stderr or sub-agent text.
Provider-reported failure categories are classified only from documented,
structured fields; model prose is never treated as billing information.

Claude Code: https://code.claude.com/docs/en/headless (stream-json, `system/init`,
`system/api_retry` error categories, final `result`). Gemini CLI: the
`JsonStreamEventType` definitions in packages/core/src/output/types.ts.
"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from uuid import UUID

from .delegation_protocol import decode, is_envelope
from . import planning
from . import providers
from .managed_orchestration import VIOLATION

MAX_EVENT = 1048576
MAX_MESSAGES = 4096
TOOL_OUTPUT_LINES = 40

# Claude Code `system/api_retry` error categories -> Stage 2F conditions.
CLAUDE_RETRY_ERRORS = {
    'authentication_failed': providers.AUTH_FAILED, 'oauth_org_not_allowed': providers.AUTH_FAILED,
    'account_on_hold': providers.AUTH_FAILED, 'cloud_credential_error': providers.AUTH_FAILED,
    'billing_error': providers.BILLING_ERROR, 'rate_limit': providers.RATE_LIMITED,
    'overloaded': providers.PROVIDER_UNAVAILABLE, 'server_error': providers.PROVIDER_UNAVAILABLE,
    'model_not_found': providers.MODEL_UNAVAILABLE,
}


@dataclass
class StreamEvents:
    provider: str = ''
    native_collaboration_prohibited: bool = False
    native_collaboration_seen: bool = False
    planning_enabled: bool = False
    plans: list = field(default_factory=list)
    expected_session: str | None = None
    session_id: str | None = None
    completed: bool = False
    failed: bool = False
    error: str | None = None
    message: str | None = None
    message_id: str | None = None
    final_text: str | None = None
    saw_request: bool = False
    promotion_message: str | None = None
    tool_activity: bool = False
    condition: str | None = None
    detail: str | None = None
    models: set = field(default_factory=set)
    seen: set = field(default_factory=set)
    count: int = 0
    # Provider-reported limit observations (documented structured events only).
    rate_limits: list = field(default_factory=list)
    reset_at: str | None = None

    label = 'provider'

    def feed(self, line):
        """Readable output only. Control state is consumed after EOF."""
        try:
            if len(line.encode('utf-8')) > MAX_EVENT:
                raise ValueError('event exceeds 1 MiB')
            if not line.strip():
                return []
            event = decode(line)
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise ValueError('event must be an object with a type')
            return self._event(event, line)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self.error = self.error or f'Invalid {providers.LABELS[self.provider]} event stream: {exc}'
            return [line.rstrip('\r\n')]

    def _session(self, value):
        if not isinstance(value, str):
            raise ValueError('invalid session ID')
        session = str(UUID(value))
        if self.session_id == session:
            return []
        if self.session_id or (self.expected_session and session != self.expected_session):
            # A resumed run that reports another session is never accepted.
            self.condition = providers.SESSION_INVALID
            raise ValueError('session mismatch')
        self.session_id = session
        return ['session id: ' + session]

    def _accept(self, identifier, text):
        """One completed main-conversation assistant message (shared with Codex semantics)."""
        if not isinstance(text, str):
            raise ValueError('invalid assistant message')
        if identifier in self.seen:
            raise ValueError('duplicate assistant message')
        if len(self.seen) >= MAX_MESSAGES:
            raise ValueError('too many assistant messages')
        self.seen.add(identifier)
        self.message, self.message_id = text, identifier
        from . import promotion, replanning
        if self.promotion_message is not None:
            raise ValueError('Promotion must be the final assistant message')
        if replanning.is_envelope(text):
            replanning.validate(text)
            self.promotion_message = identifier
        if promotion.is_envelope(text):
            promotion.validate(text)
            self.promotion_message = identifier
        if self.planning_enabled and planning.is_envelope(text):
            planning.validate(text)
            if len(self.plans) >= planning.MAX_DECISIONS:
                raise ValueError('Too many planning decisions')
            self.plans.append((identifier, text))
        else:
            self.saw_request |= is_envelope(text)
        return [self.label, *text.splitlines()]

    def _collaboration(self):
        self.native_collaboration_seen = True
        if self.native_collaboration_prohibited:
            self.error = self.error or VIOLATION

    def finish(self, returncode):
        if self.error:
            raise ValueError(self.error)
        name = providers.LABELS[self.provider]
        if returncode != 0 or not self.session_id or not self.completed or self.message is None:
            raise ValueError(f'{name} execution did not complete a successful message/turn')
        if self.planning_enabled and planning.is_envelope(self.message):
            self.error = 'Plan-only final response is not proof of Task completion; explicit continuation required'
            raise ValueError(self.error)
        if self.saw_request and not is_envelope(self.message):
            raise ValueError('Delegation must be the final completed agent message')
        return self.message

    def outcome(self, returncode):
        """Ordinary (non-managed) execution: success, the final message and a classified failure."""
        try:
            message = self.finish(returncode)
            return {'success': True, 'message': message, 'condition': None, 'detail': None}
        except ValueError as exc:
            condition = self.condition
            if condition is None:
                condition = providers.WORKER_CRASH if returncode not in (0, 1) and not self.session_id else providers.UNKNOWN_FAILURE
            return {'success': False, 'message': self.message, 'condition': condition,
                    'detail': self.detail or str(exc)}

    def fingerprint(self, line):
        return sha256(line.strip().encode()).hexdigest()


@dataclass
class ClaudeEvents(StreamEvents):
    provider: str = 'claude'
    label = 'claude'
    retry_error: str | None = None
    limit_rejected: bool = False

    def _event(self, event, line):
        kind = event['type']
        if kind == 'rate_limit_event':
            # Documented SDKRateLimitEvent: status of the active limit and its reset.
            info = event.get('rate_limit_info')
            if not isinstance(info, dict) or info.get('status') not in ('allowed', 'allowed_warning', 'rejected'):
                return []
            observed = providers._now()
            # `rateLimitType` is kept verbatim when present (an undocumented raw identifier).
            self.rate_limits.append({**{k: info.get(k) for k in ('status', 'resetsAt', 'errorCode', 'rateLimitType')},
                                     'observed_at': observed})
            if info.get('status') == 'rejected':
                self.limit_rejected = True
                try:
                    self.reset_at = providers._iso(info.get('resetsAt'))
                except (ValueError, OverflowError, OSError):
                    self.reset_at = None
                note = ' (credits required: the Control Center never buys credits)' if info.get('errorCode') == 'credits_required' else ''
                return [f'--- Provider limit reached{note}'
                        + (f'; provider-reported reset {self.reset_at}' if self.reset_at else '; reset time not reported') + ' ---']
            return []
        if kind == 'system':
            subtype = event.get('subtype')
            if subtype == 'init':
                if isinstance(event.get('model'), str):
                    self.models.add(event['model'][:128])
                return self._session(event.get('session_id'))
            if subtype == 'api_retry':
                category = event.get('error')
                if isinstance(category, str):
                    self.retry_error = category
                return [f'provider retry: {category} (attempt {event.get("attempt")})']
            return []  # hooks, plugin installs, informational notices: not control
        if kind == 'assistant':
            message = event.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), list):
                raise ValueError('invalid assistant event')
            if event.get('parent_tool_use_id') is not None:
                # A provider-internal sub-agent is running inside this execution.
                self._collaboration()
                return []
            texts, output = [], []
            for block in message['content']:
                if not isinstance(block, dict):
                    raise ValueError('invalid content block')
                if block.get('type') == 'text' and isinstance(block.get('text'), str):
                    texts.append(block['text'])
                elif block.get('type') == 'tool_use':
                    self.tool_activity = True
                    name = block.get('name')
                    if name in providers.CLAUDE_NATIVE_COLLABORATION_TOOLS:
                        self._collaboration()
                        output.append(line.rstrip('\r\n'))  # The wire event, for later inspection.
                    else:
                        output.append(f'tool: {name}')
            if texts:
                identifier = event.get('uuid') or message.get('id')
                if not isinstance(identifier, str) or not 1 <= len(identifier) <= 128:
                    identifier = self.fingerprint(line)[:32]
                output = self._accept(identifier, '\n'.join(texts)) + output
            return output
        if kind == 'user':
            if event.get('parent_tool_use_id') is not None:
                return []
            message = event.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            lines = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'tool_result':
                        text = block.get('content')
                        if isinstance(text, list):
                            text = '\n'.join(b.get('text', '') for b in text if isinstance(b, dict))
                        if isinstance(text, str):
                            shown = text.splitlines()
                            lines += ['tool result', *shown[:TOOL_OUTPUT_LINES]]
                            if len(shown) > TOOL_OUTPUT_LINES:
                                lines.append(f'... {len(shown) - TOOL_OUTPUT_LINES} more lines')
            return lines
        if kind == 'result':
            if event.get('session_id') is not None:
                self._session(event.get('session_id'))
            subtype = event.get('subtype')
            if subtype == 'success' and event.get('is_error') is False and isinstance(event.get('result'), str):
                self.completed = True
                self.final_text = event['result']
                if self.message is None or self.message.strip() != self.final_text.strip():
                    # The result text is the final assistant message: when it
                    # differs from the last streamed text block it is validated
                    # as one more message (an earlier envelope then fails closed).
                    identifier = event.get('uuid') if isinstance(event.get('uuid'), str) else self.fingerprint(line)[:32]
                    return self._accept('result:' + identifier[:120], event['result'])
                return []
            self.failed = True
            if self.limit_rejected:
                self.condition = providers.QUOTA_EXHAUSTED
                self.detail = 'Claude Code reported that the usage limit was reached'
            else:
                self.condition = CLAUDE_RETRY_ERRORS.get(self.retry_error, providers.UNKNOWN_FAILURE)
                self.detail = (f'Claude Code run ended without success ({subtype or "error"})'
                               + (f' after provider error "{self.retry_error}"' if self.retry_error else ''))
            self.error = self.error or self.detail
            return ['--- ' + self.detail + ' ---']
        if kind == 'stream_event':
            return []
        return []  # Unknown event types are displayed nowhere and grant nothing.


@dataclass
class GeminiEvents(StreamEvents):
    provider: str = 'gemini'
    label = 'gemini'
    buffer: list = field(default_factory=list)
    started: bool = False
    # The model configured at initialization. It is not evidence of which model
    # produced the response (fallback can follow), so it never joins `models`,
    # which holds only the models the result's usage statistics report.
    initial_model: str | None = None

    def _flush(self):
        if not self.buffer:
            return []
        text = ''.join(self.buffer)
        self.buffer = []
        self.count += 1
        return self._accept(f'{self.session_id or "session"}:{self.count}', text)

    def _event(self, event, line):
        kind = event['type']
        if kind == 'init':
            if isinstance(event.get('model'), str):
                self.initial_model = event['model'][:128]
            self.started = True
            return self._session(event.get('session_id'))
        if not self.started:
            raise ValueError('event before init')
        if kind == 'message':
            role, content = event.get('role'), event.get('content')
            if role not in ('user', 'assistant') or not isinstance(content, str):
                raise ValueError('invalid message event')
            if role == 'user':
                return self._flush()
            if event.get('delta') is True:
                self.buffer.append(content)
                return []
            output = self._flush()
            self.buffer.append(content)
            return output + self._flush()
        if kind == 'tool_use':
            output = self._flush()
            self.tool_activity = True
            return output + [f'tool: {event.get("tool_name")}']
        if kind == 'tool_result':
            text = event.get('output') if isinstance(event.get('output'), str) else ''
            shown = text.splitlines()
            lines = ['tool result', *shown[:TOOL_OUTPUT_LINES]]
            if len(shown) > TOOL_OUTPUT_LINES:
                lines.append(f'... {len(shown) - TOOL_OUTPUT_LINES} more lines')
            return lines
        if kind == 'error':
            message = event.get('message') if isinstance(event.get('message'), str) else ''
            return [f'provider {event.get("severity", "error")}: {message[:500]}']
        if kind == 'result':
            output = self._flush()
            stats = event.get('stats')
            if isinstance(stats, dict) and isinstance(stats.get('models'), dict):
                self.models.update(str(name)[:128] for name in stats['models'])
            if event.get('status') == 'success':
                self.completed = True
                return output
            self.failed = True
            error = event.get('error') if isinstance(event.get('error'), dict) else {}
            # Gemini CLI documents no closed set of error types: the failure is
            # shown but never classified as a quota or billing condition.
            self.condition = providers.UNKNOWN_FAILURE
            self.detail = f'Gemini CLI run ended with an error ({str(error.get("type") or "unknown")[:80]})'
            self.error = self.error or self.detail
            return output + ['--- ' + self.detail + ' ---']
        return []


PARSERS = {'claude': ClaudeEvents, 'gemini': GeminiEvents}


def parser(provider):
    return PARSERS[provider]()


def collaboration_event(line):
    """Saved Claude Code wire events that show a provider-internal sub-agent call."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return False
    if not isinstance(event, dict) or event.get('type') != 'assistant':
        return False
    message = event.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') == 'tool_use'
        and block.get('name') in providers.CLAUDE_NATIVE_COLLABORATION_TOOLS for block in content)
