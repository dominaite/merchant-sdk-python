"""Known-answer tests for webhook verification.

The vector is the cross-SDK one from the webhooks wire contract: every Dominaite SDK
pins these same bytes, so a Python-only drift shows up here first. If these fail, the
SDK is rejecting genuine deliveries (or accepting forged ones) - fix the verification,
never the expected value.

BODY is byte-exact and must never be reformatted or re-serialized: the signature is over
the raw bytes, so a single added space is a different (failing) MAC. That is also the
merchant-facing lesson these tests encode.

The secret below is the dummy from the public contract. It authenticates nothing.
"""

import hmac

import pytest

from dominaite import WebhookVerificationError, sign_webhook, verify_webhook
from dominaite import webhooks

SECRET = "whsec_abababababababababababababababababababababababababababababababab"
TIMESTAMP = "1755700000"
BODY = '{"id":"7f9c24e5-1d1f-4c0a-9b6c-2f3a4d5e6f70","type":"payment.succeeded","createdAt":"2026-08-20T14:00:00Z","data":{"transactionId":"0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0","status":"succeeded","previousStatus":"pending","kind":"sale","amount":8440,"grossAmount":8701,"surchargeAmount":261,"currency":"EUR","originalTransactionId":null,"idempotencyKey":"order-123"}}'
EXPECTED_V1 = "5305bcf1302fdaba8f8c19a20c899e916fb4d2a7d8d547c62529ff87c4697b72"
EXPECTED_HEADER = "t=" + TIMESTAMP + ",v1=" + EXPECTED_V1

NOW = int(TIMESTAMP)


def test_signature_matches_published_vector():
    assert sign_webhook(SECRET, TIMESTAMP, BODY) == EXPECTED_V1


# (1) The canonical vector verifies.
def test_canonical_vector_verifies_and_returns_the_event():
    event = verify_webhook(BODY, EXPECTED_HEADER, SECRET, now=NOW)

    assert event["id"] == "7f9c24e5-1d1f-4c0a-9b6c-2f3a4d5e6f70"
    assert event["type"] == "payment.succeeded"
    assert event["data"]["amount"] == 8440
    assert event["data"]["originalTransactionId"] is None


def test_raw_bytes_verify_the_same_as_a_string():
    """Most frameworks hand you the raw body as bytes - both must work."""
    event = verify_webhook(BODY.encode("utf-8"), EXPECTED_HEADER, SECRET, now=NOW)

    assert event["id"] == "7f9c24e5-1d1f-4c0a-9b6c-2f3a4d5e6f70"


# (2) A single-byte body tamper fails.
@pytest.mark.parametrize(
    "tampered",
    [
        BODY.replace('"amount":8440', '"amount":8441'),
        BODY.replace('"currency":"EUR"', '"currency":"USD"'),
        BODY + " ",
        " " + BODY,
        # What you get by re-serializing the parsed dict instead of keeping the raw body.
        BODY.replace('{"id":', '{ "id": '),
    ],
    ids=["amount", "currency", "trailing-space", "leading-space", "reserialized"],
)
def test_tampered_body_fails(tampered):
    assert tampered != BODY

    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(tampered, EXPECTED_HEADER, SECRET, now=NOW)

    assert raised.value.error_code == "INVALID_SIGNATURE"


# (3) A wrong secret fails.
def test_wrong_secret_fails():
    other = "whsec_cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    header = "t=" + TIMESTAMP + ",v1=" + sign_webhook(other, TIMESTAMP, BODY)

    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(BODY, header, SECRET, now=NOW)

    assert raised.value.error_code == "INVALID_SIGNATURE"


# (4) A timestamp outside tolerance fails even with a valid MAC.
@pytest.mark.parametrize("drift", [301, -301, 86400, -86400], ids=str)
def test_timestamp_outside_tolerance_fails_despite_a_valid_mac(drift):
    assert sign_webhook(SECRET, TIMESTAMP, BODY) == EXPECTED_V1  # the MAC is genuine

    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(BODY, EXPECTED_HEADER, SECRET, now=NOW + drift)

    assert raised.value.error_code == "TIMESTAMP_OUT_OF_RANGE"


