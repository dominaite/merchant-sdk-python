"""Contract tests for DominaiteClient: what it sends, and how it classifies answers."""

import email
import hashlib
import io
import json
import pickle
import pprint
import urllib.error
import urllib.request
import uuid

import pytest

from dominaite import (
    AGREEMENTS_PATH,
    AUTHORIZATION_ERROR_CODES,
    CHARGE_ERROR_CODES,
    CONTRACT_PATH,
    DEFAULT_BASE_URL,
    PAYMENT_METHODS_PATH,
    PAYMENTS_PATH,
    PING_PATH,
    PLANS_PATH,
    REFUND_ERROR_CODES,
    REFUND_FAILURE_CODES,
    REVOKE_ERROR_CODES,
    SESSION_REFUSAL_ERROR_CODES,
    SESSIONS_PATH,
    STOREFRONT_ERROR_CODES,
    VALIDATION_ERROR_CODES,
    ApiError,
    AuthenticationError,
    ChargeError,
    CheckoutIntegration,
    CheckoutRefusedError,
    DominaiteClient,
    ErrorCode,
    RateLimitError,
    RefundError,
    RefundStatus,
    RevokeError,
    StorefrontError,
    TransportError,
    order_idempotency_key,
    sign_request,
)
from dominaite.client import MAX_RESPONSE_BYTES

KEY_ID = "dmk_0123456789abcdef0123456789abcdef"
SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.test/payments"
#: The key a caller builds with order_idempotency_key("checkout", "1042", 2500, "EUR").
IDEMPOTENCY_KEY = "checkout-1042-2500-EUR"
TRANSACTION_ID = "11111111-2222-4333-8444-555555555555"
#: A UUID with hex LETTERS in it, so upper/lower case are actually different strings.
LETTERED_TRANSACTION_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

CHECKOUT = {
    "transactionId": TRANSACTION_ID,
    "orderId": "ord_1",
    "cashierKey": "ck_1",
    "cashierToken": "ct_1",
    "amount": 2500,
    "currency": "EUR",
    "expiresAt": "2026-08-16T12:00:00Z",
}


def _message(headers):
    """Build response headers the way urllib hands them over: an email.Message."""
    return email.message_from_string(
        "".join("{0}: {1}\n".format(name, value) for name, value in (headers or {}).items())
    )


class _Response:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.headers = _message(headers)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        # A real body arrives through a stream that honours read(amount). Mirroring that
        # is what lets the bounded-read cap be exercised at all.
        self._body = io.BytesIO(raw)

    def read(self, amount=None):
        return self._body.read() if amount is None else self._body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Recorder:
    """Stands in for the client's opener and records every request it builds."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        # (status, payload) or (status, payload, response_headers).
        status, payload = outcome[0], outcome[1]
        headers = outcome[2] if len(outcome) > 2 else None
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "error",
                _message(headers),
                io.BytesIO(raw),
            )
        return _Response(status, payload, headers)

    @property
    def last(self):
        return self.requests[-1]


@pytest.fixture
def client():
    return DominaiteClient(KEY_ID, SECRET, base_url=BASE_URL)


@pytest.fixture
def urlopen(monkeypatch):
    def install(*outcomes):
        recorder = _Recorder(*outcomes)
        _patch_opener(monkeypatch, recorder)
        return recorder

    return install


def _patch_opener(monkeypatch, handler):
    # The client sends through its own opener (see _NoRedirectHandler), so the seam is
    # OpenerDirector.open rather than urlopen.
    monkeypatch.setattr(
        urllib.request.OpenerDirector,
        "open",
        lambda _self, request, timeout=None: handler(request, timeout=timeout),
    )


def _ok():
    return (200, {"success": True, "checkout": CHECKOUT})


def _headers(request):
    # urllib title-cases header names on the Request object.
    return {name.lower(): value for name, value in request.headers.items()}


# --- constructor -------------------------------------------------------------


def test_rejects_key_id_without_dmk_prefix():
    with pytest.raises(ValueError, match="dmk_"):
        DominaiteClient("nope", SECRET)


def test_rejects_secret_without_dms_prefix():
    with pytest.raises(ValueError, match="dms_"):
        DominaiteClient(KEY_ID, "nope")


# --- the secret does not escape ----------------------------------------------

SENTINEL_SECRET = "dms_" + "S3CRET" * 5


@pytest.mark.parametrize(
    "surface",
    [
        repr,
        str,
        lambda c: str(vars(c)),
        lambda c: json.dumps(vars(c), default=str),
        lambda c: pprint.pformat(vars(c)),
    ],
    ids=["repr", "str", "vars", "json.dumps(vars)", "pprint"],
)
def test_the_secret_never_appears_when_the_client_is_shown_or_serialized(surface):
    """Each of these is a real way a client ends up in a log line or a crash dump."""
    client = DominaiteClient(KEY_ID, SENTINEL_SECRET)

    assert SENTINEL_SECRET not in surface(client)


def test_the_client_defines_its_own_repr_as_a_guard():
    """Today the inherited repr prints nothing, so this is about tomorrow.

    A dataclass conversion, or a convenience repr added later, prints every attribute
    by default. Owning __repr__ means the redaction survives that change instead of
    silently reopening the display path.
    """
    assert DominaiteClient.__repr__ is not object.__repr__

    text = repr(DominaiteClient(KEY_ID, SENTINEL_SECRET))
    assert "***redacted***" in text
    assert KEY_ID in text, "the key id is not secret - keep the repr useful"


def test_the_secret_cannot_be_pickled():
    client = DominaiteClient(KEY_ID, SENTINEL_SECRET)

    with pytest.raises(TypeError):
        pickle.dumps(client._secret)


def test_reveal_returns_the_real_secret_so_signing_still_works(urlopen):
    """The redaction is a display concern; signing must still see the real value."""
    client = DominaiteClient(KEY_ID, SENTINEL_SECRET, base_url=BASE_URL)
    recorder = urlopen(_ok())

    assert client._secret.reveal() == SENTINEL_SECRET

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key="00000000-0000-4000-8000-000000000001",
    )

    headers = _headers(recorder.last)
    expected = sign_request(
        SENTINEL_SECRET,
        headers["x-timestamp"],
        "POST",
        SESSIONS_PATH,
        "00000000-0000-4000-8000-000000000001",
        recorder.last.data.decode("utf-8"),
    )
    assert headers["x-signature"] == expected


# --- what goes on the wire ---------------------------------------------------


def test_post_sends_signed_headers_and_compact_json(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key="00000000-0000-4000-8000-000000000001",
    )

    request = recorder.last
    headers = _headers(request)
    assert request.full_url == BASE_URL + SESSIONS_PATH
    assert request.get_method() == "POST"
    assert headers["x-api-key-id"] == KEY_ID
    assert headers["idempotency-key"] == "00000000-0000-4000-8000-000000000001"
    assert headers["content-type"] == "application/json"

    body = request.data.decode("utf-8")
    assert body == '{"amount":2500,"currency":"EUR","orderReference":"order-1042"}'

    expected = sign_request(
        SECRET,
        headers["x-timestamp"],
        "POST",
        SESSIONS_PATH,
        "00000000-0000-4000-8000-000000000001",
        body,
    )
    assert headers["x-signature"] == expected


def test_post_reproduces_the_published_vector_end_to_end(client, urlopen, monkeypatch):
    """The signature the client actually puts on the wire, against the published vector.

    test_signing.py pins the sign_request() function; this pins the whole path -
    argument order, JSON serialization, header assembly - to the same answer.
    """
    monkeypatch.setattr("dominaite.client.time.time", lambda: 1755302400)
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key="00000000-0000-4000-8000-000000000001",
    )

    headers = _headers(recorder.last)
    assert headers["x-timestamp"] == "1755302400"
    assert (
        headers["x-signature"]
        == "8f5fba0b29a8eea81b76a0e6d7119e79ec68f586910f77713b045652e5ce9b74"
    )


def test_get_status_signs_empty_idempotency_key_and_empty_body(client, urlopen):
    recorder = urlopen((200, {"transactionId": TRANSACTION_ID, "status": "succeeded"}))

    client.get_status(TRANSACTION_ID)

    request = recorder.last
    headers = _headers(request)
    path = SESSIONS_PATH + "/" + TRANSACTION_ID

    assert request.get_method() == "GET"
    assert request.data is None
    assert "idempotency-key" not in headers

    # The signed payload uses an EMPTY idempotency key and the hash of an EMPTY body.
    expected = sign_request(SECRET, headers["x-timestamp"], "GET", path, "", "")
    assert headers["x-signature"] == expected

    payload = "\n".join([headers["x-timestamp"], "GET", path, "", EMPTY_SHA256])
    assert payload.count("\n") == 4


def test_ping_signs_empty_idempotency_key_and_empty_body(client, urlopen):
    recorder = urlopen(
        (
            200,
            {
                "success": True,
                "data": {
                    "pong": True,
                    "merchantId": "mer_1",
                    "serverTime": "2026-08-20T12:00:00Z",
                    "serverUnixTime": 1755691200,
                    "clockSkewSeconds": 2,
                },
            },
        )
    )

    result = client.ping()

    request = recorder.last
    headers = _headers(request)

    assert request.full_url == BASE_URL + PING_PATH
    assert request.get_method() == "GET"
    assert request.data is None
    assert "idempotency-key" not in headers

    # The signed path is the canonical path only - never the base URL's own prefix.
    expected = sign_request(SECRET, headers["x-timestamp"], "GET", PING_PATH, "", "")
    assert headers["x-signature"] == expected

    payload = "\n".join([headers["x-timestamp"], "GET", PING_PATH, "", EMPTY_SHA256])
    assert payload.count("\n") == 4

    # The ping read is FLAT inside the envelope: no checkout wrapper, no inner success.
    assert result["pong"] is True
    assert result["merchantId"] == "mer_1"
    assert result["clockSkewSeconds"] == 2


def test_ping_401_raises_authentication_with_the_error_code(client, urlopen):
    urlopen((401, {"errorCode": "IP_NOT_ALLOWED"}))

    with pytest.raises(AuthenticationError) as caught:
        client.ping()

    assert caught.value.error_code == "IP_NOT_ALLOWED"


def _gateway_error(status, code, message):
    """An error body in the gateway's envelope shape."""
    return {
        "success": False,
        "error": {"statusCode": status, "code": code, "message": message},
        "metadata": {"requestId": "req_1"},
    }


def test_a_401_points_at_the_credentials(client, urlopen):
    urlopen((401, _gateway_error(401, "INVALID_SIGNATURE", "Signature mismatch")))

    with pytest.raises(AuthenticationError) as caught:
        client.ping()

    assert caught.value.http_status == 401
    assert caught.value.error_code == "INVALID_SIGNATURE"
    assert "check your key id, secret, and server clock" in str(caught.value)


def test_a_missing_scope_is_a_403_that_names_the_scope_and_never_the_clock(client, urlopen):
    """The signature was good, so a credential hint would send the caller after the wrong
    problem. The gateway's message names the scope the route needs; it has to survive."""
    body = _gateway_error(
        403,
        "INSUFFICIENT_SCOPE",
        "This API key does not have the 'payments' scope this route requires",
    )
    urlopen((403, body))

    with pytest.raises(AuthenticationError) as caught:
        client.get_status(TRANSACTION_ID)

    error = caught.value
    assert error.http_status == 403
    assert error.error_code == ErrorCode.INSUFFICIENT_SCOPE
    assert "'payments' scope" in str(error)
    assert "key that holds that scope" in str(error)
    for credential_hint in ("clock", "secret", "key id"):
        assert credential_hint not in str(error)
    assert error.result == body


