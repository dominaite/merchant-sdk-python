"""Pins the merchant-API response contract against the canonical fixture.

``merchant-api-contract.json`` in this directory is a byte-identical vendored copy of
the cross-SDK fixture. Every Dominaite SDK carries the same file and asserts the same
three things against it, so a field or status value cannot ship in one SDK and be
mirrored wrong into the siblings.

If one of these fails, the fixture is right and this SDK is wrong - change the SDK.
The only way the fixture moves is a gateway DTO change landing first, and then the
same edit lands in every SDK's copy at once.
"""

import io
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dominaite import (
    PAYMENT_STATUSES,
    SESSION_REFUSAL_ERROR_CODES,
    CheckoutRefusedError,
    DominaiteClient,
    PaymentStatus,
)

CONTRACT = json.loads(
    (Path(__file__).resolve().parent / "merchant-api-contract.json").read_text("utf-8")
)
ENDPOINTS = CONTRACT["endpoints"]

KEY_ID = "dmk_0123456789abcdef0123456789abcdef"
SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.test/payments"


class _Response:
    def __init__(self, payload):
        self.status = 200
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client():
    return DominaiteClient(KEY_ID, SECRET, base_url=BASE_URL)


@pytest.fixture
def answers_with(monkeypatch):
    """Make the next call return one fixture example verbatim."""

    def install(payload):
        monkeypatch.setattr(
            urllib.request, "urlopen", lambda request, timeout=None: _Response(payload)
        )

    return install


# --- (1) the status vocabulary -----------------------------------------------


def test_status_enum_equals_the_contract_vocabulary():
    """Exact and ordered: a value added, dropped or renamed on either side fails here."""
    assert list(PAYMENT_STATUSES) == CONTRACT["statusVocabulary"]
    assert [member.value for member in PaymentStatus] == CONTRACT["statusVocabulary"]


@pytest.mark.parametrize("status", CONTRACT["statusVocabulary"])
def test_every_contract_status_is_a_named_enum_member(status):
    """Named, so callers can match on the member instead of a bare string literal."""
    assert PaymentStatus(status).value == status
    assert PaymentStatus[status.upper()] is PaymentStatus(status)


def test_status_members_compare_equal_to_the_raw_string_get_status_returns():
    assert PaymentStatus.REQUIRES_CAPTURE == "requires_capture"
    assert ENDPOINTS["getStatus"]["example"]["status"] == PaymentStatus.SUCCEEDED


# --- (2) response shapes ------------------------------------------------------


def test_ping_returns_exactly_the_contract_fields(client, answers_with):
    endpoint = ENDPOINTS["ping"]
    answers_with(endpoint["example"])

    result = client.ping()

    assert sorted(result) == sorted(endpoint["fields"])
    assert result == endpoint["example"]


def test_create_checkout_session_returns_exactly_the_checkout_fields(
    client, answers_with
):
    endpoint = ENDPOINTS["createCheckoutSession"]
    answers_with(endpoint["successExample"])

    checkout = client.create_checkout_session(
        amount=8440, currency="EUR", order_reference="order-1042"
    )

    # create_checkout_session() hands back the `checkout` object, not the envelope.
    assert sorted(checkout) == sorted(endpoint["checkoutFields"])
    assert checkout == endpoint["successExample"]["checkout"]


def test_a_refusal_is_raised_with_the_whole_envelope_intact(client, answers_with):
    endpoint = ENDPOINTS["createCheckoutSession"]
    refusal = endpoint["refusalExample"]
    answers_with(refusal)

    with pytest.raises(CheckoutRefusedError) as raised:
        client.create_checkout_session(
            amount=8440, currency="EUR", order_reference="order-1042"
        )

    # HTTP 200 with success=false is a refusal, not a success - branching on the
    # status code instead of on `success` is the bug this asserts against.
    assert sorted(raised.value.result) == sorted(endpoint["fields"])
    assert raised.value.error_code == refusal["errorCode"]
    assert raised.value.transaction_id == refusal["transactionId"]
    assert str(raised.value) == refusal["errorMessage"]


def test_get_status_returns_exactly_the_contract_fields(client, answers_with):
    endpoint = ENDPOINTS["getStatus"]
    answers_with(endpoint["example"])

    result = client.get_status(endpoint["example"]["transactionId"])

    assert sorted(result) == sorted(endpoint["fields"])
    assert result == endpoint["example"]
    assert result["status"] in PAYMENT_STATUSES


def test_nullable_contract_fields_survive_as_none(client, answers_with):
    """`refundedAmount: null` and `expiresAt: null` must arrive as keys, not vanish."""
    endpoint = ENDPOINTS["getStatus"]
    answers_with(endpoint["example"])

    result = client.get_status(endpoint["example"]["transactionId"])

    assert result["refundedAmount"] is None
    # expiresAt is null once the payer's window is over, which is NOT the same as
    # the payment being finished - liveness is read off `status`.
    assert result["expiresAt"] is None


def test_the_envelope_form_of_each_example_unwraps_the_same(client, answers_with):
    """The gateway may wrap a response as {success, data} - the fields must not shift."""
    endpoint = ENDPOINTS["getStatus"]
    answers_with({"success": True, "data": endpoint["example"]})

    result = client.get_status(endpoint["example"]["transactionId"])

    assert result == endpoint["example"]


# --- (3) refusal error codes --------------------------------------------------


@pytest.mark.parametrize("code", CONTRACT["sessionRefusalErrorCodes"])
def test_every_contract_refusal_code_is_recognized(code):
    assert code in SESSION_REFUSAL_ERROR_CODES


@pytest.mark.parametrize("code", CONTRACT["sessionRefusalErrorCodes"])
def test_every_contract_refusal_code_surfaces_on_the_exception(
    code, client, answers_with
):
    answers_with(
        {
            "success": False,
            "checkout": None,
            "transactionId": None,
            "errorCode": code,
            "errorMessage": "refused",
        }
    )

    with pytest.raises(CheckoutRefusedError) as raised:
        client.create_checkout_session(
            amount=8440, currency="EUR", order_reference="order-1042"
        )

    assert raised.value.error_code == code
    assert raised.value.transaction_id is None


def test_the_sdk_also_recognizes_prior_attempt_failed():
    """The gateway emits it (MerchantCheckoutSessionService.cs) but the fixture omits it.

    Deliberately asserted OUTSIDE the fixture loop: the fixture is the source of truth
    for what it lists, not proof that its list is complete. Drop this test only when
    PRIOR_ATTEMPT_FAILED is added to sessionRefusalErrorCodes in every SDK's copy.
    """
    assert "PRIOR_ATTEMPT_FAILED" in SESSION_REFUSAL_ERROR_CODES


# --- the vendored copy itself -------------------------------------------------


def test_the_fixture_is_the_v1_contract():
    """Guards against a half-applied fixture update landing here unnoticed."""
    assert CONTRACT["version"] == "v1"
    assert sorted(ENDPOINTS) == ["createCheckoutSession", "getStatus", "ping"]
