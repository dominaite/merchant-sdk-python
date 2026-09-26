"""The payment status vocabulary :meth:`DominaiteClient.get_status` returns.

Only ``SUCCEEDED`` means the payment is complete. ``PENDING``, ``PROCESSING`` and
``REQUIRES_CAPTURE`` are all still open - keep polling. ``REQUIRES_CAPTURE`` in
particular is not "unpaid": the payer has paid and the funds are held awaiting
capture.

Match on these values, but do not treat the list as closed when you branch: a status
added to the API later must make you keep polling, never silently close an order that
is still live. That is why ``get_status()`` hands you the raw string rather than
parsing it into :class:`PaymentStatus` - an unknown value has to survive the trip to
your code instead of blowing up inside the SDK.
"""

from enum import Enum
from typing import Optional, Tuple


class PaymentStatus(str, Enum):
    """A payment status, in the order the API contract lists them.

    Subclasses ``str``, so ``status == PaymentStatus.SUCCEEDED`` works against the
    plain string ``get_status()`` returns, with no conversion on your side.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    CANCELLED = "cancelled"
    DISPUTED = "disputed"
    REQUIRES_CAPTURE = "requires_capture"
    ABANDONED = "abandoned"


#: Every status value the API can return today, in contract order.
PAYMENT_STATUSES: Tuple[str, ...] = tuple(member.value for member in PaymentStatus)

#: The statuses a payment does not leave on its own: stop polling on these. Everything
#: else, including a value added to the API later, is still open. ``DISPUTED`` is not
#: here: a dispute is still being decided.
TERMINAL_PAYMENT_STATUSES: Tuple[str, ...] = (
    PaymentStatus.SUCCEEDED.value,
    PaymentStatus.FAILED.value,
    PaymentStatus.CANCELLED.value,
    PaymentStatus.ABANDONED.value,
    PaymentStatus.REFUNDED.value,
    PaymentStatus.PARTIALLY_REFUNDED.value,
)


def is_paid(status: Optional[str]) -> bool:
    """True only for ``succeeded``: the one status that means you have been paid.

    ``requires_capture`` is an approved hold, not a payment, and a refunded payment is no
    longer money in hand, so both are False. Takes the raw string from
    :meth:`DominaiteClient.get_status` or a :class:`PaymentStatus` member.
    """
    return status == PaymentStatus.SUCCEEDED.value


def is_terminal(status: Optional[str]) -> bool:
    """True when the payment has settled and polling can stop.

    Terminal: ``succeeded``, ``failed``, ``cancelled``, ``abandoned``, ``refunded``,
    ``partially_refunded``. Not terminal: ``pending``, ``processing``,
    ``requires_capture``, ``disputed``, and any value this SDK does not recognise, so
    a status added to the API later keeps you polling instead of closing a live order.
    """
    return status in TERMINAL_PAYMENT_STATUSES


class StoredPaymentMethodStatus(str, Enum):
    """A stored payment method's state, in the order the API contract lists them.

    Read off ``get_status()["storedPaymentMethod"]["status"]``. Only ``ACTIVE`` methods
    can be charged. ``REVOKED`` is what :meth:`DominaiteClient.revoke_payment_method`
    leaves behind; ``EXPIRED`` means the card's expiry date has passed. ``RETIRED`` means
    the platform stopped the card on its own (``retiredReason`` says why); it never
    becomes active again, so ask the customer to save a card again. Treat a value you do
    not recognise as not chargeable.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    RETIRED = "retired"


#: Every stored payment method status the API can return today, in contract order.
STORED_PAYMENT_METHOD_STATUSES: Tuple[str, ...] = tuple(
    member.value for member in StoredPaymentMethodStatus
)


class StoredPaymentMethodRetiredReason(str, Enum):
    """Why the platform retired a stored payment method, in contract order.

    Read off ``get_status()["storedPaymentMethod"]["retiredReason"]``: None unless the
    status is ``retired``, and kept if you revoke the card afterwards. ``HARD_DECLINE``:
    a charge on it was declined as final. ``CHARGEBACK``: a charge on it was disputed.
    ``SOURCE_SALE_REVERSED``: the payment that saved it was fully refunded or disputed.
    Treat a value you do not recognise as retired for an unknown reason.
    """

    HARD_DECLINE = "hard_decline"
    CHARGEBACK = "chargeback"
    SOURCE_SALE_REVERSED = "source_sale_reversed"


#: Every retired reason the API can return today, in contract order.
STORED_PAYMENT_METHOD_RETIRED_REASONS: Tuple[str, ...] = tuple(
    member.value for member in StoredPaymentMethodRetiredReason
)


class ChargeStatus(str, Enum):
    """The outcome of :meth:`DominaiteClient.charge_payment_method`.

    ``SUCCEEDED``: the money moved. ``FAILED``: it did not; on a 402 the charge's
    ``declineClass`` says why. ``PENDING`` is not terminal: poll
    :meth:`DominaiteClient.get_status` with the charge's ``transactionId``.
    ``CANCELLED``: an authorization voided before capture, no money moved. Treat an
    unknown value as still open.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"
    CANCELLED = "cancelled"


#: Every charge status the API can return today, in contract order.
CHARGE_STATUSES: Tuple[str, ...] = tuple(member.value for member in ChargeStatus)


class DeclineClass(str, Enum):
    """Why a charge was declined (HTTP 402), coarse enough to act on without reading
    the issuer's code. None on every other charge, including a 502 ``CHARGE_FAILED`` row.

    - ``HARD``: do not retry this card, ask the customer for another one.
    - ``SOFT_FUNDS``: insufficient funds; retry later (after payday, not in a loop).
    - ``SOFT_SCA_REQUIRED``: the issuer wants the customer present; send them through
      a hosted checkout session with ``save_card=True`` instead of charging off-session
      again.
    - ``SOFT_OTHER``: a transient issuer or network condition; one retry later is
      reasonable.
    """

    HARD = "hard"
    SOFT_FUNDS = "soft_funds"
    SOFT_SCA_REQUIRED = "soft_sca_required"
    SOFT_OTHER = "soft_other"


#: Every decline class the API can return today, in contract order.
DECLINE_CLASSES: Tuple[str, ...] = tuple(member.value for member in DeclineClass)


class RefundStatus(str, Enum):
    """The state of a refund from :meth:`DominaiteClient.create_refund` or
    :meth:`DominaiteClient.get_refund`, in the order the API contract lists them.

    ``PENDING``: accepted and queued. ``PROCESSING``: with the payment provider now.
    ``SUCCEEDED``: the money went back to the payer. ``FAILED``: the refund did not happen,
    read ``failureCode``. Only ``SUCCEEDED`` and ``FAILED`` are final, and ``FAILED`` is final
    for that idempotency key: a new attempt needs a new key. Treat an unknown value as still
    open.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Every refund status the API can return today, in contract order.
REFUND_STATUSES: Tuple[str, ...] = tuple(member.value for member in RefundStatus)
