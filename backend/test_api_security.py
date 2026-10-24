import asyncio
import json
import logging
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app import security
from app.main import app
from app.realtime import ChangeBroker

TOKEN = app.state.csrf_token
ALLOWED_ORIGIN = 'http://127.0.0.1:5173'

# Reviewed inventory. A new route must be added here deliberately: every
# state-changing route is guarded; every GET must stay free of side effects.
UNSAFE_ROUTES = {
    # Stage 2F providers: explicit checks, settings, usage decisions, provider change and continue.
    ('POST', '/providers/check'), ('POST', '/providers/{provider}/check'), ('PUT', '/providers/{provider}/settings'),
    ('POST', '/providers/{provider}/usage/allow'), ('POST', '/agents/{agent_id}/provider'),
    ('POST', '/agents/{agent_id}/continue'), ('POST', '/agents/{agent_id}/resume'),
    ('POST', '/providers/{provider}/usage/refresh'), ('POST', '/agents/{agent_id}/defaults'),
    ('POST', '/providers/{provider}/models/refresh'), ('PUT', '/tasks/{task_id}/auto-allocation'), ('DELETE', '/tasks/{task_id}/auto-allocation'),
    ('PUT', '/provider-policy'), ('PUT', '/tasks/{task_id}/provider-restrictions'),
    ('DELETE', '/tasks/{task_id}/provider-restrictions'), ('POST', '/providers/{provider}/operations/plan'),
    ('POST', '/providers/{provider}/operations'), ('POST', '/provider-operations/{operation_id}/cancel'),
    ('POST', '/providers/updates/check'), ('POST', '/providers/{provider}/notifications/dismiss'),
    ('POST', '/provider-operations/{operation_id}/resolve'),
    ('POST', '/notifications/preferences'), ('POST', '/notifications/heartbeat'),
    ('POST', '/agents/start'), ('POST', '/agents/{parent_id}/children/start'),
    ('POST', '/tasks'), ('POST', '/projects'), ('POST', '/tasks/{task_id}/start-agent'),
    ('POST', '/agents/{agent_id}/stop'), ('POST', '/agents/{agent_id}/redirect'),
    ('POST', '/agents/{agent_id}/reply'), ('POST', '/agents/{agent_id}/decide'),
    ('POST', '/agents/{agent_id}/decide-similar'), ('POST', '/agents/{agent_id}/decide-similar/disable'),
    ('POST', '/agents/{agent_id}/decide-always'), ('POST', '/agents/{agent_id}/decide-always/disable'),
    ('POST', '/agents/{agent_id}/stop-branch'), ('POST', '/agents/{agent_id}/rename'),
    ('POST', '/agents/{agent_id}/color'), ('POST', '/tasks/{task_id}/integrate'),
    ('POST', '/tasks/{task_id}/pause'), ('POST', '/tasks/{task_id}/resume'),
    ('POST', '/tasks/{task_id}/cancel'), ('POST', '/tasks/{task_id}/dependencies'),
    ('DELETE', '/tasks/{task_id}/dependencies/{depends_on_task_id}'),
    ('POST', '/tasks/{task_id}/control-impact'), ('POST', '/tasks/{task_id}/resource-claims'),
    ('DELETE', '/tasks/{task_id}/resource-claims/{claim_id}'), ('POST', '/tasks/{task_id}/work-intents'),
    ('DELETE', '/tasks/{task_id}/work-intents/{intent_id}'), ('POST', '/tasks/{task_id}/orchestration/wake'),
    ('POST', '/agents/{agent_id}/recovery/handoff'), ('POST', '/tasks/{task_id}/process-ownership/confirm-stopped'),
    ('POST', '/delegations/{delegation_id}/recovery/resolve-launch'), ('POST', '/tasks/{task_id}/recovery/rearm'),
    ('POST', '/tasks/{task_id}/recovery/reverify'),
    ('POST', '/agents/{agent_id}/removal-preview'), ('POST', '/agents/{agent_id}/remove'),
    ('POST', '/agent-removals/{removal_id}/retry'),
    ('POST', '/executions/{execution_id}/diagnostics'), ('POST', '/executions/{execution_id}/attestation'),
    ('POST', '/tasks/{task_id}/responsibility/transfer'),
}
GET_ROUTES = {
    '/providers',  # Stage 2F: reports the last check; never probes.
    '/providers/{provider}/models',  # Cached catalog; never runs discovery.
    '/tasks/{task_id}/auto-allocation',  # Policy and recorded decisions; never allocates.
    '/provider-policy', '/tasks/{task_id}/provider-restrictions',  # Stage 2G permissions (read only).
    '/providers/onboarding', '/provider-operations/{operation_id}', '/providers/notifications',  # No probe, no run.
    security.TOKEN_PATH, '/health', '/system/process-supervision', '/workspace', '/agents/{agent_id}/activity', '/agents/{agent_id}/hierarchy',
    '/agents', '/agents/{agent_id}', '/projects', '/projects/{project_id}', '/tasks', '/tasks/{task_id}',
    '/tasks/{task_id}/assignments', '/tasks/{task_id}/workspace', '/agents/{agent_id}/output', '/events',
    '/agents/{agent_id}/name-history', '/tasks/{task_id}/integrations', '/integrations/{integration_id}',
    '/tasks/{task_id}/dependencies', '/tasks/{task_id}/dependents', '/tasks/{task_id}/control-operations',
    '/tasks/{task_id}/deadlocks', '/resource-deadlocks/{deadlock_id}', '/tasks/{task_id}/resource-claims',
    '/tasks/{task_id}/work-intents', '/tasks/{task_id}/work-overlaps', '/tasks/{task_id}/delegations',
    '/delegations/{delegation_id}', '/tasks/{task_id}/orchestration',
    '/agents/{agent_id}/removals', '/agent-removals/{removal_id}',
    '/executions/unresolved', '/executions/{execution_id}', '/tasks/{task_id}/responsibility',
}


