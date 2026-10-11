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


class _CheckoutSessionRequired(TypedDict):
    transactionId: str
    orderId: str
    cashierKey: str
    cashierToken: str
    amount: int
    currency: str
    expiresAt: str
    integration: str


class CheckoutSession(_CheckoutSessionRequired, total=False):
    """A checkout session, as :meth:`DominaiteClient.create_checkout_session` returns it.

    The method returns a plain dict; this type names its keys. The gateway leaves out a key
    whose value is null, so read these with ``.get``:

    - ``clientSecret``: set only on a ``"fields"`` session. Keep it out of your logs.
    - ``agreementId``: the agreement this session takes the first payment for, when you
      sent ``agreement_id``.
    - ``verifyOnly``: True on a verify-only session, absent on every other session.
    """

    clientSecret: str
    agreementId: str
    verifyOnly: bool


class _CheckoutAgreementRequired(TypedDict):
    id: str
    status: str


class CheckoutAgreement(_CheckoutAgreementRequired, total=False):
    """``agreement`` on :meth:`DominaiteClient.get_status`: the agreement that a subscription
    session took the first payment for, or that a charge was placed under.

    ``status`` is the agreement's CURRENT status, for example ``pending``, ``active`` or
    ``past_due``. Read it, not the payment status, to know if the subscription started.
    ``nextChargeAt`` is the next scheduled charge, ISO 8601 UTC. It is absent while the
    agreement is pending and once it has ended.
    """

    nextChargeAt: Optional[str]


class _CheckoutStatusRequired(TypedDict):
    transactionId: str
    orderId: str
    status: str
    amount: int
    currency: str
    createdAt: str


class CheckoutStatus(_CheckoutStatusRequired, total=False):
    """The status read, as :meth:`DominaiteClient.get_status` returns it.

    The method returns the gateway's object as sent, in a plain dict; this type names its
    keys. The gateway leaves out a key whose value is null, so read these with ``.get``.
    Every status and class is a plain string: a value the SDK does not know yet is valid.

    - ``declineClass``: what to do with a ``failed`` payment (:data:`DECLINE_CLASSES`). It
      can be None or absent on a failure too, when the decline cannot be classified yet.
    - ``isRecurring``: True for a charge on a stored card, False for a checkout session.
    - ``verifyOnly``: True on a verify-only session, absent on every other session. The
      status is ``requires_capture``, then ``cancelled`` once the hold is released. A
      ``storedPaymentMethod`` is what says that the card is verified.
    - ``storedPaymentMethod``: the card kept on file, a :class:`StoredPaymentMethod`.
    - ``agreement``: a :class:`CheckoutAgreement`, absent on a one-off payment.
    - ``sequence``: orders this read against the ``charge.*`` events of the same object.
      0 on a checkout payment.
    - ``expiresAt``: until when the payer can still act. Absent once the payer's part is
      over, which is not the same as a final status.
    """

    orderReference: Optional[str]
    pspReference: Optional[str]
    refundedAmount: Optional[int]
    paymentMethod: Optional[str]
    walletType: Optional[str]
    declineClass: Optional[str]
    isRecurring: bool
    verifyOnly: bool
    storedPaymentMethod: Optional[StoredPaymentMethod]
    agreement: Optional[CheckoutAgreement]
    updatedAt: Optional[str]
    expiresAt: Optional[str]
    sequence: int
