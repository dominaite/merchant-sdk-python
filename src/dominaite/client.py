"""Server-side client for the Dominaite merchant API."""

import hashlib
import hmac
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, NamedTuple, Optional, Union

from .exceptions import (
    STOREFRONT_ERROR_CODES,
    ApiError,
    AuthenticationError,
    ChargeError,
    CheckoutRefusedError,
    DominaiteError,
    ErrorCode,
    RateLimitError,
    RevokeError,
    StorefrontError,
    TransportError,
)

__version__ = "0.3.0"

DEFAULT_BASE_URL = "https://api.dominaite.com/payments"
SESSIONS_PATH = "/merchant-api/checkout/sessions"
PAYMENT_METHODS_PATH = "/merchant-api/payment-methods"
PING_PATH = "/merchant-api/ping"
DEFAULT_TIMEOUT_SECONDS = 45.0  # serverless cold starts hit 10+s on dev; 15s was a coin flip

#: Hosts allowed to be addressed over plain http. Everything else must be https, so the
#: signed headers cannot be read off the wire.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Longest ``order_reference`` the API accepts, counted in Unicode code points.
MAX_ORDER_REFERENCE_LENGTH = 100

#: Longest ``Idempotency-Key`` the API accepts. The key is ASCII (see below), so
#: characters and bytes are the same count here.
MAX_IDEMPOTENCY_KEY_LENGTH = 100

#: The key travels as an HTTP header and is signed as UTF-8, so it is held to visible
#: ASCII. Outside that the request cannot be made to match on both ends: http.client
#: refuses anything past Latin-1 with a UnicodeEncodeError, sends Latin-1 letters as one
#: byte while the signature hashed two, and a proxy may trim surrounding whitespace.
_IDEMPOTENCY_KEY_RE = re.compile(r"[\x21-\x7e]+")

#: Hard cap on how much of a response body we will buffer. A well-behaved API answer is
#: a few kilobytes; anything past this is a misconfigured proxy or something hostile in
#: front of the API, and reading it to the end is how one bad response takes the process
#: down with it.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

_TRANSACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

#: A payment method id is opaque (``pm_...``), so this only pins what keeps it a single
#: path segment: no slash, no query, no whitespace, nothing that needs percent-encoding.
#: The id goes into the signed canonical path verbatim, so anything else would sign one
#: path and request another.
_PAYMENT_METHOD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

#: Statuses that stay generic on the payment-method routes even when the envelope
#: carries a code: validation (400), authentication (401, 403), unknown id (404) and
#: rate limiting (429) mean the same thing on every route and keep their usual errors.
#: Everything else with a code is the gateway telling this route something specific (a
#: decline, an unknown outcome, a refusal) and arrives as ChargeError / RevokeError.
_GENERIC_FAILURE_STATUSES = frozenset({400, 401, 403, 404, 429})


class _Reply(NamedTuple):
    """A parsed reply, whatever its status. Auth and rate-limit failures never get here."""

    status: int
    #: The whole JSON body ({} for a 204).
    envelope: Dict[str, Any]
    #: ``envelope["data"]`` when the gateway wrapped the answer, the envelope otherwise.
    payload: Dict[str, Any]
    #: ``envelope["error"]`` when present, {} otherwise.
    error: Dict[str, Any]


class _Secret:
    """Holds the API secret so that showing or serializing the client cannot leak it.

    ``repr`` and ``str`` redact, and pickling refuses outright. That covers the accidents:
    ``vars(client)`` in a debugger, ``json.dumps(vars(client), default=str)`` in a
    structured log line, ``pprint`` in a crash dump. :meth:`reveal` is the only way back
    to the real value, and signing is the only caller.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the real secret. Only for signing - never log or serialize this."""
        return self._value

    def __repr__(self) -> str:
        return "dms_***redacted***"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        # Refuses pickle and, through it, copy.deepcopy. The client is already
        # unpicklable because of its opener; this keeps the secret unpicklable on its
        # own, so a future picklable client cannot quietly start shipping it.
        raise TypeError("the Dominaite API secret cannot be pickled or copied")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses every 3xx instead of following it.

    The API never redirects, so a 3xx means something in front of it answered. Following
    it would replay the signed headers against a host we never authenticated, and a
    301/302/303 would downgrade the POST to a GET, so whatever JSON came back would be
    accepted as a real checkout session. Not retryable: this is a misdirected request,
    not a blip.

    This stops the header leak on the codes urllib dispatches here. The codes it does
    not dispatch (300, 305) never reach a redirect handler at all - the 2xx gate in
    ``_request`` is what refuses those.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ApiError(
            code,
            "Unexpected redirect response; the Dominaite API never redirects. "
            "Check base_url and anything proxying it.",
            error_code="UNEXPECTED_REDIRECT",
        )

    # CPython grew http_error_308 in 3.11; we support 3.9, where a 308 would otherwise
    # skip the handler entirely.
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_301