def setUpModule():
    # Rejections are expected here; assertLogs still captures them where checked.
    logging.getLogger(security.__name__).propagate = False


def tearDownModule():
    logging.getLogger(security.__name__).propagate = True


def concrete(path):
    return path.replace('{', '').replace('}', '')


def call(method, path, headers=(), body=b'', target=app, chunks=None):
    """Run one in-process ASGI request and return (status, headers, body)."""
    path, _, query = path.partition('?')

    async def run():
        messages = []
        parts = list(chunks) if chunks is not None else [body]

        async def receive():
            if parts:
                part = parts.pop(0)
                return {'type': 'http.request', 'body': part, 'more_body': bool(parts)}
            return {'type': 'http.disconnect'}

        async def send(message):
            messages.append(message)
        await target({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': method,
                      'scheme': 'http', 'path': path, 'raw_path': path.encode(), 'query_string': query.encode(),
                      'headers': [(k.lower().encode(), v.encode()) for k, v in headers], 'root_path': '',
                      'client': ('127.0.0.1', 1234), 'server': ('127.0.0.1', 8000)}, receive, send)
        start = next(m for m in messages if m['type'] == 'http.response.start')
        payload = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
        return start['status'], {k.decode().lower(): v.decode() for k, v in start['headers']}, payload
    return asyncio.run(run())


def headers(*, host='127.0.0.1:8000', token=TOKEN, origin=None, content_type=None, extra=()):
    result = [('Host', host)] if host is not None else []
    if origin is not None:
        result.append(('Origin', origin))
    if token is not None:
        result.append((security.CSRF_HEADER, token))
    if content_type is not None:
        result.append(('Content-Type', content_type))
    return result + list(extra)


def request_guard():
    call('GET', '/health', headers(token=None))  # build the middleware stack
    layer = app.middleware_stack
    while not isinstance(layer, security.RequestGuard):
        layer = layer.app
    return layer


