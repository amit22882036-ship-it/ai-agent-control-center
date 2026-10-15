"""Local API request guards (ASGI, streaming-safe).

HostGuard rejects any Host other than the loopback names (DNS rebinding).
RequestGuard requires, for every state-changing method, an allowed Origin
when one is sent, the application CSRF token, a body of at most
MAX_BODY_BYTES, and JSON for any non-empty body. Cross-site Fetch-Metadata is
not a rejection reason for those methods: the dashboard on localhost:5173 is
cross-site to 127.0.0.1:8000, and the token is the control. A GET/HEAD whose
Sec-Fetch-Site is neither same-origin nor none must carry an allowed Origin.
Every rejection happens before the application sees the request. Token values
are never logged, persisted or placed in URLs.
"""
import hmac
import json
import logging
import os
import re
import secrets

logger = logging.getLogger(__name__)

CSRF_HEADER = 'X-Control-Center-CSRF'
TOKEN_PATH = '/security/csrf-token'
MAX_BODY_BYTES = 1024 * 1024
ALLOWED_HOSTNAMES = frozenset({'127.0.0.1', 'localhost', '::1'})
# Vite development server and the backend's own /docs page.
DEFAULT_ALLOWED_ORIGINS = ('http://127.0.0.1:5173', 'http://localhost:5173',
                           'http://127.0.0.1:8000', 'http://localhost:8000')
SAFE_METHODS = frozenset({'GET', 'HEAD', 'OPTIONS'})
CSRF_MISSING = 'csrf_missing'
CSRF_INVALID = 'csrf_invalid'
SAME_SITE_FETCHES = frozenset({'same-origin', 'none'})

_csrf_header = CSRF_HEADER.lower().encode('latin-1')
_port = re.compile(r'\d{1,5}')


def allowed_origins():
    configured = os.environ.get('CONTROL_CENTER_ALLOWED_ORIGINS')
    if configured is None:
        return DEFAULT_ALLOWED_ORIGINS
    return tuple(origin.strip().rstrip('/') for origin in configured.split(',') if origin.strip())


def new_csrf_token():
    return secrets.token_urlsafe(32)


def host_name(value):
    """Return the lower-case hostname of a Host header value, or None if malformed."""
    if not value or value != value.strip() or any(c.isspace() for c in value):
        return None
    if value.startswith('['):
        end = value.find(']')
        if end < 0:
            return None
        name, rest = value[1:end], value[end + 1:]
    elif value.count(':') > 1:
        return None
    else:
        name, colon, port = value.partition(':')
        rest = colon + port
    if rest and not (rest.startswith(':') and _port.fullmatch(rest[1:])):
        return None
    return name.lower() or None


def _values(scope, name):
    return [value.decode('latin-1') for key, value in scope['headers'] if key == name]


async def _reject(send, status, code, detail):
    body = json.dumps({'detail': detail, 'code': code}).encode()
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json'), (b'cache-control', b'no-store'),
                            (b'content-length', str(len(body)).encode())]})
    await send({'type': 'http.response.body', 'body': body})


def _log(scope, code):
    # Method, path and reason only: never header values or the token.
    logger.warning('Rejected %s %s: %s', scope.get('method'), scope.get('path'), code)


class HostGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket'):
            hosts = _values(scope, b'host')
            if len(hosts) != 1 or host_name(hosts[0]) not in ALLOWED_HOSTNAMES:
                _log(scope, 'invalid_host')
                if scope['type'] == 'websocket':
                    await send({'type': 'websocket.close', 'code': 1008})
                    return
                await _reject(send, 421, 'invalid_host', 'Requests must address the local API by loopback name.')
                return
        await self.app(scope, receive, send)


class RequestGuard:
    def __init__(self, app, token, origins=None):
        self.app = app
        self.token = token
        self.origins = frozenset(allowed_origins() if origins is None else origins)

    def _origin_allowed(self, scope, *, required):
        origins = _values(scope, b'origin')
        if not origins:
            return not required
        return len(origins) == 1 and origins[0] in self.origins

    def _read_forbidden(self, scope):
        sites = [site.strip().lower() for site in _values(scope, b'sec-fetch-site')]
        if sites and not all(site in SAME_SITE_FETCHES for site in sites):
            return not self._origin_allowed(scope, required=True)
        # The token must never be served to a foreign page, whatever its headers.
        return scope['path'] == TOKEN_PATH and not self._origin_allowed(scope, required=False)

    def _token_problem(self, scope):
        supplied = _values(scope, _csrf_header)
        if not supplied:
            return CSRF_MISSING
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode('latin-1'), self.token.encode()):
            return CSRF_INVALID
        return None

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        method = scope['method'].upper()
        if method in SAFE_METHODS:
            if method != 'OPTIONS' and self._read_forbidden(scope):
                _log(scope, 'cross_site_forbidden')
                await _reject(send, 403, 'cross_site_forbidden', 'Cross-site reads require an allowed Origin.')
                return
            await self.app(scope, receive, send)
            return
        if not self._origin_allowed(scope, required=False):
            _log(scope, 'origin_rejected')
            await _reject(send, 403, 'origin_rejected', 'Requests from this origin are not allowed.')
            return
        if code := self._token_problem(scope):
            _log(scope, code)
            await _reject(send, 403, code, 'Missing or expired CSRF token. Reload the token and retry.')
            return
        lengths = _values(scope, b'content-length')
        try:
            if any(int(value) > MAX_BODY_BYTES for value in lengths):
                raise OverflowError
        except ValueError:
            await _reject(send, 400, 'invalid_content_length', 'Invalid Content-Length.')
            return
        except OverflowError:
            _log(scope, 'body_too_large')
            await _reject(send, 413, 'body_too_large', f'Request bodies are limited to {MAX_BODY_BYTES} bytes.')
            return
        # Buffer the bounded body so size and media type are checked against
        # the bytes actually sent, before any handler runs.
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                _log(scope, 'body_too_large')
                await _reject(send, 413, 'body_too_large', f'Request bodies are limited to {MAX_BODY_BYTES} bytes.')
                return
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        body = b''.join(chunks)
        types = _values(scope, b'content-type')
        if (body or types) and not (len(types) == 1 and json_media_type(types[0])):
            _log(scope, 'unsupported_media_type')
            await _reject(send, 415, 'unsupported_media_type', 'State-changing requests must send JSON (application/json).')
            return
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {'type': 'http.request', 'body': body, 'more_body': False}

        await self.app(scope, replay, send)


def json_media_type(value):
    media, *params = [part.strip().lower() for part in value.split(';')]
    if media != 'application/json':
        return False
    return all(param in ('charset=utf-8', 'charset="utf-8"') for param in params)