def _read_bounded(stream: Any) -> str:
    """Read a response body, refusing to buffer more than :data:`MAX_RESPONSE_BYTES`.

    ``read()`` with no argument hands whatever is on the other end a blank cheque against
    this process's memory. We ask for one byte more than the cap instead: if that byte
    arrives, the body is over the limit and we stop rather than finish reading it.

    An oversized body is reported as a :class:`TransportError`, alongside the other
    "the answer never really arrived" cases - it is safe to retry with the same
    idempotency key.
    """
    chunks = []
    remaining = MAX_RESPONSE_BYTES + 1
    while remaining > 0:
        # Loop rather than one read(): a socket-backed stream is allowed to hand back
        # fewer bytes than asked for without being at the end.
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)

    body = b"".join(chunks)
    if len(body) > MAX_RESPONSE_BYTES:
        raise TransportError(
            "The Dominaite API response exceeded the {0} byte limit and was not read; "
            "retry with the same idempotency key.".format(MAX_RESPONSE_BYTES)
        )
    return body.decode("utf-8", errors="replace")


def _retry_after_seconds(headers: Any) -> Optional[int]:
    """Read ``Retry-After`` as a whole number of seconds, or None.

    The header has a second, HTTP-date form. We do not translate it: a date only means
    something against the server's clock, and the caller's clock is exactly what might be
    wrong. None means "the API did not hand us a number of seconds" - back off on your
    own schedule.
    """
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter("Retry-After")
    if value is None:
        return None
    try:
        seconds = int(str(value).strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _require_secure_base_url(base_url: str) -> None:
    """Refuse a base_url that would put the signed headers on the wire in the clear.

    Every request carries the key id and a signature minted from the secret. Over plain
    http anyone on the path reads them, and can replay the request verbatim inside the
    server's 5 minute timestamp window. The only exception is loopback, where there is no
    wire and no https certificate to be had, so local development and test doubles work.

    Same failure mode as the other constructor checks: a ValueError at construction, not
    a surprise at the first call.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and (parsed.hostname or "") in LOOPBACK_HOSTS:
        return
    raise ValueError(
        "base_url must be https:// (http:// is allowed only for localhost, 127.0.0.1 "
        "and ::1); got {0!r}".format(base_url)
    )


def _validate_money_params(amount: Any, currency: Any, order_reference: Any) -> None:
    """The checks shared by every request that moves money."""
    # bool is a subclass of int in Python, so True would otherwise sail through
    # and get serialized as `true`.
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValueError(
            "amount must be a positive integer in MINOR units (e.g. 2500 for 25.00 EUR)"
        )
    if not currency:
        raise ValueError("currency is required")
    if not order_reference:
        raise ValueError("order_reference is required")
    # Characters, not bytes. len() on a str counts Unicode code points, which is what
    # the API's own limit counts - measuring len(s.encode("utf-8")) instead would
    # refuse a 100-character Cyrillic or Greek reference the platform accepts, and
    # the caller would never see the API say yes.
    #
    # Caveat: the server counts UTF-16 units, so a character outside the Basic
    # Multilingual Plane (emoji, rarer CJK) counts as one here and two there. Those
    # are vanishingly rare in an order id; the server stays the final arbiter, and
    # this check exists to catch the ordinary mistake locally, not to mirror the
    # server exactly.
    if len(order_reference) > MAX_ORDER_REFERENCE_LENGTH:
        raise ValueError(
            "order_reference must be at most {0} characters".format(
                MAX_ORDER_REFERENCE_LENGTH
            )
        )


def _normalize_idempotency_key(idempotency_key: Optional[str]) -> str:
    # Required, and never minted here. A random key per call is the double-payment bug:
    # the customer who reloads or presses back gets a second session for the same order.
    # Only the caller knows what "the same payment" is, so only the caller can name it.
    if idempotency_key is None or idempotency_key == "":
        raise ValueError(
            "idempotency_key is required. Derive it from the order, e.g. "
            "order_idempotency_key('checkout', order_id, amount, currency)"
        )
    key = idempotency_key
    if (
        not isinstance(key, str)
        or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH
        or not _IDEMPOTENCY_KEY_RE.fullmatch(key)
    ):
        raise ValueError(
            "idempotency_key must be 1 to {0} visible ASCII characters (no spaces, "
            "no accented or non-Latin letters)".format(MAX_IDEMPOTENCY_KEY_LENGTH)
        )
    return key


def order_idempotency_key(
    scope: str, order_id: Union[str, int], amount_minor: int, currency: str
) -> str:
    """Build an idempotency key from the order it pays for.

    Returns ``"{scope}-{order_id}-{amount_minor}-{CURRENCY}"``, e.g.
    ``order_idempotency_key("checkout", "1042", 2500, "eur")`` is
    ``"checkout-1042-2500-EUR"``.

    Why derive it instead of generating one: the same order at the same amount always
    yields the same key, so a reload, a back button or a retry after a timeout replays
    the session the customer already has instead of opening a second one. When the
    amount or currency changes (a coupon, an edited basket), the key changes with it and
    the gateway opens a fresh session, rather than refusing the new amount with
    ``IDEMPOTENCY_KEY_REUSED``.

    :param scope: A fixed label for the kind of payment, so keys for different flows on
        the same order cannot collide (``"checkout"``, ``"deposit"``, ``"sub-2026-10"``).
    :param order_id: Your order id. ASCII only: the key is an HTTP header.
    :param amount_minor: The amount in MINOR units, the same integer you send as
        ``amount``.
    :param currency: ISO 4217 code; upper-cased for you.
    :raises ValueError: An empty part, a non-integer amount, a currency that is not
        three letters, or a result the API would refuse as a key (over 100 characters,
        or anything outside visible ASCII).
    """
    if not isinstance(scope, str) or not scope:
        raise ValueError("scope must be a non-empty string")
    # bool is an int; str(True) would put "True" in the key.
    if isinstance(order_id, bool) or not isinstance(order_id, (str, int)) or order_id == "":
        raise ValueError("order_id must be a non-empty string or an integer")
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int) or amount_minor <= 0:
        raise ValueError(
            "amount_minor must be a positive integer in MINOR units (e.g. 2500 for 25.00 EUR)"
        )
    code = currency.upper() if isinstance(currency, str) else ""
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ValueError("currency must be a three-letter ISO 4217 code")
    return _normalize_idempotency_key(
        "{0}-{1}-{2}-{3}".format(scope, order_id, amount_minor, code)
    )


def _normalize_payment_method_id(payment_method_id: Any) -> str:
    normalized = str(payment_method_id or "").strip()
    if not _PAYMENT_METHOD_ID_RE.match(normalized):
        raise ValueError(
            "payment_method_id must be the storedPaymentMethod id from get_status()"
        )
    return normalized


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    idempotency_key: str,
    body: str,
) -> str:
    """Build the ``X-Signature`` value for one request.

    Lowercase hex HMAC-SHA256 over
    ``"{timestamp}\\n{METHOD}\\n{path}\\n{idempotencyKey}\\n{sha256hex(body)}"``.

    The idempotency key is INSIDE the signature, so a captured request cannot be
    replayed with a different key to mint extra sessions. The server rejects
    timestamps more than 5 minutes off - keep your server clock on NTP.

    Exposed so you can pin it in your own test against the published vector.
    """
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    payload = "\n".join([timestamp, method.upper(), path, idempotency_key, body_hash])
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class DominaiteClient:
    """Server-side client for the Dominaite merchant API.

    Keep your API secret on the server. Never ship it to a browser, never commit it,
    never log it. Card details never touch your backend or this SDK - the payer enters
    them inside the hosted checkout widget.

    Usage::

        client = DominaiteClient(os.environ["DOMINAITE_KEY_ID"], os.environ["DOMINAITE_SECRET"])
        session = client.create_checkout_session(
            amount=2500,              # minor units: 25.00 EUR
            currency="EUR",
            order_reference="order-1042",
            customer={"firstName": "Ana", "lastName": "K", "email": "ana@example.com"},
            idempotency_key=order_idempotency_key("checkout", "1042", 2500, "EUR"),
        )
        # Hand session["cashierKey"] + session["cashierToken"] to the embed snippet.
    """

    def __init__(
        self,
        key_id: str,
        secret: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        :param key_id: Your API key id (``dmk_...``), from the Dominaite dashboard.
        :param secret: Your API secret (``dms_...``). Server-side only.
        :param base_url: Override for non-production environments. Must be ``https://``
            unless the host is localhost, 127.0.0.1 or ::1.
        :param timeout: Per-request socket timeout in seconds.
        """
        if not key_id.startswith("dmk_"):
            raise ValueError("key_id must start with dmk_")
        if not secret.startswith("dms_"):
            raise ValueError("secret must start with dms_")
        _require_secure_base_url(base_url)
        self._key_id = key_id
        self._secret = _Secret(secret)
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        # Own opener rather than urlopen(): the default one follows redirects, and the
        # signed headers must never leave the host we addressed.
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    def __repr__(self) -> str:
        # Explicit, so that a later convenience repr (or a dataclass conversion) cannot
        # reopen the display path by falling back to one that prints every attribute.
        return "DominaiteClient(key_id={0!r}, base_url={1!r}, secret={2})".format(
            self._key_id, self._base_url, self._secret
        )

    def ping(self) -> Dict[str, Any]:
        """Check your credentials, your signing and your clock without creating anything.

        Make this your first live call. It isolates the setup problems from the payment
        ones: a failure here is the key id, the secret, the signing or the clock, and
        never anything about the payment you were about to create.

        :returns: ``{"pong", "merchantId", "serverTime", "serverUnixTime",
            "clockSkewSeconds"}``. Watch ``clockSkewSeconds`` - the server rejects
            requests once it passes 300, so fix NTP well before then.

        :raises AuthenticationError: Wrong/revoked credentials, bad signature, clock
            too far off, or the IP is not allowlisted.
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds`` before retrying.
        :raises ApiError: Unexpected API response.
        :raises TransportError: Network-level failure.
        """
        # GET signs an EMPTY idempotency key and an EMPTY body, and sends no
        # Idempotency-Key header.
        return self._request("GET", PING_PATH, None, "")

    def create_checkout_session(
        self,
        amount: int,
        currency: str,
        order_reference: str,
        customer: Optional[Mapping[str, Any]] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        theme: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        save_card: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Create a hosted checkout session for one payment.

        :param amount: Integer in MINOR units (2500 = 25.00 EUR, but 2500 JPY). Never a
            float; :func:`to_minor_units` converts a decimal price exactly.
        :param currency: ISO 4217 code.
        :param order_reference: Your own order id, 100 chars or fewer.
        :param customer: ``{"firstName", "lastName", "email", "phone"}``. Pass everything
            you already know - prefilled fields are hidden from the payer, so the
            checkout form stays short.
        :param country: ISO 3166-1 alpha-2.
        :param language: ISO 639-1, the widget UI language.
        :param theme: ``light``, ``dark``, or ``bright``.
        :param description: Free-text description shown on the checkout.
        :param idempotency_key: Required. Build it with :func:`order_idempotency_key` so
            a reload or retry of the same order replays the same session; retrying with
            the same key never creates a second payment. Omitting it raises
            ``ValueError`` before anything is sent.
        :param save_card: Ask the gateway to keep the card on file once this payment is
            approved, so you can charge it again with :meth:`charge_payment_method`.
            The stored method shows up as ``storedPaymentMethod`` on :meth:`get_status` after
            the payment succeeds; a declined first payment stores nothing. The card
            details never reach you: you get an id, a brand and the last four digits.
        :returns: ``{"transactionId", "orderId", "cashierKey", "cashierToken", "amount",
            "currency", "expiresAt"}``.

        :raises ValueError: A missing ``idempotency_key`` or another invalid argument,
            before any request is made.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (fix config; do not retry).
        :raises CheckoutRefusedError: The gateway refused the session (inspect
            ``error_code``).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises ApiError: Unexpected API response.
        :raises TransportError: Network-level failure (safe to retry WITH the same
            ``idempotency_key``).
        """
        _validate_money_params(amount, currency, order_reference)
        key = _normalize_idempotency_key(idempotency_key)

        body: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "orderReference": order_reference,
        }
        if customer is not None:
            body["customer"] = dict(customer)
        if country is not None:
            body["country"] = country
        if language is not None:
            body["language"] = language
        if theme is not None:
            body["theme"] = theme
        if description is not None:
            body["description"] = description
        if save_card is not None:
            body["saveCard"] = bool(save_card)

        response = self._request("POST", SESSIONS_PATH, body, key)

        if response.get("success") is not True or "checkout" not in response:
            # A replay refusal names the transaction your key collided with. Carry it
            # (and the whole payload) so the caller can reconcile with get_status()
            # instead of minting a second payment for the same order.
            transaction_id = response.get("transactionId")
            raise CheckoutRefusedError(
                str(response.get("errorCode") or "UNKNOWN"),
                str(response.get("errorMessage") or "The checkout session was refused."),
                transaction_id=str(transaction_id) if transaction_id else None,
                result=dict(response),
            )

        return response["checkout"]

    def create_checkout_session_with_retry(
        self,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Create a session, retrying transient failures with the SAME idempotency key.

        Two things are retried. A ``TransportError`` (network blip, 5xx, including a 503
        carrying ``MERCHANT_API_UNAVAILABLE`` or ``PAYMENT_PROCESSING_UNAVAILABLE``) leaves
        you not knowing whether the request landed. A ``PAYMENT_PROCESSING_UNAVAILABLE``
        refusal means card payments are briefly off and nothing was charged; the API
        contract marks it retryable with the same key. Reusing the key is what makes the retry
        safe: if the first attempt did land, the server answers the retry from that
        attempt instead of opening a second session. Generating a fresh key here would be
        the double-charge bug this method exists to prevent.

        While the first session is still open, the retry returns that same session.
        Otherwise it arrives as a :class:`CheckoutRefusedError` with a replay code
        (``DUPLICATE_REQUEST``, ``ALREADY_PROCESSED``, ``PRIOR_ATTEMPT_FAILED``,
        ``IDEMPOTENCY_KEY_REUSED``) and no cashier fields. When the refusal names a
        ``transaction_id``, read it with :meth:`get_status` to find out what the earlier
        attempt did.

        Every other refusal, storefront errors and authentication failures are raised
        immediately - retrying them just
        burns time. So is a :class:`RateLimitError`: the API has just told us it is
        already seeing too much from this key, and answering that with more traffic on a
        half-second backoff is how a spike becomes a lockout. Catch it and wait out
        ``retry_after_seconds`` yourself.

        :param max_attempts: Total attempts including the first one.
        :param backoff_seconds: Base delay; doubles after each failed attempt.
        :param kwargs: Passed straight to :meth:`create_checkout_session`, including the
            required ``idempotency_key``, which every attempt reuses.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        # Checked up front so a missing key fails before the first attempt, never after
        # a retry has already been spent.
        kwargs["idempotency_key"] = _normalize_idempotency_key(kwargs.get("idempotency_key"))

        delay = backoff_seconds
        for attempt in range(1, max_attempts + 1):
            try:
                return self.create_checkout_session(**kwargs)
            except TransportError:
                if attempt == max_attempts:
                    raise
            except CheckoutRefusedError as refusal:
                # The one refusal that is transient: processing is off for a moment,
                # nothing was charged, and the gateway asks for the same key again.
                if (
                    refusal.error_code != ErrorCode.PAYMENT_PROCESSING_UNAVAILABLE
                    or attempt == max_attempts
                ):
                    raise
            if delay > 0:
                time.sleep(delay)
            delay *= 2

        raise AssertionError("unreachable")

    def get_status(self, transaction_id: str) -> Dict[str, Any]:
        """Read the payment status of one of your checkout sessions.

        Status values: ``pending``, ``processing``, ``succeeded``, ``failed``,
        ``refunded``, ``partially_refunded``, ``cancelled``, ``disputed``,
        ``requires_capture``, ``abandoned``. While a session is still payable the
        response carries ``expiresAt``; amounts are integers in MINOR units.

        ``succeeded`` is the only value that means the payment is complete. Keep
        polling on ``pending``, ``processing`` and ``requires_capture`` - none of them
        is terminal.

        ``requires_capture`` is NOT "unpaid": the payer has already paid and the funds
        are held awaiting capture. Never treat it as an abandoned order.

        Treat any status you do not recognise as still-open too: a value added to the
        API later must make you keep polling, never silently close an order that is
        still live.

        ``storedPaymentMethod`` is the card kept on file by a session created with
        ``save_card=True``: ``{"id", "brand", "last4", "expiryMonth", "expiryYear",
        "status"}``, present once the payment is approved (and it stays after a revoke,
        with ``status`` ``revoked``); absent or None until then, for sessions without
        ``save_card`` and for declined or abandoned ones. ``brand``, ``last4`` and the
        expiry are None when the provider did not report them. It is not the
        ``paymentMethod`` field, which is the gateway's string category of how the
        payer paid (``card``, ``wallet``, ...) and passes through untouched.

        :param transaction_id: The ``transactionId`` from
            :meth:`create_checkout_session`.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature.
        :raises RateLimitError: HTTP 429; polling too fast. Wait
            ``retry_after_seconds`` before the next poll.
        :raises ApiError: Unknown transaction id (HTTP 404) or unexpected response.
        :raises TransportError: Network-level failure (safe to retry).
        """
        normalized = transaction_id.strip().lower()
        if not _TRANSACTION_ID_RE.match(normalized):
            raise ValueError(
                "transaction_id must be the UUID returned by create_checkout_session()"
            )

        status = self._request("GET", SESSIONS_PATH + "/" + normalized, None, "")
        # Passed through as sent, except the card on file: the gateway omits its null
        # fields on the wire, and the caller gets one shape for it, not two. When the
        # gateway sent no storedPaymentMethod at all there is no key here either.
        stored = status.get("storedPaymentMethod")
        if isinstance(stored, dict):
            status = dict(status, storedPaymentMethod=_stored_payment_method(stored))
        return status

    def charge_payment_method(
        self,
        payment_method_id: str,
        amount: int,
        currency: str,
        order_reference: str,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Charge a card kept on file, off-session: no widget, no payer present.

        The charge is signed like a session and carries the ``Idempotency-Key`` you
        pass, so retrying after a timeout WITH THE SAME KEY never charges the card twice:
        the gateway replays its first answer.

        A decline is not an exception: the gateway answers HTTP 402 and this returns a
        charge with ``status`` ``failed`` plus a ``declineClass`` telling you whether
        to give up on the card (``hard``), wait (``soft_funds``, ``soft_other``) or
        bring the customer back for a hosted session (``soft_sca_required``).
        ``pending`` is not terminal - poll :meth:`get_status` with the returned
        ``transactionId``.

        :param payment_method_id: The ``storedPaymentMethod["id"]`` from
            :meth:`get_status` of a session created with ``save_card=True``.
        :param amount: Integer in MINOR units (2500 = 25.00 EUR). Never a float.
        :param currency: ISO 4217 code.
        :param order_reference: Your own order id, 100 chars or fewer.
        :param description: Free-text description for your dashboard.
        :param idempotency_key: Required. Derive it from the billing period, e.g.
            ``order_idempotency_key("sub-2026-10", "8817", 2500, "EUR")``, never
            mint one per attempt. Omitting it raises ``ValueError`` before anything is
            sent.
        :returns: ``{"chargeId", "status", "declineClass", "declineCode",
            "transactionId"}``. ``status`` is ``succeeded``, ``pending`` or
            ``cancelled`` on a 201 (200 on a replay) and ``failed`` on a 402;
            ``declineClass`` and ``declineCode`` are None unless the charge was
            declined (the gateway omits them on the wire; absent reads as None).

        :raises ChargeError: The gateway answered with a code instead of a charge:
            ``CHARGE_OUTCOME_UNKNOWN`` (502, the charge MAY have happened - poll
            ``error.transaction_id``, never retry under a new key), ``CHARGE_FAILED``
            (502, nothing charged), ``PAYMENT_METHOD_NOT_ACTIVE`` or
            ``DUPLICATE_REQUEST`` (409), ``IDEMPOTENCY_KEY_REUSED`` (422),
            ``PAYMENT_METHOD_CHARGES_DISABLED`` or ``PAYMENT_PROCESSING_UNAVAILABLE``
            (503, retry later with the same key).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature.
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises ApiError: An id that is not yours (HTTP 404), input validation (400)
            or an unexpected response.
        :raises TransportError: Network-level failure (safe to retry WITH the same
            ``idempotency_key``).
        """
        method_id = _normalize_payment_method_id(payment_method_id)
        _validate_money_params(amount, currency, order_reference)
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string")
        key = _normalize_idempotency_key(idempotency_key)

        # Built field by field: the body is what gets signed, and the contract for this
        # route is exactly these fields in this order.
        body: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "orderReference": order_reference,
        }
        if description is not None:
            body["description"] = description

        reply = self._send(
            "POST", PAYMENT_METHODS_PATH + "/" + method_id + "/charges", body, key
        )

        data = reply.envelope.get("data")
        charge = (
            _charge(data)
            if isinstance(data, dict) and isinstance(data.get("chargeId"), str)
            else None
        )
        error_code = reply.error.get("code")

        # 201 (200 on a durable replay): the charge was placed, whatever its status.
        # 402: the provider declined; the envelope says success=false but the charge is
        # right there, status failed with its decline class, so it is a result.
        if charge is not None and (reply.envelope.get("success") is True or reply.status == 402):
            return charge

        if error_code and reply.status >= 400 and reply.status not in _GENERIC_FAILURE_STATUSES:
            raise ChargeError(
                reply.status,
                str(error_code),
                str(reply.error.get("message") or "The charge was refused."),
                charge=charge,
                result=dict(reply.envelope),
            )
        if reply.status >= 400:
            raise _rejection(reply)
        raise ApiError(
            reply.status,
            "The API answered the charge without a charge body",
            error_code="UNEXPECTED_RESPONSE",
        )

    def revoke_payment_method(self, payment_method_id: str) -> None:
        """Revoke a card kept on file.

        The saved credential is deleted at the payment provider and the method's status
        becomes ``revoked``; a later :meth:`charge_payment_method` on it is refused with
        ``PAYMENT_METHOD_NOT_ACTIVE``. Returns nothing on success (HTTP 204), and again
        for an already revoked method, so retrying a timed-out revoke is safe. Not a
        payment operation: no idempotency key is signed.

        :raises RevokeError: The gateway refused and nothing changed:
            ``MERCHANT_API_UNAVAILABLE`` (503, retry later) or
            ``UPSTREAM_CONTRACT_ERROR`` (502, the provider refused for good - contact
            support with the id).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature.
        :raises RateLimitError: HTTP 429.
        :raises ApiError: An id that is not yours (HTTP 404) or unexpected response.
        :raises TransportError: Network-level failure (safe to retry).
        """
        method_id = _normalize_payment_method_id(payment_method_id)
        # DELETE signs an EMPTY idempotency key and an EMPTY body, like GET.
        reply = self._send("DELETE", PAYMENT_METHODS_PATH + "/" + method_id, None, "")
        if reply.status < 400:
            return

        error_code = reply.error.get("code")
        if error_code and reply.status not in _GENERIC_FAILURE_STATUSES:
            raise RevokeError(
                reply.status,
                str(error_code),
                str(reply.error.get("message") or "The revoke was refused."),
                result=dict(reply.envelope),
            )
        raise _rejection(reply)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Send and apply the generic failure rules: 5xx is transport, 4xx is ApiError."""
        reply = self._send(method, path, body, idempotency_key)
        if reply.status >= 400:
            raise _rejection(reply)
        if not 200 <= reply.status < 300:
            # Everything above tests for a KNOWN failure, so anything left that is not a
            # 2xx would be decoded into a session on the way out. A 300 or a 305 reaches
            # here (no redirect handler claims those codes), and so would any status the
            # API does not send. Not the API talking: refuse it, and do not retry it.
            raise ApiError(
                reply.status,
                "Unexpected HTTP {0} response; the Dominaite API answers 2xx or a "
                "documented error. Check base_url and anything proxying it.".format(
                    reply.status
                ),
                error_code="UNEXPECTED_STATUS",
            )
        return reply.payload

    def _send(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
    ) -> _Reply:
        """Sign, send and parse one request.

        ``body`` is None for GET and DELETE: an empty body (and an empty idempotency
        key) is what gets signed.

        Transport failures, redirects, non-JSON bodies, authentication failures (401,
        403) and rate limiting (429) raise here because they mean the same thing on
        every route. Any other status comes back parsed, so a route can read the code
        and the data the gateway attached before deciding what it is.
        """
        if body is None:
            payload = ""
        else:
            # Compact separators and no ASCII escaping: the bytes we hash must be the
            # exact bytes we send, so the JSON is serialized once and reused.
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        timestamp = str(int(time.time()))
        signature = sign_request(
            self._secret.reveal(), timestamp, method, path, idempotency_key, payload
        )

        headers = {
            "Content-Type": "application/json",
            "X-Api-Key-Id": self._key_id,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
            # Some edges block requests without a real User-Agent - always send one.
            "User-Agent": "dominaite-python/" + __version__,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = payload.encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path, data=data, headers=headers, method=method
        )

        headers_received: Any = None
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status = response.status
                headers_received = getattr(response, "headers", None)
                raw = _read_bounded(response)
        except urllib.error.HTTPError as error:
            # 4xx/5xx: the body still carries the machine-readable code.
            status = error.code
            headers_received = getattr(error, "headers", None)
            raw = _read_bounded(error)
        except (urllib.error.URLError, OSError) as error:
            raise TransportError(
                "Could not reach the Dominaite API: {0}".format(error)
            ) from error

        # 204 carries nothing to parse; the status is the whole answer.
        if status == 204:
            return _Reply(204, {}, {}, {})

        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if not isinstance(decoded, dict):
            raise ApiError(status, "The API returned a non-JSON response")

        # The gateway wraps responses as { success, data, ... }; unwrap when present.
        # Error responses carry the machine-readable code at error.code.
        inner = decoded.get("data")
        result = inner if isinstance(inner, dict) else decoded
        envelope_error = decoded.get("error")
        if not isinstance(envelope_error, dict):
            envelope_error = {}

        if status in (401, 403):
            raise AuthenticationError(
                str(result.get("errorCode") or envelope_error.get("code") or "UNAUTHORIZED"),
                "Authentication failed - check your key id, secret, and server clock.",
            )
        if status == 429:
            # Deliberately not retried here, not even by
            # create_checkout_session_with_retry: the API is telling us it is already
            # taking more from this key than it wants, and a client that answers that by
            # sending more is how a brief spike turns into a sustained lockout. The
            # caller owns the backoff, because only the caller knows what else is queued.
            retry_after = _retry_after_seconds(headers_received)
            raise RateLimitError(
                str(
                    result.get("errorMessage")
                    or envelope_error.get("message")
                    or "Rate limit exceeded; slow down and retry later."
                ),
                retry_after_seconds=retry_after,
                error_code=str(
                    result.get("errorCode") or envelope_error.get("code") or "RATE_LIMITED"
                ),
            )
        return _Reply(status, decoded, result, envelope_error)


def _rejection(reply: _Reply) -> DominaiteError:
    """The generic reading of a failed reply: 5xx is the API being unavailable, 4xx a rejection."""
    if reply.status >= 500:
        return TransportError(
            "The Dominaite API is unavailable (HTTP {0}); "
            "retry with the same idempotency key.".format(reply.status)
        )
    # Input validation (IDEMPOTENCY_KEY_REQUIRED and friends) answers 400 with the code
    # at error.code, not as a success=false refusal. Carry it through instead of
    # flattening every 4xx into a bare message.
    error_code = reply.payload.get("errorCode") or reply.error.get("code")
    # Storefront refusals get their own type so a caller can catch them apart from a
    # malformed request; still an ApiError underneath.
    error_type = StorefrontError if error_code in STOREFRONT_ERROR_CODES else ApiError
    return error_type(
        reply.status,
        str(
            reply.payload.get("errorMessage")
            or reply.error.get("message")
            or "Request rejected"
        ),
        error_code=str(error_code) if error_code else None,
    )


def _charge(data: Dict[str, Any]) -> Dict[str, Any]:
    """The charge body as one shape.

    The gateway omits ``declineClass`` and ``declineCode`` when they are null (every
    201, and a 502 ``CHARGE_FAILED`` row), so absent reads as None. Anything else the
    gateway sends is carried through.
    """
    charge = dict(data)
    charge["chargeId"] = str(data.get("chargeId"))
    charge["status"] = str(data.get("status") or "")
    charge["declineClass"] = data["declineClass"] if isinstance(data.get("declineClass"), str) else None
    charge["declineCode"] = data["declineCode"] if isinstance(data.get("declineCode"), str) else None
    charge["transactionId"] = str(data.get("transactionId") or "")
    return charge


def _stored_payment_method(data: Dict[str, Any]) -> Dict[str, Any]:
    """Same rule for the card on file: brand, last4 and the expiry are absent when unreported."""
    stored = dict(data)
    stored["id"] = str(data.get("id") or "")
    stored["brand"] = data["brand"] if isinstance(data.get("brand"), str) else None
    stored["last4"] = data["last4"] if isinstance(data.get("last4"), str) else None
    # bool is an int in Python; a month of True would be nonsense, keep it out.
    stored["expiryMonth"] = (
        data["expiryMonth"]
        if isinstance(data.get("expiryMonth"), int) and not isinstance(data.get("expiryMonth"), bool)
        else None
    )
    stored["expiryYear"] = (
        data["expiryYear"]
        if isinstance(data.get("expiryYear"), int) and not isinstance(data.get("expiryYear"), bool)
        else None
    )
    stored["status"] = str(data.get("status") or "")
    return stored