@contextmanager
def probe():
    """Replace everything behind RequestGuard with a recorder; nothing real can execute."""
    layer = request_guard()
    seen = []

    async def recorder(scope, receive, send):
        message = await receive()
        seen.append((scope['method'], scope['path'], message.get('body', b'')))
        await send({'type': 'http.response.start', 'status': 299, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
    original, layer.app = layer.app, recorder
    try:
        yield seen
    finally:
        layer.app = original


class RouteInventoryTests(unittest.TestCase):
    def test_inventory_is_reviewed(self):
        unsafe, safe = set(), set()
        for route in app.routes:
            if isinstance(route, APIRoute):
                for method in route.methods - {'HEAD'}:
                    (safe.add(route.path) if method == 'GET' else unsafe.add((method, route.path)))
        self.assertEqual(unsafe, UNSAFE_ROUTES)
        self.assertEqual(safe, GET_ROUTES)

    def test_every_unsafe_route_rejects_untrusted_requests_before_execution(self):
        bad = {
            'missing token': (headers(token=None), security.CSRF_MISSING),
            'missing token, allowed origin': (headers(token=None, origin=ALLOWED_ORIGIN), security.CSRF_MISSING),
            'missing token, cross-site fetch': (headers(token=None, origin='http://localhost:5173',
                                                        extra=[('Sec-Fetch-Site', 'cross-site')]), security.CSRF_MISSING),
            'wrong token': (headers(token=TOKEN[:-1] + ('A' if TOKEN[-1] != 'A' else 'B')), security.CSRF_INVALID),
            'empty token': (headers(token=''), security.CSRF_INVALID),
            'duplicate token': (headers(extra=[(security.CSRF_HEADER, TOKEN)]), security.CSRF_INVALID),
            'foreign origin': (headers(origin='http://evil.example'), 'origin_rejected'),
            'null origin': (headers(origin='null'), 'origin_rejected'),
            'preview origin': (headers(origin='http://127.0.0.1:4173'), 'origin_rejected'),
            'duplicate origin': (headers(origin=ALLOWED_ORIGIN, extra=[('Origin', ALLOWED_ORIGIN)]), 'origin_rejected'),
        }
        with probe() as seen:
            for method, path in sorted(UNSAFE_ROUTES):
                for body, content_type in ((b'', None), (b'{}', 'application/json')):
                    for name, (request, code) in bad.items():
                        with self.subTest(method=method, path=path, case=name, body=body):
                            if content_type:
                                request = request + [('Content-Type', content_type)]
                            status, _, payload = call(method, concrete(path), request, body)
                            self.assertEqual(status, 403)
                            self.assertEqual(json.loads(payload)['code'], code)
            self.assertEqual(seen, [])

    def test_valid_token_reaches_application_with_or_without_origin(self):
        with probe() as seen:
            for method, path in sorted(UNSAFE_ROUTES):
                for origin in (None, 'http://127.0.0.1:5173', 'http://localhost:5173',
                               'http://127.0.0.1:8000', 'http://localhost:8000'):
                    with self.subTest(method=method, path=path, origin=origin):
                        status, _, _ = call(method, concrete(path), headers(origin=origin))
                        self.assertEqual(status, 299)
                # localhost:5173 -> 127.0.0.1:8000 is cross-site; the token, not Fetch-Metadata, decides.
                for site, origin in (('cross-site', 'http://localhost:5173'), ('same-site', ALLOWED_ORIGIN),
                                     ('cross-site', None)):
                    status, _, _ = call(method, concrete(path), headers(
                        origin=origin, content_type='application/json', extra=[('Sec-Fetch-Site', site)]), b'{}')
                    self.assertEqual(status, 299)
        self.assertEqual(len(seen), len(UNSAFE_ROUTES) * 8)

    def test_get_routes_need_no_token_but_still_need_local_host(self):
        with probe() as seen:
            for path in sorted(GET_ROUTES - {'/events'}):
                with self.subTest(path=path):
                    self.assertEqual(call('GET', concrete(path), headers(token=None))[0], 299)
                    self.assertEqual(call('GET', concrete(path), headers(token=None, host='evil.example'))[0], 421)
        self.assertEqual(len(seen), len(GET_ROUTES) - 1)

    def test_cross_site_reads_require_an_allowed_origin(self):
        allowed = [('same-origin', None), ('none', None), ('cross-site', 'http://localhost:5173'),
                   ('same-site', ALLOWED_ORIGIN), ('cross-site', 'http://127.0.0.1:8000')]
        forbidden = [('cross-site', None), ('same-site', None), ('cross-site', 'http://evil.example'),
                     ('cross-site', 'null'), ('same-site', 'http://127.0.0.1:4173')]
        with probe() as seen:
            for method in ('GET', 'HEAD'):
                for path in sorted(GET_ROUTES):
                    for site, origin in allowed:
                        with self.subTest(method=method, path=path, site=site, origin=origin):
                            request = headers(token=None, origin=origin, extra=[('Sec-Fetch-Site', site)])
                            self.assertEqual(call(method, concrete(path), request)[0], 299)
                    for site, origin in forbidden:
                        with self.subTest(method=method, path=path, site=site, origin=origin):
                            request = headers(token=None, origin=origin, extra=[('Sec-Fetch-Site', site)])
                            status, _, payload = call(method, concrete(path), request)
                            self.assertEqual((status, json.loads(payload)['code']), (403, 'cross_site_forbidden'))
        self.assertEqual(len(seen), 2 * len(GET_ROUTES) * len(allowed))


class HostTests(unittest.TestCase):
    def test_loopback_names_are_accepted(self):
        for host in ('127.0.0.1:8000', 'localhost:8000', '[::1]:8000', 'LOCALHOST:8000', '127.0.0.1', 'localhost'):
            with self.subTest(host=host):
                self.assertEqual(call('GET', '/health', headers(host=host, token=None))[0], 200)

    def test_other_or_malformed_hosts_are_rejected_on_every_path(self):
        hosts = ('evil.example', 'evil.example:8000', '127.0.0.1.evil.example', 'localhost.', 'localhost.:8000',
                 '127.0.0.2:8000', '0.0.0.0:8000', '[::1', '::1', '127.0.0.1:abc', '127.0.0.1:8000:1',
                 ' 127.0.0.1', 'local host', '')
        for host in hosts:
            for method, path in (('GET', '/health'), ('GET', '/docs'), ('GET', '/openapi.json'),
                                 ('GET', security.TOKEN_PATH), ('OPTIONS', '/agents/start'),
                                 ('POST', '/agents/x/stop'), ('GET', '/events')):
                with self.subTest(host=host, path=path):
                    status, _, payload = call(method, path, headers(host=host, origin=ALLOWED_ORIGIN))
                    self.assertEqual(status, 421)
                    self.assertEqual(json.loads(payload)['code'], 'invalid_host')

    def test_missing_or_duplicate_host_is_rejected(self):
        self.assertEqual(call('GET', '/health', headers(host=None, token=None))[0], 421)
        duplicate = headers(token=None, extra=[('Host', '127.0.0.1:8000')])
        self.assertEqual(call('GET', '/health', duplicate)[0], 421)

    def test_host_parser(self):
        self.assertEqual(security.host_name('[::1]:8000'), '::1')
        self.assertEqual(security.host_name('LocalHost'), 'localhost')
        for value in ('[::1]x', '127.0.0.1:', '127.0.0.1:123456', 'a b'):
            self.assertIsNone(security.host_name(value))

    def test_websocket_with_foreign_host_is_closed(self):
        sent = []

        async def run():
            async def receive():
                return {'type': 'websocket.connect'}

            async def send(message):
                sent.append(message)
            await app({'type': 'websocket', 'path': '/events', 'headers': [(b'host', b'evil.example')],
                       'query_string': b'', 'root_path': ''}, receive, send)
        asyncio.run(run())
        self.assertEqual(sent, [{'type': 'websocket.close', 'code': 1008}])


class TokenTests(unittest.TestCase):
    def test_token_endpoint_serves_local_and_allowed_origins_only(self):
        for origin in (None, 'http://127.0.0.1:5173', 'http://localhost:5173', 'http://127.0.0.1:8000'):
            with self.subTest(origin=origin):
                status, response, payload = call('GET', security.TOKEN_PATH, headers(token=None, origin=origin))
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload), {'csrf_token': TOKEN})
                self.assertEqual(response['cache-control'], 'no-store')
                if origin:
                    self.assertEqual(response['access-control-allow-origin'], origin)
        for name, request in (('foreign', headers(token=None, origin='http://evil.example')),
                              ('null', headers(token=None, origin='null')),
                              ('preview', headers(token=None, origin='http://127.0.0.1:4173')),
                              ('cross-site', headers(token=None, extra=[('Sec-Fetch-Site', 'cross-site')]))):
            with self.subTest(case=name):
                status, response, payload = call('GET', security.TOKEN_PATH, request)
                self.assertEqual((status, json.loads(payload)['code']), (403, 'cross_site_forbidden'))
                self.assertNotIn(TOKEN, payload.decode())
                self.assertNotIn('access-control-allow-origin', response)

    def test_tokens_are_random_per_process_and_not_logged_or_echoed(self):
        self.assertGreaterEqual(len(TOKEN), 43)
        self.assertNotEqual(security.new_csrf_token(), security.new_csrf_token())
        with self.assertLogs(security.logger, level='WARNING') as logs, probe():
            call('POST', '/agents/x/stop', headers(token=TOKEN + 'x'))
            call('POST', '/agents/x/stop', headers(origin='http://evil.example'))
            status, _, payload = call('POST', '/agents/x/stop', headers(token=TOKEN + 'x'))
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertNotIn(TOKEN, payload.decode())
        self.assertIn('Rejected POST /agents/x/stop: csrf_invalid', '\n'.join(logs.output))

    def test_openapi_documents_header_for_every_unsafe_operation(self):
        status, _, payload = call('GET', '/openapi.json', headers(token=None))
        self.assertEqual(status, 200)
        schema = json.loads(payload)
        self.assertEqual(schema['components']['securitySchemes']['csrfToken'],
                         {'type': 'apiKey', 'in': 'header', 'name': security.CSRF_HEADER,
                          'description': f'Value from GET {security.TOKEN_PATH}'})
        for method, path in UNSAFE_ROUTES:
            self.assertEqual(schema['paths'][path][method.lower()]['security'], [{'csrfToken': []}])
        self.assertNotIn('security', schema['paths']['/health']['get'])

    def test_docs_origin_still_requires_the_token(self):
        with probe() as seen:
            status, _, _ = call('POST', '/agents/x/stop', headers(token=None, origin='http://127.0.0.1:8000'))
        self.assertEqual(status, 403)
        self.assertEqual(seen, [])


