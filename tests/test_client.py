"""Contract tests for DominaiteClient: what it sends, and how it classifies answers."""

import email
import hashlib
import io
import json
import pickle
import pprint
import re
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from dominaite import (
    DEFAULT_BASE_URL,
    PING_PATH,
    SESSIONS_PATH,
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    DominaiteClient,
    RateLimitError,
    TransportError,
    sign_request,
)
from dominaite.client import MAX_RESPONSE_BYTES

KEY_ID = "dmk_0123456789abcdef0123456789abcdef"
SECRET = "dms_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
BASE_URL = "https://api.example.test/payments"
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
    _patch_opener(
        monkeypatch,
        lambda request, timeout=None: _Response(200, b"<html>502 Bad Gateway</html>"),
    )

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
            amount=2500, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.error_code == "UNEXPECTED_STATUS"


def test_the_success_gate_does_not_disturb_a_2xx(client, urlopen):
    """201 is still a real answer - the gate refuses non-2xx, not non-200."""
    urlopen((201, {"success": True, "checkout": CHECKOUT}))

    session = client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042"
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
        amount=2500, currency="EUR", order_reference=CYRILLIC_100
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
            amount=2500, currency="EUR", order_reference=order_reference
        )


def test_a_100_character_cyrillic_idempotency_key_is_accepted(client, urlopen):
    recorder = urlopen(_ok())

    client.create_checkout_session(
        amount=2500,
        currency="EUR",
        order_reference="order-1042",
        idempotency_key=CYRILLIC_100,
    )

    assert _headers(recorder.last)["idempotency-key"] == CYRILLIC_100


def test_rejects_an_idempotency_key_past_the_character_limit(client):
    with pytest.raises(ValueError, match="idempotency_key"):
        client.create_checkout_session(
            amount=2500,
            currency="EUR",
            order_reference="order-1042",
            idempotency_key="з" * 101,
        )


# --- rate limiting -----------------------------------------------------------


RATE_LIMITED_BODY = {
    "success": False,
    "error": {"code": "RATE_LIMIT_EXCEEDED", "message": "Too many requests."},
}


def test_429_raises_rate_limit_error_with_the_retry_after_seconds(client, urlopen):
    urlopen((429, RATE_LIMITED_BODY, {"Retry-After": "30"}))

    with pytest.raises(RateLimitError) as caught:
        client.create_checkout_session(
            amount=2500, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
        )

    assert caught.value.retry_after_seconds is None


def test_a_rate_limit_is_still_catchable_as_an_api_error(client, urlopen):
    """RateLimitError subclasses ApiError, so handlers written before it existed keep
    catching 429s instead of letting them escape as something unhandled."""
    urlopen((429, RATE_LIMITED_BODY, {"Retry-After": "1"}))

    with pytest.raises(ApiError):
        client.get_status(TRANSACTION_ID)


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
            amount=2500, currency="EUR", order_reference="order-1042"
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
            amount=2500, currency="EUR", order_reference="order-1042"
        )


def test_a_body_at_the_limit_is_still_read(client, urlopen):
    """The cap refuses what is over it, not what is near it."""
    padding = "B" * (MAX_RESPONSE_BYTES - 1000)
    payload = json.dumps({"success": True, "checkout": CHECKOUT, "padding": padding})
    assert len(payload.encode("utf-8")) <= MAX_RESPONSE_BYTES
    urlopen((200, payload.encode("utf-8")))

    session = client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042"
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
        amount=2500, currency="EUR", order_reference="order-1042"
    )

    assert session == CHECKOUT


# --- the version on the wire --------------------------------------------------


def test_the_user_agent_version_tracks_pyproject(client, urlopen):
    """One version, two homes: pyproject.toml and client.py's __version__ must move
    together, and the User-Agent header is where a stale one would ship."""
    from dominaite import __version__

    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
        "utf-8"
    )
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    assert match is not None, "pyproject.toml must declare [project] version"
    assert __version__ == match.group(
        1
    ), "__version__ in client.py must track pyproject.toml's version"

    recorder = urlopen(_ok())
    client.create_checkout_session(
        amount=2500, currency="EUR", order_reference="order-1042"
    )
    assert _headers(recorder.last)["user-agent"] == "dominaite-python/" + __version__
