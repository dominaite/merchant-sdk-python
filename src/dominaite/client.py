"""Server-side client for the Dominaite merchant API."""

import datetime
import hashlib
import hmac
import json
import platform
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Type,
    Union,
    cast,
)

from .exceptions import (
    STOREFRONT_ERROR_CODES,
    ApiError,
    AuthenticationError,
    ChargeError,
    CheckoutRefusedError,
    DominaiteError,
    ErrorCode,
    RateLimitError,
    RefundError,
    RevokeError,
    StorefrontError,
    TransportError,
)
from .models import (
    Agreement,
    Plan,
    Refund,
    RefundList,
    Transaction,
    TransactionPage,
    WebhookEndpoint,
)

__version__ = "0.6.0"

# There is no default base URL: an unset setting must fail at construction, not send
# sandbox credentials to production. Pass one of these, or your own value.
SANDBOX_BASE_URL = "https://api.staging.dominaite.com/payments"
PRODUCTION_BASE_URL = "https://api.dominaite.com/payments"
SESSIONS_PATH = "/merchant-api/checkout/sessions"
PAYMENT_METHODS_PATH = "/merchant-api/payment-methods"
PING_PATH = "/merchant-api/ping"
PAYMENTS_PATH = "/merchant-api/payments"
CONTRACT_PATH = "/merchant-api/integration/contract"
PLANS_PATH = "/merchant-api/plans"
AGREEMENTS_PATH = "/merchant-api/agreements"
WEBHOOKS_PATH = "/merchant-api/webhooks"
TRANSACTIONS_PATH = "/merchant-api/transactions"
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
#: carries a code: validation (400), authentication and scope (401, 403), unknown id (404) and
#: rate limiting (429) mean the same thing on every route and keep their usual errors.
#: Everything else with a code is the gateway telling this route something specific (a
#: decline, an unknown outcome, a refusal) and arrives as ChargeError / RevokeError.
_GENERIC_FAILURE_STATUSES = frozenset({400, 401, 403, 404, 429})

#: The charge route keeps a coded 404 as a ChargeError: PAYMENT_METHOD_NOT_FOUND and
#: AGREEMENT_NOT_FOUND are answers about this charge, like the 409 and 422 codes.
_CHARGE_GENERIC_FAILURE_STATUSES = frozenset({400, 401, 403, 429})

#: A refund id is ``re_`` + 32 hex today, but held to the same single-path-segment rule as a
#: payment method id rather than to its current shape.
_REFUND_ID_RE = _PAYMENT_METHOD_ID_RE

#: Plan and agreement ids (``plan_...``, ``agr_...``) are signed exactly as sent and are
#: case-sensitive, so they are never case-changed; the same single-segment rule applies.
_RECURRING_ID_RE = _PAYMENT_METHOD_ID_RE

#: CR, LF and the other control characters. A User-Agent suffix carrying one could split
#: the header, so the constructor refuses it.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


#: The response header the API uses to tell an old SDK version that it is deprecated.
NOTICE_HEADER = "Dominaite-Sdk-Notice"

#: The response header that carries the id support uses to find one call.
REQUEST_ID_HEADER = "Dominaite-Request-Id"

#: Longest notice value the SDK reads, and how many distinct notices one client remembers.
_MAX_NOTICE_LENGTH = 512
_MAX_NOTICES_SEEN = 32
_MAX_REQUEST_ID_LENGTH = 128

#: Ping warns above this clock skew. The API refuses requests at 300 seconds.
CLOCK_SKEW_WARNING_SECONDS = 60


def _clean_header(value: Any, limit: int) -> str:
    """The header value cut to ``limit`` characters, without control characters."""
    if value is None:
        return ""
    return _CONTROL_CHARS_RE.sub("", str(value)[:limit]).strip()


def _notice_message(notice: str) -> str:
    """The warning for one ``Dominaite-Sdk-Notice`` value."""
    parts = [part.strip() for part in notice.split(";")]
    if parts[0].lower() != "deprecated":
        return "dominaite-python {0}: {1}".format(__version__, notice)
    params: Dict[str, str] = {}
    for part in parts[1:]:
        name, sep, value = part.partition("=")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            params[name.strip().lower()] = value
    message = "dominaite-python {0} is deprecated".format(__version__)
    if params.get("current"):
        message += "; upgrade to " + params["current"]
    if params.get("sunset"):
        message += " (sunset " + params["sunset"] + ")"
    message += "."
    if params.get("info"):
        message += " " + params["info"]
    return message


def _caller_stacklevel() -> int:
    """The ``stacklevel`` that points a warning at the first frame outside this package."""
    level = 1
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__", "").startswith("dominaite"):
        frame = frame.f_back
        level += 1
    return level


def _user_agent(suffix: Optional[str]) -> str:
    """``dominaite-python/<version> (python <version>)``, then the caller's suffix."""
    base = "dominaite-python/{0} (python {1})".format(__version__, platform.python_version())
    if suffix is None:
        return base
    if not isinstance(suffix, str):
        raise ValueError("user_agent must be a string")
    if _CONTROL_CHARS_RE.search(suffix):
        raise ValueError("user_agent must not contain control characters")
    trimmed = suffix.strip()
    return base + " " + trimmed if trimmed else base


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


def _require_base_url(base_url: Optional[str]) -> str:
    """Return base_url without surrounding whitespace and trailing slashes.

    Raises ValueError when it is missing, empty or whitespace-only. The SDK has no default,
    so a base URL read from an unset environment variable fails here instead of calling
    production with the wrong key.
    """
    trimmed = (base_url or "").strip().rstrip("/")
    if not trimmed:
        raise ValueError(
            "base_url is required. Use {0} for the sandbox or {1} for production.".format(
                SANDBOX_BASE_URL, PRODUCTION_BASE_URL
            )
        )
    return trimmed


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


