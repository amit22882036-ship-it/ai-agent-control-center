"""Strict v2 control messages. v1 retains its original, closed schema."""
from uuid import UUID

from . import delegation_protocol as v1

PROTOCOL = 'control-center.delegation.v2'


def identifier(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('Expected a canonical UUID')
    return value


def text(value, limit=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or '\x00' in value:
        raise ValueError('Expected bounded non-blank text without NUL')
    return value


def validate(message):
    if not isinstance(message, str) or len(message.encode('utf-8')) > v1.MAX_PAYLOAD:
        raise ValueError('Control message exceeds 256 KiB')
    try:
        payload = v1.decode(message)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Malformed continuation JSON') from exc
    if not isinstance(payload, dict) or payload.get('protocol') != PROTOCOL:
        raise ValueError('Unsupported continuation protocol')
    action = payload.get('action')
    if action == 'request_delegations':
        if set(payload) != {'protocol', 'action', 'requests', 'continuation'}:
            raise ValueError('Invalid v2 delegation envelope fields')
        # Reuse all v1 request bounds, duplicate checks and validation unchanged.
        import json
        v1.validate(json.dumps({k: (v1.PROTOCOL if k == 'protocol' else payload[k])
                               for k in ('protocol', 'action', 'requests')}))
        plan = payload['continuation']
        if plan is not None:
            if not isinstance(plan, dict) or set(plan) != {'instruction', 'intent_ids'}:
                raise ValueError('Continuation requires instruction and authoritative intent IDs')
            text(plan['instruction'])
            ids = plan['intent_ids']
            if not isinstance(ids, list) or not 1 <= len(ids) <= 16:
                raise ValueError('Continuation requires 1-16 intent IDs')
            for value in ids:
                identifier(value)
            if len(set(ids)) != len(ids):
                raise ValueError('Duplicate continuation intent ID')
    elif action == 'acknowledge_results':
        if set(payload) != {'protocol', 'action', 'delivery_id', 'results'}:
            raise ValueError('Invalid acknowledgement fields')
        identifier(payload['delivery_id'])
        results = payload['results']
        if not isinstance(results, list) or not 1 <= len(results) <= 16:
            raise ValueError('Acknowledgement requires 1-16 results')
        seen = set()
        for result in results:
            if not isinstance(result, dict) or set(result) != {'result_id', 'decision', 'reason'}:
                raise ValueError('Invalid result acknowledgement fields')
            identifier(result['result_id'])
            if result['result_id'] in seen:
                raise ValueError('Duplicate result acknowledgement')
            seen.add(result['result_id'])
            if result['decision'] not in ('accept', 'reject', 'failure'):
                raise ValueError('Invalid acknowledgement decision')
            text(result['reason'], 2000)
    else:
        raise ValueError('Unsupported v2 action')
    return payload
