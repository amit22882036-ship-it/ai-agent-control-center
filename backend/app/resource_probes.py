"""Non-destructive, short-lived external observations, never reservations."""
from contextlib import ExitStack
from dataclasses import dataclass
import errno
import socket


@dataclass(frozen=True)
class Observation:
    status: str
    reason: str


def probe_resource(resource_type, resource_key):
    """Probe dispatch: other resource types deliberately remain managed-only."""
    if resource_type != 'port':
        return Observation('not_supported', 'managed_coordination_only')
    return probe_port(resource_key)


def probe_port(resource_key):
    protocol, number = resource_key.split(':')
    kind = socket.SOCK_STREAM if protocol == 'tcp' else socket.SOCK_DGRAM
    # Binding wildcard addresses detects occupancy on individual interfaces too.
    # Keep both family checks alive together, then close everything before return.
    try:
        with ExitStack() as sockets:
            for family, address in ((socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')):
                try:
                    sock = sockets.enter_context(socket.socket(family, kind))
                except OSError as exc:
                    if family == socket.AF_INET6 and exc.errno in (errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT):
                        continue  # IPv6 genuinely unsupported on this machine.
                    raise
                if family == socket.AF_INET6:
                    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((address, int(number)))
        return Observation('available', 'bind_succeeded')
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, errno.EACCES, 10048, 10013):
            return Observation('unavailable', 'address_in_use_or_reserved')
        return Observation('unknown', 'socket_check_failed')
    except Exception:
        # No raw OS errors, paths or provider text in durable diagnostics.
        return Observation('unknown', 'probe_failed')