def test_verify_only_not_enabled_is_a_403_that_says_we_switch_it_on(client, urlopen):
    urlopen(
        (
            403,
            _gateway_error(
                403,
                "VERIFY_ONLY_NOT_ENABLED",
                "Verify-only sessions are not enabled for this account.",
            ),
        )
    )

    with pytest.raises(AuthenticationError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    error = caught.value
    assert error.http_status == 403
    assert error.error_code == ErrorCode.VERIFY_ONLY_NOT_ENABLED
    assert "We switch it on for your account" in str(error)
    assert "clock" not in str(error)


@pytest.mark.parametrize(
    "body, code, text",
    [
        (_gateway_error(403, "SOMETHING_NEW", "Not for this key."), "SOMETHING_NEW", "Not for this key."),
        ({}, "FORBIDDEN", "(HTTP 403)."),
    ],
    ids=["unknown-code", "no-code"],
)
def test_any_other_403_keeps_the_server_message_and_no_credential_hint(
    client, urlopen, body, code, text
):
    urlopen((403, body))

    with pytest.raises(AuthenticationError) as caught:
        client.ping()

    assert caught.value.http_status == 403
    assert caught.value.error_code == code
    assert text in str(caught.value)
    assert "clock" not in str(caught.value)


def test_a_4xx_rejection_carries_the_whole_body_the_gateway_sent(client, urlopen):
    body = _gateway_error(404, "NOT_FOUND", "Transaction not found")
    body["error"]["validationErrors"] = [{"field": "id", "code": "NOT_FOUND"}]
    urlopen((404, body))

    with pytest.raises(ApiError) as caught:
        client.get_status(TRANSACTION_ID)

    assert caught.value.http_status == 404
    assert caught.value.result == body


def test_get_status_normalizes_transaction_id_casing(client, urlopen):
    # LETTERED_TRANSACTION_ID, not TRANSACTION_ID: the latter is all digits, so .upper()
    # returns it unchanged and this test would pass with the lowercasing deleted.
    assert LETTERED_TRANSACTION_ID.upper() != LETTERED_TRANSACTION_ID
    recorder = urlopen((200, {"transactionId": LETTERED_TRANSACTION_ID}))

    client.get_status("  " + LETTERED_TRANSACTION_ID.upper() + "  ")

    assert recorder.last.full_url.endswith(
        SESSIONS_PATH + "/" + LETTERED_TRANSACTION_ID
    )


def test_get_status_rejects_non_uuid(client):
    with pytest.raises(ValueError, match="UUID"):
        client.get_status("order-1042")


def test_optional_fields_are_omitted_when_not_passed(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        customer={"firstName": "Ana", "email": "ana@example.com"},
        language="bg",
        idempotency_key=IDEMPOTENCY_KEY,
    )

    body = json.loads(recorder.last.data.decode("utf-8"))
    assert body["customer"] == {"firstName": "Ana", "email": "ana@example.com"}
    assert body["language"] == "bg"
    assert "theme" not in body
    assert "country" not in body
    assert "description" not in body


@pytest.mark.parametrize("key", [None, ""], ids=["none", "empty"])
def test_refuses_a_session_without_an_idempotency_key_before_sending(client, urlopen, key):
    """No random fallback: a key minted per call gives a reload a second session for
    the same order. Only the caller can say which payment this is."""
    recorder = urlopen(_ok())

    with pytest.raises(ValueError, match="idempotency_key is required"):
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042", idempotency_key=key
        )

    assert recorder.requests == []


def test_refuses_a_session_when_the_key_is_left_out_entirely(client, urlopen):
    recorder = urlopen(_ok())

    with pytest.raises(ValueError, match="idempotency_key is required"):
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert recorder.requests == []


# --- order-derived idempotency keys ------------------------------------------


def test_order_key_is_scope_order_amount_and_upper_cased_currency():
    assert order_idempotency_key("checkout", "1042", 2500, "eur") == "checkout-1042-2500-EUR"
    assert order_idempotency_key("checkout", 1042, 2500, "EUR") == "checkout-1042-2500-EUR"


def test_the_same_order_at_the_same_amount_always_gets_the_same_key():
    """A reload or back button rebuilds the key from the order, so it has to come out
    identical, or the replay turns into a second session."""
    assert order_idempotency_key("checkout", "1042", 2500, "EUR") == order_idempotency_key(
        "checkout", "1042", 2500, "eur"
    )


@pytest.mark.parametrize(
    "changed",
    [
        ("checkout", "1042", 2400, "EUR"),
        ("checkout", "1042", 2500, "BGN"),
        ("checkout", "1043", 2500, "EUR"),
        ("deposit", "1042", 2500, "EUR"),
    ],
    ids=["amount", "currency", "order", "scope"],
)
def test_changing_any_part_changes_the_key(changed):
    assert order_idempotency_key(*changed) != order_idempotency_key("checkout", "1042", 2500, "EUR")


def test_the_order_key_is_sent_as_the_idempotency_header(client, urlopen):
    recorder = urlopen(_ok())
    key = order_idempotency_key("checkout", "1042", 2500, "EUR")

    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042", idempotency_key=key
    )

    assert _headers(recorder.last)["idempotency-key"] == "checkout-1042-2500-EUR"


@pytest.mark.parametrize(
    "args",
    [
        ("", "1042", 2500, "EUR"),
        (None, "1042", 2500, "EUR"),
        ("checkout", "", 2500, "EUR"),
        ("checkout", None, 2500, "EUR"),
        ("checkout", True, 2500, "EUR"),
        ("checkout", "1042", 25.0, "EUR"),
        ("checkout", "1042", "2500", "EUR"),
        ("checkout", "1042", 0, "EUR"),
        ("checkout", "1042", -2500, "EUR"),
        ("checkout", "1042", True, "EUR"),
        ("checkout", "1042", 2500, "EU"),
        ("checkout", "1042", 2500, "EURO"),
        ("checkout", "1042", 2500, "E1R"),
        ("checkout", "1042", 2500, ""),
        ("checkout", "1042", 2500, None),
    ],
    ids=[
        "empty-scope", "none-scope", "empty-order", "none-order", "bool-order",
        "float-amount", "string-amount", "zero-amount", "negative-amount", "bool-amount",
        "short-currency", "long-currency", "digit-currency", "empty-currency", "none-currency",
    ],
)
def test_the_order_key_refuses_bad_parts(args):
    with pytest.raises(ValueError):
        order_idempotency_key(*args)


@pytest.mark.parametrize(
    "order_id", ["заказ-1042", "café", "order 1042", "o" * 100], ids=["cyrillic", "latin-1", "space", "too-long"]
)
def test_the_order_key_is_held_to_the_same_rules_as_any_key(order_id):
    """A key the helper builds must be one the client will send."""
    with pytest.raises(ValueError, match="idempotency_key"):
        order_idempotency_key("checkout", order_id, 2500, "EUR")


# --- amounts -----------------------------------------------------------------


