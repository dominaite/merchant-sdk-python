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
