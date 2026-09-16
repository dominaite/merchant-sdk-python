"""Known-answer tests for the request signature.

The vector is the one published on the Website-integration tab and pinned in
web-platform's `SIGNING_TEST_VECTOR`. If these fail, the SDK cannot authenticate
against the gateway - fix the signing, never the expected value.

The secret below is a dummy from the public docs. It authenticates nothing.
"""

import hashlib

from dominaite import PAYMENT_METHODS_PATH, SESSIONS_PATH, sign_request

SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
TIMESTAMP = "1755302400"
IDEMPOTENCY_KEY = "00000000-0000-4000-8000-000000000001"
BODY = '{"amount":2500,"currency":"EUR","orderReference":"order-1042"}'
EXPECTED_BODY_SHA256 = "aa3edd72cd1829f4e053abb048b08c1ae91c2d67b08955997c4b6c4dab4f98ff"
EXPECTED_SIGNATURE = "8f5fba0b29a8eea81b76a0e6d7119e79ec68f586910f77713b045652e5ce9b74"

# Stored-payment-method vectors, same secret and timestamp. The charge is the only POST
# besides sessions and the only one whose canonical path carries a resource id; the
# revoke pins that DELETE signs an empty key and an empty body exactly like GET. Shared
# byte-for-byte with the gateway's MerchantApiRequestAuthenticator tests.
PAYMENT_METHOD_ID = "pm_0123456789abcdef0123456789abcdef"
CHARGE_PATH = PAYMENT_METHODS_PATH + "/" + PAYMENT_METHOD_ID + "/charges"
CHARGE_IDEMPOTENCY_KEY = "00000000-0000-4000-8000-000000000003"
CHARGE_BODY = '{"amount":2500,"currency":"EUR","orderReference":"order-1043"}'
EXPECTED_CHARGE_BODY_SHA256 = "641a0d2b08f88ebc458dca49410dede0a166359a5030bff5c977e507f13ab828"
EXPECTED_CHARGE_SIGNATURE = "9ce9f54efa2533a46aa4493b97b56aeb657f41d6a18f1c008c7fd412029aebf9"
REVOKE_PATH = PAYMENT_METHODS_PATH + "/" + PAYMENT_METHOD_ID
EXPECTED_REVOKE_SIGNATURE = "9330100343c4b820504890a09829a193d5815ca39e92160fdfc13d320a802a02"


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


def test_charge_signature_matches_published_vector():
    """POST with a body and an Idempotency-Key on a path carrying a resource id."""
    assert hashlib.sha256(CHARGE_BODY.encode("utf-8")).hexdigest() == EXPECTED_CHARGE_BODY_SHA256

    signature = sign_request(
        SECRET, TIMESTAMP, "POST", CHARGE_PATH, CHARGE_IDEMPOTENCY_KEY, CHARGE_BODY
    )

    assert signature == EXPECTED_CHARGE_SIGNATURE


def test_revoke_signature_matches_published_vector():
    """DELETE signs an empty idempotency key and an empty body, like GET."""
    signature = sign_request(SECRET, TIMESTAMP, "DELETE", REVOKE_PATH, "", "")

    assert signature == EXPECTED_REVOKE_SIGNATURE
    # Only the method differs from a GET on the same path, and it is signed.
    assert sign_request(SECRET, TIMESTAMP, "GET", REVOKE_PATH, "", "") != signature