@pytest.mark.parametrize("amount", [25.0, "2500", 0, -1, True])
def test_amount_must_be_a_positive_integer_in_minor_units(client, amount):
    with pytest.raises(ValueError, match="MINOR units"):
        client.create_checkout_session(
            amount=amount,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


# --- refusal vs transport ----------------------------------------------------


def test_business_refusal_raises_checkout_refused_with_the_error_code(client, urlopen):
    urlopen(
        (
            200,
            {
                "success": False,
                "errorCode": "PAYMENT_PROCESSING_UNAVAILABLE",
                "errorMessage": "Card payments are off",
            },
        )
    )

    with pytest.raises(CheckoutRefusedError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == "PAYMENT_PROCESSING_UNAVAILABLE"


def test_replay_refusal_carries_the_transaction_id_for_recovery(client, urlopen):
    """A DUPLICATE_REQUEST names the payment the key collided with.

    Without this the documented recovery - read it back with get_status() - is
    unreachable from the exception, and the caller's only option is a second payment.
    """
    urlopen(
        (
            200,
            {
                "success": False,
                "transactionId": TRANSACTION_ID,
                "errorCode": "DUPLICATE_REQUEST",
                "errorMessage": "A checkout session for this idempotency key is already open.",
            },
        )
    )

    with pytest.raises(CheckoutRefusedError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == "DUPLICATE_REQUEST"
    assert caught.value.transaction_id == TRANSACTION_ID
    assert caught.value.result["errorCode"] == "DUPLICATE_REQUEST"


def test_refusal_without_a_transaction_id_leaves_it_none(client, urlopen):
    """The concurrent-race DUPLICATE_REQUEST knows the key is taken, but not by which row."""
    urlopen((200, {"success": False, "errorCode": "DUPLICATE_REQUEST"}))

    with pytest.raises(CheckoutRefusedError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.transaction_id is None


def test_503_raises_transport_not_refusal(client, urlopen):
    urlopen((503, {"errorCode": "MERCHANT_API_UNAVAILABLE"}))

    with pytest.raises(TransportError):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_network_failure_raises_transport(client, urlopen):
    urlopen(urllib.error.URLError("connection reset"))

    with pytest.raises(TransportError):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


@pytest.mark.parametrize(
    "code",
    ["INVALID_API_KEY", "INVALID_SIGNATURE", "TIMESTAMP_OUT_OF_RANGE", "IP_NOT_ALLOWED"],
)
def test_401_raises_authentication_with_the_error_code(client, urlopen, code):
    urlopen((401, {"errorCode": code}))

    with pytest.raises(AuthenticationError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == code


def test_422_key_reuse_raises_api_error_not_transport(client, urlopen):
    urlopen((422, {"errorCode": "IDEMPOTENCY_KEY_REUSED", "errorMessage": "Use a fresh key"}))

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.http_status == 422


def test_non_json_response_raises_api_error(client, urlopen, monkeypatch):
    _patch_opener(
        monkeypatch,
        lambda request, timeout=None: _Response(200, b"<html>502 Bad Gateway</html>"),
    )

    with pytest.raises(ApiError):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_envelope_wrapped_response_is_unwrapped(client, urlopen):
    urlopen((200, {"success": True, "data": {"success": True, "checkout": CHECKOUT}}))

    session = client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert session == CHECKOUT


# --- retry with the same key -------------------------------------------------


def test_retry_helper_reuses_the_same_idempotency_key(client, urlopen):
    recorder = urlopen((503, {"errorCode": "MERCHANT_API_UNAVAILABLE"}), _ok())

    session = client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        max_attempts=2,
        backoff_seconds=0,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert session == CHECKOUT
    assert len(recorder.requests) == 2
    keys = {_headers(r)["idempotency-key"] for r in recorder.requests}
    assert len(keys) == 1, "a retry must not mint a new key - that is the double-charge bug"


def test_retry_helper_retries_a_503_payment_processing_unavailable_with_the_same_key(
    client, urlopen
):
    """The 503 form of the code (the stored-card route answers this way, and a proxy or
    a future gateway may do so on create) must be retried, not surfaced on attempt one."""
    unavailable = {
        "success": False,
        "error": {"code": "PAYMENT_PROCESSING_UNAVAILABLE", "message": "Card payments are unavailable right now."},
    }
    recorder = urlopen((503, unavailable), (503, unavailable), _ok())

    session = client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
        max_attempts=3,
        backoff_seconds=0,
    )

    assert session == CHECKOUT
    assert [_headers(r)["idempotency-key"] for r in recorder.requests] == [IDEMPOTENCY_KEY] * 3


def test_retry_helper_retries_a_payment_processing_unavailable_refusal_with_the_same_key(
    client, urlopen
):
    """On create the gateway answers this code as HTTP 200 success=false, and its contract
    says: retry later with the same key. It is the one refusal the helper retries."""
    refusal = {
        "success": False,
        "errorCode": "PAYMENT_PROCESSING_UNAVAILABLE",
        "errorMessage": "Card payments are not available right now. Retry later with the same idempotency key.",
    }
    recorder = urlopen((200, refusal), _ok())

    session = client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
        max_attempts=3,
        backoff_seconds=0,
    )

    assert session == CHECKOUT
    assert [_headers(r)["idempotency-key"] for r in recorder.requests] == [IDEMPOTENCY_KEY] * 2


def test_retry_helper_raises_the_payment_processing_refusal_once_attempts_run_out(
    client, urlopen
):
    recorder = urlopen((200, {"success": False, "errorCode": "PAYMENT_PROCESSING_UNAVAILABLE"}))

    with pytest.raises(CheckoutRefusedError) as raised:
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
            max_attempts=3,
            backoff_seconds=0,
        )

    assert raised.value.error_code == "PAYMENT_PROCESSING_UNAVAILABLE"
    assert len(recorder.requests) == 3


def test_retry_helper_honours_a_caller_supplied_key(client, urlopen):
    recorder = urlopen((503, {}), _ok())

    client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key="my-own-key",
        max_attempts=2,
        backoff_seconds=0,
    )

    assert all(_headers(r)["idempotency-key"] == "my-own-key" for r in recorder.requests)


def test_retry_helper_does_not_retry_a_refusal(client, urlopen):
    recorder = urlopen((200, {"success": False, "errorCode": "ALREADY_PROCESSED"}))

    with pytest.raises(CheckoutRefusedError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert len(recorder.requests) == 1


def test_retry_helper_does_not_retry_an_auth_failure(client, urlopen):
    recorder = urlopen((401, {"errorCode": "INVALID_SIGNATURE"}))

    with pytest.raises(AuthenticationError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert len(recorder.requests) == 1


def test_retry_helper_gives_up_and_raises_the_transport_error(client, urlopen):
    recorder = urlopen(urllib.error.URLError("down"))

    with pytest.raises(TransportError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert len(recorder.requests) == 3


def test_retry_helper_sends_the_one_key_on_every_attempt(client, urlopen):
    recorder = urlopen((503, {}), (503, {}), _ok())

    client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        max_attempts=3,
        backoff_seconds=0,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert len(recorder.requests) == 3
    keys = {_headers(r)["idempotency-key"] for r in recorder.requests}
    assert keys == {IDEMPOTENCY_KEY}


@pytest.mark.parametrize("key", [None, ""], ids=["none", "empty"])
def test_retry_helper_refuses_to_start_without_a_key(client, urlopen, key):
    """It used to mint one. Now a missing key fails before the first attempt, like the
    plain create call, instead of hiding the choice of key from the caller."""
    recorder = urlopen(_ok())

    with pytest.raises(ValueError, match="idempotency_key is required"):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=key,
            max_attempts=3,
            backoff_seconds=0,
        )

    assert recorder.requests == []


# --- error codes and storefront refusals --------------------------------------


def _storefront_refusal(status, code):
    # The gateway's error envelope: the code at error.code, like every non-200 failure.
    return (
        status,
        {"success": False, "error": {"code": code, "message": "Storefront refused.", "statusCode": status}},
    )


def test_a_409_storefront_not_whitelisted_is_a_storefront_error_matchable_on_its_code(
    client, urlopen
):
    urlopen(_storefront_refusal(409, "STOREFRONT_NOT_WHITELISTED"))

    with pytest.raises(StorefrontError) as raised:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert raised.value.error_code == ErrorCode.STOREFRONT_NOT_WHITELISTED
    assert raised.value.error_code == "STOREFRONT_NOT_WHITELISTED"
    assert raised.value.http_status == 409
    assert str(raised.value) == "Storefront refused."


@pytest.mark.parametrize(
    "status, code",
    [(409, "STOREFRONT_NOT_WHITELISTED"), (409, "STOREFRONT_INACTIVE"), (400, "STOREFRONT_MISMATCH")],
)
def test_every_storefront_code_is_a_storefront_error_and_still_an_api_error(
    client, urlopen, status, code
):
    """Existing ``except ApiError`` handlers must keep catching these."""
    urlopen(_storefront_refusal(status, code))

    with pytest.raises(ApiError) as raised:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert isinstance(raised.value, StorefrontError)
    assert not isinstance(raised.value, CheckoutRefusedError)
    assert raised.value.error_code == code
    assert raised.value.http_status == status


def test_other_4xx_codes_stay_a_plain_api_error(client, urlopen):
    urlopen(_storefront_refusal(400, "IDEMPOTENCY_KEY_REQUIRED"))

    with pytest.raises(ApiError) as raised:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert not isinstance(raised.value, StorefrontError)


def test_a_storefront_mismatch_on_replay_is_a_refusal_carrying_the_code(client, urlopen):
    """The replay path answers 200 with success false, not 400."""
    urlopen(
        (
            200,
            {
                "success": False,
                "errorCode": "STOREFRONT_MISMATCH",
                "errorMessage": "This API key is bound to a different storefront than the request names.",
            },
        )
    )

    with pytest.raises(CheckoutRefusedError) as raised:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert raised.value.error_code == ErrorCode.STOREFRONT_MISMATCH


def test_the_retry_helper_does_not_retry_a_storefront_refusal(client, urlopen):
    recorder = urlopen(_storefront_refusal(409, "STOREFRONT_NOT_WHITELISTED"))

    with pytest.raises(StorefrontError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
            max_attempts=3,
            backoff_seconds=0,
        )

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "name",
    [
        "STOREFRONT_NOT_WHITELISTED",
        "STOREFRONT_INACTIVE",
        "STOREFRONT_MISMATCH",
        "ALREADY_PROCESSED",
        "PRIOR_ATTEMPT_FAILED",
        "DUPLICATE_REQUEST",
        "PAYMENT_PROCESSING_UNAVAILABLE",
        "IDEMPOTENCY_KEY_REUSED",
    ],
)
def test_the_checkout_codes_are_named_constants_equal_to_the_wire_string(name):
    assert getattr(ErrorCode, name) == name
    assert getattr(ErrorCode, name).value == name


def test_every_code_the_sdk_groups_has_a_named_constant():
    grouped = (
        AUTHORIZATION_ERROR_CODES
        + SESSION_REFUSAL_ERROR_CODES
        + VALIDATION_ERROR_CODES
        + STOREFRONT_ERROR_CODES
        + CHARGE_ERROR_CODES
        + REVOKE_ERROR_CODES
        + REFUND_ERROR_CODES
        + REFUND_FAILURE_CODES
    )
    assert set(grouped) <= {member.value for member in ErrorCode}


def test_storefront_codes_are_not_counted_as_session_refusals():
    """SESSION_REFUSAL_ERROR_CODES is pinned to the gateway's 200 set; these are 409/400."""
    assert not set(STOREFRONT_ERROR_CODES) & set(SESSION_REFUSAL_ERROR_CODES)


# --- redirects ---------------------------------------------------------------


FORGED = {
    "success": True,
    "checkout": dict(CHECKOUT, cashierKey="ck_ATTACKER", cashierToken="ct_ATTACKER"),
}


class _Redirect:
    """A 3xx as it arrives from the transport, before any handler has seen it.

    The body is what an attacker's proxy would answer with: a complete, plausible
    checkout session the merchant would hand straight to a payer.
    """

    def __init__(self, code, location="https://attacker.test/steal"):
        self.code = code
        self.status = code
        self.msg = "redirect"
        self.url = BASE_URL + SESSIONS_PATH
        self.headers = email.message_from_string("Location: " + location + "\n")
        self._body = io.BytesIO(json.dumps(FORGED).encode())

    def info(self):
        return self.headers

    def read(self, *args):
        return self._body.read(*args)

    def close(self):
        pass


def _redirecting_transport(monkeypatch, code):
    """Answers every real HTTPS request with a 3xx, and counts the requests."""
    sent = []

    def https_open(_handler, request):
        sent.append(request)
        return _Redirect(code)

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    return sent


@pytest.mark.parametrize("code", [300, 301, 302, 303, 305, 307, 308])
def test_no_3xx_is_ever_accepted_as_a_session(client, monkeypatch, code):
    """No 3xx may come back as a checkout session, whoever answered it.

    301/302/303/307/308 are refused by the redirect handler, which is also what keeps
    the signed headers from being replayed at the Location host. 300 and 305 reach no
    redirect handler at all - urllib dispatches neither - so the 2xx gate is what stops
    those from being decoded into a session.
    """
    sent = _redirecting_transport(monkeypatch, code)

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.http_status == code
    assert caught.value.error_code in ("UNEXPECTED_REDIRECT", "UNEXPECTED_STATUS")
    assert "ATTACKER" not in str(caught.value)
    assert len(sent) == 1, "the redirect target must never be requested"
    assert sent[0].full_url == BASE_URL + SESSIONS_PATH


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_followable_redirects_are_refused_by_the_redirect_handler(
    client, monkeypatch, code
):
    """The codes urllib would otherwise follow, carrying the signed headers with them.

    308 is pinned because CPython only grew http_error_308 in 3.11, and the package
    supports 3.9.
    """
    _redirecting_transport(monkeypatch, code)

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == "UNEXPECTED_REDIRECT"


@pytest.mark.parametrize("code", [300, 305])
def test_undispatched_3xx_is_refused_by_the_success_gate(client, monkeypatch, code):
    """A 300 or 305 with a session-shaped body used to be returned as a real session.

    Nothing dispatches these to a redirect handler, so they arrive as an HTTPError with
    a JSON body and, without a positive 2xx gate, sail past the 4xx and 5xx branches.
    """
    _redirecting_transport(monkeypatch, code)

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == "UNEXPECTED_STATUS"


def test_the_success_gate_does_not_disturb_a_2xx(client, urlopen):
    """201 is still a real answer - the gate refuses non-2xx, not non-200."""
    urlopen((201, {"success": True, "checkout": CHECKOUT}))

    session = client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert session == CHECKOUT


def test_a_redirect_is_not_retried(client, monkeypatch):
    sent = _redirecting_transport(monkeypatch, 302)

    with pytest.raises(ApiError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert len(sent) == 1


# --- base_url must be https --------------------------------------------------


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.test/payments",
        "http://192.168.1.10:8080/payments",
        "http://localhost.attacker.test/payments",
        "http://127.0.0.1.attacker.test/payments",
        "ftp://api.example.test/payments",
        "//api.example.test/payments",
        "api.example.test/payments",
    ],
)
def test_rejects_a_base_url_that_is_not_https(base_url):
    """Plain http puts the key id and the signature on the wire for anyone to replay.

    The near-miss hosts matter: `localhost.attacker.test` is a real registerable name
    that a prefix or substring check would wave through.
    """
    with pytest.raises(ValueError, match="https"):
        DominaiteClient(KEY_ID, SECRET, base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8080/payments",
        "http://localhost/payments",
        "http://127.0.0.1:9000/payments",
        "http://[::1]:9000/payments",
        "https://api.example.test/payments",
        "https://localhost:8443/payments",
    ],
)
def test_accepts_https_and_plain_http_on_loopback(base_url):
    """Loopback has no wire to sniff and no certificate to be had - local dev must work."""
    assert DominaiteClient(KEY_ID, SECRET, base_url=base_url) is not None


def test_the_default_base_url_is_https():
    assert DEFAULT_BASE_URL.startswith("https://")
    assert DominaiteClient(KEY_ID, SECRET) is not None


# --- length limits count characters, not bytes -------------------------------


CYRILLIC_100 = "з" * 100


def test_a_100_character_cyrillic_order_reference_is_accepted(client, urlopen):
    """100 Cyrillic characters is 200 UTF-8 bytes. Counting bytes would refuse a
    reference the API accepts, and the caller would never get to hear the API say yes."""
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference=CYRILLIC_100,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    body = json.loads(recorder.last.data.decode("utf-8"))
    assert body["orderReference"] == CYRILLIC_100
    assert len(CYRILLIC_100) == 100
    assert len(CYRILLIC_100.encode("utf-8")) == 200


