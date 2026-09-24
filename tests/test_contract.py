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
    CHARGE_ERROR_CODES,
    CHARGE_STATUSES,
    DECLINE_CLASSES,
    PAYMENT_STATUSES,
    REVOKE_ERROR_CODES,
    SESSION_REFUSAL_ERROR_CODES,
    STORED_PAYMENT_METHOD_STATUSES,
    VALIDATION_ERROR_CODES,
    ApiError,
    ChargeError,
    ChargeStatus,
    CheckoutRefusedError,
    DeclineClass,
    DominaiteClient,
    PaymentStatus,
    RevokeError,
    StoredPaymentMethodStatus,
    TransportError,
)

CONTRACT = json.loads(
    (Path(__file__).resolve().parent / "merchant-api-contract.json").read_text("utf-8")
)
ENDPOINTS = CONTRACT["endpoints"]

KEY_ID = "dmk_0123456789abcdef0123456789abcdef"
SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.test/payments"
#: The key a caller builds with order_idempotency_key("checkout", "1042", 2500, "EUR").
IDEMPOTENCY_KEY = "checkout-1042-2500-EUR"


class _Response:
    def __init__(self, payload, status=200):
        self.status = status
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
    """Make the next call return one fixture example verbatim, with the given status."""

    def install(payload, status=200):
        def handler(request, timeout=None):
            if status >= 400:
                raise urllib.error.HTTPError(
                    request.full_url,
                    status,
                    "error",
                    None,
                    io.BytesIO(json.dumps(payload).encode("utf-8")),
                )
            return _Response(payload, status)

        _patch_opener(monkeypatch, handler)

    return install


def _without_nulls(value):
    """The wire form of an example: the gateway serializes WhenWritingNull, so null keys are absent."""
    if isinstance(value, list):
        return [_without_nulls(entry) for entry in value]
    if isinstance(value, dict):
        return {key: _without_nulls(entry) for key, entry in value.items() if entry is not None}
    return value


PAYMENT_METHOD_ID = ENDPOINTS["getStatus"]["savedCardExample"]["storedPaymentMethod"]["id"]


def _charge(client):
    return client.charge_payment_method(
        PAYMENT_METHOD_ID,
        amount=8440,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )


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
        amount=8440,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
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
            amount=8440,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
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


def test_get_status_returns_the_saved_card_example_stored_payment_method_included(
    client, answers_with
):
    endpoint = ENDPOINTS["getStatus"]
    example = endpoint["savedCardExample"]
    answers_with(example)

    result = client.get_status(example["transactionId"])

    assert result == example
    assert sorted(result) == sorted(endpoint["fields"])
    assert sorted(result["storedPaymentMethod"]) == sorted(endpoint["storedPaymentMethodFields"])
    assert result["storedPaymentMethod"]["status"] in STORED_PAYMENT_METHOD_STATUSES
    # A status without a saved card carries the key as null in the fixture.
    assert "storedPaymentMethod" in endpoint["example"]
    assert endpoint["example"]["storedPaymentMethod"] is None


def test_get_status_reads_absent_card_fields_as_none_like_the_wire(client, answers_with):
    # The gateway serializes WhenWritingNull: a session without a saved card has no
    # storedPaymentMethod key at all, and an unreported brand is a missing key.
    endpoint = ENDPOINTS["getStatus"]
    wire = _without_nulls(endpoint["example"])
    assert "storedPaymentMethod" not in wire
    answers_with(wire)
    bare = client.get_status(endpoint["example"]["transactionId"])
    # The status passes through as sent; absent and null both read as "no card on file".
    assert bare == wire
    assert bare.get("storedPaymentMethod") is None

    unreported = dict(
        endpoint["savedCardExample"],
        storedPaymentMethod={"id": PAYMENT_METHOD_ID, "status": "active"},
    )
    answers_with(unreported)
    result = client.get_status(unreported["transactionId"])
    assert result["storedPaymentMethod"] == {
        "id": PAYMENT_METHOD_ID,
        "brand": None,
        "last4": None,
        "expiryMonth": None,
        "expiryYear": None,
        "status": "active",
    }


