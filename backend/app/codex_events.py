"""Opt-in Codex exec JSONL adapter. Stderr never enters this parser.

Exec emits one turn per invocation, with no wire turn ID. The execution UUID
and local turn ordinal (1) identify that turn; item IDs identify messages.
Unknown/malformed events make the invocation ineligible for control actions.
"""
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from .delegation_protocol import decode, is_envelope
from . import planning
from .managed_orchestration import VIOLATION

MAX_EVENT = 1048576
MAX_ITEMS = 4096
ITEM_TYPES = {'agent_message', 'reasoning', 'command_execution', 'file_change',
              'mcp_tool_call', 'web_search', 'todo_list', 'collab_tool_call'}


class StructuredCommand(str):
    """Internal launch option, never inferred from arbitrary command text."""


@dataclass
class Execution:
    events: object = field(default_factory=lambda: CodexEvents())
    origin: object = None
    revoked: bool = False
    processed: bool = False
    policy_stop_requested: bool = False


@dataclass
class CodexEvents:
    native_collaboration_prohibited: bool = False
    native_collaboration_seen: bool = False
    planning_enabled: bool = False
    plans: list = field(default_factory=list)
    expected_session: str | None = None
    session_id: str | None = None
    started: bool = False
    completed: bool = False
    error: str | None = None
    message: str | None = None
    message_id: str | None = None
    saw_request: bool = False
    promotion_message: str | None = None
    items: dict = field(default_factory=dict)
    completion: str | None = None

    def feed(self, line):
        """Return readable output only. Control state is consumed after EOF."""
        try:
            return self._feed(line)
        except (ValueError, TypeError, KeyError, RecursionError) as exc:
            self.error = self.error or f'Invalid Codex event stream: {exc}'
            return [line.rstrip('\r\n')]

    def _feed(self, line):
        if len(line.encode('utf-8')) > MAX_EVENT:
            raise ValueError('event exceeds 1 MiB')
        event = decode(line)
        if not isinstance(event, dict):
            raise ValueError('event must be an object')
        kind = event.get('type')
        fields = {'thread.started': {'type', 'thread_id'}, 'turn.started': {'type'},
                  'turn.completed': {'type', 'usage'}, 'item.started': {'type', 'item'},
                  'item.updated': {'type', 'item'}, 'item.completed': {'type', 'item'}}
        if kind in fields and set(event) != fields[kind]:
            raise ValueError('unexpected event fields')
        if kind == 'thread.started':
            if not isinstance(event['thread_id'], str):
                raise ValueError('invalid session ID')
            session = str(UUID(event['thread_id']))
            if self.session_id == session:
                return []
            if self.started or self.session_id or (self.expected_session and session != self.expected_session):
                raise ValueError('session mismatch')
            self.session_id = session
            return ['session id: ' + session]
        if kind == 'turn.started':
            if not self.session_id or self.started:
                raise ValueError('unexpected turn start')
            self.started = True
            return []
        if kind in ('error', 'turn.failed'):
            self.error = 'Codex turn failed'
            detail = event.get('error')
            if isinstance(detail, dict) and detail.get('code') in (
                    'rate_limit_exceeded', 'usage_limit_reached', 'insufficient_quota'):
                self.error = 'Codex provider limit reported; explicit continuation required (reset time unknown)'
            return [line.rstrip('\r\n')]
        if not self.started:
            raise ValueError('event outside a turn')
        if kind == 'turn.completed':
            usage = event.get('usage')
            if not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0
                    for k in ('input_tokens', 'cached_input_tokens', 'output_tokens')):
                raise ValueError('invalid turn completion')
            fingerprint = sha256(line.strip().encode()).hexdigest()
            if self.completed and self.completion != fingerprint:
                raise ValueError('conflicting turn completion')
            self.completed, self.completion = True, fingerprint
            return []
        if kind not in ('item.started', 'item.updated', 'item.completed'):
            raise ValueError('unknown event type')
        item = event['item']
        if (not isinstance(item, dict) or item.get('type') not in ITEM_TYPES
                or not isinstance(item.get('id'), str) or not 1 <= len(item['id']) <= 128):
            raise ValueError('invalid item')
        if item['type'] == 'collab_tool_call':
            self.native_collaboration_seen = True
            if self.native_collaboration_prohibited:
                # Valid provider event, never authority to delegate here.
                self.error = self.error or VIOLATION
        fingerprint = sha256(line.strip().encode()).hexdigest()
        identifier = item['id']
        if identifier in self.items:
            if self.items[identifier] == fingerprint:
                return []
            raise ValueError('completed item changed')
        if self.completed:
            raise ValueError('item after completed turn')
        if kind != 'item.completed':
            return []
        if len(self.items) >= MAX_ITEMS:
            raise ValueError('too many completed items')
        self.items[identifier] = fingerprint
        if item['type'] == 'agent_message':
            if set(item) != {'id', 'type', 'text'}:
                raise ValueError('unexpected agent message fields')
            text = item['text']
            if not isinstance(text, str):
                raise ValueError('invalid agent message')
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
            return ['codex', *text.splitlines()]
        # Display provider tool/reasoning results, but never parse their content.
        text = item.get('aggregated_output', item.get('text'))
        return [item['type'], *text.splitlines()] if isinstance(text, str) else [line.rstrip('\r\n')]

    def finish(self, returncode):
        if self.error:
            raise ValueError(self.error)
        if returncode != 0 or not self.session_id or not self.completed or self.message is None:
            raise ValueError('Codex execution did not complete a successful message/turn')
        if self.planning_enabled and planning.is_envelope(self.message):
            self.error = 'Plan-only final response is not proof of Task completion; explicit continuation required'
            raise ValueError(self.error)
        if self.saw_request and not is_envelope(self.message):
            raise ValueError('Delegation must be the final completed agent message')
        return self.message
