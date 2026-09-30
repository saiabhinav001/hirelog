"""Pure Safe Exam Browser (SEB) Config Key / Browser Exam Key verification.

Algorithm confirmed directly from SEB's own developer documentation
(https://www.safeexambrowser.org/developer/seb-config-key.html, fetched
during implementation of this module):

    "Create a SHA256 hash value from the absolute URL without Fragment
    part with appended Config Key hash string ... concatenate this URL
    string with the Config Key hash string and apply SHA256."
    Header: "X-SafeExamBrowser-ConfigKeyHash"

That is the ONE part of the protocol precisely and confidently implemented
here: `compute_seb_hash(url, key_hash)`. The Browser Exam Key (BEK) header
is commonly documented by SEB integrations (e.g. Moodle's SEB quiz access
rule) as `X-SafeExamBrowser-RequestHash`, using the identical url+key
SHA256 formula per SEB's own integration overview page — but that specific
header NAME was not independently re-confirmed against SEB's own BEK
specification PDF in this session (the document was unreachable when
fetched). Treated as the conventional, widely-used name, not re-verified
against SEB's primary source.

Deliberately NOT implemented: deriving a Config Key from a full .seb
settings file. SEB's own algorithm for that requires converting the
config's plist XML into a specific canonical "SEB-JSON" form (alphabetical
dict-key sorting at every nesting level, exact date/real/data formatting
rules, zero whitespace, `originatorVersion` removed) before hashing — a
substantial, easy-to-get-subtly-wrong canonicalization with no official
reference test vectors available to validate against in this environment.
Institutions instead copy the already-computed Config Key / Browser Exam
Key hash directly out of SEB's own Config Tool / Preferences "Exam" tab —
the same "safe default, real institutional input required" pattern as the
Judge0/generation-provider adapters.
"""

from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import urldefrag

from app.domain.lockdown.models import LockdownAdmissionOutcome, LockdownProfile

CONFIG_KEY_HASH_HEADER = "X-SafeExamBrowser-ConfigKeyHash"
REQUEST_HASH_HEADER = "X-SafeExamBrowser-RequestHash"


def compute_seb_hash(url: str, key_hash: str) -> str:
    """SHA256(absolute_url_without_fragment + key_hash), lowercase hex —
    the exact formula SEB documents for the Config Key header (and, per
    SEB's integration overview, the same formula for the Browser Exam
    Key)."""
    absolute_url_no_fragment, _fragment = urldefrag(url)
    return hashlib.sha256((absolute_url_no_fragment + key_hash).encode("utf-8")).hexdigest()


def verify_seb_admission(
    profile: LockdownProfile,
    *,
    url: str,
    config_key_hash_header: Optional[str],
    request_hash_header: Optional[str],
) -> LockdownAdmissionOutcome:
    """Never uses User-Agent as proof (contract) — only these two
    cryptographic headers are ever consulted. A run with `enabled=True`
    but no key configured fails closed (`DENIED_NOT_CONFIGURED`) rather
    than silently admitting everyone; a run with `enabled=False` admits
    unconditionally (it isn't a lockdown run)."""
    if not profile.enabled:
        return LockdownAdmissionOutcome.ADMITTED

    has_config_key = bool(profile.expected_config_key_hash)
    has_bek = bool(profile.expected_browser_exam_key_hashes)
    if not has_config_key and not has_bek:
        return LockdownAdmissionOutcome.DENIED_NOT_CONFIGURED

    if not config_key_hash_header and not request_hash_header:
        return LockdownAdmissionOutcome.DENIED_MISSING_HEADERS

    if has_config_key and config_key_hash_header:
        if compute_seb_hash(url, profile.expected_config_key_hash) == config_key_hash_header:
            return LockdownAdmissionOutcome.ADMITTED

    if has_bek and request_hash_header:
        for key_hash in profile.expected_browser_exam_key_hashes:
            if compute_seb_hash(url, key_hash) == request_hash_header:
                return LockdownAdmissionOutcome.ADMITTED

    return LockdownAdmissionOutcome.DENIED_HASH_MISMATCH
