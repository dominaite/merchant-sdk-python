"""Exceptions raised by the Dominaite merchant API client.

The split matters when you write your error handling: a CheckoutRefused means the
gateway understood you and said no, a TransportError means you do not know whether
the request landed. Only the second one is safe to retry.

A RateLimitError is a third case: the request definitely did not land, and it is safe
to send again once you have waited out ``retry_after_seconds``. The SDK never retries
it for you.
"""

from enum import Enum
from typing import Any, Dict, Optional, Tuple


class ErrorCode(str, Enum):
    """Every machine-readable ``error_code`` this SDK documents, as named constants.

    Subclasses ``str``, so ``error.error_code == ErrorCode.STOREFRONT_NOT_WHITELISTED``
    works against the plain string on any exception. The tuples below say which codes
    arrive on which exception; this is just the names. Treat a code that is not listed
    as a failure too rather than crashing on it.
    """

    # Authentication (AuthenticationError, HTTP 401).
    INVALID_API_KEY = "INVALID_API_KEY"
    INVALID_SIGNATURE = "INVALID_SIGNATURE"
    TIMESTAMP_OUT_OF_RANGE = "TIMESTAMP_OUT_OF_RANGE"
    IP_NOT_ALLOWED = "IP_NOT_ALLOWED"

    # Permission (AuthenticationError, HTTP 403). The key authenticated, but it may not do this.
    INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
    VERIFY_ONLY_NOT_ENABLED = "VERIFY_ONLY_NOT_ENABLED"

    # Rate limiting (RateLimitError, HTTP 429). The two CHARGE_ codes come from the charge route.
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    CHARGE_VELOCITY_EXCEEDED = "CHARGE_VELOCITY_EXCEEDED"
    CHARGE_DECLINE_RATE_EXCEEDED = "CHARGE_DECLINE_RATE_EXCEEDED"

    # Input validation on create (ApiError, HTTP 400).
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"

    # Session refusals (CheckoutRefusedError, HTTP 200 with success false). Some of these
    # also come back from a charge, as ChargeError.
    PAYMENT_PROCESSING_UNAVAILABLE = "PAYMENT_PROCESSING_UNAVAILABLE"
    DUPLICATE_REQUEST = "DUPLICATE_REQUEST"
    ALREADY_PROCESSED = "ALREADY_PROCESSED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    PRIOR_ATTEMPT_FAILED = "PRIOR_ATTEMPT_FAILED"

    # Storefront refusals on create (StorefrontError).
    STOREFRONT_NOT_WHITELISTED = "STOREFRONT_NOT_WHITELISTED"
    STOREFRONT_INACTIVE = "STOREFRONT_INACTIVE"
    STOREFRONT_MISMATCH = "STOREFRONT_MISMATCH"

    # Stored payment method charges and revokes (ChargeError, RevokeError).
    PAYMENT_METHOD_NOT_ACTIVE = "PAYMENT_METHOD_NOT_ACTIVE"
    CHARGE_OUTCOME_UNKNOWN = "CHARGE_OUTCOME_UNKNOWN"
    CHARGE_FAILED = "CHARGE_FAILED"
    PAYMENT_METHOD_CHARGES_DISABLED = "PAYMENT_METHOD_CHARGES_DISABLED"
    UPSTREAM_CONTRACT_ERROR = "UPSTREAM_CONTRACT_ERROR"
    MERCHANT_API_UNAVAILABLE = "MERCHANT_API_UNAVAILABLE"

    # Refunds (RefundError). REFUND_AMOUNT_EXCEEDED and PAYMENT_NOT_REFUNDABLE are also
    # failureCode values on a failed refund; REFUND_FAILED is only ever a failureCode.
    PAYMENT_NOT_FOUND = "PAYMENT_NOT_FOUND"
    REFUND_NOT_FOUND = "REFUND_NOT_FOUND"
    PAYMENT_NOT_REFUNDABLE = "PAYMENT_NOT_REFUNDABLE"
    REFUND_AMOUNT_EXCEEDED = "REFUND_AMOUNT_EXCEEDED"
    REFUND_FAILED = "REFUND_FAILED"