class CorsTests(unittest.TestCase):
    def preflight(self, origin, method='POST', requested='content-type,x-control-center-csrf'):
        return call('OPTIONS', '/agents/start', headers(token=None, origin=origin, extra=[
            ('Access-Control-Request-Method', method), ('Access-Control-Request-Headers', requested)]))

    def test_allowed_origins_preflight_custom_header_and_methods(self):
        for origin in ('http://127.0.0.1:5173', 'http://localhost:5173'):
            for method in ('POST', 'DELETE', 'PUT', 'PATCH'):
                with self.subTest(origin=origin, method=method):
                    status, response, _ = self.preflight(origin, method)
                    self.assertEqual(status, 200)
                    self.assertEqual(response['access-control-allow-origin'], origin)
                    self.assertIn(security.CSRF_HEADER.lower(), response['access-control-allow-headers'].lower())

    def test_disallowed_origins_get_no_cors_grant(self):
        for origin in ('http://evil.example', 'null', 'http://127.0.0.1:4173'):
            with self.subTest(origin=origin):
                status, response, _ = self.preflight(origin)
                self.assertEqual(status, 400)
                self.assertNotIn('access-control-allow-origin', response)

    def test_rejections_are_readable_by_the_frontend_for_token_refresh(self):
        status, response, payload = call('POST', '/agents/x/stop', headers(token='stale', origin=ALLOWED_ORIGIN))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)['code'], security.CSRF_INVALID)
        self.assertEqual(response['access-control-allow-origin'], ALLOWED_ORIGIN)


