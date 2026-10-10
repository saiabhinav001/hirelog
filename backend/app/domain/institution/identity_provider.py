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
import smtplib
import ssl
import threading
from email.message import EmailMessage
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
    configured: bool

    def send(self, *, contact_email: str, code: str) -> None: ...


class OtpDeliveryError(Exception):
    """Raised by a delivery adapter when a code could not be handed off."""


class UnconfiguredOtpDelivery:
    """Fail-closed default: nothing is sent and nothing is logged."""

    configured = False

    def send(self, *, contact_email: str, code: str) -> None:
        raise OtpDeliveryError("no OTP delivery provider configured")


class SmtpOtpDelivery:
    """Real email delivery over SMTP (STARTTLS or implicit TLS, certificates
    verified). The code is placed in the message body only — never logged."""

    configured = True

    def __init__(
        self, *, host: str, port: int, username: str | None, password: str | None, from_address: str,
        use_ssl: bool = False, timeout: float = 10.0,
    ) -> None:
        self._host, self._port = host, port
        self._username, self._password = username, password
        self._from, self._use_ssl, self._timeout = from_address, use_ssl, timeout

    def _message(self, contact_email: str, code: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = "Your HireLog Exams sign-in code"
        message["From"] = self._from
        message["To"] = contact_email
        message.set_content(
            f"Your one-time sign-in code is {code}.\n\n"
            "It expires in a few minutes and can be used once. "
            "If you did not request it, you can ignore this email.\n"
        )
        return message

    def send(self, *, contact_email: str, code: str) -> None:
        context = ssl.create_default_context()
        try:
            if self._use_ssl:
                client = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=context)
            else:
                client = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
            with client:
                if not self._use_ssl:
                    client.starttls(context=context)
                if self._username:
                    client.login(self._username, self._password or "")
                client.send_message(self._message(contact_email, code))
        except (smtplib.SMTPException, OSError) as exc:
            raise OtpDeliveryError(f"SMTP delivery failed: {type(exc).__name__}") from exc


class DevSinkOtpDelivery:
    """Dev/test-only delivery sink. Logs the code (at WARNING) and keeps the
    last N codes in memory so tests/developers can retrieve what was 'sent'
    without a real inbox. Only constructed when EXAM_OTP_ALLOW_DEV_SINK is set
    explicitly — never a silent production default."""

    configured = True

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


_shared_dev_sink: DevSinkOtpDelivery | None = None


def default_otp_delivery() -> OtpDelivery:
    """SMTP when configured; the dev sink only when explicitly allowed;
    otherwise fail closed. Never silently falls back to logging codes."""
    from app.core.config import settings

    if settings.EXAM_OTP_SMTP_HOST and settings.EXAM_OTP_FROM_ADDRESS:
        return SmtpOtpDelivery(
            host=settings.EXAM_OTP_SMTP_HOST, port=settings.EXAM_OTP_SMTP_PORT,
            username=settings.EXAM_OTP_SMTP_USERNAME, password=settings.EXAM_OTP_SMTP_PASSWORD,
            from_address=settings.EXAM_OTP_FROM_ADDRESS, use_ssl=settings.EXAM_OTP_SMTP_USE_SSL,
        )
    if settings.EXAM_OTP_ALLOW_DEV_SINK:
        global _shared_dev_sink
        if _shared_dev_sink is None:
            _shared_dev_sink = DevSinkOtpDelivery()
        return _shared_dev_sink
    return UnconfiguredOtpDelivery()