def _validate_money_params(
    amount: Any, currency: Any, order_reference: Any, allow_zero: bool = False
) -> None:
    """The checks shared by every request that moves money.

    ``allow_zero`` is for a verify-only session, which saves a card without a payment and
    sends ``amount`` 0. The gateway decides on any other amount there.
    """
    # bool is a subclass of int in Python, so True would otherwise sail through
    # and get serialized as `true`.
    minimum = 0 if allow_zero else 1
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < minimum:
        raise ValueError(
            "amount must be 0 or a positive integer in MINOR units on a verify_only session"
            if allow_zero
            else "amount must be a positive integer in MINOR units (e.g. 2500 for 25.00 EUR)"
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


def _normalize_transaction_id(transaction_id: str) -> str:
    # The canonical path is signed with the lowercase hyphenated form, so that is what
    # goes into the URL too.
    normalized = transaction_id.strip().lower()
    if not _TRANSACTION_ID_RE.match(normalized):
        raise ValueError(
            "transaction_id must be the UUID returned by create_checkout_session()"
        )
    return normalized


def _query_instant(value: Any, name: str) -> str:
    """A window bound for the transaction list: a string as given, a datetime as UTC."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            # A naive datetime has no zone, and guessing one shifts the window silently.
            raise ValueError("{0} must be a timezone-aware datetime".format(name))
        utc = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return utc.isoformat() + "Z"
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("{0} must be an ISO 8601 UTC string, a datetime or a date".format(name))


def _query_date(value: Any, name: str) -> str:
    """A settlement window bound: a calendar date. The API refuses a time of day."""
    if isinstance(value, datetime.datetime):
        # A datetime would send a time the API refuses; a date has no zone to get wrong.
        raise ValueError("{0} must be a date, not a datetime".format(name))
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError("{0} must be a date or a yyyy-MM-dd string".format(name))


def _normalize_webhook_id(webhook_id: Any) -> str:
    # Signed in the lowercase hyphenated form, the same rule as a transaction id.
    normalized = str(webhook_id or "").strip().lower()
    if not _TRANSACTION_ID_RE.match(normalized):
        raise ValueError("webhook_id must be the endpoint id from create_webhook_endpoint()")
    return normalized


def _normalize_recurring_id(value: Any, name: str) -> str:
    """A plan or agreement id for the path: one segment, its case kept as sent."""
    normalized = str(value or "").strip()
    if not _RECURRING_ID_RE.match(normalized):
        raise ValueError("{0} must be the id the API returned".format(name))
    return normalized


def _validate_whole_number(value: Any, name: str, minimum: int) -> None:
    """A count or an amount must go out as a JSON integer: never a bool, a float or a string."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError("{0} must be an integer of at least {1}".format(name, minimum))


def _normalize_payment_method_id(payment_method_id: Any) -> str:
    # The gateway signs the canonical path with the lowercase ``pm_`` id, so an id typed
    # in upper case would sign one path and be checked against another.
    normalized = str(payment_method_id or "").strip().lower()
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

        client = DominaiteClient(
            os.environ["DOMINAITE_KEY_ID"],
            os.environ["DOMINAITE_SECRET"],
            base_url=os.environ["DOMINAITE_BASE_URL"],
        )
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
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        *,
        user_agent: Optional[str] = None,
        on_warning: Optional[Callable[[str], None]] = None,
        on_debug: Optional[Callable[[str], None]] = None,
    ) -> None:
        """
        :param key_id: Your API key id (``dmk_...``), from the Dominaite dashboard.
        :param secret: Your API secret (``dms_...``). Server-side only.
        :param base_url: Required. ``SANDBOX_BASE_URL`` or ``PRODUCTION_BASE_URL``, or your
            own value. Must be ``https://`` unless the host is localhost, 127.0.0.1 or ::1.
            A missing, empty or whitespace-only value raises ``ValueError``.
        :param timeout: Per-request socket timeout in seconds.
        :param user_agent: Your own identifier, appended to the SDK's User-Agent after one
            space (for example ``"shop/1.2"``). It helps support find your calls in the
            access logs. Blank means none; a control character raises ``ValueError``.
        :param on_warning: Called with each warning message: a ``Dominaite-Sdk-Notice``
            from the API (once per distinct value), a retry in
            :meth:`create_checkout_session_with_retry`, and a ping clock skew over 60
            seconds. Without it the SDK calls ``warnings.warn``: ``DeprecationWarning`` for
            the notice, ``UserWarning`` for the others.
        :param on_debug: Called with one line per request: method, path, status, time in
            ms, the ``Dominaite-Request-Id`` and the idempotency key. Off when not set.
            Pass ``logging.getLogger("dominaite").debug`` to send it to logging.

        A warning or debug line never carries the secret, the signature, a cashier token,
        a query string or a request or response body. An exception from either callable
        is dropped, so it never fails the request.
        """
        if not key_id.startswith("dmk_"):
            raise ValueError("key_id must start with dmk_")
        if not secret.startswith("dms_"):
            raise ValueError("secret must start with dms_")
        base_url = _require_base_url(base_url)
        _require_secure_base_url(base_url)
        self._key_id = key_id
        self._secret = _Secret(secret)
        self._base_url = base_url
        self._timeout = timeout
        self._user_agent = _user_agent(user_agent)
        self._on_warning = on_warning
        self._on_debug = on_debug
        self._notices_seen: Set[str] = set()
        self._notices_lock = threading.Lock()
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
        pong = self._request("GET", PING_PATH, None, "")
        skew = pong.get("clockSkewSeconds")
        if isinstance(skew, (int, float)) and not isinstance(skew, bool):
            if abs(skew) > CLOCK_SKEW_WARNING_SECONDS:
                self._warn(
                    "dominaite-python: clock skew is {0} s; requests fail at 300 s. "
                    "Sync this server's clock.".format(int(skew)),
                    UserWarning,
                )
        return pong

    def get_integration_contract(self) -> Dict[str, Any]:
        """Read the public integration contract of the API.

        The contract lists the request fields, the statuses, the error codes, the webhook
        events and the signing recipe that the API uses now. Use it to check what the API
        accepts today, for example the ``recurring.consentTextVersions``.

        The route is anonymous: the request goes out without a signature, so a success
        here says nothing about your credentials. Use :meth:`ping` for that.

        :returns: The contract as the API sends it, a plain dict.

        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds`` before retrying.
        :raises ApiError: Unexpected API response.
        :raises TransportError: Network-level failure or a 5xx.
        """
        return self._request("GET", CONTRACT_PATH, None, "", signed=False)

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
        integration: Optional[str] = None,
        verify_only: Optional[bool] = None,
        agreement_id: Optional[str] = None,
        consent: Optional[Mapping[str, Any]] = None,
        location_id: Optional[Any] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a hosted checkout session for one payment.

        Returns a plain dict with the keys of :class:`CheckoutSession`.

        :param amount: Integer in MINOR units (2500 = 25.00 EUR, but 2500 JPY). Never a
            float; :func:`to_minor_units` converts a decimal price exactly. Send 0 on a
            ``verify_only`` session.
        :param currency: ISO 4217 code.
        :param order_reference: Your own order id, 100 chars or fewer.
        :param customer: ``{"firstName", "lastName", "email", "phone", "address", "city",
            "postalCode", "state"}``. Pass everything you already know - prefilled fields
            are hidden from the payer, so the checkout form stays short. The billing
            address is optional: ``address`` (street, one line, at most 200 characters),
            ``city`` (100), ``postalCode`` (20) and ``state`` (50, an ISO 3166-2
            subdivision code such as ``CA`` or ``NSW``, only used for the United States,
            Canada and Australia). A value over its limit is a 400 :class:`ApiError` with
            ``error_code`` ``VALIDATION_ERROR``; ``result["error"]["validationErrors"]``
            names the field with code ``MAX_LENGTH``.
        :param country: ISO 3166-1 alpha-2.
        :param language: ISO 639-1, the widget UI language.
        :param theme: The checkout theme, for example ``light``, ``dark`` or ``bright``. At
            most 20 characters.
        :param description: Free-text description shown on the checkout, at most 500
            characters.
        :param idempotency_key: Required. Build it with :func:`order_idempotency_key` so
            a reload or retry of the same order replays the same session; retrying with
            the same key never creates a second payment. Omitting it raises
            ``ValueError`` before anything is sent.
        :param save_card: Ask the gateway to keep the card on file once this payment is
            approved, so you can charge it again with :meth:`charge_payment_method`.
            The stored method shows up as ``storedPaymentMethod`` on :meth:`get_status` after
            the payment succeeds; a declined first payment stores nothing. The card
            details never reach you: you get an id, a brand and the last four digits.
        :param integration: How the payer enters the card: ``"widget"`` (the default when
            omitted) or ``"fields"`` for card fields in your own page (see
            :class:`CheckoutIntegration`). Card fields are enabled per merchant on request;
            ``"fields"`` on an account without them is rejected with a 400. Part of the
            idempotency identity: replaying a key with a different integration is refused
            with ``IDEMPOTENCY_KEY_REUSED``. Left out of the body when None.
        :param verify_only: True saves the card without a payment. The platform holds a
            small verification amount and releases it after the card is saved. Send
            ``amount=0``. The card is saved, ``integration`` must be ``"widget"`` or None,
            and ``agreement_id`` is refused. Verify-only is off until we switch it on for
            your account: before that the API answers 403, an :class:`AuthenticationError`
            with ``error_code`` ``VERIFY_ONLY_NOT_ENABLED``.
        :param agreement_id: The ``agr_`` id of a pending agreement (see
            :meth:`create_agreement`). The session then takes the agreement's first payment
            and saves the card for its later charges. Send it exactly as it was returned:
            it is case-sensitive. The agreement's own refusals arrive as a
            :class:`CheckoutRefusedError`, for example ``AGREEMENT_AMOUNT_MISMATCH`` or
            ``AGREEMENT_CONSENT_REQUIRED``.
        :param consent: Only with ``agreement_id``: ``{"textVersion", "acceptedAt", "ip"}``,
            the customer's acceptance of the platform consent text. ``textVersion`` is one
            of the integration contract's ``recurring.consentTextVersions``;
            ``acceptedAt`` is ISO 8601 UTC; ``ip`` is the customer's address. Required
            unless the agreement already carries consent in one of those versions.
        :param location_id: The storefront (online location) to attribute the session
            to, a UUID or its string form. A key bound to one storefront refuses another.
        :param extra: More request fields, sent as given. Use it for a field the API
            accepts that this version of the SDK does not model yet. A key here replaces
            the same key set by the arguments above.
        :returns: ``{"transactionId", "orderId", "cashierKey", "cashierToken", "amount",
            "currency", "expiresAt", "integration", "clientSecret", "agreementId",
            "verifyOnly"}``, see :class:`CheckoutSession`. ``integration`` is
            always present. ``clientSecret`` is set only for ``"fields"`` sessions (absent
            for a widget session, so read it with ``.get``): an opaque string of at most
            128 characters, the same on every replay, that the payer's page hands to
            checkout.js. Keep it out of your logs. ``agreementId`` echoes ``agreement_id``,
            and ``verifyOnly`` is True on a verify-only session; both are absent otherwise.

        :raises ValueError: A missing ``idempotency_key`` or another invalid argument,
            before any request is made.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401; fix config, do not retry), or a key without the ``payments`` scope
            (403).
        :raises CheckoutRefusedError: The gateway refused the session (inspect
            ``error_code``).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises ApiError: Unexpected API response.
        :raises TransportError: Network-level failure (safe to retry WITH the same
            ``idempotency_key``).
        """
        _validate_money_params(
            amount, currency, order_reference, allow_zero=verify_only is True
        )
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
        if integration is not None:
            # A CheckoutIntegration member goes out as its plain value.
            body["integration"] = str(getattr(integration, "value", integration))
        if verify_only is not None:
            body["verifyOnly"] = bool(verify_only)
        if agreement_id is not None:
            body["agreementId"] = str(agreement_id)
        if consent is not None:
            body["consent"] = dict(consent)
        if location_id is not None:
            # A uuid.UUID goes out in its canonical string form.
            body["locationId"] = str(location_id)
        if extra is not None:
            body.update(extra)

        response = self._request("POST", SESSIONS_PATH, body, key)

        if response.get("success") is not True or "checkout" not in response:
            if response.get("errorCode") in STOREFRONT_ERROR_CODES:
                # A key first used on another storefront: the same error as the 400 form.
                raise StorefrontError(
                    200,
                    str(response.get("errorMessage") or "The storefront refused the session."),
                    error_code=str(response.get("errorCode")),
                    result=dict(response),
                )
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
            failure: DominaiteError
            try:
                return self.create_checkout_session(**kwargs)
            except TransportError as error:
                if attempt == max_attempts:
                    raise
                failure = error
            except CheckoutRefusedError as refusal:
                # The one refusal that is transient: processing is off for a moment,
                # nothing was charged, and the gateway asks for the same key again.
                if (
                    refusal.error_code != ErrorCode.PAYMENT_PROCESSING_UNAVAILABLE
                    or attempt == max_attempts
                ):
                    raise
                failure = refusal
            self._warn(
                "dominaite-python: retrying create_checkout_session_with_retry after {0} "
                "(attempt {1} of {2})".format(_retry_reason(failure), attempt + 1, max_attempts),
                UserWarning,
            )
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
        ``save_card=True``, a :class:`StoredPaymentMethod`: ``{"id", "brand", "last4",
        "expiryMonth", "expiryYear", "status", "retiredReason"}``, present once the payment is approved (and it stays after a revoke,
        with ``status`` ``revoked``); absent or None until then, for sessions without
        ``save_card`` and for declined or abandoned ones. ``brand``, ``last4`` and the
        expiry are None when the provider did not report them. It is not the
        ``paymentMethod`` field, which is the gateway's string category of how the
        payer paid (``card``, ``wallet``, ...) and passes through untouched.

        ``pspReference`` is the payment processor's reference for the transaction. It is None
        until the processor has reported one, and a sale settled by reconciliation without a
        processor webhook can keep it None. Gateways that predate the field leave the key out.

        ``paymentMethod`` is how the payer paid (:data:`PAYMENT_METHOD_CATEGORIES`), None while
        the payment is still open and on older transactions. ``walletType`` names the wallet when
        ``paymentMethod`` is ``wallet`` (:data:`WALLET_TYPES`), None for non-wallet payments; a
        lower-cased value not in that list yet is a valid wallet, not an error. Both are
        reporting data and pass through as sent: a wallet payment refunds, captures and
        disputes like a card.

        ``declineClass`` says what to do with a ``failed`` payment (:data:`DECLINE_CLASSES`).
        It can be None, for example when nobody declined the payment. ``isRecurring`` is
        True for a charge on a stored card. ``verifyOnly`` is True
        on a verify-only session and absent otherwise: its status goes
        ``requires_capture``, then ``cancelled``, and a ``storedPaymentMethod`` is what
        says that the card is verified. ``agreement`` (``{"id", "status",
        "nextChargeAt"}``) is set when the payment belongs to an agreement; its
        ``status`` says if the subscription started. ``sequence`` orders this read
        against the ``charge.*`` events of the same object. :class:`CheckoutStatus` names
        every key.

        :param transaction_id: The ``transactionId`` from
            :meth:`create_checkout_session`.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; polling too fast. Wait
            ``retry_after_seconds`` before the next poll.
        :raises ApiError: Unknown transaction id (HTTP 404) or unexpected response.
        :raises TransportError: Network-level failure (safe to retry).
        """
        normalized = _normalize_transaction_id(transaction_id)

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
        agreement_id: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Charge a card kept on file, off-session: no widget, no payer present.

        The charge is signed like a session and carries the ``Idempotency-Key`` you
        pass, so retrying after a timeout WITH THE SAME KEY never charges the card twice:
        the gateway replays its first answer.

        A decline is not an exception: the gateway answers HTTP 402 and this returns a
        charge with ``status`` ``failed``. Its ``declineClass`` tells you whether to give
        up on the card (``hard``), wait (``soft_funds``, ``soft_other``) or bring the
        customer back for a hosted session (``soft_sca_required``). It can be None, for
        example when nobody declined the payment. ``pending`` is not terminal - poll
        :meth:`get_status` with the returned ``transactionId``.

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
        :param agreement_id: The ``agr_`` id of the agreement this charge bills, sent
            exactly as returned (it is case-sensitive). The gateway then checks the charge
            against the agreement: the card, the currency, the amount and the period.
        :param extra: More request fields, sent as given in the signed body. A key here
            replaces the same key set by the arguments above.
        :returns: ``{"chargeId", "status", "declineClass", "declineCode",
            "transactionId", "agreementId"}``, plus ``sequence`` when the gateway sends
            it. ``agreementId`` is None on a charge outside an agreement. ``status`` is
            ``succeeded``, ``pending`` or ``cancelled`` on a 201 (200 on a replay) and
            ``failed`` on a 402.
            ``declineClass`` and ``declineCode`` can be None, for example when nobody
            declined the payment (the gateway omits them on the wire; absent reads as
            None). ``agreementId`` names the
            agreement the charge was placed under. ``sequence`` orders this answer against
            the ``charge.*`` events of the same object; it is 0 while the charge is
            pending.

        :raises ChargeError: The gateway answered with a code instead of a charge:
            ``CHARGE_OUTCOME_UNKNOWN`` (502, the charge MAY have happened - poll
            ``error.transaction_id``, never retry under a new key), ``CHARGE_FAILED``
            (502, nothing charged), ``PAYMENT_METHOD_NOT_ACTIVE`` or
            ``DUPLICATE_REQUEST`` (409), ``IDEMPOTENCY_KEY_REUSED`` (422),
            ``PAYMENT_PROCESSING_UNAVAILABLE`` (503, retry later with the same key) or
            ``PAYMENT_METHOD_CHARGES_DISABLED`` (503). If we never switched stored-card
            charges on for your account, a retry does not help: ask Dominaite support. If
            they are on, retry later with the same idempotency key. The code is the same in
            both cases. A charge with ``agreement_id`` can
            also be refused with 422 ``AGREEMENT_METHOD_MISMATCH``,
            ``AGREEMENT_CONSENT_MISSING``, ``AGREEMENT_CURRENCY_MISMATCH``,
            ``CHARGE_AMOUNT_ABOVE_AGREEMENT``, ``AGREEMENT_AMOUNT_MISMATCH``,
            ``AGREEMENT_NOT_STARTED``, ``AGREEMENT_ENDED`` or
            ``AGREEMENT_PERIOD_ALREADY_CHARGED``. Nothing was charged on any of them.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``. ``error_code`` is ``CHARGE_VELOCITY_EXCEEDED`` or
            ``CHARGE_DECLINE_RATE_EXCEEDED`` for the charge route's own limits.
        :raises ApiError: An id that is not yours (HTTP 404, ``PAYMENT_METHOD_NOT_FOUND``
            or ``AGREEMENT_NOT_FOUND``), input validation (400) or an unexpected
            response.
        :raises TransportError: Network-level failure (safe to retry WITH the same
            ``idempotency_key``).
        """
        method_id = _normalize_payment_method_id(payment_method_id)
        _validate_money_params(amount, currency, order_reference)
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string")
        key = _normalize_idempotency_key(idempotency_key)

        # The body is what gets signed, so it is built once, here, and sent as built.
        body: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "orderReference": order_reference,
        }
        if description is not None:
            body["description"] = description
        if agreement_id is not None:
            body["agreementId"] = str(agreement_id)
        if extra is not None:
            body.update(extra)

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

        if (
            error_code
            and reply.status >= 400
            and reply.status not in _CHARGE_GENERIC_FAILURE_STATUSES
        ):
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
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
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

    def create_refund(
        self,
        transaction_id: str,
        amount: Optional[int] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Refund:
        """Refund a payment, in full or in part.

        The refund is queued, not done, when this returns (HTTP 202): its ``status`` is
        ``pending``. Read it back with :meth:`get_refund` or wait for ``payment.refunded``,
        which fires once the money has moved. A refund that fails sends no webhook, so poll
        if you need to know about failures.

        Signed like a charge and carrying the ``Idempotency-Key`` you pass: the same key
        answers the same refund and never refunds twice, so retry a timeout WITH THE SAME
        KEY. A replay answers 202 with the refund as it stands now.

        :param transaction_id: The ``transactionId`` of the payment, from
            :meth:`create_checkout_session` or :meth:`charge_payment_method`.
        :param amount: Integer in MINOR units of the payment's currency (2500 = 25.00 EUR,
            but 2500 HUF is 2500 Ft); :func:`to_minor_units` converts a decimal exactly.
            Leave it out to refund everything still refundable. Partial refunds add up.
        :param reason: Free text stored with the refund, at most 500 characters.
        :param idempotency_key: Required. Derive it from YOUR refund (the return or credit
            note id), never mint one per attempt. Omitting it raises ``ValueError`` before
            anything is sent.
        :param extra: More request fields, sent as given in the signed body. A key here
            replaces the same key set by the arguments above.
        :returns: The :class:`Refund`, every field present (None where the gateway sent
            none).

        :raises RefundError: The gateway answered with a code (see :class:`RefundError`
            for each one and whether it is ``retryable``).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises ApiError: An unexpected response.
        :raises TransportError: Network-level failure or a 5xx; nothing was queued on a
            5xx. Retry WITH the same ``idempotency_key``.
        """
        normalized = _normalize_transaction_id(transaction_id)
        # bool is an int in Python; True must not go out as a refund of 1.
        if amount is not None and (
            isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0
        ):
            raise ValueError(
                "amount must be a positive integer in MINOR units, or left out to refund "
                "everything still refundable"
            )
        if reason is not None and not isinstance(reason, str):
            raise ValueError("reason must be a string")
        key = _normalize_idempotency_key(idempotency_key)

        # A full refund sends no amount key at all, not a null: the body is what gets
        # signed, and the gateway reads a missing amount as "everything left".
        body: Dict[str, Any] = {}
        if amount is not None:
            body["amount"] = amount
        if reason is not None:
            body["reason"] = reason
        if extra is not None:
            body.update(extra)

        reply = self._send("POST", PAYMENTS_PATH + "/" + normalized + "/refunds", body, key)
        return _refund_result(reply)

    def get_refund(self, transaction_id: str, refund_id: str) -> Refund:
        """Read one refund of a payment.

        ``REFUND_NOT_FOUND`` right after :meth:`create_refund` means the refund is not
        picked up yet: poll again for up to 60 seconds (the :class:`RefundError` says
        ``retryable``). Signed with an empty idempotency key and an empty body, like
        :meth:`get_status`.

        :param transaction_id: The ``transactionId`` of the refunded payment.
        :param refund_id: The ``refundId`` from :meth:`create_refund`.
        :returns: The :class:`Refund`, every field present (None where the gateway sent
            none).

        :raises RefundError: ``PAYMENT_NOT_FOUND`` or ``REFUND_NOT_FOUND`` (404).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises ApiError: An unexpected response.
        :raises TransportError: Network-level failure (safe to retry).
        """
        normalized = _normalize_transaction_id(transaction_id)
        refund = str(refund_id or "").strip()
        if not _REFUND_ID_RE.match(refund):
            raise ValueError("refund_id must be the refundId from create_refund()")
        reply = self._send(
            "GET", PAYMENTS_PATH + "/" + normalized + "/refunds/" + refund, None, ""
        )
        return _refund_result(reply)

    def list_refunds(self, transaction_id: str) -> RefundList:
        """List the refunds of a payment, with what is refunded and what is still refundable.

        Signed with an empty idempotency key and an empty body, like :meth:`get_status`.
        The list is not paged.

        :param transaction_id: The ``transactionId`` of the payment.
        :returns: A :class:`RefundList`: ``refunds`` (newest first, each a :class:`Refund`
            with every field present), ``refundedAmount``, ``refundableAmount``,
            ``currency`` and ``transactionId``. ``refunds`` holds the refunds created
            through this API; the two amounts count every refund of the payment.

        :raises RefundError: ``PAYMENT_NOT_FOUND`` (404): no payment with this id under
            your account.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises ApiError: An unexpected response.
        :raises TransportError: Network-level failure or a 5xx, ``MERCHANT_API_UNAVAILABLE``
            included (safe to retry).
        """
        normalized = _normalize_transaction_id(transaction_id)
        reply = self._send("GET", PAYMENTS_PATH + "/" + normalized + "/refunds", None, "")
        return _refund_list(reply)

    def create_plan(
        self,
        name: str,
        amount: int,
        currency: str,
        interval_unit: str,
        interval_count: Optional[int] = None,
        period_count: Optional[int] = None,
        trial_days: Optional[int] = None,
        cancel_url: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Plan:
        """Create a subscription plan: a price that agreements bill every period.

        Amount, currency and interval cannot change after this. To change a price, create
        a new plan and archive the old one with :meth:`delete_plan`.

        :param name: The display name of the plan, 1 to 100 characters.
        :param amount: The charge per period, an integer in MINOR units.
        :param currency: ISO 4217 code in upper case, for example ``"EUR"``.
        :param interval_unit: ``"day"``, ``"week"``, ``"month"`` or ``"year"``.
        :param interval_count: How many units make one period: 3 with ``"month"`` bills
            every 3 months. The API uses 1 when you leave it out. One period is at most
            one year.
        :param period_count: The number of periods after which agreements complete. Leave
            it out to bill until the agreement is cancelled.
        :param trial_days: Days between activation and the first charge, 0 to 365.
        :param cancel_url: Your page where a customer cancels, an absolute https URL. The
            customer gets it on every receipt and decline notice.
        :param idempotency_key: Required. The same key with the same body answers the
            first plan again (HTTP 200), so a create that timed out is safe to repeat.
        :param extra: More request fields, sent as given in the signed body. A key here
            replaces the same key set by the arguments above.
        :returns: The :class:`Plan`.

        :raises ApiError: A 4xx with ``error_code``: ``VALIDATION_ERROR`` (400; the fields
            are in ``result["error"]["validationErrors"]``, an unsupported currency
            included) or ``IDEMPOTENCY_KEY_REUSED`` (422).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises TransportError: Network-level failure or a 5xx; nothing was created.
            Retry WITH the same ``idempotency_key``.
        """
        if not name:
            raise ValueError("name is required")
        _validate_whole_number(amount, "amount", 1)
        if not currency:
            raise ValueError("currency is required")
        if not interval_unit:
            raise ValueError("interval_unit is required")
        for value, field in ((interval_count, "interval_count"), (period_count, "period_count")):
            if value is not None:
                _validate_whole_number(value, field, 1)
        if trial_days is not None:
            _validate_whole_number(trial_days, "trial_days", 0)
        key = _normalize_idempotency_key(idempotency_key)

        body: Dict[str, Any] = {
            "name": name,
            "amount": amount,
            "currency": currency,
            "intervalUnit": str(getattr(interval_unit, "value", interval_unit)),
        }
        if interval_count is not None:
            body["intervalCount"] = interval_count
        if period_count is not None:
            body["periodCount"] = period_count
        if trial_days is not None:
            body["trialDays"] = trial_days
        if cancel_url is not None:
            body["cancelUrl"] = cancel_url
        if extra is not None:
            body.update(extra)
        return cast(Plan, self._request("POST", PLANS_PATH, body, key))

    def list_plans(self) -> List[Plan]:
        """List your plans: active ones first, the newest first within a status.

        The list is not paged and holds at most 200 plans. Read one plan with
        :meth:`get_plan` when you have more.

        :returns: A list of :class:`Plan`.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises ApiError: An unexpected response.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        return cast(List[Plan], self._request_list(PLANS_PATH, "plan list"))

    def get_plan(self, plan_id: str) -> Plan:
        """Read one of your plans.

        :param plan_id: The ``plan_`` id, exactly as returned: it is case-sensitive.
        :returns: The :class:`Plan`.

        :raises ApiError: ``PLAN_NOT_FOUND`` (404), or an unexpected response.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_recurring_id(plan_id, "plan_id")
        return cast(Plan, self._request("GET", PLANS_PATH + "/" + normalized, None, ""))

    def update_plan(
        self,
        plan_id: str,
        name: Optional[str] = None,
        trial_days: Optional[int] = None,
        cancel_url: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Plan:
        """Change the name, the trial or the cancel page of a plan.

        Only these fields can change. A field you leave out keeps its value. A trial change
        applies to agreements created after it. The update sends no idempotency key: the
        API does not replay it, and sending the same update twice gives the same plan.

        :param plan_id: The ``plan_`` id, exactly as returned.
        :param name: The new display name, 1 to 100 characters.
        :param trial_days: The new trial in days; 0 removes the trial.
        :param cancel_url: The new cancel page, an absolute https URL; ``""`` removes it.
        :param extra: More request fields, sent as given in the signed body.
        :returns: The updated :class:`Plan`.

        :raises ValueError: Nothing to change, before any request is made.
        :raises ApiError: ``PLAN_NOT_FOUND`` (404), ``VALIDATION_ERROR`` (400), or an
            unexpected response.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_recurring_id(plan_id, "plan_id")
        if trial_days is not None:
            _validate_whole_number(trial_days, "trial_days", 0)
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if trial_days is not None:
            body["trialDays"] = trial_days
        if cancel_url is not None:
            # An empty string is meaningful here: it removes the cancel page.
            body["cancelUrl"] = cancel_url
        if extra is not None:
            body.update(extra)
        if not body:
            raise ValueError("update_plan needs name, trial_days, cancel_url or extra")
        return cast(Plan, self._request("PATCH", PLANS_PATH + "/" + normalized, body, ""))

    def delete_plan(self, plan_id: str) -> None:
        """Archive a plan. No new agreement can use it; existing agreements continue.

        Archiving an archived plan succeeds again, so a retry after a timeout is safe.

        :param plan_id: The ``plan_`` id, exactly as returned.

        :raises ApiError: ``PLAN_NOT_FOUND`` (404), or an unexpected response.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_recurring_id(plan_id, "plan_id")
        self._request("DELETE", PLANS_PATH + "/" + normalized, None, "")

    def create_agreement(
        self,
        plan_id: str,
        customer_reference: str,
        stored_payment_method_id: Optional[str] = None,
        max_amount_per_period: Optional[int] = None,
        consent: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> Agreement:
        """Subscribe a customer to a plan. The agreement starts ``pending`` and nothing is
        charged.

        To start it, call :meth:`create_checkout_session` with ``agreement_id``. That
        payment activates the agreement and saves the card. With scheduled charges on, the
        platform charges the stored card each period.

        :param plan_id: The ``plan_`` id of an active plan, exactly as returned.
        :param customer_reference: Your own stable id for the customer, 1 to 100
            characters. Use the same value for the same customer every time.
        :param stored_payment_method_id: The ``pm_`` id of a card that is already saved,
            to bill that card. Needs ``consent``.
        :param max_amount_per_period: The most one charge may be, in MINOR units, when the
            customer agreed to a variable amount. At least the plan's amount. Needs
            ``consent``. Leave it out for a fixed agreement.
        :param consent: ``{"textVersion", "acceptedAt", "ip"}``: the customer's acceptance,
            when you captured it on your own page. ``textVersion`` names the wording, 1 to
            50 characters; ``acceptedAt`` is ISO 8601 UTC, not in the future; ``ip`` is the
            customer's IPv4 or IPv6 address. Required with ``stored_payment_method_id`` or
            ``max_amount_per_period``. A subscription session uses this consent only when
            ``textVersion`` is one of ``recurring.consentTextVersions`` in
            :meth:`get_integration_contract`.
        :param idempotency_key: Required. The same key with the same body answers the
            first agreement again (HTTP 200), so a create that timed out is safe to repeat.
        :param extra: More request fields, sent as given in the signed body. A key here
            replaces the same key set by the arguments above.
        :returns: The :class:`Agreement`.

        :raises ApiError: A 4xx with ``error_code``; nothing was created on any of them:
            ``PLAN_NOT_FOUND`` or ``PAYMENT_METHOD_NOT_FOUND`` (404); ``PLAN_NOT_ACTIVE``
            or ``PAYMENT_METHOD_NOT_ACTIVE`` (409); ``MAX_AMOUNT_BELOW_PLAN_AMOUNT``,
            ``PAYMENT_METHOD_CURRENCY_MISMATCH``, ``PAYMENT_METHOD_CURRENCY_UNKNOWN``,
            ``AGREEMENT_CONSENT_REQUIRED`` or ``IDEMPOTENCY_KEY_REUSED`` (422);
            ``VALIDATION_ERROR`` (400). A missing ``consent`` usually arrives as a 400 on
            the ``consent`` field: the same refusal as ``AGREEMENT_CONSENT_REQUIRED``.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises TransportError: Network-level failure or a 5xx; nothing was created.
            Retry WITH the same ``idempotency_key``.
        """
        normalized_plan = _normalize_recurring_id(plan_id, "plan_id")
        if not customer_reference:
            raise ValueError("customer_reference is required")
        if max_amount_per_period is not None:
            _validate_whole_number(max_amount_per_period, "max_amount_per_period", 1)
        key = _normalize_idempotency_key(idempotency_key)

        body: Dict[str, Any] = {
            "planId": normalized_plan,
            "customerReference": customer_reference,
        }
        if stored_payment_method_id is not None:
            body["storedPaymentMethodId"] = str(stored_payment_method_id)
        if max_amount_per_period is not None:
            body["maxAmountPerPeriod"] = max_amount_per_period
        if consent is not None:
            body["consent"] = dict(consent)
        if extra is not None:
            body.update(extra)
        return cast(Agreement, self._request("POST", AGREEMENTS_PATH, body, key))

    def get_agreement(self, agreement_id: str) -> Agreement:
        """Read one of your agreements.

        :param agreement_id: The ``agr_`` id, exactly as returned: it is case-sensitive.
        :returns: The :class:`Agreement`.

        :raises ApiError: ``AGREEMENT_NOT_FOUND`` (404), or an unexpected response.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_recurring_id(agreement_id, "agreement_id")
        return cast(Agreement, self._request("GET", AGREEMENTS_PATH + "/" + normalized, None, ""))

    def cancel_agreement(self, agreement_id: str) -> None:
        """Cancel an agreement: the platform schedules no further charge.

        Allowed from ``pending``, ``active`` and ``past_due``. A charge already placed is
        not reversed. Cancelling a cancelled agreement succeeds again, so a retry after a
        timeout is safe.

        :param agreement_id: The ``agr_`` id, exactly as returned.

        :raises ApiError: ``AGREEMENT_NOT_FOUND`` (404), ``AGREEMENT_ENDED`` (409: it
            already completed or failed, so there is nothing to cancel), or an
            unexpected response.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``payments`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_recurring_id(agreement_id, "agreement_id")
        self._request("DELETE", AGREEMENTS_PATH + "/" + normalized, None, "")

    def create_webhook_endpoint(
        self,
        url: str,
        events: Sequence[str],
        idempotency_key: Optional[str] = None,
        extra: Optional[Mapping[str, Any]] = None,
    ) -> WebhookEndpoint:
        """Register an endpoint that receives webhook deliveries.

        Needs a key with the ``webhooks:manage`` scope. A key with the ``payments`` scope
        cannot hold it, so use a separate key for this.

        :param url: The HTTPS URL on a public host that receives the deliveries, at most
            500 characters.
        :param events: The event types to deliver, 1 to 50 of them, for example
            ``["payment.succeeded", "payment.refunded"]``. The SDK does not check the names.
            The API refuses an unknown type with ``VALIDATION_ERROR``.
        :param idempotency_key: Required. The same key with the same url and events answers
            the same endpoint again, without ``secret``. After a timeout or a 503, retry
            with the SAME key: that replaces an endpoint the lost attempt registered.
        :param extra: More request fields, sent as given in the signed body.
        :returns: The :class:`WebhookEndpoint`. ``secret`` is on the first answer only.

        :raises ApiError: A 4xx with ``error_code``; nothing new was registered:
            ``WEBHOOK_URL_REJECTED`` or ``VALIDATION_ERROR`` (400);
            ``WEBHOOK_LIMIT_REACHED`` (409, delete an endpoint first);
            ``WEBHOOK_ALREADY_REGISTERED`` (409, the key registered an endpoint more than
            a day ago; ``result["error"]["params"]["webhookId"]`` names it);
            ``DUPLICATE_REQUEST`` (409, retry with the same key after a second);
            ``IDEMPOTENCY_KEY_REUSED`` (422).
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``webhooks:manage`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``, then retry WITH
            the same ``idempotency_key``.
        :raises TransportError: Network-level failure or a 5xx. Retry WITH the same
            ``idempotency_key``.
        """
        if not url:
            raise ValueError("url is required")
        if isinstance(events, (str, bytes)) or not events:
            raise ValueError("events must be a list of event types, for example ['payment.succeeded']")
        key = _normalize_idempotency_key(idempotency_key)

        body: Dict[str, Any] = {"url": url, "events": list(events)}
        if extra is not None:
            body.update(extra)
        return cast(WebhookEndpoint, self._request("POST", WEBHOOKS_PATH, body, key))

    def list_webhook_endpoints(self) -> List[WebhookEndpoint]:
        """List your webhook endpoints, the newest first, never with a secret.

        The list has every active endpoint and your newest 1000 inactive ones. It is not
        paged. Needs a key with the ``webhooks:manage`` scope.

        :returns: A list of :class:`WebhookEndpoint`.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``webhooks:manage`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises ApiError: An unexpected response.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        return cast(
            List[WebhookEndpoint], self._request_list(WEBHOOKS_PATH, "webhook endpoint list")
        )

    def delete_webhook_endpoint(self, webhook_id: str) -> None:
        """Delete a webhook endpoint. It stops receiving deliveries at once.

        Needs a key with the ``webhooks:manage`` scope.

        :param webhook_id: The endpoint ``id`` (a UUID). Any letter case is accepted.

        :raises ApiError: ``WEBHOOK_NOT_FOUND`` (404): the id is unknown or already
            deleted. A retry after a lost answer gets this too, so treat it as done.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``webhooks:manage`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        normalized = _normalize_webhook_id(webhook_id)
        self._request("DELETE", WEBHOOKS_PATH + "/" + normalized, None, "")

    def list_transactions(
        self,
        from_: Union[str, datetime.date],
        to: Union[str, datetime.date],
        status: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        settled_from: Optional[Union[str, datetime.date]] = None,
        settled_to: Optional[Union[str, datetime.date]] = None,
    ) -> TransactionPage:
        """List your card payments created in a window, one page at a time.

        Needs a key with the ``transactions:read`` scope. A payment is listed in the window
        of its ``createdAt``, with its status at the time of the read. A refund is not a row
        of its own: it shows on its payment as ``refundedAmount`` and ``status``. Rows come
        oldest first. To read every page, send ``nextCursor`` back as ``cursor`` with the
        same window and status, until ``nextCursor`` is None::

            cursor = None
            while True:
                page = client.list_transactions(start, end, cursor=cursor)
                handle(page["items"])
                cursor = page["nextCursor"]
                if cursor is None:
                    break

        :param from_: The start of the window, inclusive: an ISO 8601 UTC string such as
            ``"2026-10-01T00:00:00Z"``, a timezone-aware ``datetime`` (sent as UTC) or a
            ``date`` (midnight UTC).
        :param to: The end of the window, exclusive, in the same forms. Later than
            ``from_`` and at most 31 days after it.
        :param status: Only payments in this status now, for example ``"succeeded"``.
        :param limit: Rows per page, 1 to 100. The API uses 50 when you leave it out.
        :param cursor: The ``nextCursor`` of the previous page, unchanged. Leave it out for
            the first page.
        :param settled_from: The start of the settlement window, inclusive: a ``date`` or a
            ``"2026-10-13"`` string. With it, the list has only payments the acquirer settled
            in the window. ``from_`` and ``to`` still apply on ``createdAt``. Send it with
            ``settled_to``.
        :param settled_to: The end of the settlement window, exclusive, in the same forms.
            Later than ``settled_from`` and at most 31 days after it.
        :returns: A :class:`TransactionPage`.

        :raises ValueError: A naive ``datetime``, a ``datetime`` as a settlement bound or a
            ``limit`` that is not an integer, before any request is made.
        :raises ApiError: ``VALIDATION_ERROR`` (400): the window, the settlement window,
            ``status``, ``limit`` or ``cursor`` is not valid. The fields are in
            ``result["error"]["validationErrors"]``.
        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (401), or a key without the ``transactions:read`` scope (403).
        :raises RateLimitError: HTTP 429; wait ``retry_after_seconds``.
        :raises TransportError: Network-level failure or a 5xx (safe to retry).
        """
        query: Dict[str, str] = {
            "from": _query_instant(from_, "from_"),
            "to": _query_instant(to, "to"),
        }
        if status is not None:
            query["status"] = str(getattr(status, "value", status))
        if limit is not None:
            _validate_whole_number(limit, "limit", 1)
            query["limit"] = str(limit)
        if cursor is not None:
            query["cursor"] = cursor
        if settled_from is not None:
            query["settledFrom"] = _query_date(settled_from, "settled_from")
        if settled_to is not None:
            query["settledTo"] = _query_date(settled_to, "settled_to")

        reply = _checked(self._send("GET", TRANSACTIONS_PATH, None, "", query=query))
        items = reply.payload.get("items")
        if not isinstance(items, list):
            raise ApiError(
                reply.status,
                "The API answered the transaction list without a list",
                error_code="UNEXPECTED_RESPONSE",
            )
        page = dict(reply.payload)
        page["items"] = [cast(Transaction, dict(item)) for item in items if isinstance(item, dict)]
        page["nextCursor"] = reply.payload.get("nextCursor")
        return cast(TransactionPage, page)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
        signed: bool = True,
    ) -> Dict[str, Any]:
        """Send and apply the generic failure rules: 5xx is transport, 4xx is ApiError."""
        return _checked(self._send(method, path, body, idempotency_key, signed=signed)).payload

    def _request_list(self, path: str, what: str) -> List[Dict[str, Any]]:
        """GET a route whose ``data`` is a bare JSON array, and return its objects."""
        reply = _checked(self._send("GET", path, None, ""))
        items = reply.envelope.get("data")
        if not isinstance(items, list):
            raise ApiError(
                reply.status,
                "The API answered the {0} without a list".format(what),
                error_code="UNEXPECTED_RESPONSE",
            )
        return [dict(item) for item in items if isinstance(item, dict)]

    def _send(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
        signed: bool = True,
        query: Optional[Mapping[str, str]] = None,
    ) -> _Reply:
        """Sign, send and parse one request.

        ``body`` is None for GET and DELETE: an empty body (and an empty idempotency
        key) is what gets signed. ``signed=False`` is for the one anonymous route, the
        integration contract: no key id, timestamp or signature goes out. ``query`` goes
        into the URL only: the gateway signs the bare path, never a query string.

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

        headers = {
            "Content-Type": "application/json",
            # Some edges block requests without a real User-Agent - always send one.
            "User-Agent": self._user_agent,
        }
        if signed:
            timestamp = str(int(time.time()))
            headers["X-Api-Key-Id"] = self._key_id
            headers["X-Timestamp"] = timestamp
            headers["X-Signature"] = sign_request(
                self._secret.reveal(), timestamp, method, path, idempotency_key, payload
            )
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = payload.encode("utf-8") if body is not None else None
        url = self._base_url + path
        if query:
            # urlencode percent-encodes "+", so an offset such as +02:00 does not arrive as
            # a space.
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, data=data, headers=headers, method=method)

        headers_received: Any = None
        started = time.monotonic()
        try:
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
            self._debug(method, path, "error", started, headers_received, idempotency_key)
            raise TransportError(
                "Could not reach the Dominaite API: {0}".format(error)
            ) from error
        except BaseException:
            self._debug(method, path, "error", started, headers_received, idempotency_key)
            raise
        self._debug(method, path, str(status), started, headers_received, idempotency_key)
        # Before any status handling, so a notice on an error or a non-JSON body still warns.
        self._read_notice(headers_received)

        # 204 carries nothing to parse; the status is the whole answer.
        if status == 204:
            return _Reply(204, {}, {}, {})

        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if not isinstance(decoded, dict):
            # A 429 is a rate limit whatever its body: something in front of the API can
            # throttle too, with an HTML page or nothing at all. It gets the same error and
            # the same Retry-After reading as the API's own.
            if status >= 500:
                # A proxy or a load balancer in front of the API answers a 502 or a 504
                # with an HTML page. It is the API being unreachable, not a rejection.
                raise TransportError(
                    "The Dominaite API is unavailable (HTTP {0}); "
                    "retry with the same idempotency key.".format(status),
                    http_status=status,
                )
            if status != 429:
                raise ApiError(status, "The API returned a non-JSON response")
            decoded = {}

        # The gateway wraps responses as { success, data, ... }; unwrap when present.
        # Error responses carry the machine-readable code at error.code.
        inner = decoded.get("data")
        result = inner if isinstance(inner, dict) else decoded
        envelope_error = decoded.get("error")
        if not isinstance(envelope_error, dict):
            envelope_error = {}

        if status in (401, 403):
            code = str(
                result.get("errorCode")
                or envelope_error.get("code")
                or ("FORBIDDEN" if status == 403 else "UNAUTHORIZED")
            )
            raise AuthenticationError(
                code,
                _auth_message(
                    status,
                    code,
                    result.get("errorMessage") or envelope_error.get("message"),
                ),
                http_status=status,
                result=dict(decoded),
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
                # The gateway's own code for its limits. A charge-specific limit keeps its
                # own code (CHARGE_VELOCITY_EXCEEDED, CHARGE_DECLINE_RATE_EXCEEDED).
                error_code=str(
                    result.get("errorCode")
                    or envelope_error.get("code")
                    or ErrorCode.RATE_LIMIT_EXCEEDED.value
                ),
                result=dict(decoded),
            )
        return _Reply(status, decoded, result, envelope_error)


    def _warn(self, message: str, category: Type[Warning]) -> None:
        """Hand one warning to the caller's sink, or to ``warnings.warn``. Never raises."""
        try:
            if self._on_warning is not None:
                self._on_warning(message)
            else:
                warnings.warn(message, category, stacklevel=_caller_stacklevel())
        except Exception:
            # A sink that fails (or a warnings filter set to "error") must not fail the
            # payment call that triggered it.
            pass

    def _read_notice(self, headers: Any) -> None:
        """Warn once per distinct ``Dominaite-Sdk-Notice`` value this client sees."""
        getter = getattr(headers, "get", None)
        if getter is None:
            return
        notice = _clean_header(getter(NOTICE_HEADER), _MAX_NOTICE_LENGTH)
        if not notice:
            return
        with self._notices_lock:
            if notice in self._notices_seen or len(self._notices_seen) >= _MAX_NOTICES_SEEN:
                return
            self._notices_seen.add(notice)
        self._warn(_notice_message(notice), DeprecationWarning)

    def _debug(
        self,
        method: str,
        path: str,
        status: str,
        started: float,
        headers: Any,
        idempotency_key: str,
    ) -> None:
        """One debug line for one request, when the caller asked for them. Never raises."""
        if self._on_debug is None:
            return
        try:
            line = "dominaite-python: {0} {1} {2} {3}ms".format(
                method, path, status, int((time.monotonic() - started) * 1000)
            )
            getter = getattr(headers, "get", None)
            request_id = (
                _clean_header(getter(REQUEST_ID_HEADER), _MAX_REQUEST_ID_LENGTH) if getter else ""
            )
            if request_id:
                line += " request_id=" + request_id
            if idempotency_key:
                line += " idempotency_key=" + idempotency_key
            self._on_debug(line)
        except Exception:
            pass


