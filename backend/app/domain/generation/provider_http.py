"""Provider-neutral JSON-over-HTTPS transport for the generation and review adapters (ADR 0021).

The contract is deliberately vendor-neutral: POST one JSON body, get one JSON object back
(optionally wrapped as `{"output": {...}, "model": "...", "usage": {...}}`). A vendor whose
API differs sits behind a thin gateway the institution runs; nothing here knows a vendor.
Bounded: timeout, retries with backoff on timeouts / 429 / 5xx, response size cap. The API
key is sent only as a bearer header and never logged.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpCallError(Exception):
    """kind: not_configured | timeout | http | invalid_json | too_large"""

    def __init__(self, kind: str, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


@dataclass(frozen=True)
class HttpResult:
    output: dict
    model: Optional[str]
    usage: dict
    attempts: int
    latency_ms: int


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
    if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise HttpCallError("not_configured", "the provider base_url must use https")
    if not parsed.hostname:
        raise HttpCallError("not_configured", "the provider base_url has no host")


def _is_groq(url: str) -> bool:
    return urlparse(url).hostname == "api.groq.com"


def _groq_payload(body: dict) -> dict:
    """Translate the neutral {system_prompt, input} body into an OpenAI-style chat completion request."""
    from app.core.config import settings

    system = f"{body.get('system_prompt', '')}\n\nRespond with a single JSON object only."
    return {
        "model": settings.EXAM_GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(body.get("input", {}))},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }


def post_json(
    base_url: Optional[str],
    api_key: Optional[str],
    body: dict,
    *,
    timeout: float = 60.0,
    max_retries: int = 2,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> HttpResult:
    if not base_url or not api_key:
        raise HttpCallError("not_configured", "provider base_url/api_key are not configured")
    _check_url(base_url)
    groq = _is_groq(base_url)
    if groq:
        body = _groq_payload(body)
    owns_client = client is None
    http = client or httpx.Client()
    started = clock()
    last: Optional[HttpCallError] = None
    try:
        for attempt in range(1, max_retries + 2):
            try:
                response = http.post(
                    base_url, json=body, timeout=timeout,
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                )
            except httpx.TimeoutException:
                last = HttpCallError("timeout", f"the provider did not answer within {timeout:g}s")
            except httpx.HTTPError as exc:
                last = HttpCallError("http", f"the provider could not be reached ({type(exc).__name__})")
            else:
                if response.status_code in RETRYABLE_STATUSES:
                    last = HttpCallError("http", f"the provider answered HTTP {response.status_code}", status_code=response.status_code)
                elif response.status_code >= 400:
                    raise HttpCallError("http", f"the provider answered HTTP {response.status_code}", status_code=response.status_code)
                else:
                    return _parse(response, attempt, int((clock() - started) * 1000), groq=groq)
            if attempt <= max_retries:
                sleep(min(8.0, 0.5 * (2 ** (attempt - 1))))
        assert last is not None
        raise last
    finally:
        if owns_client:
            http.close()


def _parse(response: httpx.Response, attempts: int, latency_ms: int, *, groq: bool = False) -> HttpResult:
    content = response.content
    if len(content) > MAX_RESPONSE_BYTES:
        raise HttpCallError("too_large", "the provider response is larger than the allowed size")
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HttpCallError("invalid_json", "the provider did not return valid JSON") from exc
    if not isinstance(data, dict):
        raise HttpCallError("invalid_json", "the provider must return a JSON object")
    if groq:
        try:
            inner = json.loads(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise HttpCallError("invalid_json", "the provider did not return a JSON completion") from exc
        if not isinstance(inner, dict):
            raise HttpCallError("invalid_json", "the provider must return a JSON object")
        data = {"output": inner, "model": data.get("model"), "usage": data.get("usage")}
    output = data.get("output") if isinstance(data.get("output"), dict) else data
    model = data.get("model")
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    return HttpResult(
        output=output, model=str(model)[:100] if isinstance(model, str) else None, usage=usage, attempts=attempts, latency_ms=latency_ms
    )
