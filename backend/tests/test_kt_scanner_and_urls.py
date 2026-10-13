"""Prompt 4 (ADR 0020): the clamd scanner adapter (against a local stand-in daemon speaking the
INSTREAM protocol) and URL-fetch hardening (SSRF classes, redirects, size/time caps)."""

from __future__ import annotations

import ipaddress
import socket
import struct
import threading

import httpx
import pytest

from app.api.routes import exams_kt
from app.core.config import settings
from app.domain.kt import url_resolver
from app.domain.kt.malware_scan import ClamdScanner, DeterministicFakeScanner, DevBypassScanner, NullMalwareScanner, build_scanner
from app.domain.kt.url_resolver import (
    LimitedHttpClient,
    UnsafeUrlError,
    fetch_url_safely,
    host_is_allowed,
    resolve_and_validate_host,
    validate_url_scheme_and_host,
)


# -- scanner ------------------------------------------------------------------------------------------

class _FakeClamd:
    """One-connection-at-a-time stand-in that records what a client sent and replies as told."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.received = b""
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        with conn:
            buffer = b""
            conn.settimeout(5)
            while not buffer.endswith(struct.pack(">I", 0)):
                part = conn.recv(65536)
                if not part:
                    break
                buffer += part
            self.received = buffer
            conn.sendall(self.reply)

    def close(self) -> None:
        self._server.close()


def test_clamd_scanner_streams_the_content_in_instream_frames_and_accepts_ok() -> None:
    daemon = _FakeClamd(b"stream: OK\0")
    try:
        payload = b"A" * 150_000  # spans three 64 KiB frames
        result = ClamdScanner("127.0.0.1", daemon.port).scan(payload)
    finally:
        daemon.close()
    assert result.clean and not result.infected and not result.unavailable
    assert daemon.received.startswith(b"zINSTREAM\0")
    assert daemon.received.endswith(struct.pack(">I", 0))
    frame_bytes = daemon.received[len(b"zINSTREAM\0"):]
    assert struct.unpack(">I", frame_bytes[:4])[0] == 65536
    assert frame_bytes.count(b"A") == 150_000


def test_clamd_signature_match_is_infected_not_unavailable() -> None:
    daemon = _FakeClamd(b"stream: Eicar-Test-Signature FOUND\0")
    try:
        result = ClamdScanner("127.0.0.1", daemon.port).scan(b"x")
    finally:
        daemon.close()
    assert not result.clean and result.infected and not result.unavailable
    assert "Eicar-Test-Signature" in result.detail


@pytest.mark.parametrize("reply", [b"INSTREAM size limit exceeded. ERROR\0", b"", b"garbage"])
def test_clamd_errors_and_odd_replies_are_never_clean(reply: bytes) -> None:
    daemon = _FakeClamd(reply)
    try:
        result = ClamdScanner("127.0.0.1", daemon.port).scan(b"x")
    finally:
        daemon.close()
    assert not result.clean and result.unavailable and not result.infected


def test_an_unreachable_daemon_is_unavailable_not_clean() -> None:
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()  # nothing listens here now
    result = ClamdScanner("127.0.0.1", port, timeout=0.5).scan(b"x")
    assert not result.clean and result.unavailable


@pytest.mark.parametrize("endpoint,expected", [
    (None, NullMalwareScanner), ("", NullMalwareScanner), ("   ", NullMalwareScanner), ("clam:notaport", NullMalwareScanner),
    ("clam:0", NullMalwareScanner), ("clam:70000", NullMalwareScanner),
    ("clamav.internal:3310", ClamdScanner), ("tcp://clamav.internal:3310", ClamdScanner), ("clamav.internal", ClamdScanner),
])
def test_scanner_selection_fails_closed_on_anything_unusable(endpoint, expected) -> None:
    assert type(build_scanner(endpoint)) is expected


def test_default_scanners_report_configuration_and_never_say_clean_without_a_real_engine() -> None:
    assert NullMalwareScanner().configured is False
    assert not NullMalwareScanner().scan(b"hello").clean
    assert DeterministicFakeScanner().scan(b"EICAR-TEST-MARKER").infected
    assert build_scanner("h:3310").configured is True


@pytest.mark.parametrize("env,flag,honoured", [
    ("development", True, True), ("development", False, False),
    ("production", True, False), ("staging", True, False), ("test", True, False),
])
def test_the_scan_bypass_is_honoured_only_in_development_and_only_when_asked_for(monkeypatch, env, flag, honoured) -> None:
    monkeypatch.setattr(settings, "ENV", env)
    monkeypatch.setattr(settings, "EXAM_MALWARE_SCAN_DEV_BYPASS", flag)
    assert settings.malware_scan_dev_bypass_allowed() is honoured


def test_the_upload_route_picks_the_bypass_scanner_only_when_the_gate_allows_it(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_MALWARE_SCANNER_ENDPOINT", None)
    monkeypatch.setattr(settings, "EXAM_MALWARE_SCAN_DEV_BYPASS", True)

    monkeypatch.setattr(exams_kt, "_scanner_singleton", None)
    monkeypatch.setattr(settings, "ENV", "development")
    assert isinstance(exams_kt._scanner(), DevBypassScanner) and exams_kt._scanner().scan(b"x").clean

    monkeypatch.setattr(exams_kt, "_scanner_singleton", None)
    monkeypatch.setattr(settings, "ENV", "production")
    scanner = exams_kt._scanner()
    assert not isinstance(scanner, DevBypassScanner) and not scanner.scan(b"x").clean


# -- URL validation -----------------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "https://user:pass@example.com/a", "https://user@example.com/a", "https://example.com:8443/a",
    "https://example.com:22/a", "http://example.com/a", "ftp://example.com/a", "file:///etc/passwd",
    "javascript:alert(1)", "https:///nohost", "//example.com/a",
])
def test_unsafe_url_shapes_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        validate_url_scheme_and_host(url)


def test_default_and_explicit_443_ports_are_fine() -> None:
    assert validate_url_scheme_and_host("https://example.com/a") == "example.com"
    assert validate_url_scheme_and_host("https://example.com:443/a") == "example.com"


@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0", "224.0.0.1",
    "198.18.0.1", "::1", "fe80::1", "fc00::1", "fd00:ec2::254", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:10.0.0.1",
])
def test_non_public_addresses_are_blocked(host: str) -> None:
    with pytest.raises(UnsafeUrlError):
        resolve_and_validate_host(host)


@pytest.mark.parametrize("host", ["2130706433", "0x7f000001", "017700000001"])
def test_obfuscated_loopback_spellings_are_blocked(host: str) -> None:
    with pytest.raises(UnsafeUrlError):
        resolve_and_validate_host(host)


def test_a_host_resolving_to_a_private_address_is_blocked_even_when_it_also_has_a_public_one(monkeypatch) -> None:
    monkeypatch.setattr(url_resolver.socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, 0, 0, "", ("8.8.8.8", 0)), (socket.AF_INET, 0, 0, "", ("10.0.0.7", 0)),
    ])
    with pytest.raises(UnsafeUrlError, match="disallowed"):
        resolve_and_validate_host("rebind.example")


def test_the_host_allowlist_matches_suffixes_only() -> None:
    allowed = ["college.edu", "docs.example.org"]
    assert host_is_allowed("college.edu", allowed) and host_is_allowed("cs.college.edu", allowed)
    assert not host_is_allowed("evilcollege.edu", allowed) and not host_is_allowed("college.edu.evil.com", allowed)
    assert host_is_allowed("anything.example", [])
    with pytest.raises(UnsafeUrlError, match="approved source host"):
        validate_url_scheme_and_host("https://other.example/a", allowed_hosts=allowed)


# -- redirects ------------------------------------------------------------------------------------------

class _Response:
    def __init__(self, status_code: int, *, location=None, content=b"", content_type="text/plain") -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type, **({"location": location} if location else {})}
        self.content = content


class _Client:
    def __init__(self, responses: dict[str, _Response]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def get(self, url, *, timeout, follow_redirects):
        assert follow_redirects is False
        self.requested.append(url)
        return self.responses[url]


@pytest.fixture
def public_dns(monkeypatch):
    """Every hostname is treated as resolving to a public address (no real DNS in tests)."""
    seen: list[str] = []
    real = url_resolver.resolve_and_validate_host

    def fake(hostname: str):
        seen.append(hostname)
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            return ["8.8.8.8"]  # a DNS name: pretend it resolves to a public address
        return real(hostname)  # IP literals go through the real classifier

    monkeypatch.setattr(url_resolver, "resolve_and_validate_host", fake)
    return seen


def test_a_relative_redirect_is_resolved_and_revalidated(public_dns) -> None:
    client = _Client({
        "https://docs.example/a/b": _Response(302, location="/final/page"),
        "https://docs.example/final/page": _Response(200, content=b"done"),
    })
    result = fetch_url_safely("https://docs.example/a/b", http_client=client)
    assert result.final_url == "https://docs.example/final/page" and result.content == b"done"
    assert result.fetched_at is not None and result.fetched_at.tzinfo is not None


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/x", "https://169.254.169.254/latest/meta-data", "https://[::ffff:7f00:1]/x", "http://docs.example/x",
    "https://user:pw@docs.example/x", "https://docs.example:8080/x", "file:///etc/passwd", "https://10.0.0.5/admin",
])
def test_a_redirect_into_an_unsafe_target_is_refused_before_it_is_requested(public_dns, target) -> None:
    client = _Client({"https://docs.example/a": _Response(301, location=target)})
    with pytest.raises(UnsafeUrlError):
        fetch_url_safely("https://docs.example/a", http_client=client)
    assert client.requested == ["https://docs.example/a"]  # the unsafe hop was never contacted


def test_a_redirect_loop_is_cut_off(public_dns) -> None:
    client = _Client({"https://docs.example/a": _Response(302, location="/b"), "https://docs.example/b": _Response(302, location="/a")})
    with pytest.raises(UnsafeUrlError, match="redirect"):
        fetch_url_safely("https://docs.example/a", http_client=client)


def test_http_errors_are_not_ingested_as_content(public_dns) -> None:
    client = _Client({"https://docs.example/a": _Response(404, content=b"<html>Not found</html>")})
    with pytest.raises(UnsafeUrlError, match="HTTP 404"):
        fetch_url_safely("https://docs.example/a", http_client=client)


def test_the_allowlist_applies_to_every_redirect_hop(public_dns) -> None:
    client = _Client({"https://docs.college.edu/a": _Response(302, location="https://elsewhere.example/b")})
    with pytest.raises(UnsafeUrlError, match="approved source host"):
        fetch_url_safely("https://docs.college.edu/a", http_client=client, allowed_hosts=["college.edu"])


# -- streamed size and time limits --------------------------------------------------------------------------

def _limited(handler, *, max_bytes: int) -> LimitedHttpClient:
    return LimitedHttpClient(httpx.Client(transport=httpx.MockTransport(handler)), max_bytes=max_bytes)


def test_the_size_cap_stops_reading_while_the_body_streams() -> None:
    chunks_sent = []

    def body():
        for _ in range(1000):
            chunks_sent.append(1)
            yield b"x" * 1024

    client = _limited(lambda request: httpx.Response(200, content=body()), max_bytes=10 * 1024)
    with pytest.raises(UnsafeUrlError, match="exceeds"):
        client.get("https://docs.example/big", timeout=5, follow_redirects=False)
    assert len(chunks_sent) < 50  # nowhere near the whole (1 MB) body was pulled


def test_a_declared_content_length_over_the_cap_is_refused_without_reading() -> None:
    def handler(request):
        return httpx.Response(200, headers={"content-length": "99999999"}, content=b"tiny")

    client = _limited(handler, max_bytes=1024)
    with pytest.raises(UnsafeUrlError, match="exceeds"):
        client.get("https://docs.example/a", timeout=5, follow_redirects=False)


def test_a_body_within_the_cap_is_returned_with_status_and_headers() -> None:
    client = _limited(lambda request: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>ok</p>"), max_bytes=1024)
    response = client.get("https://docs.example/a", timeout=5, follow_redirects=False)
    assert (response.status_code, response.content, response.headers["content-type"]) == (200, b"<p>ok</p>", "text/html")


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_timeouts_and_network_errors_become_actionable_url_errors(error) -> None:
    def handler(request):
        raise error

    with pytest.raises(UnsafeUrlError):
        _limited(handler, max_bytes=1024).get("https://docs.example/a", timeout=0.1, follow_redirects=False)
