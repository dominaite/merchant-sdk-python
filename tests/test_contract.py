"""Pins the merchant-API response contract against the canonical fixture.

``merchant-api-contract.json`` in this directory is a byte-identical vendored copy of
the cross-SDK fixture. Every Dominaite SDK carries the same file and asserts the same
things against it - the status vocabulary, the per-endpoint response fields, the
refusal codes and the validation codes - so a field or status value cannot ship in one
SDK and be mirrored wrong into the siblings.

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
    VALIDATION_ERROR_CODES,
    ApiError,
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
        self.headers = None
        # A stream, because the client reads in bounded chunks rather than all at once.
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self, amount=None):
        return self._body.read() if amount is None else self._body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def client():
    return DominaiteClient(KEY_ID, SECRET, base_url=BASE_URL)


def _patch_opener(monkeypatch, handler):
    # The client sends through its own opener (it refuses redirects), so the seam is
    # OpenerDirector.open rather than urlopen.
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda _self, request, timeout=None: handler(request, timeout=timeout),
    )


@pytest.fixture
def answers_with(monkeypatch):
    """Make the next call return one fixture example verbatim."""

    def install(payload):
        _patch_opener(monkeypatch, lambda request, timeout=None: _Response(payload))

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


def test_the_recognized_refusal_codes_are_exactly_the_contract_set():
    """No extras either: a code the SDK invents is drift the same as a missing one."""
    assert sorted(SESSION_REFUSAL_ERROR_CODES) == sorted(
        CONTRACT["sessionRefusalErrorCodes"]
    )


# --- (4) validation error codes -----------------------------------------------
#
# These are the OTHER failure shape on the create endpoint: HTTP 400 with the code at
# error.code, not a 200 with success=false. Mixing the two up is the bug worth pinning
# - a validation error must not surface as a refusal, and must not lose its code.


def test_the_recognized_validation_codes_are_exactly_the_contract_set():
    assert sorted(VALIDATION_ERROR_CODES) == sorted(CONTRACT["validationErrorCodes"])


@pytest.mark.parametrize("code", CONTRACT["validationErrorCodes"])
def test_a_validation_error_surfaces_as_an_api_error_carrying_its_code(
    code, client, monkeypatch
):
    body = {
        "success": False,
        "error": {"code": code, "message": "Idempotency-Key header is required.", "statusCode": 400},
    }

    def raise_400(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 400, "error", {}, io.BytesIO(json.dumps(body).encode("utf-8"))
        )

    _patch_opener(monkeypatch, raise_400)

    with pytest.raises(ApiError) as raised:
        client.create_checkout_session(
            amount=8440, currency="EUR", order_reference="order-1042"
        )

    assert raised.value.error_code == code
    assert raised.value.http_status == 400
    assert str(raised.value) == body["error"]["message"]


def test_a_validation_error_is_not_reported_as_a_refusal(client, monkeypatch):
    """400 is a malformed call; 200 + success=false is a business refusal. Different bugs."""

    def raise_400(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "error",
            {},
            io.BytesIO(
                json.dumps(
                    {"success": False, "error": {"code": "IDEMPOTENCY_KEY_REQUIRED"}}
                ).encode("utf-8")
            ),
        )

    _patch_opener(monkeypatch, raise_400)

    with pytest.raises(ApiError) as raised:
        client.create_checkout_session(
            amount=8440, currency="EUR", order_reference="order-1042"
        )

    assert not isinstance(raised.value, CheckoutRefusedError)


# --- the vendored copy itself -------------------------------------------------


def test_the_fixture_is_the_v1_contract():
    """Guards against a half-applied fixture update landing here unnoticed."""
    assert CONTRACT["version"] == "v1"
    assert sorted(ENDPOINTS) == ["createCheckoutSession", "getStatus", "ping"]
