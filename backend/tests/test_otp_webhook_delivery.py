"""The webhook OTP delivery adapter: what it sends, what it refuses, and that the code never reaches a log."""

from __future__ import annotations

import logging

import pytest

from app.core.config import settings
from app.domain.institution import identity_provider as idp
from app.domain.institution.identity_provider import (
    OtpDeliveryError,
    UnconfiguredOtpDelivery,
    WebhookOtpDelivery,
    build_otp_delivery,
)

URL = "https://script.google.com/macros/s/ABC123/exec"


def _delivery(post, **overrides) -> WebhookOtpDelivery:
    kwargs = dict(url=URL, secret="shh", ttl_minutes=5, timeout=3.0, post=post)
    kwargs.update(overrides)
    return WebhookOtpDelivery(**kwargs)


def test_it_posts_the_code_and_the_secret_to_the_relay() -> None:
    seen: dict = {}

    def post(url, body, timeout):
        seen.update(url=url, body=body, timeout=timeout)
        return 200, '{"ok": true}'

    _delivery(post).send(contact_email="a@college.edu", code="123456")

    assert seen["url"] == URL and seen["timeout"] == 3.0
    assert seen["body"] == {"secret": "shh", "to": "a@college.edu", "code": "123456", "ttl_minutes": 5}


@pytest.mark.parametrize("answer", [(500, ""), (403, ""), (200, "not json"), (200, '{"ok": false}'), (200, "[]"), (200, "")])
def test_anything_but_an_accepted_message_is_a_delivery_error(answer) -> None:
    with pytest.raises(OtpDeliveryError):
        _delivery(lambda *_: answer).send(contact_email="a@college.edu", code="123456")


def test_a_network_failure_is_a_delivery_error() -> None:
    def post(*_):
        raise TimeoutError("timed out")

    with pytest.raises(OtpDeliveryError, match="unreachable"):
        _delivery(post).send(contact_email="a@college.edu", code="123456")


def test_the_relay_must_be_https() -> None:
    with pytest.raises(ValueError):
        WebhookOtpDelivery(url="http://example.com/relay", secret="s", ttl_minutes=5)


def test_neither_the_code_nor_the_secret_appears_in_an_error(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(OtpDeliveryError) as raised:
        _delivery(lambda *_: (500, "boom 987654 shh")).send(contact_email="a@college.edu", code="987654")
    assert "987654" not in str(raised.value) and "shh" not in str(raised.value)
    assert "987654" not in caplog.text and "shh" not in caplog.text


def test_the_provider_name_selects_the_webhook_adapter(monkeypatch) -> None:
    monkeypatch.setattr(settings, "EXAM_OTP_WEBHOOK_URL", URL)
    monkeypatch.setattr(settings, "EXAM_OTP_WEBHOOK_SECRET", "shh")
    monkeypatch.setattr(settings, "EXAM_OTP_TTL_SECONDS", 300)
    built = build_otp_delivery(provider=" Webhook ", production=True)
    assert isinstance(built, WebhookOtpDelivery)
    assert built._ttl == 5


@pytest.mark.parametrize("url, secret", [(None, "shh"), (URL, None), ("", ""), ("http://insecure.example/x", "shh")])
def test_a_half_configured_or_insecure_webhook_leaves_sign_in_unavailable(monkeypatch, url, secret) -> None:
    monkeypatch.setattr(settings, "EXAM_OTP_WEBHOOK_URL", url)
    monkeypatch.setattr(settings, "EXAM_OTP_WEBHOOK_SECRET", secret)
    monkeypatch.setattr(idp, "_logged", set())
    assert isinstance(build_otp_delivery(provider="webhook", production=True), UnconfiguredOtpDelivery)


def test_the_dev_sink_is_still_ignored_in_production() -> None:
    assert isinstance(build_otp_delivery(provider="dev_sink", production=True), UnconfiguredOtpDelivery)