def test_payment_method_vocabularies_equal_the_contract():
    assert list(STORED_PAYMENT_METHOD_STATUSES) == CONTRACT["storedPaymentMethodStatusVocabulary"]
    assert list(CHARGE_STATUSES) == CONTRACT["chargeStatusVocabulary"]
    assert list(DECLINE_CLASSES) == CONTRACT["declineClassVocabulary"]
    assert list(CHARGE_ERROR_CODES) == CONTRACT["chargeErrorCodes"]
    assert list(REVOKE_ERROR_CODES) == CONTRACT["revokeErrorCodes"]
    assert StoredPaymentMethodStatus.ACTIVE == "active"
    assert ChargeStatus.CANCELLED == "cancelled"
    assert DeclineClass.SOFT_SCA_REQUIRED == "soft_sca_required"
    assert "CHARGE_DECLINED" not in CHARGE_ERROR_CODES


def test_charge_payment_method_returns_the_201_charge_out_of_the_envelope(client, answers_with):
    endpoint = ENDPOINTS["chargePaymentMethod"]
    example = endpoint["successExample"]
    answers_with(example, endpoint["httpStatus"])

    charge = _charge(client)

    assert sorted(charge) == sorted(endpoint["fields"])
    assert charge == example["data"]
    assert charge["status"] in CHARGE_STATUSES
    assert charge["declineClass"] is None
    assert charge["declineCode"] is None


def test_charge_payment_method_returns_the_402_decline_as_a_charge_never_raises(client, answers_with):
    endpoint = ENDPOINTS["chargePaymentMethod"]
    example = endpoint["declinedExample"]
    answers_with(example, endpoint["declinedHttpStatus"])

    charge = _charge(client)

    assert charge == example["data"]
    assert charge["status"] == ChargeStatus.FAILED
    assert charge["declineClass"] in DECLINE_CLASSES
    assert example["error"]["code"] == "CHARGE_DECLINED"


def test_charge_payment_method_reads_absent_decline_fields_as_none_like_the_wire(client, answers_with):
    endpoint = ENDPOINTS["chargePaymentMethod"]
    wire = _without_nulls(endpoint["successExample"])
    assert "declineClass" not in wire["data"]
    answers_with(wire, endpoint["httpStatus"])

    assert _charge(client) == endpoint["successExample"]["data"]


@pytest.mark.parametrize(
    "example",
    ENDPOINTS["chargePaymentMethod"]["errorExamples"],
    ids=[
        "{0}-{1}-{2}".format(e["httpStatus"], e["code"], "data" if e["body"].get("data") else "nodata")
        for e in ENDPOINTS["chargePaymentMethod"]["errorExamples"]
    ],
)
@pytest.mark.parametrize("wire_form", ["nulls-spelled-out", "nulls-omitted"])
def test_every_charge_error_example_is_a_charge_error_with_code_status_and_data(
    client, answers_with, example, wire_form
):
    body = example["body"] if wire_form == "nulls-spelled-out" else _without_nulls(example["body"])
    answers_with(body, example["httpStatus"])

    with pytest.raises(ChargeError) as raised:
        _charge(client)

    error = raised.value
    assert not isinstance(error, TransportError)
    assert error.http_status == example["httpStatus"]
    assert error.error_code == example["code"]
    assert error.error_code in CHARGE_ERROR_CODES
    assert str(error) == example["body"]["error"]["message"]
    assert error.result == body
    if example["body"].get("data"):
        assert error.charge == example["body"]["data"]
        assert error.transaction_id == example["body"]["data"]["transactionId"]
    else:
        assert error.charge is None
        assert error.transaction_id is None


def test_the_charge_error_examples_cover_every_code_the_sdk_claims():
    seen = {e["code"] for e in ENDPOINTS["chargePaymentMethod"]["errorExamples"]}
    assert seen == set(CHARGE_ERROR_CODES)
    seen = {e["code"] for e in ENDPOINTS["revokePaymentMethod"]["errorExamples"]}
    assert seen == set(REVOKE_ERROR_CODES)


def test_a_charge_against_an_unknown_id_is_the_generic_api_error_404(client, answers_with):
    example = ENDPOINTS["chargePaymentMethod"]["notFoundExample"]
    answers_with(example["body"], example["httpStatus"])

    with pytest.raises(ApiError) as raised:
        _charge(client)

    assert not isinstance(raised.value, ChargeError)
    assert raised.value.http_status == 404
    assert raised.value.error_code == example["code"]