def _retry_reason(error: BaseException) -> str:
    """What a retry warning names: the error code, else the HTTP status, else the class."""
    code = getattr(error, "error_code", None)
    if code:
        return str(getattr(code, "value", code))
    status = getattr(error, "http_status", None)
    if status:
        return "HTTP {0}".format(status)
    return type(error).__name__


def _rejection(reply: _Reply) -> DominaiteError:
    """The generic reading of a failed reply: 5xx is the API being unavailable, 4xx a rejection."""
    if reply.status >= 500:
        # Still a transport error (nothing to fix in the call, retry with the same key),
        # but the code the API sent is kept.
        code = reply.error.get("code") or reply.payload.get("errorCode")
        return TransportError(
            "The Dominaite API is unavailable (HTTP {0}{1}); "
            "retry with the same idempotency key.".format(
                reply.status, ", " + str(code) if code else ""
            ),
            http_status=reply.status,
            error_code=str(code) if code else None,
            result=dict(reply.envelope),
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
        result=dict(reply.envelope),
    )


def _auth_message(status: int, code: str, server_message: Any) -> str:
    """The message of an AuthenticationError: a credential hint on 401 only.

    A 403 means the signature was good, so pointing the caller at the key id, the secret or
    the clock would send them after the wrong problem.
    """
    if status == 401:
        return "Authentication failed - check your key id, secret, and server clock."
    if code == ErrorCode.INSUFFICIENT_SCOPE:
        # The server names the scope the route needs; keep that, then say what to do.
        named = str(server_message or "This API key does not hold the scope this route needs")
        return (
            named.rstrip(".") + ". Use a key that holds that scope; a key's scopes are "
            "fixed when it is issued."
        )
    if code == ErrorCode.VERIFY_ONLY_NOT_ENABLED:
        return (
            "Verify-only sessions are not enabled for your account. Nothing was created. "
            "We switch it on for your account: ask Dominaite support."
        )
    return "The API refused this request for this key (HTTP 403){0}".format(
        ": " + str(server_message) if server_message else "."
    )


