"""Typed shapes of the objects the API hands back.

These are ``TypedDict``s: at runtime every one is a plain dict, so indexing and ``.get``
work as usual and nothing is parsed or validated on the way in.
"""

from typing import Optional, TypedDict


class StoredPaymentMethod(TypedDict):
    """A card kept on file, as ``storedPaymentMethod`` on :meth:`DominaiteClient.get_status`
    and as ``data["storedPaymentMethod"]`` on ``payment.*`` webhooks.

    ``id`` is the opaque handle (``pm_`` + 32 hex characters) you charge and revoke with.
    ``status`` is a :class:`StoredPaymentMethodStatus` value; ``retiredReason`` is a
    :class:`StoredPaymentMethodRetiredReason` value, None unless the card was retired.
    ``brand``, ``last4`` and the expiry are None when the provider did not report them.

    Missing or None means no card was saved as far as this read or event knows. On a
    webhook it can also be None when a card WAS saved: the card can be stored after the
    approval was already announced (a server-to-server payment approved on the spot, or
    one settled later), and it is only ever set on ``payment.succeeded`` and
    ``payment.requires_capture``. The status read is the source of truth: on a
    ``save_card`` session whose webhook has no card, call :meth:`DominaiteClient.get_status`.
    """

    id: str
    brand: Optional[str]
    last4: Optional[str]
    expiryMonth: Optional[int]
    expiryYear: Optional[int]
    status: str
    retiredReason: Optional[str]


class Refund(TypedDict):
    """A refund, as :meth:`DominaiteClient.create_refund` and
    :meth:`DominaiteClient.get_refund` return it.

    ``refundId`` is ``re_`` + 32 hex characters; ``transactionId`` is the payment being
    refunded. ``status`` is a :class:`RefundStatus` value. The gateway omits null fields on
    the wire; the SDK fills them in as None, so every key is always present.

    - ``amount``: minor units. On ``pending``, the amount you asked for (None for a full
      refund); on ``processing``, the amount being refunded (None until a full refund has
      been sized); on ``succeeded``, the amount actually refunded; always None on ``failed``.
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