@pytest.mark.parametrize(
    "example",
    ENDPOINTS["revokePaymentMethod"]["errorExamples"],
    ids=[e["code"] for e in ENDPOINTS["revokePaymentMethod"]["errorExamples"]],
)
def test_every_revoke_error_example_is_a_revoke_error_with_code_and_status(
    client, answers_with, example
):
    answers_with(example["body"], example["httpStatus"])

    with pytest.raises(RevokeError) as raised:
        client.revoke_payment_method(PAYMENT_METHOD_ID)

    error = raised.value
    assert not isinstance(error, TransportError)
    assert error.http_status == example["httpStatus"]
    assert error.error_code == example["code"]
    assert error.error_code in REVOKE_ERROR_CODES
    assert str(error) == example["body"]["error"]["message"]
    assert error.result == example["body"]


def test_a_revoke_of_an_unknown_id_is_the_generic_api_error_404(client, answers_with):
    example = ENDPOINTS["revokePaymentMethod"]["notFoundExample"]
    answers_with(example["body"], example["httpStatus"])

    with pytest.raises(ApiError) as raised:
        client.revoke_payment_method(PAYMENT_METHOD_ID)

    assert not isinstance(raised.value, RevokeError)
    assert raised.value.http_status == 404
    assert raised.value.error_code == example["code"]


def test_the_contract_examples_carry_exactly_their_declared_fields():
    charge_endpoint = ENDPOINTS["chargePaymentMethod"]
    fields = sorted(charge_endpoint["fields"])
    assert sorted(charge_endpoint["successExample"]["data"]) == fields
    assert sorted(charge_endpoint["declinedExample"]["data"]) == fields
    for example in charge_endpoint["errorExamples"]:
        assert example["body"]["success"] is False
        assert example["body"]["error"]["code"] == example["code"]
        assert example["body"]["error"]["statusCode"] == example["httpStatus"]
        if example["body"].get("data"):
            assert sorted(example["body"]["data"]) == fields
    status_endpoint = ENDPOINTS["getStatus"]
    assert sorted(status_endpoint["savedCardExample"]["storedPaymentMethod"]) == sorted(
        status_endpoint["storedPaymentMethodFields"]
    )


def test_charge_and_revoke_hit_the_contract_paths_and_methods(client, monkeypatch):
    seen = []

    class _NoContent:
        status = 204
        headers = None

        def read(self, amount=None):
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def handler(request, timeout=None):
        seen.append(request)
        if request.get_method() == "DELETE":
            return _NoContent()
        return _Response(ENDPOINTS["chargePaymentMethod"]["successExample"], 201)

    _patch_opener(monkeypatch, handler)
    payment_method_id = PAYMENT_METHOD_ID

    client.charge_payment_method(
        payment_method_id,
        amount=8440,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )
    assert client.revoke_payment_method(payment_method_id) is None

    charge_endpoint = ENDPOINTS["chargePaymentMethod"]
    revoke_endpoint = ENDPOINTS["revokePaymentMethod"]
    assert seen[0].get_method() == charge_endpoint["method"]
    assert seen[0].full_url == BASE_URL + charge_endpoint["path"].replace(
        "{paymentMethodId}", payment_method_id
    )
    assert "Idempotency-key" in seen[0].headers
    assert seen[1].get_method() == revoke_endpoint["method"]
    assert seen[1].full_url == BASE_URL + revoke_endpoint["path"].replace(
        "{paymentMethodId}", payment_method_id
    )
    assert "Idempotency-key" not in seen[1].headers
    assert revoke_endpoint["httpStatus"] == 204
    assert revoke_endpoint["fields"] == []


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
            amount=8440,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
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
            amount=8440,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
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
            amount=8440,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert not isinstance(raised.value, CheckoutRefusedError)


# --- the vendored copy itself -------------------------------------------------


def test_the_fixture_is_the_v1_contract():
    """Guards against a half-applied fixture update landing here unnoticed."""
    assert CONTRACT["version"] == "v1"
    assert sorted(ENDPOINTS) == [
        "chargePaymentMethod",
        "createCheckoutSession",
        "getStatus",
        "ping",
        "revokePaymentMethod",
    ]