def _charge(data: Dict[str, Any]) -> Dict[str, Any]:
    """The charge body as one shape: every contract field is a key.

    The gateway omits a null key (``declineClass`` and ``declineCode`` on a 201, or
    ``agreementId`` on a charge outside an agreement), so absent reads as None. Anything
    else the gateway sends is carried through as sent, ``sequence`` included.
    """
    charge = dict(data)
    charge["chargeId"] = str(data.get("chargeId"))
    charge["status"] = str(data.get("status") or "")
    charge["declineClass"] = data["declineClass"] if isinstance(data.get("declineClass"), str) else None
    charge["declineCode"] = data["declineCode"] if isinstance(data.get("declineCode"), str) else None
    charge["transactionId"] = str(data.get("transactionId") or "")
    charge["agreementId"] = data["agreementId"] if isinstance(data.get("agreementId"), str) else None
    return charge


def _refund_result(reply: _Reply) -> Refund:
    """Read a refund route's reply: a refund on 2xx, a RefundError when the gateway sent a code."""
    if 200 <= reply.status < 300:
        if isinstance(reply.payload.get("refundId"), str):
            return _refund(reply.payload)
        raise ApiError(
            reply.status,
            "The API answered the refund without a refund body",
            error_code="UNEXPECTED_RESPONSE",
        )
    raise _refund_failure(reply)


