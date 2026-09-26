"""Typed shapes of the objects the API hands back.

These are ``TypedDict``s: at runtime every one is a plain dict, so indexing and ``.get``
work as usual and nothing is parsed or validated on the way in.
"""

from typing import Optional, TypedDict


class Refund(TypedDict):
    """A refund, as :meth:`DominaiteClient.create_refund` and
    :meth:`DominaiteClient.get_refund` return it.

    ``refundId`` is ``re_`` + 32 hex characters; ``transactionId`` is the payment being
    refunded. ``status`` is a :class:`RefundStatus` value. The gateway omits null fields on
    the wire; the SDK fills them in as None, so every key is always present.

    - ``amount``: minor units. Before success, the amount you asked for (None for a full
      refund); on ``succeeded``, the amount actually refunded; always None on ``failed``.
    - ``failureCode`` and ``failureMessage``: set on ``failed`` only. A code outside
      :data:`REFUND_FAILURE_CODES` means the same as ``REFUND_FAILED``.
    - ``completedAt``: ISO 8601 UTC, when the refund reached ``succeeded`` or ``failed``.
    """

    refundId: str
    transactionId: str
    status: str
    amount: Optional[int]
    currency: str
    failureCode: Optional[str]
    failureMessage: Optional[str]
    completedAt: Optional[str]
