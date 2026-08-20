"""Known-answer tests for the request signature.

The vector is the one published on the Website-integration tab and pinned in
web-platform's `SIGNING_TEST_VECTOR`. If these fail, the SDK cannot authenticate
against the gateway - fix the signing, never the expected value.

The secret below is a dummy from the public docs. It authenticates nothing.
"""

import hashlib

from dominaite import SESSIONS_PATH, sign_request

SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TIMESTAMP = "1755302400"
IDEMPOTENCY_KEY = "00000000-0000-4000-8000-000000000001"
BODY = '{"amount":2500,"currency":"EUR","orderReference":"order-1042"}'
EXPECTED_BODY_SHA256 = "aa3edd72cd1829f4e053abb048b08c1ae91c2d67b08955997c4b6c4dab4f98ff"
EXPECTED_SIGNATURE = "95759958a0a0a9bd3e6e37101c01e8e7fee1166406e4ac2ff488764f5f742cbf"


def test_body_hash_matches_published_vector():
    assert hashlib.sha256(BODY.encode("utf-8")).hexdigest() == EXPECTED_BODY_SHA256


def test_post_signature_matches_published_vector():
    signature = sign_request(
        secret=SECRET,
        timestamp=TIMESTAMP,
        method="POST",
        path=SESSIONS_PATH,
        idempotency_key=IDEMPOTENCY_KEY,
        body=BODY,
    )

    assert signature == EXPECTED_SIGNATURE


def test_signature_is_lowercase_hex():
    signature = sign_request(SECRET, TIMESTAMP, "POST", SESSIONS_PATH, IDEMPOTENCY_KEY, BODY)

    assert signature == signature.lower()
    assert len(signature) == 64


def test_method_is_uppercased_before_signing():
    lower = sign_request(SECRET, TIMESTAMP, "post", SESSIONS_PATH, IDEMPOTENCY_KEY, BODY)

    assert lower == EXPECTED_SIGNATURE


def test_changing_the_idempotency_key_changes_the_signature():
    """The key is inside the signature, which is what makes replay harmless."""
    other = sign_request(
        SECRET,
        TIMESTAMP,
        "POST",
        SESSIONS_PATH,
        "00000000-0000-4000-8000-000000000002",
        BODY,
    )

    assert other != EXPECTED_SIGNATURE
