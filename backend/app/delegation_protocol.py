"""Provider-neutral, strictly bounded delegation envelope validation."""
import json
import re
from contextlib import contextmanager

from .delegations import validate_request

PROTOCOL = 'control-center.delegation.v1'
MAX_PAYLOAD = 262144
MAX_REQUESTS = 16


# Why a structured control message was refused. Unexpected internal failures are never one of these.
REJECTION_KINDS = ('format', 'missing_step', 'authority', 'admission')


class ControlRejected(ValueError):
    """A structured control message refused by validation or orchestration rules.

    `kind` says which rule refused it: `format` (shape, schema or message composition),
    `missing_step` (a required earlier protocol step is absent), `authority` (stale, removed,
    stopped or unauthorized execution, or Task control gates) and `admission` (orchestration
    rules such as conflicting replay or durable promotion history). It is still a ValueError,
    so every existing fail-closed path treats it exactly as before.
    """

    def __init__(self, kind, reason):
        if kind not in REJECTION_KINDS:
            raise ValueError('Unknown control rejection kind')
        super().__init__(reason)
        self.kind = kind


@contextmanager
def rejected_as(kind):
    """Classify ValueError/LookupError raised by a gate; an existing classification wins."""
    try:
        yield
    except ControlRejected:
        raise
    except (ValueError, LookupError) as exc:
        raise ControlRejected(kind, str(exc).strip("'\"")) from exc


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def decode(text):
    def invalid_constant(_):
        raise ValueError('Invalid JSON constant')
    return json.loads(text, object_pairs_hook=unique_object, parse_constant=invalid_constant)


def validate(text):
    if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_PAYLOAD:
        raise ValueError('Delegation payload exceeds 256 KiB')
    try:
        payload = decode(text)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Malformed delegation JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {'protocol', 'action', 'requests'}:
        raise ValueError('Invalid delegation envelope fields')
    if payload['protocol'] != PROTOCOL or payload['action'] != 'request_delegations':
        raise ValueError('Unsupported delegation protocol or action')
    requests = payload['requests']
    if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_REQUESTS:
        raise ValueError('Delegation batch must contain 1-16 requests')
    seen = set()
    for request in requests:
        if not isinstance(request, dict) or set(request) != {'request_key', 'instruction'}:
            raise ValueError('Invalid delegation request fields')
        validate_request(**request)
        if request['request_key'] in seen:
            raise ValueError('Duplicate delegation request key')
        seen.add(request['request_key'])
    return requests


def is_envelope(text):
    """Only whole JSON objects designate a request; never search prose/code fences.

    Invalid objects with protocol/action fields are rejected, not treated as a
    completed task. Malformed object-shaped messages fail closed as well.
    """
    if not text.lstrip().startswith('{'):
        return False
    try:
        value = decode(text)
    except (ValueError, RecursionError):
        return True
    return isinstance(value, dict) and bool({'protocol', 'action', 'requests'} & value.keys())


CONTROL_PREFIX = 'control-center.'
STANDALONE_REQUIRED = ('Control Center protocol messages must be standalone JSON assistant messages. This message '
                       'combined a protocol object with other content, so nothing in it was accepted.')
MALFORMED_EMBEDDED = ('Control Center protocol messages must be standalone, valid JSON assistant messages. This message '
                      'contained a malformed Control Center protocol object (for example duplicate fields or invalid '
                      'JSON), so nothing in it was accepted.')
# CommonMark fences: up to three spaces of indentation, then three or more backticks or tildes.
_FENCE_OPEN = re.compile(r' {0,3}(`{3,}|~{3,})(.*)')
# Invalid JSON is recognized as a protocol attempt only by a "protocol" key and a "control-center." string.
_PROTOCOL_KEY = re.compile(r'"protocol"')
_PROTOCOL_VALUE = re.compile(r'"' + re.escape(CONTROL_PREFIX))


def _fence_open(line):
    match = _FENCE_OPEN.fullmatch(line.rstrip('\r\n'))
    if not match or (match[1][0] == '`' and '`' in match[2]):
        return None  # A backtick fence's info string may not contain backticks.
    return match[1]


def _fence_closes(line, fence):
    # Same character, at least as long as the opener, and nothing but whitespace after it.
    match = re.fullmatch(r' {0,3}(' + re.escape(fence[0]) + r'+)[ \t]*', line.rstrip('\r\n'))
    return bool(match) and len(match[1]) >= len(fence)


def _candidate(text, start, lines):
    """Diagnostic for a JSON-looking block starting at `start`, or None when it is not a protocol object."""
    try:
        value, _ = json.JSONDecoder(object_pairs_hook=unique_object).raw_decode(text, start)
    except (ValueError, RecursionError):
        pass  # Strict decoding failed; decide below whether it was meant as a protocol object.
    else:
        if isinstance(value, dict) and isinstance(value.get('protocol'), str) \
                and value['protocol'].startswith(CONTROL_PREFIX):
            return STANDALONE_REQUIRED
        return None
    try:
        # Recognition only: keep every pair, so duplicate fields are visible and never resolved.
        pairs, _ = json.JSONDecoder(object_pairs_hook=lambda items: items).raw_decode(text, start)
    except (ValueError, RecursionError):
        # Not even lenient JSON: recognize a protocol field within this paragraph only.
        block = ''.join(lines)
        return MALFORMED_EMBEDDED if _PROTOCOL_KEY.search(block) and _PROTOCOL_VALUE.search(block) else None
    recognized = isinstance(pairs, list) and any(
        k == 'protocol' and isinstance(v, str) and v.startswith(CONTROL_PREFIX) for k, v in pairs)
    return MALFORMED_EMBEDDED if recognized else None


def embedded_control(text):
    """Diagnostic when an assistant message is not itself one JSON object, yet carries a
    Control Center protocol object (`protocol` starting with "control-center.") beginning
    on a line of its own, outside fenced code: an attempted control message in the wrong
    shape. A valid object yields STANDALONE_REQUIRED; a recognizable but malformed one
    (duplicate fields, invalid JSON) yields MALFORMED_EMBEDDED. Returns None otherwise.
    Whole-object messages keep their own validation; mentions in prose, inline code,
    quoted lines, fenced examples and unrelated JSON are not matched, and nothing found
    here is ever executed or accepted.
    """
    if not isinstance(text, str) or text.lstrip().startswith('{'):
        return None
    lines = text.splitlines(keepends=True)
    fence, offset, starts = None, 0, []
    for line in lines:
        starts.append(offset)
        offset += len(line)
    for index, line in enumerate(lines):
        if fence:
            if _fence_closes(line, fence):
                fence = None
            continue  # An unclosed fence runs to the end of the message.
        fence = _fence_open(line)
        if fence or not line.strip().startswith('{'):
            continue
        paragraph = []
        for following in lines[index:]:
            if paragraph and (not following.strip() or _fence_open(following)):
                break
            paragraph.append(following)
        problem = _candidate(text, starts[index] + line.index('{'), paragraph)
        if problem:
            return problem
    return None
