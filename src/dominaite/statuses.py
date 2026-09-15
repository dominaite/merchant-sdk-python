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
from typing import Tuple


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


class PaymentMethodStatus(str, Enum):
    """A stored payment method's state, in the order the API contract lists them.

    Only ``ACTIVE`` methods can be charged. ``REVOKED`` is what
    :meth:`DominaiteClient.revoke_payment_method` leaves behind; ``EXPIRED`` means the
    card's expiry date has passed. Treat a value you do not recognise as not chargeable.
    """

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


#: Every stored payment method status the API can return today, in contract order.
PAYMENT_METHOD_STATUSES: Tuple[str, ...] = tuple(
    member.value for member in PaymentMethodStatus
)


class ChargeStatus(str, Enum):
    """The outcome of :meth:`DominaiteClient.charge_payment_method`.

    ``PENDING`` is not terminal: poll :meth:`DominaiteClient.get_status` with the
    charge's ``transactionId``. Treat an unknown value as still open.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PENDING = "pending"


#: Every charge status the API can return today, in contract order.
CHARGE_STATUSES: Tuple[str, ...] = tuple(member.value for member in ChargeStatus)


class DeclineClass(str, Enum):
    """Why a charge failed, coarse enough to act on without reading the issuer's code.

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
