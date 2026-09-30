"""Pure SEB Config Key / Browser Exam Key verification tests.

`compute_seb_hash` is tested against a manually-computed SHA256 vector (not
just round-tripped against itself) so a transcription error in the
hex-encoding/concatenation order would be caught, not just an internal
inconsistency.
"""

from __future__ import annotations

import hashlib

from app.domain.lockdown.decisions import compute_seb_hash, verify_seb_admission
from app.domain.lockdown.models import LockdownAdmissionOutcome, LockdownProfile

URL = "https://exams.example.edu/api/exams/v1/student/runs/run-1/admit"
KEY = "test-config-key-abc123"


def _reference_hash(url: str, key: str) -> str:
    # Independently re-derives the same SHA256(url + key) SEB documents,
    # using stdlib hashlib directly rather than calling the function under test.
    return hashlib.sha256((url + key).encode("utf-8")).hexdigest()


def _profile(**overrides) -> LockdownProfile:
    defaults = dict(run_id="run-1")
    defaults.update(overrides)
    return LockdownProfile(**defaults)


# ── compute_seb_hash ──────────────────────────────────────────────────────

def test_compute_seb_hash_matches_independently_computed_sha256() -> None:
    assert compute_seb_hash(URL, KEY) == _reference_hash(URL, KEY)


def test_compute_seb_hash_strips_url_fragment() -> None:
    # SEB's documented algorithm uses the absolute URL WITHOUT its fragment.
    with_fragment = compute_seb_hash(URL + "#section", KEY)
    without_fragment = compute_seb_hash(URL, KEY)
    assert with_fragment == without_fragment


def test_compute_seb_hash_is_deterministic_and_key_sensitive() -> None:
    assert compute_seb_hash(URL, KEY) == compute_seb_hash(URL, KEY)
    assert compute_seb_hash(URL, KEY) != compute_seb_hash(URL, "different-key")
    assert compute_seb_hash(URL, KEY) != compute_seb_hash("https://other.example/", KEY)


# ── verify_seb_admission ─────────────────────────────────────────────────

def test_disabled_profile_always_admits_regardless_of_headers() -> None:
    profile = _profile(enabled=False)
    outcome = verify_seb_admission(profile, url=URL, config_key_hash_header=None, request_hash_header=None)
    assert outcome == LockdownAdmissionOutcome.ADMITTED


def test_enabled_profile_with_no_keys_configured_fails_closed() -> None:
    # "PWA installation alone does not pass" — an enabled-but-unconfigured
    # profile must never silently admit anyone, even with correct-looking headers.
    profile = _profile(enabled=True)
    outcome = verify_seb_admission(
        profile, url=URL, config_key_hash_header=_reference_hash(URL, "anything"), request_hash_header=None
    )
    assert outcome == LockdownAdmissionOutcome.DENIED_NOT_CONFIGURED


def test_enabled_profile_denies_missing_headers() -> None:
    profile = _profile(enabled=True, expected_config_key_hash=KEY)
    outcome = verify_seb_admission(profile, url=URL, config_key_hash_header=None, request_hash_header=None)
    assert outcome == LockdownAdmissionOutcome.DENIED_MISSING_HEADERS


def test_enabled_profile_admits_on_correct_config_key_header() -> None:
    profile = _profile(enabled=True, expected_config_key_hash=KEY)
    correct_header = compute_seb_hash(URL, KEY)
    outcome = verify_seb_admission(profile, url=URL, config_key_hash_header=correct_header, request_hash_header=None)
    assert outcome == LockdownAdmissionOutcome.ADMITTED


def test_enabled_profile_denies_wrong_config_key_header() -> None:
    profile = _profile(enabled=True, expected_config_key_hash=KEY)
    wrong_header = compute_seb_hash(URL, "not-the-real-key")
    outcome = verify_seb_admission(profile, url=URL, config_key_hash_header=wrong_header, request_hash_header=None)
    assert outcome == LockdownAdmissionOutcome.DENIED_HASH_MISMATCH


def test_enabled_profile_admits_on_any_matching_browser_exam_key() -> None:
    profile = _profile(enabled=True, expected_browser_exam_key_hashes=["bek-old", "bek-new"])
    correct_header = compute_seb_hash(URL, "bek-new")
    outcome = verify_seb_admission(profile, url=URL, config_key_hash_header=None, request_hash_header=correct_header)
    assert outcome == LockdownAdmissionOutcome.ADMITTED


def test_user_agent_is_never_a_parameter_of_verification() -> None:
    # Structural guarantee, not just a docstring claim: the function
    # signature has no user-agent-shaped parameter at all.
    import inspect

    params = inspect.signature(verify_seb_admission).parameters
    assert "user_agent" not in params
    assert not any("agent" in name.lower() for name in params)


def test_denies_when_only_wrong_header_type_is_sent() -> None:
    # A config-key-only profile must not be satisfied by a request-hash
    # header alone, even if it happens to look well-formed.
    profile = _profile(enabled=True, expected_config_key_hash=KEY)
    outcome = verify_seb_admission(
        profile, url=URL, config_key_hash_header=None, request_hash_header=compute_seb_hash(URL, KEY)
    )
    assert outcome == LockdownAdmissionOutcome.DENIED_HASH_MISMATCH
