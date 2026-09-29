"""SSRF-safety tests for the KT URL resolver — scheme allowlist, private/
loopback/link-local/metadata IP blocking, and redirect re-validation."""

from __future__ import annotations

import pytest

from app.domain.kt.url_resolver import (
    UnsafeUrlError,
    resolve_and_validate_host,
    validate_url_scheme_and_host,
)


def test_http_scheme_is_rejected() -> None:
    with pytest.raises(UnsafeUrlError, match="scheme"):
        validate_url_scheme_and_host("http://example.com/doc.pdf")


def test_https_scheme_is_accepted() -> None:
    hostname = validate_url_scheme_and_host("https://example.com/doc.pdf")
    assert hostname == "example.com"


def test_file_scheme_is_rejected() -> None:
    with pytest.raises(UnsafeUrlError):
        validate_url_scheme_and_host("file:///etc/passwd")


@pytest.mark.parametrize(
    "hostname",
    ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1", "0.0.0.0"],
)
def test_disallowed_hosts_are_rejected(hostname: str) -> None:
    with pytest.raises(UnsafeUrlError):
        resolve_and_validate_host(hostname)


def test_public_ip_literal_is_allowed() -> None:
    # 8.8.8.8 (a well-known public IP) must pass the address-class check.
    ips = resolve_and_validate_host("8.8.8.8")
    assert ips == ["8.8.8.8"]


class _FakeResponse:
    def __init__(self, status_code: int, *, location: str | None = None, content: bytes = b"", content_type: str = "text/plain"):
        self.status_code = status_code
        self.headers = {}
        if location:
            self.headers["location"] = location
        self.headers["content-type"] = content_type
        self.content = content


class _FakeHttpClient:
    """Deterministic stand-in for httpx.Client — no real network access."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses

    def get(self, url: str, *, timeout, follow_redirects):
        assert follow_redirects is False  # we always re-validate each hop ourselves
        return self._responses[url]


def test_fetch_follows_and_revalidates_redirect_chain() -> None:
    from app.domain.kt.url_resolver import fetch_url_safely

    client = _FakeHttpClient(
        {
            "https://example.com/a": _FakeResponse(302, location="https://example.com/b"),
            "https://example.com/b": _FakeResponse(200, content=b"final content"),
        }
    )
    result = fetch_url_safely("https://example.com/a", http_client=client)
    assert result.final_url == "https://example.com/b"
    assert result.content == b"final content"


def test_fetch_rejects_redirect_to_private_ip() -> None:
    from app.domain.kt.url_resolver import fetch_url_safely

    client = _FakeHttpClient(
        {"https://example.com/a": _FakeResponse(302, location="https://127.0.0.1/steal")}
    )
    with pytest.raises(UnsafeUrlError):
        fetch_url_safely("https://example.com/a", http_client=client)


def test_fetch_rejects_too_many_redirects() -> None:
    from app.domain.kt.limits import URL_MAX_REDIRECTS
    from app.domain.kt.url_resolver import fetch_url_safely

    responses = {}
    for i in range(URL_MAX_REDIRECTS + 3):
        responses[f"https://example.com/{i}"] = _FakeResponse(302, location=f"https://example.com/{i + 1}")
    client = _FakeHttpClient(responses)
    with pytest.raises(UnsafeUrlError, match="redirect"):
        fetch_url_safely("https://example.com/0", http_client=client)


def test_fetch_rejects_oversized_response() -> None:
    from app.domain.kt.limits import URL_MAX_RESPONSE_BYTES
    from app.domain.kt.url_resolver import fetch_url_safely

    client = _FakeHttpClient(
        {"https://example.com/big": _FakeResponse(200, content=b"x" * (URL_MAX_RESPONSE_BYTES + 1))}
    )
    with pytest.raises(UnsafeUrlError, match="exceeds"):
        fetch_url_safely("https://example.com/big", http_client=client)
