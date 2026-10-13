"""Identity-provider adapters.

`SsoIdentityProvider` is a CONTRACT ONLY — no real SAML/OIDC integration is
implemented or tested here; no institutional IdP was configured/available.
`OtpDelivery` is the delivery contract for the college-ID/OTP fallback. Until
an institution chooses SSO or verified-e-mail OTP, the only adapter is the
synthetic `DevSinkOtpDelivery` (development only: it records the code instead
of sending it). In production `build_otp_delivery` returns
`UnconfiguredOtpDelivery`, which makes OTP sign-in unavailable rather than
writing one-time codes to logs. A real provider (SES/Twilio/etc.) implements
the same `OtpDelivery` protocol and is registered in `build_otp_delivery`.
An e-mail OTP alone is a proof of mailbox control, NOT multi-factor
authentication, and must never be described as MFA.

The `webhook` adapter hands the code to an HTTPS mail relay you operate (a Google Apps Script web app, a small
serverless function, any service that accepts one JSON POST). It exists because a Hugging Face Space can only make
outbound calls on ports 80, 443 and 8080, so SMTP cannot work there. See docs/exams/otp-email-relay.md.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import Callable, Protocol
from urllib.parse import urlparse


logger = logging.getLogger("hirelog.institution.identity_provider")

_logged: set[str] = set()
_logged_lock = threading.Lock()


class SsoIdentityProvider(Protocol):
    """Institutional SSO contract stub. Real implementations (SAML/OIDC)
    require an actual institutional IdP to integrate and test against —
    none was configured. Calling any real adapter should raise until an
    institution supplies IdP metadata/credentials."""

    def initiate(self, *, institution_id: str, return_url: str) -> str:
        """Returns a redirect URL to the institution's IdP."""
        ...

    def complete(self, *, institution_id: str, callback_params: dict) -> str:
        """Validates the IdP's callback and returns the verified identity_key."""
        ...


class UnconfiguredSsoProvider:
    def initiate(self, *, institution_id: str, return_url: str) -> str:
        raise NotImplementedError(
            f"No SSO provider configured for institution {institution_id!r} — "
            "this is an institutional setup requirement, not a code change."
        )

    def complete(self, *, institution_id: str, callback_params: dict) -> str:
        raise NotImplementedError("No SSO provider configured.")


class OtpDelivery(Protocol):
    def send(self, *, contact_email: str, code: str) -> None: ...


class OtpDeliveryUnavailableError(RuntimeError):
    """No OTP delivery provider is configured for this deployment."""


class OtpDeliveryError(RuntimeError):
    """The provider was reached but did not accept the message (never carries the code)."""


def _post_json(url: str, body: dict, timeout: float) -> tuple[int, str]:
    """One JSON POST. Redirects are followed (an Apps Script web app answers through one) and an HTTP error status is
    returned rather than raised, so the caller decides."""
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - https only, checked at construction
            return response.status, response.read(4096).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""


class WebhookOtpDelivery:
    """Posts `{secret, to, code, ttl_minutes}` to an HTTPS relay that composes and sends the e-mail itself, so a leaked
    secret can only ever send sign-in-code messages. The relay answers `{"ok": true}`. The code is never logged."""

    def __init__(
        self, *, url: str, secret: str, ttl_minutes: int, timeout: float = 10.0,
        post: Callable[[str, dict, float], tuple[int, str]] = _post_json,
    ) -> None:
        if urlparse(url).scheme != "https":
            raise ValueError("the OTP relay URL must be https")
        self._url, self._secret, self._ttl, self._timeout, self._post = url, secret, ttl_minutes, timeout, post

    def send(self, *, contact_email: str, code: str) -> None:
        try:
            status_code, text = self._post(
                self._url, {"secret": self._secret, "to": contact_email, "code": code, "ttl_minutes": self._ttl}, self._timeout
            )
        except OSError as exc:  # timeouts, DNS and TLS failures are all OSError subclasses
            raise OtpDeliveryError(f"relay unreachable: {type(exc).__name__}") from exc
        if status_code != 200:
            raise OtpDeliveryError(f"relay answered HTTP {status_code}")
        try:
            accepted = bool(json.loads(text).get("ok"))
        except (ValueError, AttributeError):
            accepted = False
        if not accepted:
            raise OtpDeliveryError("relay did not accept the message")


class UnconfiguredOtpDelivery:
    """Fail-closed placeholder: nothing can be delivered, so OTP sign-in is
    reported as unavailable instead of leaking codes into logs."""

    def send(self, *, contact_email: str, code: str) -> None:
        raise OtpDeliveryUnavailableError("no OTP delivery provider is configured")


class DevSinkOtpDelivery:
    """Dev/test-only delivery sink (never constructed in production by
    `build_otp_delivery`). Logs the code and keeps the last N codes in memory so
    tests/developers can retrieve what was 'sent' without a real inbox."""

    def __init__(self, *, capacity: int = 200) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._sent: list[tuple[str, str]] = []  # (contact_email, code)

    def send(self, *, contact_email: str, code: str) -> None:
        logger.warning(
            "DEV OTP SINK — no real delivery provider configured. "
            "contact_email=%s code=%s (development only)",
            contact_email,
            code,
        )
        with self._lock:
            self._sent.append((contact_email, code))
            if len(self._sent) > self._capacity:
                self._sent.pop(0)

    def last_sent_code_for(self, contact_email: str) -> str | None:
        with self._lock:
            for email, code in reversed(self._sent):
                if email == contact_email:
                    return code
        return None


def _log_once(message: str, *args) -> None:
    """The service is built per request; a misconfiguration must be loud, not spammy."""
    key = message % args
    with _logged_lock:
        if key in _logged:
            return
        _logged.add(key)
    logger.error(message, *args)


def build_otp_delivery(*, provider: str, production: bool) -> OtpDelivery:
    """`provider` is EXAM_OTP_DELIVERY_PROVIDER. Empty means "not chosen yet":
    the synthetic sink outside production, nothing in production. A name with no
    registered adapter also yields nothing (and an error log) rather than
    guessing."""
    name = (provider or "").strip().lower()
    if name in ("", "dev_sink"):
        if production:
            if name:
                _log_once("EXAM_OTP_DELIVERY_PROVIDER=dev_sink is ignored when ENV=production")
            _log_once("No OTP delivery provider is configured: institutional OTP sign-in is unavailable")
            return UnconfiguredOtpDelivery()
        return DevSinkOtpDelivery()
    if name == "webhook":
        from app.core.config import settings

        url, secret = (settings.EXAM_OTP_WEBHOOK_URL or "").strip(), (settings.EXAM_OTP_WEBHOOK_SECRET or "").strip()
        if not url or not secret:
            _log_once("EXAM_OTP_DELIVERY_PROVIDER=webhook needs EXAM_OTP_WEBHOOK_URL and EXAM_OTP_WEBHOOK_SECRET")
            return UnconfiguredOtpDelivery()
        try:
            return WebhookOtpDelivery(
                url=url, secret=secret, ttl_minutes=max(1, round(settings.EXAM_OTP_TTL_SECONDS / 60)),
                timeout=float(settings.EXAM_OTP_WEBHOOK_TIMEOUT_SECONDS),
            )
        except ValueError:
            _log_once("EXAM_OTP_WEBHOOK_URL must be an https URL; OTP sign-in is unavailable")
            return UnconfiguredOtpDelivery()
    _log_once("EXAM_OTP_DELIVERY_PROVIDER=%r has no registered adapter; OTP sign-in is unavailable", name)
    return UnconfiguredOtpDelivery()