@pytest.mark.parametrize("drift", [0, 300, -300], ids=str)
def test_timestamp_inside_tolerance_is_accepted(drift):
    """300s is the documented default and is inclusive at the boundary."""
    assert verify_webhook(BODY, EXPECTED_HEADER, SECRET, now=NOW + drift)["type"]


def test_tolerance_is_configurable():
    with pytest.raises(WebhookVerificationError):
        verify_webhook(BODY, EXPECTED_HEADER, SECRET, tolerance_seconds=10, now=NOW + 11)

    assert verify_webhook(
        BODY, EXPECTED_HEADER, SECRET, tolerance_seconds=3600, now=NOW + 3599
    )["id"]


# (5) Malformed headers fail with the SDK's own error type, not an incidental one.
@pytest.mark.parametrize(
    "header",
    [
        "",
        "v1=" + EXPECTED_V1,
        "t=" + TIMESTAMP,
        EXPECTED_V1,
        "t=,v1=" + EXPECTED_V1,
        "t=" + TIMESTAMP + ",v1=",
        "t=not-a-number,v1=" + EXPECTED_V1,
        "t=" + TIMESTAMP + ",v1=nothex" + EXPECTED_V1[6:],
        "t=" + TIMESTAMP + ",v1=" + EXPECTED_V1[:63],
        "t=" + TIMESTAMP + ",v1=" + EXPECTED_V1.upper(),
        "t=" + TIMESTAMP + ",t=" + TIMESTAMP + ",v1=" + EXPECTED_V1,
        "t=" + TIMESTAMP + ",v1=" + EXPECTED_V1 + ",v1=" + EXPECTED_V1,
        "garbage",
        "{}",
        "t 1755700000 v1 " + EXPECTED_V1,
    ],
    ids=[
        "empty",
        "missing-t",
        "missing-v1",
        "bare-signature",
        "blank-t",
        "blank-v1",
        "non-numeric-t",
        "non-hex-v1",
        "short-v1",
        "uppercase-v1",
        "duplicate-t",
        "duplicate-v1",
        "garbage",
        "json",
        "spaces-not-commas",
    ],
)
def test_malformed_header_fails(header):
    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(BODY, header, SECRET, now=NOW)

    assert raised.value.error_code == "MALFORMED_SIGNATURE"


# The ten shared header-grammar vectors from the wire contract. Every SDK pins these
# same strings, so a Python-only reading of the grammar shows up here.
@pytest.mark.parametrize(
    "header",
    [
        "t=1755700000",
        "v1=" + EXPECTED_V1,
        "t=1755700000,v1=" + EXPECTED_V1.upper(),
        "t=1755700000,v1=" + EXPECTED_V1 + ",v1=" + EXPECTED_V1,
        "t=1755700000,t=1755700000,v1=" + EXPECTED_V1,
        "t=,v1=garbage,v1=" + EXPECTED_V1,
        "t=1755700000, v1=" + EXPECTED_V1,
        "t=+1755700000,v1=" + EXPECTED_V1,
        "garbage",
    ],
    ids=[
        "1-missing-v1",
        "2-missing-t",
        "3-uppercase-hex",
        "4-repeated-v1",
        "5-repeated-t",
        "6-empty-t-plus-repeat",
        "7-whitespace-after-comma",
        "8-non-digit-in-t",
        "9-element-without-equals",
    ],
)
def test_contract_grammar_vector_rejects(header):
    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(BODY, header, SECRET, now=NOW)

    assert raised.value.error_code == "MALFORMED_SIGNATURE"


def test_contract_grammar_vector_10_ignores_an_unknown_key():
    """A future scheme version (v2 rollover) must not break v1 consumers."""
    header = "t=1755700000,v1=" + EXPECTED_V1 + ",v9=deadbeef"

    assert verify_webhook(BODY, header, SECRET, now=NOW)["id"]


