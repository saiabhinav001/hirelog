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
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol


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
    _log_once("EXAM_OTP_DELIVERY_PROVIDER=%r has no registered adapter; OTP sign-in is unavailable", name)
    return UnconfiguredOtpDelivery()
