"""Server-side client for the Dominaite merchant API."""

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from .exceptions import (
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    RateLimitError,
    TransportError,
)

__version__ = "0.2.0"

DEFAULT_BASE_URL = "https://api.dominaite.com/payments"
SESSIONS_PATH = "/merchant-api/checkout/sessions"
PING_PATH = "/merchant-api/ping"
DEFAULT_TIMEOUT_SECONDS = 45.0  # serverless cold starts hit 10+s on dev; 15s was a coin flip

#: Hosts allowed to be addressed over plain http. Everything else must be https, so the
#: signed headers cannot be read off the wire.
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: Longest ``order_reference`` the API accepts, counted in Unicode code points.
MAX_ORDER_REFERENCE_LENGTH = 100

#: Longest ``Idempotency-Key`` the API accepts, counted in Unicode code points.
MAX_IDEMPOTENCY_KEY_LENGTH = 100

#: Hard cap on how much of a response body we will buffer. A well-behaved API answer is
#: a few kilobytes; anything past this is a misconfigured proxy or something hostile in
#: front of the API, and reading it to the end is how one bad response takes the process
#: down with it.
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

_TRANSACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


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
    ) -> Dict[str, Any]:
        """Create a hosted checkout session for one payment.

        :param amount: Integer in MINOR units (2500 = 25.00 EUR). Never a float.
        :param currency: ISO 4217 code.
        :param order_reference: Your own order id, 100 chars or fewer.
        :param customer: ``{"firstName", "lastName", "email", "phone"}``. Pass everything
            you already know - prefilled fields are hidden from the payer, so the
            checkout form stays short.
        :param country: ISO 3166-1 alpha-2.
        :param language: ISO 639-1, the widget UI language.
        :param theme: ``light``, ``dark``, or ``bright``.
        :param description: Free-text description shown on the checkout.
        :param idempotency_key: Auto-generated when omitted. Retrying with the same key
            never creates a second payment.
        :returns: ``{"transactionId", "orderId", "cashierKey", "cashierToken", "amount",
            "currency", "expiresAt"}``.

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

        key = idempotency_key if idempotency_key is not None else secrets.token_hex(16)
        # Characters again, for the same reason as order_reference above.
        if not isinstance(key, str) or not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                "idempotency_key must be a non-empty string of at most {0} "
                "characters".format(MAX_IDEMPOTENCY_KEY_LENGTH)
            )

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
        """Create a session, retrying transport failures with the SAME idempotency key.

        A ``TransportError`` (network blip, 5xx, ``MERCHANT_API_UNAVAILABLE``) leaves you
        not knowing whether the request landed. Reusing the key is what makes the retry
        safe: if the first attempt did land, the server refuses the retry instead of
        opening a second session. Generating a fresh key here would be the double-charge
        bug this method exists to prevent.

        The refusal does NOT hand back the original session. It arrives as a
        :class:`CheckoutRefusedError` with a replay code (``DUPLICATE_REQUEST``,
        ``ALREADY_PROCESSED``, ``PRIOR_ATTEMPT_FAILED``, ``IDEMPOTENCY_KEY_REUSED``) and
        no cashier fields, so there is nothing to hand the embed snippet. When the
        refusal names a ``transaction_id``, read it with :meth:`get_status` to find out
        what the earlier attempt did.

        Refusals and authentication failures are raised immediately - retrying them just
        burns time. So is a :class:`RateLimitError`: the API has just told us it is
        already seeing too much from this key, and answering that with more traffic on a
        half-second backoff is how a spike becomes a lockout. Catch it and wait out
        ``retry_after_seconds`` yourself.

        :param max_attempts: Total attempts including the first one.
        :param backoff_seconds: Base delay; doubles after each failed attempt.
        :param kwargs: Passed straight to :meth:`create_checkout_session`.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        # Minted once, here, for every attempt. setdefault() would leave an explicit
        # None in place and let each attempt generate its own key downstream, which is
        # exactly the second payment this method exists to prevent.
        if kwargs.get("idempotency_key") is None:
            kwargs["idempotency_key"] = secrets.token_hex(16)

        delay = backoff_seconds
        for attempt in range(1, max_attempts + 1):
            try:
                return self.create_checkout_session(**kwargs)
            except TransportError:
                if attempt == max_attempts:
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

        return self._request("GET", SESSIONS_PATH + "/" + normalized, None, "")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Sign and send one request.

        ``body`` is None for GET: an empty body (and an empty idempotency key) is what
        gets signed.
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
        if status >= 500:
            raise TransportError(
                "The Dominaite API is unavailable (HTTP {0}); "
                "retry with the same idempotency key.".format(status)
            )
        if status >= 400:
            # Input validation (IDEMPOTENCY_KEY_REQUIRED and friends) answers 400 with
            # the code at error.code, not as a success=false refusal. Carry it through
            # instead of flattening every 4xx into a bare message.
            error_code = result.get("errorCode") or envelope_error.get("code")
            raise ApiError(
                status,
                str(
                    result.get("errorMessage")
                    or envelope_error.get("message")
                    or "Request rejected"
                ),
                error_code=str(error_code) if error_code else None,
            )
        if not 200 <= status < 300:
            # Everything above tests for a KNOWN failure, so anything left that is not a
            # 2xx would be decoded into a session on the way out. A 300 or a 305 reaches
            # here (no redirect handler claims those codes), and so would any status the
            # API does not send. Not the API talking: refuse it, and do not retry it.
            raise ApiError(
                status,
                "Unexpected HTTP {0} response; the Dominaite API answers 2xx or a "
                "documented error. Check base_url and anything proxying it.".format(status),
                error_code="UNEXPECTED_STATUS",
            )

        return result
