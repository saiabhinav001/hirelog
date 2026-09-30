"""`DeterministicFakeJudgeProvider` and `Judge0HttpAdapter` tests. The fake
must never execute or interpret `source_code` — every outcome is scripted by
stdin alone — and the real-provider adapter must stay a genuine stub until
an institution configures it.
"""

from __future__ import annotations

import pytest

from app.domain.judge.provider import (
    DeterministicFakeJudgeProvider,
    Judge0HttpAdapter,
    JudgeSubmitRequest,
    STATUS_ACCEPTED,
    STATUS_WRONG_ANSWER,
)


def _request(**overrides) -> JudgeSubmitRequest:
    defaults = dict(
        source_code="print(1)", language_id=92, stdin="", expected_output="",
        cpu_time_limit_seconds=5.0, memory_limit_kb=262_144,
    )
    defaults.update(overrides)
    return JudgeSubmitRequest(**defaults)


def test_submit_returns_a_unique_token_per_call() -> None:
    provider = DeterministicFakeJudgeProvider()
    t1 = provider.submit(_request())
    t2 = provider.submit(_request())
    assert t1 != t2


def test_get_result_falls_back_to_default_accepted_when_unscripted() -> None:
    provider = DeterministicFakeJudgeProvider()
    token = provider.submit(_request(stdin="2 2"))
    result = provider.get_result(token)
    assert result is not None
    assert result.status_id == STATUS_ACCEPTED
    assert result.token == token


def test_get_result_uses_scripted_outcome_keyed_by_stdin() -> None:
    from app.domain.judge.provider import JudgeRawResult

    scripted = JudgeRawResult(token="", status_id=STATUS_WRONG_ANSWER, status_description="Wrong Answer")
    provider = DeterministicFakeJudgeProvider(outcomes_by_stdin={"3 3": scripted})
    token = provider.submit(_request(stdin="3 3"))
    result = provider.get_result(token)
    assert result.status_id == STATUS_WRONG_ANSWER


def test_get_result_returns_none_while_marked_still_processing() -> None:
    """Queue-saturation / slow-provider scenario: a submission the provider
    hasn't finished must poll as None, not as a false result."""
    provider = DeterministicFakeJudgeProvider()
    token = provider.submit(_request())
    provider.mark_still_processing(token)
    assert provider.get_result(token) is None
    provider.release(token)
    assert provider.get_result(token) is not None


def test_get_result_returns_none_for_unknown_token() -> None:
    provider = DeterministicFakeJudgeProvider()
    assert provider.get_result("never-submitted") is None


def test_fake_provider_never_executes_or_interprets_source_code() -> None:
    """Two submissions with identical stdin but wildly different source code
    (one deliberately malicious-looking) must produce IDENTICAL scripted
    results — proving the result depends only on the scripted stdin lookup,
    never on interpreting `source_code`."""
    provider = DeterministicFakeJudgeProvider()
    benign_token = provider.submit(_request(source_code="print('hello')", stdin="x"))
    malicious_token = provider.submit(
        _request(source_code="import os; os.system('rm -rf / --no-preserve-root')", stdin="x")
    )
    benign_result = provider.get_result(benign_token)
    malicious_result = provider.get_result(malicious_token)
    assert benign_result.status_id == malicious_result.status_id
    assert benign_result.status_description == malicious_result.status_description
    assert benign_result.stdout == malicious_result.stdout


def test_judge0_http_adapter_is_a_genuine_unconfigured_stub() -> None:
    adapter = Judge0HttpAdapter()
    with pytest.raises(NotImplementedError):
        adapter.submit(_request())
    with pytest.raises(NotImplementedError):
        adapter.get_result("any-token")


def test_judge0_http_adapter_still_raises_even_when_configured() -> None:
    """Configuring base_url/api_key alone must never silently make this
    'work' — no real HTTP call has actually been implemented/tested."""
    adapter = Judge0HttpAdapter(base_url="https://judge.example.edu", api_key="secret")
    with pytest.raises(NotImplementedError):
        adapter.submit(_request())
    with pytest.raises(NotImplementedError):
        adapter.get_result("any-token")