@pytest.mark.parametrize(
    "order_reference", ["z" * 101, "з" * 101], ids=["ascii", "cyrillic"]
)
def test_rejects_an_order_reference_past_the_character_limit(client, order_reference):
    with pytest.raises(ValueError, match="order_reference"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference=order_reference,
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_a_100_character_idempotency_key_is_accepted(client, urlopen):
    recorder = urlopen(_ok())
    key = "k" * 100

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=key,
    )

    assert _headers(recorder.last)["idempotency-key"] == key


def test_rejects_an_idempotency_key_past_the_character_limit(client):
    with pytest.raises(ValueError, match="idempotency_key"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key="k" * 101,
        )


@pytest.mark.parametrize(
    "key",
    ["заказ-1042", "café-1042", "order 1042", " order-1042", "order-1042\n", "order\t1042"],
    ids=["cyrillic", "latin-1", "inner-space", "leading-space", "newline", "tab"],
)
def test_rejects_an_idempotency_key_that_cannot_travel_as_a_header(client, urlopen, key):
    """The key is an HTTP header and part of the signature. Cyrillic crashes http.client
    outright, a Latin-1 letter goes out as one byte after being signed as two, and
    whitespace can be trimmed by anything in between. Refused locally, before sending."""
    recorder = urlopen(_ok())

    with pytest.raises(ValueError, match="idempotency_key"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=key,
        )

    assert recorder.requests == []


# --- rate limiting -----------------------------------------------------------


RATE_LIMITED_BODY = {
    "success": False,
    "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Too many requests."},
}


def test_429_raises_rate_limit_error_with_the_retry_after_seconds(client, urlopen):
    urlopen((429, RATE_LIMITED_BODY, {"Retry-After": "30"}))

    with pytest.raises(RateLimitError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.retry_after_seconds == 30
    assert caught.value.http_status == 429
    assert caught.value.error_code == "RATE_LIMIT_EXCEEDED"


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
        {"Retry-After": "soon"},
        {"Retry-After": "-5"},
    ],
    ids=["absent", "http-date", "garbage", "negative"],
)
def test_retry_after_seconds_is_none_when_the_api_did_not_give_a_number(
    client, urlopen, headers
):
    """None means "back off on your own schedule". The HTTP-date form is deliberately
    not translated: a date only means something against the server's clock, and the
    caller's clock is exactly what may be wrong."""
    urlopen((429, RATE_LIMITED_BODY, headers))

    with pytest.raises(RateLimitError) as caught:
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.retry_after_seconds is None


def test_a_rate_limit_is_still_catchable_as_an_api_error(client, urlopen):
    """RateLimitError subclasses ApiError, so handlers written before it existed keep
    catching 429s instead of letting them escape as something unhandled."""
    urlopen((429, RATE_LIMITED_BODY, {"Retry-After": "1"}))

    with pytest.raises(ApiError):
        client.get_status(TRANSACTION_ID)


def test_a_429_without_a_code_gets_the_gateways_rate_limit_code(client, urlopen):
    urlopen((429, {"success": False}, {"Retry-After": "7"}))

    with pytest.raises(RateLimitError) as caught:
        client.get_status(TRANSACTION_ID)

    assert caught.value.error_code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert caught.value.retry_after_seconds == 7


def test_a_429_that_is_not_json_is_still_a_rate_limit_with_its_retry_after(client, urlopen):
    """A proxy in front of the API can throttle with an HTML page. It is still a rate limit,
    not an unexplained failure, and its Retry-After still counts."""
    urlopen((429, b"<html><body>Too Many Requests</body></html>", {"Retry-After": "12"}))

    with pytest.raises(RateLimitError) as caught:
        client.get_status(TRANSACTION_ID)

    assert caught.value.http_status == 429
    assert caught.value.error_code == ErrorCode.RATE_LIMIT_EXCEEDED
    assert caught.value.retry_after_seconds == 12
    assert caught.value.result == {}


