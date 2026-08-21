"""Exceptions raised by the Dominaite merchant API client.

The split matters when you write your error handling: a CheckoutRefused means the
gateway understood you and said no, a TransportError means you do not know whether
the request landed. Only the second one is safe to retry.
"""

from typing import Any, Dict, Optional, Tuple

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
    """

    def __init__(
        self, http_status: int, message: str, error_code: Optional[str] = None
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code


class AuthenticationError(DominaiteError):
    """The API rejected your credentials or signature.

    Not retryable - fix the key id, secret, or server clock. Machine-readable code
    on ``error_code``:

    - ``INVALID_API_KEY``: wrong or revoked key id.
    - ``INVALID_SIGNATURE``: your signing is wrong; re-run the test vector.
    - ``TIMESTAMP_OUT_OF_RANGE``: server clock is off; fix NTP, do not retry-loop.
    - ``IP_NOT_ALLOWED``: this key is locked to addresses that don't include yours.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


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