class ContentTypeTests(unittest.TestCase):
    def send(self, content_type, body, **options):
        with probe() as seen:
            status, _, payload = call('POST', '/agents/start', headers(content_type=content_type, **options), body)
        return status, (json.loads(payload)['code'] if payload else None), seen

    def test_non_json_bodies_are_rejected_before_execution(self):
        body = json.dumps({'task': 'x'}).encode()
        for content_type in (None, 'text/plain', 'text/plain;charset=UTF-8', 'application/x-www-form-urlencoded',
                             'multipart/form-data; boundary=x', 'application/json; charset=latin-1',
                             'application/jsonx', 'application/vnd.api+json'):
            with self.subTest(content_type=content_type):
                status, code, seen = self.send(content_type, body)
                self.assertEqual((status, code, seen), (415, 'unsupported_media_type', []))

    def test_body_less_request_with_non_json_type_is_rejected(self):
        self.assertEqual(self.send('text/plain', b'')[:2], (415, 'unsupported_media_type'))

    def test_json_bodies_and_body_less_requests_pass(self):
        for content_type, body in (('application/json', b'{}'), ('application/json; charset=utf-8', b'{}'),
                                   ('Application/JSON;Charset="UTF-8"', b'{}'), (None, b''),
                                   ('application/json', b'')):
            with self.subTest(content_type=content_type, body=body):
                status, _, seen = self.send(content_type, body)
                self.assertEqual(status, 299)
                self.assertEqual(seen[0][2], body)

    def test_token_and_origin_are_checked_before_media_type(self):
        self.assertEqual(self.send('text/plain', b'{}', token=None)[:2], (403, security.CSRF_MISSING))
        self.assertEqual(self.send('text/plain', b'{}', origin='http://evil.example')[:2], (403, 'origin_rejected'))

    def test_one_mebibyte_limit_by_header_and_by_bytes(self):
        limit = security.MAX_BODY_BYTES
        exact = b'"' + b'a' * (limit - 2) + b'"'
        self.assertEqual(self.send('application/json', exact)[0], 299)
        over = headers(content_type='application/json', extra=[('Content-Length', str(limit + 1))])
        with probe() as seen:
            self.assertEqual(call('POST', '/agents/start', over, b'')[0], 413)
            # Chunked or under-declared bodies are counted as they arrive.
            status, _, _ = call('POST', '/agents/start', headers(content_type='application/json'),
                                chunks=[b'a' * limit, b'a'])
            self.assertEqual(status, 413)
            invalid = headers(content_type='application/json', extra=[('Content-Length', 'abc')])
            self.assertEqual(call('POST', '/agents/start', invalid, b'{}')[0], 400)
        self.assertEqual(seen, [])

    def test_fastapi_strict_content_type_is_a_second_layer(self):
        """The application behind the guards must not parse non-JSON or untyped bodies as JSON."""
        inner = request_guard().app
        with patch('app.main.notifications') as notifications:
            for content_type in (None, 'text/plain'):
                request = [('Host', '127.0.0.1:8000')] + ([('Content-Type', content_type)] if content_type else [])
                status, _, _ = call('POST', '/notifications/preferences', request, b'{"enabled": true}',
                                    target=inner)
                self.assertEqual(status, 422)
            notifications.set_enabled.assert_not_called()
            status, _, _ = call('POST', '/notifications/preferences',
                                [('Content-Type', 'application/json')], b'{"enabled": true}', target=inner)
            self.assertEqual(status, 200)
            notifications.set_enabled.assert_called_once_with(True)

    def test_installed_fastapi_default_behaviour(self):
        probe_app = FastAPI()

        class Body(BaseModel):
            value: int

        ran = []

        @probe_app.post('/x')
        def handler(body: Body):
            ran.append(body.value)
            return {'value': body.value}

        @probe_app.post('/bodyless')
        def bodyless():
            ran.append('bodyless')
            return {}
        for content_type, expected in ((None, 422), ('text/plain', 422), ('application/x-www-form-urlencoded', 422),
                                       ('multipart/form-data; boundary=x', 422), ('application/json', 200),
                                       ('application/vnd.api+json', 200), ('application/json; charset=latin-1', 200)):
            request = [('Content-Type', content_type)] if content_type else []
            with self.subTest(content_type=content_type):
                ran.clear()
                self.assertEqual(call('POST', '/x', request, b'{"value": 1}', target=probe_app)[0], expected)
                self.assertEqual(bool(ran), expected == 200)
        # Unguarded, a body-less route runs for any simple cross-site request:
        # this is the gap RequestGuard closes with the token requirement.
        for content_type, body in ((None, b''), ('text/plain', b'anything')):
            ran.clear()
            request = [('Content-Type', content_type)] if content_type else []
            self.assertEqual(call('POST', '/bodyless', request, body, target=probe_app)[0], 200)
            self.assertEqual(ran, ['bodyless'])


