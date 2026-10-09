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
from datetime import datetime, timezone
from typing import Optional, Sequence
from urllib.parse import urljoin, urlparse

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
    fetched_at: Optional[datetime] = None


def _is_disallowed_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped  # ::ffff:127.0.0.1 is loopback, whatever the family says
    if ip in _METADATA_SERVICE_IPS:
        return True
    # `is_global` also rejects shared (100.64/10), documentation, benchmarking and other non-routable ranges.
    return (not ip.is_global) or bool(ip.is_multicast or ip.is_unspecified)


def host_is_allowed(hostname: str, allowed_hosts: Sequence[str]) -> bool:
    """Empty allowlist = any public host (faculty review is the approval step)."""
    if not allowed_hosts:
        return True
    host = hostname.lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in (h.lower().lstrip(".") for h in allowed_hosts))


def validate_url_scheme_and_host(url: str, *, allowed_hosts: Sequence[str] = ()) -> str:
    """Returns the validated hostname, or raises UnsafeUrlError."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} is not allowed (only https)")
    if not parsed.hostname:
        raise UnsafeUrlError("URL has no hostname")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeUrlError("URLs with embedded credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeUrlError("URL has an invalid port") from exc
    if port not in (None, 443):
        raise UnsafeUrlError("only the default HTTPS port is allowed")
    if not host_is_allowed(parsed.hostname, allowed_hosts):
        raise UnsafeUrlError(f"host {parsed.hostname!r} is not on the approved source host list")
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
        resolved_ips.append(raw_ip)

    if not resolved_ips:
        raise UnsafeUrlError(f"host {hostname!r} did not resolve to any usable address")
    return resolved_ips


def fetch_url_safely(url: str, *, http_client, allowed_hosts: Sequence[str] = ()) -> FetchedResource:
    """`http_client` is an injected httpx.Client-like object (constructor
    arg, not a module-level singleton) so tests can supply a fake transport
    with no real network access. Use `LimitedHttpClient` in production so the
    size cap applies while the body streams in, not after it is in memory."""

    current_url = url
    for _hop in range(URL_MAX_REDIRECTS + 1):
        hostname = validate_url_scheme_and_host(current_url, allowed_hosts=allowed_hosts)
        resolve_and_validate_host(hostname)

        response = http_client.get(current_url, timeout=URL_FETCH_TIMEOUT_SECONDS, follow_redirects=False)
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise UnsafeUrlError("redirect response missing Location header")
            current_url = urljoin(current_url, location)  # relative redirects are legal; each hop is re-validated above
            continue
        if response.status_code >= 400:
            raise UnsafeUrlError(f"the server answered with HTTP {response.status_code}")

        content = response.content
        if len(content) > URL_MAX_RESPONSE_BYTES:
            raise UnsafeUrlError(f"response exceeds {URL_MAX_RESPONSE_BYTES} byte limit")
        return FetchedResource(
            final_url=current_url, content=content, content_type=response.headers.get("content-type"),
            fetched_at=datetime.now(timezone.utc),
        )

    raise UnsafeUrlError(f"exceeded {URL_MAX_REDIRECTS} redirect(s)")


class _BufferedResponse:
    def __init__(self, status_code: int, headers, content: bytes) -> None:
        self.status_code = status_code
        self.headers = headers
        self.content = content


class LimitedHttpClient:
    """httpx wrapper with the `get(url, timeout=, follow_redirects=)` shape the
    resolver expects, that stops reading once the size cap is passed. A
    declared Content-Length above the cap is refused before the body is read."""

    def __init__(self, client, *, max_bytes: int = URL_MAX_RESPONSE_BYTES) -> None:
        self._client = client
        self._max_bytes = max_bytes

    def get(self, url: str, *, timeout: float, follow_redirects: bool) -> _BufferedResponse:
        import httpx

        try:
            with self._client.stream("GET", url, timeout=timeout, follow_redirects=follow_redirects) as response:
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self._max_bytes:
                    raise UnsafeUrlError(f"response exceeds {self._max_bytes} byte limit")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise UnsafeUrlError(f"response exceeds {self._max_bytes} byte limit")
                return _BufferedResponse(response.status_code, response.headers, bytes(body))
        except httpx.TimeoutException as exc:
            raise UnsafeUrlError("the server did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise UnsafeUrlError(f"the page could not be fetched ({type(exc).__name__})") from exc
