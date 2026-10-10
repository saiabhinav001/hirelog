"""Judge0-compatible provider adapter.

`JudgeProvider` mirrors Judge0's actual submit/poll shape (source_code,
language_id, stdin, expected_output, limits -> token; token -> status/stdout/
stderr/time/memory) so a real Judge0-compatible endpoint can be swapped in
via configuration alone. `DeterministicFakeJudgeProvider` NEVER executes or
interprets `source_code` — every outcome is explicitly scripted by the
caller (tests), keyed by stdin. This is deliberate: contract #8 says student
code executes only in a separate hardened judge, never the application
host — a "smart" fake that tries to actually run code would violate that
even in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Protocol
from uuid import uuid4

from app.domain.judge.errors import JudgeUnavailableError
from app.domain.judge.limits import JudgeLimits


@dataclass(frozen=True)
class JudgeSubmitRequest:
    source_code: str
    language_id: int
    stdin: str
    expected_output: str
    cpu_time_limit_seconds: float
    memory_limit_kb: int
    # Wall time, stack, process and file-size limits and the network switch; always server-owned (see limits.py).
    limits: JudgeLimits = field(default_factory=JudgeLimits)


@dataclass(frozen=True)
class JudgeRawResult:
    token: str
    status_id: int  # Judge0 status ids: 3=Accepted, 4=Wrong Answer, 5=TLE, 6=Compile Error, 7-12=Runtime Error variants, 13=Internal Error, 14=Exec Format Error
    status_description: str
    stdout: str = ""
    stderr: str = ""
    compile_output: str = ""
    time_ms: Optional[int] = None
    memory_kb: Optional[int] = None


# Judge0's own status taxonomy — used by grading.py to classify code vs infra failure.
STATUS_ACCEPTED = 3
STATUS_WRONG_ANSWER = 4
STATUS_TIME_LIMIT_EXCEEDED = 5
STATUS_COMPILE_ERROR = 6
STATUS_RUNTIME_ERROR_MIN = 7
STATUS_RUNTIME_ERROR_MAX = 12
STATUS_INTERNAL_ERROR = 13
STATUS_EXEC_FORMAT_ERROR = 14
STATUS_OUTPUT_LIMIT_EXCEEDED = 15


class JudgeProvider(Protocol):
    def submit(self, request: JudgeSubmitRequest) -> str:
        """Returns a provider token. Never raises for a queued submission —
        only for a genuine connectivity/auth failure."""
        ...

    def get_result(self, token: str) -> Optional[JudgeRawResult]:
        """None means still processing (poll again later)."""
        ...


class DeterministicFakeJudgeProvider:
    def __init__(
        self,
        *,
        outcomes_by_stdin: Optional[dict[str, JudgeRawResult]] = None,
        default_result: Optional[JudgeRawResult] = None,
        still_processing_tokens: Optional[set] = None,
    ) -> None:
        self._outcomes_by_stdin = outcomes_by_stdin or {}
        self._default_result = default_result or JudgeRawResult(
            token="", status_id=STATUS_ACCEPTED, status_description="Accepted"
        )
        self._still_processing = still_processing_tokens or set()
        self._pending: dict[str, JudgeSubmitRequest] = {}

    def submit(self, request: JudgeSubmitRequest) -> str:
        token = uuid4().hex
        self._pending[token] = request
        return token

    def get_result(self, token: str) -> Optional[JudgeRawResult]:
        if token in self._still_processing:
            return None
        request = self._pending.get(token)
        if request is None:
            return None
        scripted = self._outcomes_by_stdin.get(request.stdin)
        base = scripted if scripted is not None else self._default_result
        return replace(base, token=token)

    def mark_still_processing(self, token: str) -> None:
        self._still_processing.add(token)

    def release(self, token: str) -> None:
        self._still_processing.discard(token)


class UnconfiguredJudgeProvider:
    """Used where no real judge is configured and the fake must not stand in (production). Reports the judge as
    unavailable instead of inventing a verdict: the fake's default result is "Accepted", which in production would
    award full marks to unexecuted code."""

    def submit(self, request: JudgeSubmitRequest) -> str:
        raise JudgeUnavailableError("no judge is configured")

    def get_result(self, token: str) -> Optional[JudgeRawResult]:
        raise JudgeUnavailableError("no judge is configured")


class Judge0HttpAdapter:
    """Configurable real-provider adapter interface. NOT wired to any live
    endpoint — no Judge0-compatible instance was configured/available.
    Calling this raises until an institution supplies a base_url/api_key AND
    the deployment hardening in docs/adr/0011 has actually been provisioned
    and validated (this class cannot verify that for you)."""

    def __init__(self, *, base_url: Optional[str] = None, api_key: Optional[str] = None) -> None:
        self._base_url = base_url
        self._api_key = api_key

    def submit(self, request: JudgeSubmitRequest) -> str:
        if not self._base_url or not self._api_key:
            raise NotImplementedError(
                "Judge0HttpAdapter is not configured — set a real judge base_url/api_key "
                "(see docs/adr/0011 deployment requirements)."
            )
        raise NotImplementedError("Real Judge0 HTTP call not implemented — no provider configured/tested.")

    def get_result(self, token: str) -> Optional[JudgeRawResult]:
        raise NotImplementedError("Real Judge0 HTTP call not implemented — no provider configured/tested.")