def test_the_raw_t_substring_is_what_gets_signed():
    """Leading zeros must survive into the MAC input, not be reformatted away."""
    padded = "0001755700000"
    header = "t=" + padded + ",v1=" + sign_webhook(SECRET, padded, BODY)

    assert verify_webhook(BODY, header, SECRET, now=NOW)["id"]


def test_an_absurdly_long_timestamp_is_out_of_range_not_a_crash():
    """The grammar caps no digits, and CPython refuses huge int literals."""
    huge = "9" * 5000
    header = "t=" + huge + ",v1=" + sign_webhook(SECRET, huge, BODY)

    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(BODY, header, SECRET, now=NOW)

    assert raised.value.error_code == "TIMESTAMP_OUT_OF_RANGE"


def test_non_utf8_body_fails_verification_instead_of_crashing():
    """A public webhook URL must not 500 on bytes an attacker chose."""
    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(b"\xff\xfe\x00not utf-8", EXPECTED_HEADER, SECRET, now=NOW)

    assert raised.value.error_code == "INVALID_SIGNATURE"


def test_signed_body_that_is_not_a_json_object_fails():
    body = "not json at all"
    header = "t=" + TIMESTAMP + ",v1=" + sign_webhook(SECRET, TIMESTAMP, body)

    with pytest.raises(WebhookVerificationError) as raised:
        verify_webhook(body, header, SECRET, now=NOW)

    assert raised.value.error_code == "INVALID_PAYLOAD"


@pytest.mark.parametrize(
    "kwargs",
    [{"secret": ""}, {"tolerance_seconds": -1}, {"tolerance_seconds": "300"}],
    ids=["empty-secret", "negative-tolerance", "tolerance-not-a-number"],
)
def test_calling_it_wrong_raises_value_error(kwargs):
    call = {"secret": SECRET, "now": NOW}
    call.update(kwargs)

    with pytest.raises(ValueError):
        verify_webhook(BODY, EXPECTED_HEADER, **call)


def test_a_custom_secret_without_the_whsec_prefix_still_verifies():
    """Endpoints can be created with a merchant-supplied secret."""
    secret = "my-own-endpoint-secret"
    header = "t=" + TIMESTAMP + ",v1=" + sign_webhook(secret, TIMESTAMP, BODY)

    assert verify_webhook(BODY, header, secret, now=NOW)["id"]


def test_verification_errors_are_catchable_as_dominaite_errors():
    from dominaite import DominaiteError

    with pytest.raises(DominaiteError):
        verify_webhook(BODY, "garbage", SECRET, now=NOW)


# --- the comparison itself stays constant-time -------------------------------


def test_verification_compares_the_mac_with_hmac_compare_digest(monkeypatch):
    """Pins the comparison primitive, not just the yes/no answer.

    `expected == provided` passes every other test in this file: it accepts the genuine
    signature and rejects forged ones. What it also does is return as soon as it hits
    the first differing byte, and the time that takes leaks how much of the MAC the
    caller guessed right - enough, over many deliveries, to walk a forgery byte by byte.

    So this asserts on the call, not the result. Swap compare_digest for == and this is
    the test that goes red.
    """
    calls = []
    real = hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(webhooks.hmac, "compare_digest", spy)

    event = verify_webhook(BODY, EXPECTED_HEADER, SECRET, now=NOW)

    assert event["type"] == "payment.succeeded"
    assert calls, "the MAC comparison must go through hmac.compare_digest"
    assert (EXPECTED_V1, EXPECTED_V1) in calls


def test_a_forged_signature_is_also_rejected_through_compare_digest(monkeypatch):
    """The rejecting path is the one an attacker times, so pin it too."""
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(
        webhooks.hmac,
        "compare_digest",
        lambda left, right: (calls.append((left, right)), real(left, right))[1],
    )
    forged = "t=" + TIMESTAMP + ",v1=" + ("0" * 64)

    with pytest.raises(WebhookVerificationError) as caught:
        verify_webhook(BODY, forged, SECRET, now=NOW)

    assert caught.value.error_code == "INVALID_SIGNATURE"
    assert calls == [(EXPECTED_V1, "0" * 64)]