class StreamTests(unittest.TestCase):
    def test_events_stream_without_token_and_without_buffering(self):
        async def run():
            sent, first_chunk = [], asyncio.Event()
            requested = False

            async def receive():
                nonlocal requested
                if not requested:
                    requested = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await first_chunk.wait()
                return {'type': 'http.disconnect'}

            async def send(message):
                sent.append(message)
                if message['type'] == 'http.response.body' and message.get('body'):
                    first_chunk.set()
            scope = {'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': 'GET',
                     'scheme': 'http', 'path': '/events', 'raw_path': b'/events', 'query_string': b'',
                     'headers': [(b'host', b'127.0.0.1:8000'), (b'origin', ALLOWED_ORIGIN.encode())],
                     'root_path': '', 'client': ('127.0.0.1', 1), 'server': ('127.0.0.1', 8000)}
            with patch('app.main.changes', ChangeBroker()) as broker:
                await asyncio.wait_for(app(scope, receive, send), 5)
                self.assertEqual(len(broker._subscribers), 0)
            return sent
        sent = asyncio.run(run())
        start = sent[0]
        self.assertEqual(start['status'], 200)
        response = {k.decode(): v.decode() for k, v in start['headers']}
        self.assertIn('text/event-stream', response['content-type'])
        self.assertEqual(response['access-control-allow-origin'], ALLOWED_ORIGIN)
        self.assertEqual(sent[1]['body'], b': connected\n\n')


class ConfigurationTests(unittest.TestCase):
    def test_default_and_configured_origins(self):
        with patch.dict('os.environ', {}, clear=False) as environ:
            environ.pop('CONTROL_CENTER_ALLOWED_ORIGINS', None)
            self.assertEqual(security.allowed_origins(), security.DEFAULT_ALLOWED_ORIGINS)
            self.assertNotIn('http://127.0.0.1:4173', security.allowed_origins())
            environ['CONTROL_CENTER_ALLOWED_ORIGINS'] = ' http://127.0.0.1:4173/ , ,http://localhost:5173'
            self.assertEqual(security.allowed_origins(), ('http://127.0.0.1:4173', 'http://localhost:5173'))


if __name__ == '__main__':
    unittest.main()
