"""control-center.delegation.v3: ordinary delegation requests with an optional Child allocation.

A separate protocol version, so v1 (closed request schema) and v2 (Hybrid continuation
and acknowledgement) keep their exact meanings. A v3 request is v1's request_key and
instruction plus an optional `allocation`:

- absent: the historical default (the Parent's provider), exactly as in v1;
- {"mode": "explicit", "provider", "model"?, "effort"?}: that exact configuration,
  validated at launch and never substituted;
- {"mode": "auto", ...}: the promotion Auto request (allocation.validate_request),
  which grants nothing by itself: the user's Auto policy authorizes it.

The envelope is exactly {protocol, action, requests, continuation}; continuation is
null, or v2's Hybrid continuation (accepted only for Hybrid-enabled Parents). All v1
bounds apply: duplicate JSON keys, payload and batch size, request keys.
"""
import json

from . import allocation, delegation_continuation, delegation_protocol as v1, providers
from .delegations import validate_request

PROTOCOL = 'control-center.delegation.v3'


def validate_allocation(value):
    if not isinstance(value, dict):
        raise ValueError('allocation must be an object')
    if value.get('mode') == 'explicit':
        if not {'mode', 'provider'} <= set(value) or set(value) - {'mode', 'provider', 'model', 'effort'}:
            raise ValueError('An explicit allocation has mode, provider and optional model and effort')
        if value['provider'] not in providers.IDS:
            raise ValueError('Unknown Child provider; use one of ' + ', '.join(providers.IDS))
        providers.validate_model(value.get('model'))
        providers.validate_effort(value.get('effort'))
        if value['provider'] == 'mock' and (value.get('model') is not None or value.get('effort') is not None):
            raise ValueError('Mock has no models or effort levels')
        return value
    if value.get('mode') == 'auto':
        return allocation.validate_request(value)
    raise ValueError('allocation mode must be explicit or auto')


def validate(message):
    if not isinstance(message, str) or len(message.encode('utf-8')) > v1.MAX_PAYLOAD:
        raise ValueError('Delegation payload exceeds 256 KiB')
    try:
        payload = v1.decode(message)
    except (ValueError, RecursionError) as exc:
        raise ValueError('Malformed delegation JSON') from exc
    if not isinstance(payload, dict) or set(payload) != {'protocol', 'action', 'requests', 'continuation'}:
        raise ValueError('Invalid v3 delegation envelope fields')
    if payload['protocol'] != PROTOCOL or payload['action'] != 'request_delegations':
        raise ValueError('Unsupported delegation protocol or action')
    requests = payload['requests']
    if not isinstance(requests, list) or not 1 <= len(requests) <= v1.MAX_REQUESTS:
        raise ValueError('Delegation batch must contain 1-16 requests')
    seen = set()
    for request in requests:
        if (not isinstance(request, dict) or not {'request_key', 'instruction'} <= set(request)
                or set(request) - {'request_key', 'instruction', 'allocation'}):
            raise ValueError('Invalid delegation request fields')
        validate_request(request['request_key'], request['instruction'])
        if 'allocation' in request:
            validate_allocation(request['allocation'])
        if request['request_key'] in seen:
            raise ValueError('Duplicate delegation request key')
        seen.add(request['request_key'])
    # The continuation keeps v2's contract exactly (validated by v2 itself).
    delegation_continuation.validate(json.dumps(dict(
        protocol=delegation_continuation.PROTOCOL, action='request_delegations',
        requests=[dict(request_key=r['request_key'], instruction=r['instruction']) for r in requests],
        continuation=payload['continuation'])))
    return payload


def normalized(value):
    """The canonical stored form of a request's allocation (None: the historical default)."""
    return None if value is None else json.dumps(value, sort_keys=True, separators=(',', ':'))


def instruction(db, task_id):
    """For delegation-enabled Parents (the caller checks): explicit allocation always; the Auto
    request only where the user's effective policy authorizes it (never an invitation otherwise)."""
    policy = allocation.policy_for(db, task_id)
    authorized = bool(policy and policy['enabled'])
    text = ('\nOrdinary delegation with a Child allocation (' + PROTOCOL + '): a FINAL standalone JSON object with '
            'exactly protocol, action ("request_delegations"), requests (1-16) and continuation (null, or the Hybrid '
            '{"instruction":"...","intent_ids":["known UUID"]} when Hybrid continuation is enabled). Each request has '
            'request_key, instruction and optionally allocation {"mode":"explicit","provider":one of '
            + ', '.join(providers.IDS) + ',"model"?,"effort"?} for an exact Child configuration: it is validated at '
            'launch and never substituted. Without allocation the Child uses your provider. Results and '
            'acknowledgements are unchanged.')
    if authorized:
        text += ' An allocation may instead be {"mode":"auto",...} as described below.' + allocation.instruction(db, task_id)
    return text + '\n'
