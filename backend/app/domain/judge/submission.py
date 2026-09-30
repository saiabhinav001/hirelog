"""Pure submission-identity helpers: hashing and result-ownership
verification. "Do not assume signed provider callbacks" — this is the
function that stands in for that trust: a result is only ever accepted if
its token matches exactly what THIS server recorded when it submitted that
specific test to the provider.
"""

from __future__ import annotations

import hashlib


def compute_submission_hash(source_code: str) -> str:
    return hashlib.sha256(source_code.encode("utf-8")).hexdigest()


def verify_result_ownership(provider_tokens: dict[str, str], test_id: str, provider_token: str) -> bool:
    """`provider_tokens` is the submission's own recorded {test_id: token}
    map — a result claiming a token this server never issued for that exact
    test_id is rejected outright, whether it arrives via polling or (if ever
    added) a callback."""
    return provider_tokens.get(test_id) == provider_token
