"""Shared URL validation helpers for network fetches and rendered links."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlparse

_ALLOWED_SCHEMES = {"http", "https"}
_LOCAL_HOSTS = {"localhost", "localhost.localdomain"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_url(
    url: str,
    *,
    allowed_hosts: set[str] | None = None,
    resolve_host: bool = False,
) -> str | None:
    """Return *url* if it is safe for fetching/rendering, otherwise ``None``.

    The guard rejects non-http schemes, credentials in URLs, literal private
    IPs, local hostnames, and control characters. DNS lookups are intentionally
    avoided so validation does not create extra network side effects.
    """
    if not isinstance(url, str):
        return None
    candidate = url.strip()
    if not candidate or _CONTROL_CHARS.search(candidate):
        return None

    try:
        parsed = urlparse(candidate)
    except Exception:
        return None

    if parsed.scheme not in _ALLOWED_SCHEMES:
        return None
    if not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None

    host = parsed.hostname.lower().rstrip(".")
    if allowed_hosts and host not in {h.lower().rstrip(".") for h in allowed_hosts}:
        return None
    if host in _LOCAL_HOSTS or host.endswith(".localhost"):
        return None
    if host.endswith(".local"):
        return None

    host_ip = _parse_ip(host)
    if host_ip is None:
        if resolve_host and not _hostname_resolves_publicly(host):
            return None
        return candidate

    if not _is_public_ip(host_ip):
        return None
    return candidate


def safe_join_url(
    base_url: str,
    location: str,
    *,
    resolve_host: bool = False,
) -> str | None:
    """Resolve a redirect location against *base_url* and validate it."""
    try:
        joined = urljoin(base_url, location)
    except Exception:
        return None
    return sanitize_url(joined, resolve_host=resolve_host)


def _parse_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _hostname_resolves_publicly(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    ips = {
        _parse_ip(info[4][0])
        for info in infos
        if info and len(info) >= 5 and info[4]
    }
    return bool(ips) and all(ip is not None and _is_public_ip(ip) for ip in ips)
