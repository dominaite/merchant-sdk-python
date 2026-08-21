"""Contract tests for DominaiteClient: what it sends, and how it classifies answers."""

import email
import hashlib
import io
import json
import urllib.error
import urllib.request

import pytest

from dominaite import (
    PING_PATH,
    SESSIONS_PATH,
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    DominaiteClient,
    TransportError,
    sign_request,
)

KEY_ID = "dmk_0123456789abcdef0123456789abcdef"
SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.test/payments"
TRANSACTION_ID = "11111111-2222-4333-8444-555555555555"

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


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

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
        status, payload = outcome
        if status >= 400:
            raise urllib.error.HTTPError(
                request.full_url,
                status,
                "error",
                {},
                io.BytesIO(json.dumps(payload).encode("utf-8")),
            )
        return _Response(status, payload)

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
        == "95759958a0a0a9bd3e6e37101c01e8e7fee1166406e4ac2ff488764f5f742cbf"
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


def test_get_status_normalizes_transaction_id_casing(client, urlopen):
    recorder = urlopen((200, {"transactionId": TRANSACTION_ID}))

    client.get_status("  " + TRANSACTION_ID.upper() + "  ")

    assert recorder.last.full_url.endswith(SESSIONS_PATH + "/" + TRANSACTION_ID)


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
    )

    body = json.loads(recorder.last.data.decode("utf-8"))
    assert body["customer"] == {"firstName": "Ana", "email": "ana@example.com"}
    assert body["language"] == "bg"
    assert "theme" not in body
    assert "country" not in body
    assert "description" not in body


def test_generates_an_idempotency_key_when_none_is_given(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(amount=2500, currency="EUR", order_reference="o-1")
    client.create_checkout_session(amount=2500, currency="EUR", order_reference="o-2")

    first = _headers(recorder.requests[0])["idempotency-key"]
    second = _headers(recorder.requests[1])["idempotency-key"]
    assert first and second and first != second


# --- amounts -----------------------------------------------------------------


@pytest.mark.parametrize("amount", [25.0, "2500", 0, -1, True])
def test_amount_must_be_a_positive_integer_in_minor_units(client, amount):
    with pytest.raises(ValueError, match="MINOR units"):
        client.create_checkout_session(
            amount=amount, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.error_code == "DUPLICATE_REQUEST"
    assert caught.value.transaction_id == TRANSACTION_ID
    assert caught.value.result["errorCode"] == "DUPLICATE_REQUEST"


def test_refusal_without_a_transaction_id_leaves_it_none(client, urlopen):
    """The concurrent-race DUPLICATE_REQUEST knows the key is taken, but not by which row."""
    urlopen((200, {"success": False, "errorCode": "DUPLICATE_REQUEST"}))

    with pytest.raises(CheckoutRefusedError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.transaction_id is None


def test_503_raises_transport_not_refusal(client, urlopen):
    urlopen((503, {"errorCode": "MERCHANT_API_UNAVAILABLE"}))

    with pytest.raises(TransportError):
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )


def test_network_failure_raises_transport(client, urlopen):
    urlopen(urllib.error.URLError("connection reset"))

    with pytest.raises(TransportError):
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )


@pytest.mark.parametrize(
    "code",
    ["INVALID_API_KEY", "INVALID_SIGNATURE", "TIMESTAMP_OUT_OF_RANGE", "IP_NOT_ALLOWED"],
)
def test_401_raises_authentication_with_the_error_code(client, urlopen, code):
    urlopen((401, {"errorCode": code}))

    with pytest.raises(AuthenticationError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.error_code == code


def test_422_key_reuse_raises_api_error_not_transport(client, urlopen):
    urlopen((422, {"errorCode": "IDEMPOTENCY_KEY_REUSED", "errorMessage": "Use a fresh key"}))

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.http_status == 422


def test_non_json_response_raises_api_error(client, urlopen, monkeypatch):
    class _Garbage(_Response):
        def read(self):
            return b"<html>502 Bad Gateway</html>"

    _patch_opener(monkeypatch, lambda request, timeout=None: _Garbage(200, {}))

    with pytest.raises(ApiError):
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )


def test_envelope_wrapped_response_is_unwrapped(client, urlopen):
    urlopen((200, {"success": True, "data": {"success": True, "checkout": CHECKOUT}}))

    session = client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042"
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
    )

    assert session == CHECKOUT
    assert len(recorder.requests) == 2
    keys = {_headers(r)["idempotency-key"] for r in recorder.requests}
    assert len(keys) == 1, "a retry must not mint a new key - that is the double-charge bug"


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
        )

    assert len(recorder.requests) == 3


def test_retry_helper_mints_one_key_for_all_attempts_when_none_is_given(client, urlopen):
    recorder = urlopen((503, {}), (503, {}), _ok())

    client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(recorder.requests) == 3
    keys = {_headers(r)["idempotency-key"] for r in recorder.requests}
    assert len(keys) == 1


def test_retry_helper_treats_an_explicit_none_key_as_omitted(client, urlopen):
    """An explicit None used to slip past setdefault, so every attempt minted its own key.

    That turns one timed-out order into a second real payment, which is the whole thing
    this helper is for.
    """
    recorder = urlopen((503, {}), (503, {}), _ok())

    client.create_checkout_session_with_retry(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=None,
        max_attempts=3,
        backoff_seconds=0,
    )

    assert len(recorder.requests) == 3
    keys = {_headers(r)["idempotency-key"] for r in recorder.requests}
    assert len(keys) == 1, "a retry must not mint a new key - that is the double-charge bug"


# --- redirects ---------------------------------------------------------------


class _Redirect:
    """A 3xx as it arrives from the transport, before any handler has seen it."""

    def __init__(self, code, location="https://attacker.test/steal"):
        self.code = code
        self.status = code
        self.msg = "redirect"
        self.url = BASE_URL + SESSIONS_PATH
        self.headers = email.message_from_string("Location: " + location + "\n")
        self._body = io.BytesIO(json.dumps({"success": True, "checkout": CHECKOUT}).encode())

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


@pytest.mark.parametrize("code", [301, 302, 303, 307])
def test_redirects_are_refused_and_the_target_is_never_called(client, monkeypatch, code):
    """Following a 3xx would replay the signed headers at a host we never authenticated.

    On 301/302/303 the POST also degrades to a GET, so the target's own JSON would come
    back as a checkout session the merchant then hands to a payer.
    """
    sent = _redirecting_transport(monkeypatch, code)

    with pytest.raises(ApiError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.error_code == "UNEXPECTED_REDIRECT"
    assert caught.value.http_status == code
    assert len(sent) == 1, "the redirect target must never be requested"
    assert sent[0].full_url == BASE_URL + SESSIONS_PATH


def test_a_redirect_is_not_retried(client, monkeypatch):
    sent = _redirecting_transport(monkeypatch, 302)

    with pytest.raises(ApiError):
        client.create_checkout_session_with_retry(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            max_attempts=3,
            backoff_seconds=0,
        )

    assert len(sent) == 1
