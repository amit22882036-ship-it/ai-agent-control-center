"""Provider-neutral, strictly bounded delegation envelope validation."""
import json

from .delegations import validate_request

PROTOCOL = 'control-center.delegation.v1'
MAX_PAYLOAD = 262144
MAX_REQUESTS = 16


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
