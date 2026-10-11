"""Typed shapes of the objects the API hands back.

These are ``TypedDict``s: at runtime every one is a plain dict, so indexing and ``.get``
work as usual and nothing is parsed or validated on the way in.
"""

from typing import List, Optional, TypedDict


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


class RefundList(TypedDict):
    """The refunds of one payment, as :meth:`DominaiteClient.list_refunds` returns them.

    - ``refunds``: the refunds created through this API, newest first, each a
      :class:`Refund` with every field present.
    - ``refundedAmount``: minor units refunded so far, counting every refund of the
      payment, the ones made in the dashboard included.
    - ``refundableAmount``: minor units a new refund could take now: what is not refunded
      yet, minus refunds still in progress. 0 when the payment cannot be refunded.

    Both amounts are in ``currency``, the payment's currency.
    """

    transactionId: str
    currency: str
    refundedAmount: int
    refundableAmount: int
    refunds: List[Refund]


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


class _PlanRequired(TypedDict):
    id: str
    name: str
    amount: int
    currency: str
    intervalUnit: str
    intervalCount: int
    trialDays: int
    status: str
    createdAt: str


class Plan(_PlanRequired, total=False):
    """A subscription plan, as the plan methods of :class:`DominaiteClient` return it.

    ``id`` is ``plan_`` + 32 hex characters. It is case-sensitive: send it exactly as it
    was returned. ``amount`` is the charge per period in MINOR units of ``currency``. One
    period is ``intervalCount`` times ``intervalUnit`` (``day``, ``week``, ``month`` or
    ``year``). ``status`` is ``active`` or ``archived``, a plain string. The gateway leaves
    out a key whose value is null, so read these with ``.get``:

    - ``periodCount``: the number of periods after which an agreement completes. Absent
      when agreements bill until they are cancelled.
    - ``cancelUrl``: your page where a customer cancels, printed on customer emails.
    - ``archivedAt``: when the plan was archived.
    """

    periodCount: Optional[int]
    cancelUrl: Optional[str]
    archivedAt: Optional[str]


class _AgreementConsentRequired(TypedDict):
    textVersion: str
    acceptedAt: str
    ip: str


class AgreementConsent(_AgreementConsentRequired, total=False):
    """``consent`` on an :class:`Agreement`: the customer's acceptance as you sent it, plus
    the platform's receipt of it.

    ``textVersion``, ``acceptedAt`` and ``ip`` are what you sent, on
    :meth:`DominaiteClient.create_agreement` or on the subscription session. ``receivedAt``
    is when the platform received the consent. ``receivedFromIp`` is the address of your
    server that sent it, not the customer's. It is absent unless the platform recorded it.
    """

    receivedAt: Optional[str]
    receivedFromIp: Optional[str]


class _AgreementRequired(TypedDict):
    id: str
    planId: str
    customerReference: str
    status: str
    amount: int
    currency: str
    intervalUnit: str
    intervalCount: int
    trialDays: int
    version: int
    sequence: int
    createdAt: str


class Agreement(_AgreementRequired, total=False):
    """A customer's subscription to a plan, as the agreement methods of
    :class:`DominaiteClient` return it.

    ``id`` is ``agr_`` + 32 hex characters, case-sensitive. ``status`` is a plain string:
    ``pending``, ``active``, ``past_due``, ``cancelled``, ``completed`` or ``failed`` today.
    A value the SDK does not know yet is valid. ``version`` starts at 1 and goes up when
    the price changes for a future period. ``sequence`` is the number the latest
    ``agreement.*`` webhook carried, 0 before the first change. The gateway leaves out a
    key whose value is null, so read these with ``.get``:

    - ``storedPaymentMethodId``: the ``pm_`` id of the card on file, absent until saved.
    - ``maxAmountPerPeriod``: the most one charge may be on a variable agreement.
    - ``nextChargeAt``: the next scheduled charge, absent while pending and once ended.
    - ``pastDueReason``: why the agreement is ``past_due``. ``card_expired`` and
      ``card_revoked``: the card on file cannot be charged, so ask for a new card.
      ``hard_decline``: the issuer refused the card, and a retry on it is pointless.
      ``dunning_exhausted``: the retries after soft declines ran out. ``sca_required``: the
      issuer wants the customer to authenticate a payment in person, and a new card does
      not help. ``unprocessable``: the platform refused the charge for a reason that no
      retry fixes; contact support. A value the SDK does not know yet is valid.
    - ``consent``: an :class:`AgreementConsent`, absent until consent is recorded.
    """

    storedPaymentMethodId: Optional[str]
    maxAmountPerPeriod: Optional[int]
    periodCount: Optional[int]
    nextChargeAt: Optional[str]
    pastDueReason: Optional[str]
    pastDueAt: Optional[str]
    consent: Optional[AgreementConsent]
    activatedAt: Optional[str]
    cancelledAt: Optional[str]


class _WebhookEndpointRequired(TypedDict):
    id: str
    url: str
    events: List[str]
    status: str
    createdAt: str


class WebhookEndpoint(_WebhookEndpointRequired, total=False):
    """A webhook endpoint, as the webhook endpoint methods of :class:`DominaiteClient`
    return it.

    ``id`` is a UUID. ``status`` is a plain string: ``active`` (deliveries are sent) or
    ``inactive`` (deliveries are paused, in the dashboard or after repeated failed
    deliveries). ``secret`` is the ``whsec_`` signing secret for :func:`verify_webhook`. It
    is only on the first answer of :meth:`DominaiteClient.create_webhook_endpoint`, so store
    it when you get it.
    """

    secret: str


class _TransactionRequired(TypedDict):
    transactionId: str
    type: str
    status: str
    amount: int
    currency: str
    createdAt: str


class Transaction(_TransactionRequired, total=False):
    """One row of :meth:`DominaiteClient.list_transactions`: a card payment.

    ``type`` is ``payment`` (the payer paid on the checkout), ``charge`` (a saved card was
    charged) or ``verification`` (a verify-only session saved a card; not a purchase).
    ``status`` is the same value :meth:`DominaiteClient.get_status` answers. Both are plain
    strings, and a value the SDK does not know yet is valid. The gateway leaves out a key
    whose value is null, so read these with ``.get``:

    - ``orderReference``: your own order id, absent when you sent none.
    - ``completedAt``: when the payment first succeeded.
    - ``storedPaymentMethodId``: on a ``charge``, the ``pm_`` id of the card charged.
    - ``refundedAmount``: the total refunded so far in MINOR units, absent when nothing was.
    """

    orderReference: Optional[str]
    completedAt: Optional[str]
    storedPaymentMethodId: Optional[str]
    refundedAmount: Optional[int]


class TransactionPage(TypedDict):
    """One page of :meth:`DominaiteClient.list_transactions`.

    ``items`` holds the rows of this page, oldest first, and is empty when the window has
    none. ``nextCursor`` is the ``cursor`` of the next page, and None on the last page.
    """

    items: List[Transaction]
    nextCursor: Optional[str]
