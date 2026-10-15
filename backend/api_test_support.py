"""Headers for in-process ASGI test requests that satisfy the local API guards."""
from app import security
from app.main import app

HOST = (b'host', b'127.0.0.1:8000')


def request_headers(method, *, content_type=True):
    headers = [HOST]
    if content_type:
        headers.append((b'content-type', b'application/json'))
    if method.upper() not in security.SAFE_METHODS:
        headers.append((security.CSRF_HEADER.lower().encode(), app.state.csrf_token.encode()))
    return headers
