"""Identity-provider adapters.

`SsoIdentityProvider` is a CONTRACT ONLY — no real SAML/OIDC integration is
implemented or tested here; no institutional IdP was configured/available.
`DevSinkOtpDelivery` is the college-ID/OTP fallback's delivery mechanism,
explicitly dev/test-only: it never sends a real email/SMS, it just records
the (identity_key, code) pair somewhere a test or a developer can retrieve
it. A real provider (SES/Twilio/etc.) implements the same `OtpDelivery`
protocol and is swapped in via configuration, not a code change here.
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol


logger = logging.getLogger("hirelog.institution.identity_provider")


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


class DevSinkOtpDelivery:
    """Dev/test-only delivery sink. Logs the code (never in production logs
    at INFO — this is gated by the caller only ever constructing this class
    when no real provider is configured) and keeps the last N codes
    in-memory so tests/developers can retrieve what was 'sent' without a
    real inbox."""

    def __init__(self, *, capacity: int = 200) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._sent: list[tuple[str, str]] = []  # (contact_email, code)

    def send(self, *, contact_email: str, code: str) -> None:
        logger.warning(
            "DEV OTP SINK — no real delivery provider configured. "
            "contact_email=%s code=%s (never do this in production)",
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