def test_a_charge_limit_429_keeps_its_own_code_and_the_body(client, urlopen):
    body = _gateway_error(429, "CHARGE_VELOCITY_EXCEEDED", "Too many charges on this card")
    body["error"]["params"] = {"windowSeconds": 86400, "limit": 10}
    urlopen((429, body, {"Retry-After": "3600"}))

    with pytest.raises(RateLimitError) as caught:
        client.charge_payment_method(
            "pm_0123456789abcdef0123456789abcdef",
            amount=1000,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == ErrorCode.CHARGE_VELOCITY_EXCEEDED
    assert caught.value.retry_after_seconds == 3600
    assert caught.value.result["error"]["params"] == {"windowSeconds": 86400, "limit": 10}


def test_a_rate_limit_is_never_retried_automatically(client, urlopen):
    """Answering "you are sending too much" with more traffic is how a brief spike
    becomes a sustained lockout. The caller owns the backoff."""
    recorder = urlopen((429, RATE_LIMITED_BODY, {"Retry-After": "1"}))

    with pytest.raises(RateLimitError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert len(recorder.requests) == 1


# --- response bodies are bounded ---------------------------------------------


def _oversized_body():
    return b'{"success":true,"padding":"' + b"A" * (MAX_RESPONSE_BYTES + 1) + b'"}'


def test_an_oversized_success_body_is_refused_as_a_transport_error(client, urlopen):
    """read() with no argument writes a blank cheque against this process's memory.
    Whatever is on the other end - a broken proxy, something hostile - must not be able
    to cash it."""
    urlopen((200, _oversized_body()))

    with pytest.raises(TransportError, match="limit"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_an_oversized_error_body_is_refused_too(client, urlopen):
    """The 4xx/5xx path reads a body as well, and it is the likelier one to be huge -
    an error page from something in front of the API rather than the API itself."""
    urlopen((503, _oversized_body()))

    with pytest.raises(TransportError, match="limit"):
        client.get_status(TRANSACTION_ID)


class _Endless:
    """A body that never ends: read(n) always has another n bytes for you.

    Only a bounded read gets out of this. An unbounded read() would sit here consuming
    memory until the process dies, which is the whole point of the cap - a body that is
    merely large is the mild version of this.
    """

    status = 200
    headers = None

    def read(self, amount=None):
        if amount is None:
            raise AssertionError(
                "read() must be called with a bound - an endless body never returns"
            )
        return b"A" * amount

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_read_is_bounded_not_merely_checked_afterwards(client, monkeypatch):
    """Pins the bound on the read itself, not just the size check after it.

    Reading the whole body and then measuring it also raises TransportError for an
    oversized response, so the oversized-body tests above pass either way. They do not
    notice that the bad body was buffered in full first.
    """
    _patch_opener(monkeypatch, lambda request, timeout=None: _Endless())

    with pytest.raises(TransportError, match="limit"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key=IDEMPOTENCY_KEY,
        )


def test_a_body_at_the_limit_is_still_read(client, urlopen):
    """The cap refuses what is over it, not what is near it."""
    padding = "B" * (MAX_RESPONSE_BYTES - 1000)
    payload = json.dumps({"success": True, "checkout": CHECKOUT, "padding": padding})
    assert len(payload.encode("utf-8")) <= MAX_RESPONSE_BYTES
    urlopen((200, payload.encode("utf-8")))

    session = client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert session == CHECKOUT


def test_a_short_reading_stream_is_still_read_to_the_end(client, monkeypatch):
    """A socket-backed stream may hand back fewer bytes than asked for without being at
    the end. A single read() call would silently truncate the JSON and report a non-JSON
    response."""
    payload = json.dumps({"success": True, "checkout": CHECKOUT}).encode("utf-8")

    class _Dribbles:
        def __init__(self):
            self._remaining = payload

        def read(self, amount=None):
            chunk, self._remaining = self._remaining[:7], self._remaining[7:]
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        status = 200
        headers = None

    _patch_opener(monkeypatch, lambda request, timeout=None: _Dribbles())

    session = client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=IDEMPOTENCY_KEY,
    )

    assert session == CHECKOUT


# --- stored payment methods ---------------------------------------------------

PAYMENT_METHOD_ID = "pm_0123456789abcdef0123456789abcdef"
CHARGE_PATH = PAYMENT_METHODS_PATH + "/" + PAYMENT_METHOD_ID + "/charges"
CHARGE_IDEMPOTENCY_KEY = "00000000-0000-4000-8000-000000000003"
CHARGE_BODY = '{"amount":2500,"currency":"EUR","orderReference":"order-1043"}'
CHARGE_VECTOR_SIGNATURE = "9ce9f54efa2533a46aa4493b97b56aeb657f41d6a18f1c008c7fd412029aebf9"
REVOKE_VECTOR_SIGNATURE = "9330100343c4b820504890a09829a193d5815ca39e92160fdfc13d320a802a02"
# The wire form of a placed charge: success=true, the charge under data, and no
# declineClass/declineCode keys at all (the gateway omits nulls).
CHARGE = {
    "chargeId": "ch_33333333333343338333333333333333",
    "status": "succeeded",
    "transactionId": "33333333-3333-4333-8333-333333333333",
}
CHARGE_RESULT = dict(CHARGE, declineClass=None, declineCode=None)


def _placed(charge=None):
    return (201, {"success": True, "data": charge if charge is not None else CHARGE})


def _charge(client, **overrides):
    params = dict(
        amount=2500,
        currency="EUR",
        order_reference="order-1043",
        idempotency_key=CHARGE_IDEMPOTENCY_KEY,
    )
    params.update(overrides)
    return client.charge_payment_method(PAYMENT_METHOD_ID, **params)


def test_save_card_is_sent_in_the_session_body_and_nowhere_else(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042", save_card=True,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    request = recorder.last
    assert json.loads(request.data)["saveCard"] is True
    headers = _headers(request)
    assert headers["x-signature"] == sign_request(
        SECRET, headers["x-timestamp"], "POST", SESSIONS_PATH,
        headers["idempotency-key"], request.data.decode("utf-8"),
    )


def test_save_card_is_omitted_when_not_passed(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(amount=2500, currency="EUR", order_reference="order-1042", idempotency_key=IDEMPOTENCY_KEY)

    assert "saveCard" not in json.loads(recorder.last.data)


@pytest.mark.parametrize(
    "integration, sent",
    [
        ("widget", "widget"),
        ("fields", "fields"),
        (CheckoutIntegration.FIELDS, "fields"),
        (CheckoutIntegration.WIDGET, "widget"),
    ],
)
def test_integration_is_sent_in_the_signed_session_body(client, urlopen, integration, sent):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042", integration=integration,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    request = recorder.last
    assert json.loads(request.data)["integration"] == sent
    headers = _headers(request)
    assert headers["x-signature"] == sign_request(
        SECRET, headers["x-timestamp"], "POST", SESSIONS_PATH,
        headers["idempotency-key"], request.data.decode("utf-8"),
    )


def test_integration_is_omitted_when_not_passed(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(amount=2500, currency="EUR", order_reference="order-1042", idempotency_key=IDEMPOTENCY_KEY)

    assert "integration" not in json.loads(recorder.last.data)


# --- verify-only, subscription sessions, extra fields ------------------------

AGREEMENT_ID = "agr_0123456789abcdef0123456789abcdef"
CONSENT = {"textVersion": "recurring-2026-09", "acceptedAt": "2026-10-11T09:30:00Z", "ip": "203.0.113.7"}


def _assert_signed(request, method, path):
    headers = _headers(request)
    body = request.data.decode("utf-8") if request.data is not None else ""
    assert headers["x-signature"] == sign_request(
        SECRET, headers["x-timestamp"], method, path, headers.get("idempotency-key", ""), body,
    )


def test_a_verify_only_session_sends_amount_0_and_returns_the_verify_only_session(client, urlopen):
    checkout = dict(CHECKOUT, amount=0, integration="widget", verifyOnly=True)
    recorder = urlopen((200, {"success": True, "checkout": checkout}))

    session = client.create_checkout_session(
        amount=0, currency="EUR", order_reference="verify-cus-42", verify_only=True,
        idempotency_key="verify-cus-42",
    )

    sent = json.loads(recorder.last.data)
    assert sent["amount"] == 0
    assert sent["verifyOnly"] is True
    _assert_signed(recorder.last, "POST", SESSIONS_PATH)
    assert session["verifyOnly"] is True


@pytest.mark.parametrize("verify_only", [None, False])
def test_amount_0_is_still_refused_before_sending_on_every_other_session(client, urlopen, verify_only):
    recorder = urlopen(_ok())

    with pytest.raises(ValueError):
        client.create_checkout_session(
            amount=0, currency="EUR", order_reference="order-1042", verify_only=verify_only,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert recorder.requests == []


@pytest.mark.parametrize("amount", [-1, True, 0.0, "0"])
def test_a_verify_only_amount_must_still_be_a_whole_number_of_minor_units(client, urlopen, amount):
    recorder = urlopen(_ok())

    with pytest.raises(ValueError):
        client.create_checkout_session(
            amount=amount, currency="EUR", order_reference="verify-cus-42", verify_only=True,
            idempotency_key="verify-cus-42",
        )

    assert recorder.requests == []


def test_a_subscription_session_sends_the_agreement_consent_and_location(client, urlopen):
    location = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e")
    checkout = dict(CHECKOUT, integration="widget", agreementId=AGREEMENT_ID)
    recorder = urlopen((200, {"success": True, "checkout": checkout}))

    session = client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042",
        agreement_id=AGREEMENT_ID, consent=CONSENT, location_id=location,
        idempotency_key=IDEMPOTENCY_KEY,
    )

    sent = json.loads(recorder.last.data)
    assert sent["agreementId"] == AGREEMENT_ID
    assert sent["consent"] == CONSENT
    assert sent["locationId"] == "6f1c2d3e-4a5b-4c6d-8e7f-901a2b3c4d5e"
    _assert_signed(recorder.last, "POST", SESSIONS_PATH)
    assert session["agreementId"] == AGREEMENT_ID


def test_the_new_session_fields_are_left_out_when_not_passed(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042", idempotency_key=IDEMPOTENCY_KEY,
    )

    sent = json.loads(recorder.last.data)
    for field in ("verifyOnly", "agreementId", "consent", "locationId"):
        assert field not in sent


def test_extra_session_fields_are_sent_signed_and_win_over_the_arguments(client, urlopen):
    """extra is how a caller sends a field this SDK version does not model yet. It goes
    out as given, inside the signed body, and replaces a field the arguments set."""
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042",
        extra={"newField": {"nested": [1, 2]}, "orderReference": "order-1042-b"},
        idempotency_key=IDEMPOTENCY_KEY,
    )

    sent = json.loads(recorder.last.data)
    assert sent["newField"] == {"nested": [1, 2]}
    assert sent["orderReference"] == "order-1042-b"
    assert sent["amount"] == 2500
    _assert_signed(recorder.last, "POST", SESSIONS_PATH)


def test_the_retrying_create_forwards_the_new_session_fields(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session_with_retry(
        amount=0, currency="EUR", order_reference="verify-cus-42", verify_only=True,
        extra={"newField": "x"}, idempotency_key="verify-cus-42", backoff_seconds=0,
    )

    sent = json.loads(recorder.last.data)
    assert sent["verifyOnly"] is True
    assert sent["newField"] == "x"


def test_a_subscription_session_refusal_is_a_checkout_refusal_with_its_code(client, urlopen):
    """The agreement codes of a subscription session come back as success=false on HTTP
    200, like every session refusal. A code this SDK does not list is still carried."""
    urlopen((200, {"success": True, "data": {
        "success": False, "checkout": None, "transactionId": TRANSACTION_ID,
        "errorCode": "AGREEMENT_SESSION_OPEN", "errorMessage": "A session for this agreement is open.",
    }}))

    with pytest.raises(CheckoutRefusedError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042",
            agreement_id=AGREEMENT_ID, idempotency_key=IDEMPOTENCY_KEY,
        )

    assert caught.value.error_code == "AGREEMENT_SESSION_OPEN"
    assert caught.value.transaction_id == TRANSACTION_ID


def test_get_status_passes_the_verify_only_and_agreement_fields_through(client, urlopen):
    status = {
        "transactionId": TRANSACTION_ID, "orderId": "ord_1", "status": "requires_capture",
        "amount": 0, "currency": "EUR", "isRecurring": False, "verifyOnly": True,
        "agreement": {"id": AGREEMENT_ID, "status": "pending"},
        "createdAt": "2026-10-11T09:30:00Z", "sequence": 0,
    }
    urlopen((200, {"success": True, "data": status}))

    result = client.get_status(TRANSACTION_ID)

    assert result == status


def test_get_status_passes_the_stored_payment_method_through_and_leaves_payment_method_a_string(client, urlopen):
    stored = {
        "id": PAYMENT_METHOD_ID, "brand": "visa", "last4": "4242",
        "expiryMonth": 12, "expiryYear": 2029, "status": "active",
    }
    urlopen((200, {"success": True, "data": {
        "transactionId": TRANSACTION_ID, "status": "succeeded",
        "paymentMethod": "card", "storedPaymentMethod": stored,
    }}))

    status = client.get_status(TRANSACTION_ID)
    # The gateway omits a null retiredReason on the wire; the SDK reads absent as None.
    assert status["storedPaymentMethod"] == dict(stored, retiredReason=None)
    # The gateway's own paymentMethod is a category string, not the card; it is not
    # typed by this SDK but it must not be mistaken for, or clobbered by, the card on file.
    assert status["paymentMethod"] == "card"


def test_get_status_normalises_an_unreported_brand_and_expiry_and_adds_no_key_without_a_card(client, urlopen):
    bare = {"transactionId": TRANSACTION_ID, "status": "pending", "amount": 2500, "currency": "EUR"}
    urlopen((200, {"success": True, "data": bare}))
    assert client.get_status(TRANSACTION_ID) == bare

    urlopen((200, {"success": True, "data": dict(bare, status="succeeded", storedPaymentMethod={"id": PAYMENT_METHOD_ID, "status": "active"})}))
    assert client.get_status(TRANSACTION_ID)["storedPaymentMethod"] == {
        "id": PAYMENT_METHOD_ID, "brand": None, "last4": None, "expiryMonth": None, "expiryYear": None, "status": "active",
        "retiredReason": None,
    }


@pytest.mark.parametrize("extra", [{"pspReference": "psp_123"}, {"pspReference": None}, {}], ids=["set", "null", "absent"])
def test_get_status_passes_the_psp_reference_through(client, urlopen, extra):
    data = dict({"transactionId": TRANSACTION_ID, "status": "succeeded"}, **extra)
    urlopen((200, {"success": True, "data": data}))

    status = client.get_status(TRANSACTION_ID)

    assert status == data
    assert status.get("pspReference") == extra.get("pspReference")


@pytest.mark.parametrize(
    "extra",
    [
        {"paymentMethod": "wallet", "walletType": "apple_pay"},
        {"paymentMethod": "wallet", "walletType": "paypal"},
        {"paymentMethod": "card", "walletType": None},
        {"paymentMethod": None, "walletType": None},
        {},
    ],
    ids=["wallet", "unknown-wallet", "card", "open", "absent"],
)
def test_get_status_passes_the_wallet_reporting_fields_through(client, urlopen, extra):
    data = dict({"transactionId": TRANSACTION_ID, "status": "succeeded"}, **extra)
    urlopen((200, {"success": True, "data": data}))

    status = client.get_status(TRANSACTION_ID)

    assert status == data
    assert status.get("paymentMethod") == extra.get("paymentMethod")
    assert status.get("walletType") == extra.get("walletType")


def test_charge_reproduces_the_charge_vector_end_to_end(client, urlopen, monkeypatch):
    recorder = urlopen(_placed())
    monkeypatch.setattr("dominaite.client.time.time", lambda: 1755302400)

    charge = _charge(client)

    assert charge == CHARGE_RESULT
    request = recorder.last
    headers = _headers(request)
    assert request.full_url == BASE_URL + CHARGE_PATH
    assert request.get_method() == "POST"
    assert request.data.decode("utf-8") == CHARGE_BODY
    assert headers["idempotency-key"] == CHARGE_IDEMPOTENCY_KEY
    assert headers["x-timestamp"] == "1755302400"
    assert headers["x-signature"] == CHARGE_VECTOR_SIGNATURE
    assert b"idempotency" not in request.data.lower()


def test_charge_sends_the_callers_key_and_the_description(client, urlopen):
    recorder = urlopen(_placed())

    _charge(client, description="Monthly plan")

    request = recorder.last
    headers = _headers(request)
    assert headers["idempotency-key"] == CHARGE_IDEMPOTENCY_KEY
    assert json.loads(request.data) == {
        "amount": 2500, "currency": "EUR", "orderReference": "order-1043",
        "description": "Monthly plan",
    }


def test_a_charge_for_an_agreement_sends_the_agreement_and_extra_fields_signed(client, urlopen):
    recorder = urlopen(_placed(dict(CHARGE, agreementId=AGREEMENT_ID, sequence=0)))

    _charge(client, agreement_id=AGREEMENT_ID, extra={"newField": True, "currency": "HUF"})

    request = recorder.last
    assert json.loads(request.data) == {
        "amount": 2500, "currency": "HUF", "orderReference": "order-1043",
        "agreementId": AGREEMENT_ID, "newField": True,
    }
    _assert_signed(request, "POST", CHARGE_PATH)


def test_a_charge_passes_the_agreement_id_and_sequence_through(client, urlopen):
    urlopen(_placed(dict(CHARGE, status="pending", agreementId=AGREEMENT_ID, sequence=0)))

    charge = _charge(client, agreement_id=AGREEMENT_ID)

    assert charge["agreementId"] == AGREEMENT_ID
    assert charge["sequence"] == 0


def test_a_402_without_a_decline_class_is_a_result_with_decline_class_none(client, urlopen):
    """The 402 carries a class only when the decline code alone decides it. Every other
    decline is classified later, so a missing class must never make the charge an error."""
    declined = dict(CHARGE, status="failed", declineCode="05")
    urlopen((402, {
        "success": False, "data": declined,
        "error": {"code": "CHARGE_DECLINED", "message": "The payment provider declined the charge.", "statusCode": 402},
    }))

    charge = _charge(client)

    assert charge["status"] == "failed"
    assert charge["declineClass"] is None
    assert charge["declineCode"] == "05"


def test_an_agreement_charge_refusal_is_a_charge_error_with_its_code(client, urlopen):
    body = _gateway_error(422, "AGREEMENT_PERIOD_ALREADY_CHARGED", "This period was already charged.")
    urlopen((422, body))

    with pytest.raises(ChargeError) as caught:
        _charge(client, agreement_id=AGREEMENT_ID)

    assert caught.value.http_status == 422
    assert caught.value.error_code == "AGREEMENT_PERIOD_ALREADY_CHARGED"
    assert caught.value.charge is None
    assert caught.value.result == body


def test_a_charge_under_an_unknown_agreement_is_an_api_error_404_with_its_code(client, urlopen):
    urlopen((404, _gateway_error(404, "AGREEMENT_NOT_FOUND", "Agreement not found.")))

    with pytest.raises(ApiError) as caught:
        _charge(client, agreement_id=AGREEMENT_ID)

    assert not isinstance(caught.value, ChargeError)
    assert caught.value.http_status == 404
    assert caught.value.error_code == "AGREEMENT_NOT_FOUND"


def test_a_402_decline_is_a_result_with_a_decline_class_not_an_exception(client, urlopen):
    declined = dict(CHARGE, status="failed", declineClass="soft_funds", declineCode="51")
    urlopen((402, {
        "success": False, "data": declined,
        "error": {"code": "CHARGE_DECLINED", "message": "The payment provider declined the charge.", "statusCode": 402},
    }))

    charge = _charge(client)

    assert charge == declined
    assert charge["status"] == "failed"
    assert charge["declineClass"] == "soft_funds"
    assert charge["declineCode"] == "51"


def test_a_200_durable_replay_of_a_placed_charge_is_a_result_too(client, urlopen):
    urlopen((200, {"success": True, "data": dict(CHARGE, status="pending")}))

    charge = _charge(client)

    assert charge["status"] == "pending"
    assert charge["chargeId"] == CHARGE["chargeId"]


def test_a_charge_answered_with_a_code_is_a_charge_error_keeping_code_status_and_data(client, urlopen):
    unknown = dict(CHARGE, status="pending")
    urlopen((502, {
        "success": False, "data": unknown,
        "error": {"code": "CHARGE_OUTCOME_UNKNOWN", "message": "The payment provider gave no verdict.", "statusCode": 502},
    }))

    with pytest.raises(ChargeError) as raised:
        _charge(client)

    error = raised.value
    assert not isinstance(error, TransportError), "a 502 with a code must not look retryable"
    assert error.http_status == 502
    assert error.error_code == "CHARGE_OUTCOME_UNKNOWN"
    assert str(error) == "The payment provider gave no verdict."
    assert error.charge == dict(unknown, declineClass=None, declineCode=None)
    assert error.transaction_id == CHARGE["transactionId"]


@pytest.mark.parametrize(
    "status, code",
    [
        (409, "PAYMENT_METHOD_NOT_ACTIVE"),
        (409, "DUPLICATE_REQUEST"),
        (422, "IDEMPOTENCY_KEY_REUSED"),
        (503, "PAYMENT_METHOD_CHARGES_DISABLED"),
        (503, "PAYMENT_PROCESSING_UNAVAILABLE"),
        (502, "CHARGE_FAILED"),
    ],
)
def test_a_charge_refused_without_a_row_is_a_charge_error_with_no_charge_attached(client, urlopen, status, code):
    urlopen((status, {"success": False, "error": {"code": code, "message": "refused", "statusCode": status}}))

    with pytest.raises(ChargeError) as raised:
        _charge(client)

    assert raised.value.http_status == status
    assert raised.value.error_code == code
    assert raised.value.charge is None
    assert raised.value.transaction_id is None


def test_a_5xx_without_a_code_on_the_charge_route_is_still_a_transport_error(client, urlopen):
    # A proxy or a crash answering instead of the gateway: nothing to branch on, so the
    # generic rule stands and the caller retries with the same key.
    urlopen((503, {"success": False}))

    with pytest.raises(TransportError):
        _charge(client)


def test_a_charge_against_a_method_that_is_not_yours_is_an_api_error_404(client, urlopen):
    urlopen((404, {"success": False, "error": {"code": "PAYMENT_METHOD_NOT_FOUND", "message": "No stored payment method with this id."}}))

    with pytest.raises(ApiError) as raised:
        _charge(client)

    assert not isinstance(raised.value, ChargeError)
    assert raised.value.http_status == 404
    assert raised.value.error_code == "PAYMENT_METHOD_NOT_FOUND"


def test_a_2xx_without_a_charge_body_is_an_api_error_not_a_half_built_charge(client, urlopen):
    urlopen((201, {"success": True}))

    with pytest.raises(ApiError) as raised:
        _charge(client)

    assert raised.value.http_status == 201


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": 25.5},
        {"amount": 0},
        {"amount": True},
        {"order_reference": ""},
        {"description": 7},
        {"idempotency_key": ""},
        {"idempotency_key": None},
    ],
)
def test_charge_validates_money_params_like_a_session_does(client, urlopen, overrides):
    recorder = urlopen(_placed())

    with pytest.raises(ValueError):
        _charge(client, **overrides)

    assert recorder.requests == []


@pytest.mark.parametrize(
    "bad", ["", " ", "pm_1/charges", "pm_1?x=1", "pm_1#f", "pm 1", "pm_1%2F", "p" * 101, None]
)
def test_a_payment_method_id_that_would_not_stay_one_path_segment_is_refused(client, urlopen, bad):
    recorder = urlopen(_placed())

    with pytest.raises(ValueError):
        client.charge_payment_method(
            bad,
            amount=2500,
            currency="EUR",
            order_reference="o",
            idempotency_key=CHARGE_IDEMPOTENCY_KEY,
        )
    with pytest.raises(ValueError):
        client.revoke_payment_method(bad)

    assert recorder.requests == []


def test_revoke_reproduces_the_revoke_vector_and_returns_none_on_204(client, urlopen, monkeypatch):
    recorder = urlopen((204, b""))
    monkeypatch.setattr("dominaite.client.time.time", lambda: 1755302400)

    assert client.revoke_payment_method(PAYMENT_METHOD_ID) is None

    request = recorder.last
    headers = _headers(request)
    assert request.full_url == BASE_URL + PAYMENT_METHODS_PATH + "/" + PAYMENT_METHOD_ID
    assert request.get_method() == "DELETE"
    assert request.data is None
    assert "idempotency-key" not in headers
    assert headers["x-signature"] == REVOKE_VECTOR_SIGNATURE


def test_revoke_surfaces_a_404_as_api_error_and_a_coded_502_503_as_revoke_error(client, urlopen):
    urlopen((404, {"success": False, "error": {"code": "VALIDATION_ERROR", "message": "Validation failed", "statusCode": 404}}))
    with pytest.raises(ApiError) as raised:
        client.revoke_payment_method(PAYMENT_METHOD_ID)
    assert not isinstance(raised.value, RevokeError)
    assert raised.value.http_status == 404

    for status, code in [(503, "MERCHANT_API_UNAVAILABLE"), (502, "UPSTREAM_CONTRACT_ERROR")]:
        urlopen((status, {"success": False, "error": {"code": code, "message": "nothing changed", "statusCode": status}}))
        with pytest.raises(RevokeError) as refused:
            client.revoke_payment_method(PAYMENT_METHOD_ID)
        assert not isinstance(refused.value, TransportError)
        assert refused.value.http_status == status
        assert refused.value.error_code == code
        assert str(refused.value) == "nothing changed"

    # No code to branch on (a proxy answering instead of the gateway): the generic rule stands.
    urlopen((503, {"success": False}))
    with pytest.raises(TransportError):
        client.revoke_payment_method(PAYMENT_METHOD_ID)


def test_revoke_returns_none_on_a_204_for_an_already_revoked_method_too(client, urlopen):
    recorder = urlopen((204, b""))
    assert client.revoke_payment_method(PAYMENT_METHOD_ID) is None
    assert client.revoke_payment_method(PAYMENT_METHOD_ID) is None
    assert len(recorder.requests) == 2


# --- refunds -----------------------------------------------------------------


REFUND_ID = "re_7c1e9a2b4d6f48a0b3c5d7e9f1a2b3c4"
REFUND_KEY = "refund-cn-5521"
REFUNDS_PATH = PAYMENTS_PATH + "/" + TRANSACTION_ID + "/refunds"
# The wire form of a queued partial refund: the gateway omits the null fields.
QUEUED_REFUND = {
    "refundId": REFUND_ID,
    "transactionId": TRANSACTION_ID,
    "status": "pending",
    "amount": 1500,
    "currency": "HUF",
}
NULL_REFUND_FIELDS = {"failureCode": None, "failureMessage": None, "completedAt": None}


def _accepted(refund=None):
    return (202, {"success": True, "data": refund if refund is not None else QUEUED_REFUND})


def _refund_error(status, code):
    return (status, {"success": False, "error": {"code": code, "message": "refused: " + code, "statusCode": status}})


def test_create_refund_sends_the_key_in_the_header_and_the_signature(client, urlopen):
    recorder = urlopen(_accepted())

    refund = client.create_refund(TRANSACTION_ID, amount=1500, idempotency_key=REFUND_KEY)

    request = recorder.last
    headers = _headers(request)
    assert request.get_method() == "POST"
    assert request.full_url == BASE_URL + REFUNDS_PATH
    assert request.data.decode("utf-8") == '{"amount":1500}'
    assert headers["idempotency-key"] == REFUND_KEY
    assert headers["x-signature"] == sign_request(
        SECRET, headers["x-timestamp"], "POST", REFUNDS_PATH, REFUND_KEY, '{"amount":1500}'
    )
    assert refund == dict(QUEUED_REFUND, **NULL_REFUND_FIELDS)
    assert refund["status"] == RefundStatus.PENDING


def test_a_full_refund_sends_no_amount_key_at_all(client, urlopen):
    recorder = urlopen(_accepted(dict(QUEUED_REFUND, amount=None)))

    refund = client.create_refund(TRANSACTION_ID, idempotency_key=REFUND_KEY)

    assert recorder.last.data == b"{}"
    assert refund["amount"] is None

    client.create_refund(TRANSACTION_ID, reason="Returned in store", idempotency_key=REFUND_KEY)
    assert json.loads(recorder.last.data) == {"reason": "Returned in store"}


def test_create_refund_signs_the_lowercase_transaction_id_path(client, urlopen):
    recorder = urlopen(_accepted())

    client.create_refund(LETTERED_TRANSACTION_ID.upper(), amount=1500, idempotency_key=REFUND_KEY)

    path = PAYMENTS_PATH + "/" + LETTERED_TRANSACTION_ID + "/refunds"
    headers = _headers(recorder.last)
    assert recorder.last.full_url == BASE_URL + path
    assert headers["x-signature"] == sign_request(
        SECRET, headers["x-timestamp"], "POST", path, REFUND_KEY, '{"amount":1500}'
    )


@pytest.mark.parametrize("key", [None, ""])
def test_create_refund_refuses_to_send_without_a_key(client, urlopen, key):
    recorder = urlopen(_accepted())
    with pytest.raises(ValueError, match="idempotency_key is required"):
        client.create_refund(TRANSACTION_ID, amount=1500, idempotency_key=key)
    assert recorder.requests == []


@pytest.mark.parametrize("amount", [0, -1, True, 15.0, "1500"])
def test_create_refund_refuses_an_amount_that_is_not_positive_minor_units(client, urlopen, amount):
    recorder = urlopen(_accepted())
    with pytest.raises(ValueError, match="MINOR"):
        client.create_refund(TRANSACTION_ID, amount=amount, idempotency_key=REFUND_KEY)
    assert recorder.requests == []


def test_create_refund_refuses_a_transaction_id_that_is_not_a_uuid(client, urlopen):
    recorder = urlopen(_accepted())
    with pytest.raises(ValueError, match="UUID"):
        client.create_refund("../ping", idempotency_key=REFUND_KEY)
    assert recorder.requests == []


def test_get_refund_signs_an_empty_key_and_an_empty_body(client, urlopen):
    succeeded = dict(QUEUED_REFUND, status="succeeded", completedAt="2026-09-26T10:05:40Z")
    recorder = urlopen((200, {"success": True, "data": succeeded}))

    refund = client.get_refund(TRANSACTION_ID, REFUND_ID)

    request = recorder.last
    headers = _headers(request)
    path = REFUNDS_PATH + "/" + REFUND_ID
    assert request.get_method() == "GET"
    assert request.full_url == BASE_URL + path
    assert request.data is None
    assert "idempotency-key" not in headers
    assert headers["x-signature"] == sign_request(SECRET, headers["x-timestamp"], "GET", path, "", "")
    assert refund == dict(succeeded, failureCode=None, failureMessage=None)
    assert refund["status"] == RefundStatus.SUCCEEDED


def test_get_refund_reads_a_failed_refund_with_no_amount_and_its_failure_code(client, urlopen):
    failed = {
        "refundId": REFUND_ID,
        "transactionId": TRANSACTION_ID,
        "status": "failed",
        "currency": "HUF",
        "failureCode": "REFUND_FAILED",
        "failureMessage": "The refund could not be completed.",
        "completedAt": "2026-09-26T10:05:41Z",
    }
    urlopen((200, {"success": True, "data": failed}))

    refund = client.get_refund(TRANSACTION_ID, REFUND_ID)

    assert refund == dict(failed, amount=None)
    assert refund["status"] == RefundStatus.FAILED
    assert refund["failureCode"] in REFUND_FAILURE_CODES


@pytest.mark.parametrize("bad", ["", "re_1/../x", "re 1", "re_1?x=1"])
def test_get_refund_refuses_a_refund_id_that_would_not_stay_one_path_segment(client, urlopen, bad):
    recorder = urlopen((200, {"success": True, "data": QUEUED_REFUND}))
    with pytest.raises(ValueError):
        client.get_refund(TRANSACTION_ID, bad)
    assert recorder.requests == []


@pytest.mark.parametrize(
    "status, code, retry_stop_after_seconds",
    [
        (404, "PAYMENT_NOT_FOUND", None),
        (404, "REFUND_NOT_FOUND", 60),
        (422, "PAYMENT_NOT_REFUNDABLE", None),
        (422, "REFUND_AMOUNT_EXCEEDED", None),
        (422, "IDEMPOTENCY_KEY_REUSED", None),
        (409, "DUPLICATE_REQUEST", 120),
        (400, "IDEMPOTENCY_KEY_REQUIRED", None),
    ],
)
@pytest.mark.parametrize("call", ["create", "get"])
def test_every_refund_code_is_a_refund_error_with_its_retry_classification(
    client, urlopen, status, code, retry_stop_after_seconds, call
):
    urlopen(_refund_error(status, code))

    with pytest.raises(RefundError) as raised:
        if call == "create":
            client.create_refund(TRANSACTION_ID, amount=1500, idempotency_key=REFUND_KEY)
        else:
            client.get_refund(TRANSACTION_ID, REFUND_ID)

    error = raised.value
    assert isinstance(error, ApiError)
    assert not isinstance(error, TransportError)
    assert error.http_status == status
    assert error.error_code == code
    assert error.retryable is (retry_stop_after_seconds is not None)
    assert error.retry_stop_after_seconds == retry_stop_after_seconds
    assert str(error) == "refused: " + code
    assert error.result["error"]["code"] == code


def test_the_refund_error_cases_cover_every_code_the_sdk_claims():
    assert set(REFUND_ERROR_CODES) == {
        "PAYMENT_NOT_FOUND", "REFUND_NOT_FOUND", "PAYMENT_NOT_REFUNDABLE", "REFUND_AMOUNT_EXCEEDED",
        "IDEMPOTENCY_KEY_REUSED", "DUPLICATE_REQUEST", "IDEMPOTENCY_KEY_REQUIRED",
    }
    # A failed refund is a result, not an exception.
    assert "REFUND_FAILED" not in REFUND_ERROR_CODES


@pytest.mark.parametrize("outcome", [(500, {"success": False}), _refund_error(503, "MERCHANT_API_UNAVAILABLE")])
def test_a_5xx_on_a_refund_is_a_transport_error_retry_with_the_same_key(client, urlopen, outcome):
    urlopen(outcome)
    with pytest.raises(TransportError):
        client.create_refund(TRANSACTION_ID, amount=1500, idempotency_key=REFUND_KEY)


def test_a_2xx_without_a_refund_body_is_an_api_error_not_a_half_built_refund(client, urlopen):
    urlopen((202, {"success": True, "data": {}}))
    with pytest.raises(ApiError) as raised:
        client.create_refund(TRANSACTION_ID, idempotency_key=REFUND_KEY)
    assert raised.value.error_code == "UNEXPECTED_RESPONSE"


def test_create_refund_sends_extra_fields_signed(client, urlopen):
    recorder = urlopen(_accepted())

    client.create_refund(
        TRANSACTION_ID, amount=1500, idempotency_key=REFUND_KEY, extra={"newField": "x"},
    )

    request = recorder.last
    assert json.loads(request.data) == {"amount": 1500, "newField": "x"}
    _assert_signed(request, "POST", REFUNDS_PATH)


# --- refund list -------------------------------------------------------------

REFUND_LIST = {
    "transactionId": TRANSACTION_ID,
    "currency": "HUF",
    "refundedAmount": 2000,
    "refundableAmount": 500,
    "refunds": [
        dict(QUEUED_REFUND, status="succeeded", completedAt="2026-09-26T10:05:40Z"),
        {"refundId": "re_0123456789abcdef0123456789abcdef", "transactionId": TRANSACTION_ID,
         "status": "failed", "currency": "HUF", "failureCode": "REFUND_FAILED"},
    ],
}


def test_list_refunds_signs_an_empty_key_and_body_and_gives_every_refund_one_shape(client, urlopen):
    recorder = urlopen((200, {"success": True, "data": REFUND_LIST}))

    listing = client.list_refunds(TRANSACTION_ID.upper())

    request = recorder.last
    headers = _headers(request)
    assert request.get_method() == "GET"
    assert request.full_url == BASE_URL + REFUNDS_PATH
    assert request.data is None
    assert "idempotency-key" not in headers
    assert headers["x-signature"] == sign_request(SECRET, headers["x-timestamp"], "GET", REFUNDS_PATH, "", "")
    assert listing["refundedAmount"] == 2000
    assert listing["refundableAmount"] == 500
    assert listing["currency"] == "HUF"
    assert listing["refunds"][0] == dict(REFUND_LIST["refunds"][0], failureCode=None, failureMessage=None)
    assert listing["refunds"][1]["amount"] is None
    assert listing["refunds"][1]["completedAt"] is None
    assert listing["refunds"][1]["failureCode"] == "REFUND_FAILED"


def test_list_refunds_of_a_payment_without_refunds_is_an_empty_list(client, urlopen):
    urlopen((200, {"success": True, "data": dict(REFUND_LIST, refundedAmount=0, refunds=[])}))

    assert client.list_refunds(TRANSACTION_ID)["refunds"] == []


def test_list_refunds_of_an_unknown_payment_is_a_refund_error_that_is_not_retryable(client, urlopen):
    urlopen(_refund_error(404, "PAYMENT_NOT_FOUND"))

    with pytest.raises(RefundError) as caught:
        client.list_refunds(TRANSACTION_ID)

    assert caught.value.http_status == 404
    assert caught.value.error_code == "PAYMENT_NOT_FOUND"
    assert caught.value.retryable is False


def test_list_refunds_on_a_503_is_a_transport_error(client, urlopen):
    urlopen(_refund_error(503, "MERCHANT_API_UNAVAILABLE"))

    with pytest.raises(TransportError):
        client.list_refunds(TRANSACTION_ID)


# --- integration contract ----------------------------------------------------


def test_the_integration_contract_is_read_without_a_signature(client, urlopen):
    """The route is anonymous. Sending no credentials keeps a success here from looking
    like a credential check, which is ping's job."""
    contract = {"sdks": ["python"], "recurring": {"consentTextVersions": ["recurring-2026-09"]}}
    recorder = urlopen((200, {"success": True, "data": contract}))

    result = client.get_integration_contract()

    request = recorder.last
    headers = _headers(request)
    assert request.get_method() == "GET"
    assert request.full_url == BASE_URL + CONTRACT_PATH
    assert request.data is None
    for header in ("x-api-key-id", "x-timestamp", "x-signature", "idempotency-key"):
        assert header not in headers
    assert result == contract


@pytest.mark.parametrize(
    "outcome, error",
    [
        ((429, RATE_LIMITED_BODY, {"Retry-After": "5"}), RateLimitError),
        ((503, {"success": False}), TransportError),
    ],
    ids=["429", "503"],
)
def test_the_integration_contract_keeps_the_shared_failure_rules(client, urlopen, outcome, error):
    urlopen(outcome)

    with pytest.raises(error):
        client.get_integration_contract()


# --- plans and agreements ----------------------------------------------------

PLAN_ID = "plan_0123456789abcdef0123456789abcdef"
PLAN = {
    "id": PLAN_ID,
    "name": "Pro monthly",
    "amount": 2500,
    "currency": "EUR",
    "intervalUnit": "month",
    "intervalCount": 1,
    "trialDays": 0,
    "status": "active",
    "createdAt": "2026-10-11T09:00:00Z",
}
AGREEMENT = {
    "id": AGREEMENT_ID,
    "planId": PLAN_ID,
    "customerReference": "cus-42",
    "status": "pending",
    "amount": 2500,
    "currency": "EUR",
    "intervalUnit": "month",
    "intervalCount": 1,
    "trialDays": 0,
    "version": 1,
    "sequence": 0,
    "createdAt": "2026-10-11T09:05:00Z",
}
#: Agreement consent names the merchant's own wording; session consent names the platform's.
AGREEMENT_CONSENT = {"textVersion": "terms-2026-09", "acceptedAt": "2026-10-11T09:04:00Z", "ip": "203.0.113.7"}


def _created(data):
    return (201, {"success": True, "data": data})


def _plan_arguments(**overrides):
    arguments = dict(
        name="Pro monthly", amount=2500, currency="EUR", interval_unit="month",
        idempotency_key="plan-pro-monthly",
    )
    arguments.update(overrides)
    return arguments


def test_create_plan_signs_the_key_and_sends_only_the_fields_given(client, urlopen):
    recorder = urlopen(_created(PLAN))

    plan = client.create_plan(**_plan_arguments())

    request = recorder.last
    assert request.get_method() == "POST"
    assert request.full_url == BASE_URL + PLANS_PATH
    assert _headers(request)["idempotency-key"] == "plan-pro-monthly"
    _assert_signed(request, "POST", PLANS_PATH)
    assert json.loads(request.data) == {
        "name": "Pro monthly", "amount": 2500, "currency": "EUR", "intervalUnit": "month",
    }
    assert plan == PLAN


def test_create_plan_sends_trial_days_0_and_every_optional_field(client, urlopen):
    recorder = urlopen(_created(PLAN))

    client.create_plan(**_plan_arguments(
        interval_count=3, period_count=4, trial_days=0, cancel_url="https://shop.example/cancel",
    ))

    assert json.loads(recorder.last.data) == {
        "name": "Pro monthly", "amount": 2500, "currency": "EUR", "intervalUnit": "month",
        "intervalCount": 3, "periodCount": 4, "trialDays": 0,
        "cancelUrl": "https://shop.example/cancel",
    }


def test_create_plan_sends_extra_fields_in_the_signed_body_and_they_win(client, urlopen):
    recorder = urlopen(_created(PLAN))

    client.create_plan(**_plan_arguments(
        interval_count=1, extra={"intervalCount": 2, "futureField": {"a": 1}},
    ))

    sent = json.loads(recorder.last.data)
    assert sent["intervalCount"] == 2
    assert sent["futureField"] == {"a": 1}
    _assert_signed(recorder.last, "POST", PLANS_PATH)


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": None},
        {"name": ""},
        {"amount": 0},
        {"amount": True},
        {"amount": 25.0},
        {"amount": "2500"},
        {"interval_count": 0},
        {"period_count": 1.5},
        {"trial_days": -1},
    ],
    ids=["no-key", "no-name", "zero-amount", "bool-amount", "float-amount", "string-amount",
         "zero-interval", "float-periods", "negative-trial"],
)
def test_create_plan_refuses_bad_input_before_any_request(client, urlopen, overrides):
    recorder = urlopen(_created(PLAN))

    with pytest.raises(ValueError):
        client.create_plan(**_plan_arguments(**overrides))

    assert recorder.requests == []


def test_list_plans_returns_every_plan_of_the_bare_list(client, urlopen):
    archived = dict(
        PLAN, id="plan_fedcba9876543210fedcba9876543210", status="archived",
        archivedAt="2026-10-01T00:00:00Z",
    )
    recorder = urlopen((200, {"success": True, "data": [PLAN, archived]}))

    plans = client.list_plans()

    request = recorder.last
    assert request.get_method() == "GET"
    assert request.full_url == BASE_URL + PLANS_PATH
    assert request.data is None
    assert "idempotency-key" not in _headers(request)
    _assert_signed(request, "GET", PLANS_PATH)
    assert plans == [PLAN, archived]


def test_list_plans_with_no_plans_is_an_empty_list(client, urlopen):
    urlopen((200, {"success": True, "data": []}))

    assert client.list_plans() == []


@pytest.mark.parametrize(
    "envelope",
    [{"success": True}, {"success": True, "data": PLAN}],
    ids=["no-data", "an-object"],
)
def test_list_plans_without_a_list_is_an_unexpected_response(client, urlopen, envelope):
    urlopen((200, envelope))

    with pytest.raises(ApiError) as caught:
        client.list_plans()

    assert caught.value.error_code == "UNEXPECTED_RESPONSE"
    assert caught.value.http_status == 200


def test_get_plan_signs_the_id_exactly_as_it_was_returned(client, urlopen):
    """Plan ids are case-sensitive, so the SDK never changes their case."""
    mixed_case = "plan_0123456789ABCDEF0123456789abcdef"
    recorder = urlopen((200, {"success": True, "data": dict(PLAN, id=mixed_case)}))

    plan = client.get_plan(mixed_case)

    path = PLANS_PATH + "/" + mixed_case
    assert recorder.last.get_method() == "GET"
    assert recorder.last.full_url == BASE_URL + path
    _assert_signed(recorder.last, "GET", path)
    assert plan["id"] == mixed_case


@pytest.mark.parametrize(
    "method, arguments",
    [
        ("get_plan", {}),
        ("update_plan", {"name": "Pro"}),
        ("delete_plan", {}),
        ("get_agreement", {}),
        ("cancel_agreement", {}),
    ],
)
@pytest.mark.parametrize(
    "bad_id",
    [None, "", "   ", "plan_1/archive", "plan_1?status=active", "plan 1"],
    ids=["none", "empty", "blank", "slash", "query", "space"],
)
def test_an_id_that_is_not_one_path_segment_is_refused_before_any_request(
    client, urlopen, method, arguments, bad_id
):
    recorder = urlopen((200, {"success": True, "data": PLAN}))

    with pytest.raises(ValueError):
        getattr(client, method)(bad_id, **arguments)

    assert recorder.requests == []


def test_update_plan_sends_no_idempotency_key_and_can_clear_the_trial_and_the_cancel_page(
    client, urlopen
):
    recorder = urlopen((200, {"success": True, "data": PLAN}))

    plan = client.update_plan(PLAN_ID, trial_days=0, cancel_url="")

    request = recorder.last
    path = PLANS_PATH + "/" + PLAN_ID
    assert request.get_method() == "PATCH"
    assert request.full_url == BASE_URL + path
    assert "idempotency-key" not in _headers(request)
    _assert_signed(request, "PATCH", path)
    assert json.loads(request.data) == {"trialDays": 0, "cancelUrl": ""}
    assert plan == PLAN


def test_update_plan_sends_only_the_fields_given(client, urlopen):
    recorder = urlopen((200, {"success": True, "data": PLAN}))

    client.update_plan(PLAN_ID, name="Pro monthly 2026", extra={"futureField": 1})

    assert json.loads(recorder.last.data) == {"name": "Pro monthly 2026", "futureField": 1}


def test_update_plan_with_nothing_to_change_is_refused_before_any_request(client, urlopen):
    recorder = urlopen((200, {"success": True, "data": PLAN}))

    with pytest.raises(ValueError):
        client.update_plan(PLAN_ID)

    assert recorder.requests == []


@pytest.mark.parametrize(
    "method, path",
    [
        ("delete_plan", PLANS_PATH + "/" + PLAN_ID),
        ("cancel_agreement", AGREEMENTS_PATH + "/" + AGREEMENT_ID),
    ],
    ids=["archive-plan", "cancel-agreement"],
)
def test_archive_and_cancel_sign_an_empty_key_and_body_and_return_none_on_204(
    client, urlopen, method, path
):
    recorder = urlopen((204, b""))

    assert getattr(client, method)(path.rsplit("/", 1)[1]) is None

    request = recorder.last
    assert request.get_method() == "DELETE"
    assert request.full_url == BASE_URL + path
    assert request.data is None
    assert "idempotency-key" not in _headers(request)
    _assert_signed(request, "DELETE", path)


def test_create_agreement_sends_the_card_the_maximum_and_the_consent_as_given(client, urlopen):
    recorded = dict(AGREEMENT_CONSENT, receivedAt="2026-10-11T09:05:00Z")
    recorder = urlopen(_created(dict(
        AGREEMENT, storedPaymentMethodId=PAYMENT_METHOD_ID, maxAmountPerPeriod=5000, consent=recorded,
    )))

    agreement = client.create_agreement(
        PLAN_ID, "cus-42", stored_payment_method_id=PAYMENT_METHOD_ID, max_amount_per_period=5000,
        consent=AGREEMENT_CONSENT, idempotency_key="agr-cus-42",
    )

    request = recorder.last
    assert request.get_method() == "POST"
    assert request.full_url == BASE_URL + AGREEMENTS_PATH
    assert _headers(request)["idempotency-key"] == "agr-cus-42"
    _assert_signed(request, "POST", AGREEMENTS_PATH)
    assert json.loads(request.data) == {
        "planId": PLAN_ID,
        "customerReference": "cus-42",
        "storedPaymentMethodId": PAYMENT_METHOD_ID,
        "maxAmountPerPeriod": 5000,
        "consent": AGREEMENT_CONSENT,
    }
    assert agreement["consent"] == recorded


def test_create_agreement_without_a_card_sends_only_the_plan_and_the_customer(client, urlopen):
    recorder = urlopen(_created(AGREEMENT))

    agreement = client.create_agreement(PLAN_ID, "cus-42", idempotency_key="agr-cus-42")

    assert json.loads(recorder.last.data) == {"planId": PLAN_ID, "customerReference": "cus-42"}
    assert agreement["status"] == "pending"
    assert "nextChargeAt" not in agreement


def test_create_agreement_sends_extra_fields_in_the_signed_body_and_they_win(client, urlopen):
    recorder = urlopen(_created(AGREEMENT))

    client.create_agreement(
        PLAN_ID, "cus-42", idempotency_key="agr-cus-42",
        extra={"customerReference": "cus-43", "futureField": True},
    )

    sent = json.loads(recorder.last.data)
    assert sent["customerReference"] == "cus-43"
    assert sent["futureField"] is True
    _assert_signed(recorder.last, "POST", AGREEMENTS_PATH)


@pytest.mark.parametrize(
    "overrides",
    [
        {"idempotency_key": None},
        {"plan_id": None},
        {"customer_reference": ""},
        {"max_amount_per_period": 0},
        {"max_amount_per_period": 50.0},
    ],
    ids=["no-key", "no-plan", "no-customer", "zero-maximum", "float-maximum"],
)
def test_create_agreement_refuses_bad_input_before_any_request(client, urlopen, overrides):
    recorder = urlopen(_created(AGREEMENT))
    arguments = dict(plan_id=PLAN_ID, customer_reference="cus-42", idempotency_key="agr-cus-42")
    arguments.update(overrides)

    with pytest.raises(ValueError):
        client.create_agreement(**arguments)

    assert recorder.requests == []


def test_get_agreement_returns_the_past_due_fields_the_gateway_sends(client, urlopen):
    past_due = dict(
        AGREEMENT, status="past_due", pastDueReason="card_expired",
        pastDueAt="2026-11-11T09:05:00Z", sequence=3,
    )
    recorder = urlopen((200, {"success": True, "data": past_due}))

    agreement = client.get_agreement(AGREEMENT_ID)

    path = AGREEMENTS_PATH + "/" + AGREEMENT_ID
    assert recorder.last.get_method() == "GET"
    assert recorder.last.full_url == BASE_URL + path
    _assert_signed(recorder.last, "GET", path)
    assert agreement == past_due


@pytest.mark.parametrize(
    "call, status, code",
    [
        (lambda c: c.get_plan(PLAN_ID), 404, "PLAN_NOT_FOUND"),
        (lambda c: c.delete_plan(PLAN_ID), 404, "PLAN_NOT_FOUND"),
        (lambda c: c.create_plan(**_plan_arguments()), 422, "IDEMPOTENCY_KEY_REUSED"),
        (lambda c: c.create_agreement(PLAN_ID, "cus-42", idempotency_key="agr-1"), 409, "PLAN_NOT_ACTIVE"),
        (
            lambda c: c.create_agreement(
                PLAN_ID, "cus-42", max_amount_per_period=5000, idempotency_key="agr-1",
            ),
            422,
            "AGREEMENT_CONSENT_REQUIRED",
        ),
        (lambda c: c.get_agreement(AGREEMENT_ID), 404, "AGREEMENT_NOT_FOUND"),
        (lambda c: c.cancel_agreement(AGREEMENT_ID), 409, "AGREEMENT_ENDED"),
    ],
    ids=["plan-not-found", "archive-not-found", "key-reused", "plan-not-active",
         "consent-required", "agreement-not-found", "agreement-ended"],
)
def test_a_plan_or_agreement_refusal_is_an_api_error_with_the_code_and_the_envelope(
    client, urlopen, call, status, code
):
    body = _gateway_error(status, code, "Refused")
    urlopen((status, body))

    with pytest.raises(ApiError) as caught:
        call(client)

    assert caught.value.http_status == status
    assert caught.value.error_code == code
    assert caught.value.result == body


def test_a_maximum_below_the_plan_amount_carries_the_minimum_in_the_result(client, urlopen):
    body = _gateway_error(422, "MAX_AMOUNT_BELOW_PLAN_AMOUNT", "maxAmountPerPeriod 2000 is below")
    body["error"]["validationErrors"] = [{
        "field": "maxAmountPerPeriod", "code": "MAX_AMOUNT_BELOW_PLAN_AMOUNT",
        "message": "maxAmountPerPeriod 2000 is below", "params": {"min": 2500},
    }]
    urlopen((422, body))

    with pytest.raises(ApiError) as caught:
        client.create_agreement(
            PLAN_ID, "cus-42", max_amount_per_period=2000, consent=AGREEMENT_CONSENT,
            idempotency_key="agr-cus-42",
        )

    assert caught.value.error_code == "MAX_AMOUNT_BELOW_PLAN_AMOUNT"
    assert caught.value.result["error"]["validationErrors"][0]["params"]["min"] == 2500


@pytest.mark.parametrize(
    "outcome, error",
    [
        ((429, RATE_LIMITED_BODY, {"Retry-After": "5"}), RateLimitError),
        (
            (403, _gateway_error(403, "INSUFFICIENT_SCOPE", "This route needs the payments scope.")),
            AuthenticationError,
        ),
        ((503, {"success": False}), TransportError),
    ],
    ids=["429", "403", "503"],
)
def test_the_plan_and_agreement_routes_keep_the_shared_failure_rules(client, urlopen, outcome, error):
    urlopen(outcome)

    with pytest.raises(error):
        client.list_plans()
