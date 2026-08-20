"""Exceptions raised by the Dominaite merchant API client.

The split matters when you write your error handling: a CheckoutRefused means the
gateway understood you and said no, a TransportError means you do not know whether
the request landed. Only the second one is safe to retry.
"""


class DominaiteError(Exception):
    """Base class for every error this SDK raises.

    Catch this if you only care that the payment call failed. Catch the subclasses
    when you want to branch on why.
    """


class ApiError(DominaiteError):
    """The API answered, but with an unexpected or rejecting response."""

    def __init__(self, http_status: int, message: str) -> None:
        super().__init__(message)
        self.http_status = http_status


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
    - ``IDEMPOTENCY_KEY_REUSED``: same key sent with a DIFFERENT body; use a fresh key.
    """

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


class TransportError(DominaiteError):
    """Network-level failure or a 5xx.

    The request may or may not have reached the API. Safe to retry WITH THE SAME
    idempotency key; a retried key never creates a second payment.
    """