def _checked(reply: _Reply) -> _Reply:
    """Apply the generic failure rules to a reply: 5xx is transport, 4xx is ApiError."""
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
    return reply


def _refund_list(reply: _Reply) -> RefundList:
    """Read the refund list route's reply: every refund in the one :class:`Refund` shape."""
    if 200 <= reply.status < 300:
        refunds = reply.payload.get("refunds")
        if isinstance(refunds, list):
            listing = dict(reply.payload)
            listing["refunds"] = [_refund(item) for item in refunds if isinstance(item, dict)]
            return cast(RefundList, listing)
        raise ApiError(
            reply.status,
            "The API answered the refund list without a list",
            error_code="UNEXPECTED_RESPONSE",
        )
    raise _refund_failure(reply)


def _refund_failure(reply: _Reply) -> DominaiteError:
    """The error for a refund route's reply that is not a 2xx."""
    error_code = reply.error.get("code") or reply.payload.get("errorCode")
    # Every 4xx with a code is the refund route telling us something specific, 400 and 404
    # included (REFUND_NOT_FOUND is retryable, PAYMENT_NOT_FOUND is not). Auth and rate
    # limiting never get here; a 5xx stays a TransportError: nothing was queued.
    if error_code and 400 <= reply.status < 500:
        return RefundError(
            reply.status,
            str(error_code),
            str(reply.error.get("message") or reply.payload.get("errorMessage") or "The refund was refused."),
            result=dict(reply.envelope),
        )
    if reply.status >= 400:
        return _rejection(reply)
    return ApiError(
        reply.status,
        "Unexpected HTTP {0} response; the Dominaite API answers 2xx or a "
        "documented error. Check base_url and anything proxying it.".format(reply.status),
        error_code="UNEXPECTED_STATUS",
    )


def _optional_str(data: Dict[str, Any], key: str) -> Optional[str]:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _refund(data: Dict[str, Any]) -> Refund:
    """The refund body as one shape: the gateway omits null fields, absent reads as None."""
    refund = dict(data)
    refund["refundId"] = str(data.get("refundId") or "")
    refund["transactionId"] = str(data.get("transactionId") or "")
    refund["status"] = str(data.get("status") or "")
    amount = data.get("amount")
    refund["amount"] = amount if isinstance(amount, int) and not isinstance(amount, bool) else None
    refund["currency"] = str(data.get("currency") or "")
    refund["failureCode"] = _optional_str(data, "failureCode")
    refund["failureMessage"] = _optional_str(data, "failureMessage")
    refund["completedAt"] = _optional_str(data, "completedAt")
    return cast(Refund, refund)


def _stored_payment_method(data: Dict[str, Any]) -> Dict[str, Any]:
    """Same rule for the card on file: brand, last4, the expiry and retiredReason may be absent."""
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
    stored["retiredReason"] = data["retiredReason"] if isinstance(data.get("retiredReason"), str) else None
    return stored
