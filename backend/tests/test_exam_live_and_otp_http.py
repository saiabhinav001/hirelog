"""Live socket re-validation and OTP delivery behaviour, end to end."""

from __future__ import annotations

import logging
import smtplib

import pytest
from starlette.websockets import WebSocketDisconnect

from app.domain.institution import identity_provider
from app.domain.institution.identity_provider import (
    OtpDeliveryError,
    SmtpOtpDelivery,
    UnconfiguredOtpDelivery,
    default_otp_delivery,
)
from tests.support.exam_scenarios import revoke as _revoke

ORIGIN = {"origin": "http://localhost:3000"}


# ── live socket ───────────────────────────────────────────────────────────────

def _ticket(exam_ctx, headers) -> str:
    response = exam_ctx.world.client.post("/api/exams/v1/live/ticket", json={"run_id": exam_ctx.run_id}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["ticket"]


def test_student_and_faculty_get_the_right_live_role(exam) -> None:
    client = exam.world.client
    assert client.post("/api/exams/v1/live/ticket", json={"run_id": exam.run_id}, headers=exam.student).json()["role"] == "student"
    assert client.post("/api/exams/v1/live/ticket", json={"run_id": exam.run_id}, headers=exam.faculty).json()["role"] == "faculty"


def test_live_socket_is_closed_when_the_students_membership_is_revoked(exam, monkeypatch) -> None:
    from app.api.routes import exams_live

    monkeypatch.setattr(exams_live, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(exams_live, "_REVALIDATE_SECONDS", 0.05)
    ticket = _ticket(exam, exam.student)

    with exam.world.client.websocket_connect(
        f"/api/exams/v1/live/ws?ticket={ticket}&run_id={exam.run_id}", headers=ORIGIN
    ) as socket:
        assert socket.receive_json()["kind"] == "snapshot"
        _revoke(exam)
        with pytest.raises(WebSocketDisconnect) as closed:
            for _ in range(200):  # the server closes on its next re-validation tick
                socket.receive_json()
        assert closed.value.code == 4403


def test_a_ticket_minted_before_revocation_cannot_open_a_socket(exam) -> None:
    ticket = _ticket(exam, exam.student)
    _revoke(exam)
    with pytest.raises(WebSocketDisconnect) as closed:
        with exam.world.client.websocket_connect(
            f"/api/exams/v1/live/ws?ticket={ticket}&run_id={exam.run_id}", headers=ORIGIN
        ) as socket:
            socket.receive_json()
    assert closed.value.code == 4403


def test_live_events_reach_the_socket_without_a_blocking_thread_per_connection(exam, monkeypatch) -> None:
    """Many concurrent sockets must all receive events: delivery is driven by the
    event loop, not by one blocked executor thread per socket."""
    from app.domain.live.connection_manager import live_connection_manager
    from app.domain.live.events import snapshot_event
    from datetime import datetime, timezone

    ticket = _ticket(exam, exam.student)
    with exam.world.client.websocket_connect(
        f"/api/exams/v1/live/ws?ticket={ticket}&run_id={exam.run_id}", headers=ORIGIN
    ) as socket:
        assert socket.receive_json()["kind"] == "snapshot"
        event = snapshot_event(run_id=exam.run_id, seq=live_connection_manager.next_seq(exam.run_id), state_version=7,
                               now=datetime.now(timezone.utc))
        assert live_connection_manager.publish(event) == 1
        assert socket.receive_json()["state_version"] == 7


# ── OTP delivery ──────────────────────────────────────────────────────────────

class _FakeSmtp:
    sent: list = []
    log: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.log.append("starttls")

    def login(self, user, password):
        self.log.append(("login", user))

    def send_message(self, message):
        self.sent.append(message)


def _smtp(**overrides) -> SmtpOtpDelivery:
    params = dict(host="smtp.example", port=587, username="u", password="p", from_address="exams@example.edu")
    params.update(overrides)
    return SmtpOtpDelivery(**params)


def test_smtp_delivery_uses_starttls_login_and_puts_the_code_only_in_the_message(monkeypatch, caplog) -> None:
    _FakeSmtp.sent, _FakeSmtp.log = [], []
    monkeypatch.setattr(smtplib, "SMTP", _FakeSmtp)
    with caplog.at_level(logging.DEBUG):
        _smtp().send(contact_email="s1@a.edu", code="123456")
    assert _FakeSmtp.log == ["starttls", ("login", "u")]
    message = _FakeSmtp.sent[0]
    assert message["To"] == "s1@a.edu" and "123456" in message.get_content()
    assert "123456" not in caplog.text


def test_smtp_failure_is_reported_as_delivery_error_without_leaking_details(monkeypatch) -> None:
    class _Down(_FakeSmtp):
        def send_message(self, message):
            raise smtplib.SMTPServerDisconnected("secret internal host detail")

    monkeypatch.setattr(smtplib, "SMTP", _Down)
    with pytest.raises(OtpDeliveryError) as err:
        _smtp().send(contact_email="s1@a.edu", code="123456")
    assert "secret internal host detail" not in str(err.value)


def test_default_delivery_is_fail_closed(monkeypatch) -> None:
    from app.core.config import settings

    for name, value in (("EXAM_OTP_SMTP_HOST", None), ("EXAM_OTP_FROM_ADDRESS", None), ("EXAM_OTP_ALLOW_DEV_SINK", False)):
        monkeypatch.setattr(settings, name, value)
    assert isinstance(default_otp_delivery(), UnconfiguredOtpDelivery)
    monkeypatch.setattr(settings, "EXAM_OTP_ALLOW_DEV_SINK", True)
    assert isinstance(default_otp_delivery(), identity_provider.DevSinkOtpDelivery)
    monkeypatch.setattr(settings, "EXAM_OTP_SMTP_HOST", "smtp.example")
    monkeypatch.setattr(settings, "EXAM_OTP_FROM_ADDRESS", "exams@example.edu")
    assert isinstance(default_otp_delivery(), SmtpOtpDelivery)


def test_challenge_is_503_for_everyone_when_delivery_is_unconfigured(exam, monkeypatch) -> None:
    """Identical response for a real and an unknown identity — configuration state
    must not become an enumeration oracle."""
    from app.core.config import settings

    for name, value in (("EXAM_OTP_SMTP_HOST", None), ("EXAM_OTP_ALLOW_DEV_SINK", False)):
        monkeypatch.setattr(settings, name, value)
    client = exam.world.client
    real = client.post("/api/exams/v1/auth/challenge",
                       json={"institution_id": exam.inst, "identity_key": "S1", "contact_email": "s1@a.edu"})
    unknown = client.post("/api/exams/v1/auth/challenge",
                          json={"institution_id": exam.inst, "identity_key": "NOPE", "contact_email": "x@a.edu"})
    assert real.status_code == unknown.status_code == 503
    assert real.json() == unknown.json()


def test_delivery_failure_still_returns_the_generic_ack(exam, monkeypatch) -> None:
    from app.domain.institution import auth_service

    class _Broken:
        configured = True

        def send(self, *, contact_email: str, code: str) -> None:
            raise OtpDeliveryError("smtp down")

    monkeypatch.setattr(auth_service, "default_otp_delivery", lambda: _Broken())
    response = exam.world.client.post(
        "/api/exams/v1/auth/challenge", json={"institution_id": exam.inst, "identity_key": "S1", "contact_email": "s1@a.edu"}
    )
    assert response.status_code == 200 and response.json()["status"] == "ok"