#: Every ``errorCode`` the API can refuse a checkout session with - a business refusal,
#: sent as HTTP 200 with ``success: false``. Listed so you can assert your own handling
#: covers all of them; treat an unlisted code as a refusal too rather than crashing.
SESSION_REFUSAL_ERROR_CODES: Tuple[str, ...] = (
    "PAYMENT_PROCESSING_UNAVAILABLE",
    "DUPLICATE_REQUEST",
    "ALREADY_PROCESSED",
    "IDEMPOTENCY_KEY_REUSED",
    "PRIOR_ATTEMPT_FAILED",
)

#: Input validation codes on the create endpoint. These are HTTP 400, NOT the
#: ``success: false`` refusal shape, and arrive as :class:`ApiError` with the code on
#: ``error_code``. They mean the request was malformed - fix the call, do not retry.
VALIDATION_ERROR_CODES: Tuple[str, ...] = ("IDEMPOTENCY_KEY_REQUIRED",)

#: The codes for a key that authenticated but may not call the route, HTTP 403, in contract
#: order. They arrive as :class:`AuthenticationError` with ``http_status`` 403. Not retryable:
#: ``INSUFFICIENT_SCOPE`` means the key does not hold the route's scope (``payments``,
#: ``transactions:read`` or ``webhooks:manage``), and a key's scopes are fixed when it is issued.
AUTHORIZATION_ERROR_CODES: Tuple[str, ...] = ("INSUFFICIENT_SCOPE",)

#: The storefront codes a create call can be refused with, raised as
#: :class:`StorefrontError`. ``STOREFRONT_NOT_WHITELISTED`` and ``STOREFRONT_INACTIVE``
#: are HTTP 409, ``STOREFRONT_MISMATCH`` is HTTP 400; none is retryable. In contract
#: order. Not in :data:`SESSION_REFUSAL_ERROR_CODES`: these are not the 200
#: ``success: false`` shape.
STOREFRONT_ERROR_CODES: Tuple[str, ...] = (
    "STOREFRONT_MISMATCH",
    "STOREFRONT_INACTIVE",
    "STOREFRONT_NOT_WHITELISTED",
)

#: The codes :meth:`DominaiteClient.charge_payment_method` raises as
#: :class:`ChargeError`, in the gateway's own order. ``CHARGE_DECLINED`` (HTTP 402) is
#: deliberately not here: a decline is a charge result with ``status`` ``failed``, not
#: an exception.
CHARGE_ERROR_CODES: Tuple[str, ...] = (
    "PAYMENT_METHOD_NOT_ACTIVE",
    "DUPLICATE_REQUEST",
    "IDEMPOTENCY_KEY_REUSED",
    "CHARGE_OUTCOME_UNKNOWN",
    "CHARGE_FAILED",
    "PAYMENT_METHOD_CHARGES_DISABLED",
    "PAYMENT_PROCESSING_UNAVAILABLE",
)

#: The codes :meth:`DominaiteClient.revoke_payment_method` raises as
#: :class:`RevokeError`, in the gateway's own order.
REVOKE_ERROR_CODES: Tuple[str, ...] = ("UPSTREAM_CONTRACT_ERROR", "MERCHANT_API_UNAVAILABLE")

#: The codes :meth:`DominaiteClient.create_refund` and :meth:`DominaiteClient.get_refund`
#: raise as :class:`RefundError`, in contract order. ``REFUND_FAILED`` is not here: a
#: refund that fails is a result with ``status`` ``failed``, not an exception.
REFUND_ERROR_CODES: Tuple[str, ...] = (
    "PAYMENT_NOT_FOUND",
    "REFUND_NOT_FOUND",
    "PAYMENT_NOT_REFUNDABLE",
    "REFUND_AMOUNT_EXCEEDED",
    "IDEMPOTENCY_KEY_REUSED",
    "DUPLICATE_REQUEST",
    "IDEMPOTENCY_KEY_REQUIRED",
)

#: The ``failureCode`` values a failed refund can carry, in contract order. Treat a value
#: that is not listed as ``REFUND_FAILED``.
REFUND_FAILURE_CODES: Tuple[str, ...] = (
    "REFUND_AMOUNT_EXCEEDED",
    "PAYMENT_NOT_REFUNDABLE",
    "REFUND_FAILED",
)

#: The refund codes worth retrying WITH THE SAME idempotency key, and for how many
#: seconds before giving up. Every other refund code is final for that request.
REFUND_RETRY_WINDOWS_SECONDS: Dict[str, int] = {
    "REFUND_NOT_FOUND": 60,
    "DUPLICATE_REQUEST": 120,
}


