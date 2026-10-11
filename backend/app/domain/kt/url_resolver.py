"""SSRF-safe URL resolver for source-registry URL submissions.

Treats the fetched content as inert data — nothing here executes or
interprets it. Defends against the common SSRF classes: disallowed schemes,
private/loopback/link-local/metadata-service IPs, and bypass via redirect
(each hop is re-validated, not just the initial URL).

KNOWN LIMITATION: validation happens immediately before connecting, which
closes the straightforward DNS-rebinding window but does not fully eliminate
a TOCTOU race with an adversarial DNS server that changes answers between
this check and the TCP handshake. A production deployment should route this
through an egress proxy/allowlist at the network layer, not rely on
application-level checks alone.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from app.domain.kt.limits import (
    ALLOWED_URL_SCHEMES,
    URL_FETCH_TIMEOUT_SECONDS,
    URL_MAX_REDIRECTS,
    URL_MAX_RESPONSE_BYTES,
)

_METADATA_SERVICE_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure metadata
    ipaddress.ip_address("fd00:ec2::254"),  # AWS IMDSv2 IPv6
}


class UnsafeUrlError(Exception):
    pass


@dataclass(frozen=True)
class FetchedResource:
    final_url: str
    content: bytes
    content_type: Optional[str]


def _is_disallowed_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip in _METADATA_SERVICE_IPS:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_url_scheme_and_host(url: str) -> str:
    """Returns the validated hostname, or raises UnsafeUrlError."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} is not allowed (only https)")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    return parsed.hostname


def resolve_and_validate_host(hostname: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"could not resolve host {hostname!r}: {exc}") from exc

    resolved_ips: list[str] = []
    for family, _, _, _, sockaddr in infos:
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if _is_disallowed_ip(ip):
            raise UnsafeUrlError(f"host {hostname!r} resolves to a disallowed address ({raw_ip})")
        if raw_ip not in resolved_ips:  # getaddrinfo repeats an address once per socket type on Linux
            resolved_ips.append(raw_ip)

    if not resolved_ips:
        raise UnsafeUrlError(f"host {hostname!r} did not resolve to any usable address")
    return resolved_ips


def fetch_url_safely(url: str, *, http_client) -> FetchedResource:
    """`http_client` is an injected httpx.Client-like object (constructor
    arg, not a module-level singleton) so tests can supply a fake transport
    with no real network access."""

    current_url = url
    for _hop in range(URL_MAX_REDIRECTS + 1):
        hostname = validate_url_scheme_and_host(current_url)
        resolve_and_validate_host(hostname)

        response = http_client.get(current_url, timeout=URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise UnsafeUrlError("redirect response missing Location header")
            current_url = location
            continue

        content = response.content
        if len(content) > URL_MAX_RESPONSE_BYTES:
            raise UnsafeUrlError(f"response exceeds {URL_MAX_RESPONSE_BYTES} byte limit")
        return FetchedResource(
            final_url=current_url, content=content, content_type=response.headers.get("content-type")
        )

    raise UnsafeUrlError(f"exceeded {URL_MAX_REDIRECTS} redirect(s)")