class DominaiteError(Exception):
    """Base class for every error this SDK raises.

    Catch this if you only care that the payment call failed. Catch the subclasses
    when you want to branch on why.
    """


class ApiError(DominaiteError):
    """The API answered, but with an unexpected or rejecting response.

    ``error_code`` carries the machine-readable code when the API sent one - notably
    the input validation codes in :data:`VALIDATION_ERROR_CODES`, which arrive as
    HTTP 400 rather than as a business refusal. It is None when the response carried
    no code (an unexpected shape, or a bare 404); branch on it only after checking it
    is set.

    ``result`` is the whole JSON body the API sent ({} when there was none). Read the
    details the gateway attaches there: ``result["error"]["validationErrors"]`` lists the
    fields a 400 refused, and some codes carry ``params`` (for example the existing
    endpoint's ``webhookId`` on ``WEBHOOK_ALREADY_REGISTERED``).
    """

    def __init__(
        self,
        http_status: int,
        message: str,
        error_code: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        #: The full JSON body the API sent, for fields not modelled above.
        self.result = result if result is not None else {}


class RateLimitError(ApiError):
    """The API answered HTTP 429: you are sending faster than your key is allowed to.

    Current platform limits: **60 requests per minute per API key**, and **120 requests
    per minute per IP address**. Both are sliding windows, and the IP limit is shared
    across every key sending from that address, so a busy host can hit it while each
    individual key is well inside its own budget.

    The SDK does NOT retry this for you, and neither does
    :meth:`DominaiteClient.create_checkout_session_with_retry`. Retrying a 429
    immediately is what turns a short spike into a sustained lockout, and only your code
    knows what else is queued behind this call. Back off, then retry.

    ``error_code`` is ``RATE_LIMIT_EXCEEDED`` for the per-key and per-IP limits. A charge
    can also answer 429 with its own code, and nothing was charged:

    - ``CHARGE_VELOCITY_EXCEEDED``: the card already has the most charges allowed in 24
      hours, or the charge would pass your hourly limit for the currency.
    - ``CHARGE_DECLINE_RATE_EXCEEDED``: this API key collected too many declined charges in
      a short time, so its charge route is locked for a while. Other routes keep working.

    ``retry_after_seconds`` is the whole number of seconds the API asked you to wait,
    taken from the ``Retry-After`` header. Honour it - it is the shortest wait that will
    work. It is None when the API sent no such header (the block after many failed
    authentication attempts from one IP address sends none), or sent it in the HTTP-date
    form (which we do not translate, since a date is only meaningful against the server's
    clock). Fall back to your own backoff in that case::

        try:
            session = client.create_checkout_session(...)
        except RateLimitError as limit:
            time.sleep(limit.retry_after_seconds or 60)

    Subclasses :class:`ApiError`, so existing ``except ApiError`` handlers keep catching
    429s; catch this first when you want to branch on it.
    """

    def __init__(
        self,
        message: str,
        retry_after_seconds: Optional[int] = None,
        error_code: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(429, message, error_code=error_code, result=result)
        #: Seconds the API asked you to wait, or None when it did not say.
        self.retry_after_seconds = retry_after_seconds


class StorefrontError(ApiError):
    """The gateway would not open a session for this storefront (website). Nothing was
    created; retrying the same call will get the same answer until the setup changes.

    Branch on ``error_code``:

    - ``STOREFRONT_NOT_WHITELISTED`` (409): the storefront's domain is not yet approved
      by the payment provider. Nothing to fix in your code: ask Dominaite support to
      finish the domain whitelisting, then try again.
    - ``STOREFRONT_INACTIVE`` (409): the storefront was deactivated or deleted. Use an
      API key for an active storefront, or ask support to reactivate it.
    - ``STOREFRONT_MISMATCH`` (400): the API key is bound to a different storefront
      than the request names.

    An idempotency key first used on another storefront is refused with
    ``STOREFRONT_MISMATCH`` too, but in the HTTP 200 shape, so that one arrives as
    :class:`CheckoutRefusedError`. Match on ``error_code`` rather than the exception
    type if you want to handle both.

    Subclasses :class:`ApiError`, so existing ``except ApiError`` handlers keep
    catching it, with ``http_status`` and ``error_code`` set.
    """


class AuthenticationError(DominaiteError):
    """The API rejected your credentials or signature (HTTP 401), or refused the request
    for this key (HTTP 403). ``http_status`` tells the two apart.

    Not retryable. A 401 is a credential problem - fix the key id, secret, or server
    clock. Machine-readable code on ``error_code``:

    - ``INVALID_API_KEY``: wrong or revoked key id.
    - ``INVALID_SIGNATURE``: your signing is wrong; re-run the test vector.
    - ``TIMESTAMP_OUT_OF_RANGE``: server clock is off; fix NTP, do not retry-loop.
    - ``IP_NOT_ALLOWED``: this key is locked to addresses that don't include yours.

    A 403 is NOT a credential problem: the key and the signature are good, but the key may
    not do this. Do not rotate the key or touch the clock for it:

    - ``INSUFFICIENT_SCOPE``: the key does not hold the scope this route needs. A
      ``payments`` key makes payments; a reporting key holds ``transactions:read``,
      ``webhooks:manage`` or both. Use a key with the right scope.
    - ``VERIFY_ONLY_NOT_ENABLED``: ``verify_only`` sessions are not enabled for your
      account. Nothing was created. We switch it on for your account: ask support.

    ``result`` is the whole JSON body the API sent.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        http_status: int = 401,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        #: 401 for a credential failure, 403 for a key that may not do this.
        self.http_status = http_status
        #: The full JSON body the API sent, for fields not modelled above.
        self.result = result if result is not None else {}


class CheckoutRefusedError(DominaiteError):
    """The gateway understood the request but refused to open a checkout session.

    Branch on ``error_code``:

    - ``PAYMENT_PROCESSING_UNAVAILABLE``: card payments are off right now; retry later.
    - ``DUPLICATE_REQUEST``: a session for this idempotency key is already open.
    - ``ALREADY_PROCESSED``: this idempotency key's payment already completed.
    - ``PRIOR_ATTEMPT_FAILED``: a prior attempt with this key failed terminally; use a fresh key.
    - ``IDEMPOTENCY_KEY_REUSED``: same key sent with a DIFFERENT body; use a fresh key.

    On a replay refusal the API also tells you WHICH payment your key collided with,
    on ``transaction_id``. That is the recovery path: read it with
    :meth:`DominaiteClient.get_status` to find out what the earlier attempt did,
    rather than minting a second payment for the same order::

        try:
            session = client.create_checkout_session(...)
        except CheckoutRefusedError as refusal:
            if refusal.transaction_id:
                status = client.get_status(refusal.transaction_id)

    ``transaction_id`` is None when the API did not name one - notably the
    concurrent-race ``DUPLICATE_REQUEST``, which knows a key was taken but not yet
    by which row. Always check before using it.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        transaction_id: Optional[str] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.transaction_id = transaction_id
        #: The full unwrapped refusal payload, for fields not modelled above.
        self.result = result if result is not None else {}


class ChargeError(DominaiteError):
    """The gateway answered a charge with an error code instead of a charge result.

    The HTTP status is on ``http_status``, the machine-readable code on ``error_code``
    and the charge row the gateway attached (when it did) on ``charge``, with
    ``transaction_id`` as a shortcut to its ``transactionId``. Branch on ``error_code``:

    - ``CHARGE_OUTCOME_UNKNOWN`` (502): the provider gave no verdict and the charge MAY
      have happened. ``charge`` is set: poll :meth:`DominaiteClient.get_status` with
      ``transaction_id`` or wait for the webhook. Never retry under a new key.
    - ``CHARGE_FAILED`` (502): nothing was charged. ``charge`` is set when a row exists
      (its ``declineClass`` and ``declineCode`` are None), None when the provider
      refused before one.
    - ``PAYMENT_METHOD_NOT_ACTIVE`` (409): the method is revoked, expired or retired;
      bring the customer back for a hosted session with ``save_card=True``.
    - ``DUPLICATE_REQUEST`` (409): a request with this key is still in flight; retry
      with the SAME key in a moment.
    - ``IDEMPOTENCY_KEY_REUSED`` (422): same key, different body or method; a bug on
      your side.
    - ``PAYMENT_PROCESSING_UNAVAILABLE`` (503): nothing was charged; retry later with the
      SAME key.
    - ``PAYMENT_METHOD_CHARGES_DISABLED`` (503): nothing was charged. If we never switched
      stored-card charges on for your account, a retry does not help: ask Dominaite support.
      If they are on, retry later with the SAME idempotency key. The code is the same in
      both cases.

    ``result`` is the whole envelope the gateway sent, for fields not modelled above.
    """

    def __init__(
        self,
        http_status: int,
        error_code: str,
        message: str,
        charge: Optional[Dict[str, Any]] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        #: The charge row the gateway attached to its answer, when it did.
        self.charge = charge
        #: ``charge["transactionId"]`` when ``charge`` is set, for polling.
        self.transaction_id = charge.get("transactionId") if charge else None
        #: The full envelope, for fields not modelled above.
        self.result = result if result is not None else {}


class RevokeError(DominaiteError):
    """The gateway refused to revoke a stored payment method. Nothing changed.

    Branch on ``error_code``:

    - ``MERCHANT_API_UNAVAILABLE`` (503): the provider is unavailable or throttling;
      retry later.
    - ``UPSTREAM_CONTRACT_ERROR`` (502): the provider refused the deletion for a reason
      a retry will not fix; contact support with the payment method id.

    An id that is not yours is still the generic :class:`ApiError` with
    ``http_status`` 404.
    """

    def __init__(
        self,
        http_status: int,
        error_code: str,
        message: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        #: The full envelope, for fields not modelled above.
        self.result = result if result is not None else {}


class RefundError(ApiError):
    """The gateway answered a refund call with an error code instead of a refund.

    ``http_status`` and ``error_code`` say what happened, ``retryable`` whether the same
    call WITH THE SAME idempotency key can succeed later, and ``retry_stop_after_seconds``
    how long to keep trying (None when not retryable). Branch on ``error_code``:

    - ``PAYMENT_NOT_FOUND`` (404): no card-not-present payment with this id under your
      account. Not retryable.
    - ``REFUND_NOT_FOUND`` (404, :meth:`DominaiteClient.get_refund` only): right after the
      202 the refund may not be picked up yet. Retryable for up to 60 seconds; after that
      the id is unknown.
    - ``PAYMENT_NOT_REFUNDABLE`` (422): not paid, already fully refunded, or everything
      left is already being refunded. Nothing was queued and the key is not burnt.
    - ``REFUND_AMOUNT_EXCEEDED`` (422): more than what is left to refund, counting refunds
      in progress; the message names the amount left. Nothing was queued and the key is
      not burnt.
    - ``IDEMPOTENCY_KEY_REUSED`` (422): the key was first used for a different amount,
      reason or payment. Use a fresh key for a genuinely new refund.
    - ``DUPLICATE_REQUEST`` (409): a request with this key is being processed right now.
      Retry with the SAME key after a second, for up to 120 seconds.
    - ``IDEMPOTENCY_KEY_REQUIRED`` (400): the key is missing or too long.

    A 5xx is a :class:`TransportError` instead: nothing was queued, retry with the same
    key. Subclasses :class:`ApiError`, so ``except ApiError`` still catches it.
    ``result`` is the whole envelope the gateway sent.
    """

    def __init__(
        self,
        http_status: int,
        error_code: str,
        message: str,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(http_status, message, error_code=error_code, result=result)
        #: Seconds to keep retrying with the same key, or None when retrying cannot help.
        self.retry_stop_after_seconds = REFUND_RETRY_WINDOWS_SECONDS.get(error_code)
        #: True when the same call with the same key can still succeed.
        self.retryable = self.retry_stop_after_seconds is not None


class WebhookVerificationError(DominaiteError):
    """An incoming webhook is not authentic, or not fresh.

    Raised by :func:`dominaite.verify_webhook`. Never process the body after this -
    respond 400 and drop it. Machine-readable code on ``error_code``:

    - ``MALFORMED_SIGNATURE``: the header is missing, or not ``t=...,v1=...``. Usually
      the wrong header was read, or a proxy rewrote it.
    - ``INVALID_SIGNATURE``: wrong endpoint secret, or the body was modified. Also what
      you get when the body was re-serialized before verifying instead of passed raw,
      or when the raw bytes are not UTF-8 at all (we sign UTF-8, so they cannot be ours).
    - ``TIMESTAMP_OUT_OF_RANGE``: the signature is genuine but too old or too far in the
      future - a replay, or your server clock has drifted. Fix NTP; do not widen the
      tolerance to make it go away.
    - ``INVALID_PAYLOAD``: signed correctly but not a JSON object.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TransportError(DominaiteError):
    """Network-level failure or a 5xx.

    The request may or may not have reached the API. Safe to retry WITH THE SAME
    idempotency key; a retried key never creates a second payment.
    """
