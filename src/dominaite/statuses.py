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


class PaymentMethodCategory(str, Enum):
    """How the payer paid, in the order the API contract lists the categories.

    Reporting data, not a money-flow switch: a wallet payment refunds, captures and
    disputes exactly like a plain card payment. ``get_status()`` reports it as the
    raw ``paymentMethod`` string (null while the payment is still open and on
    transactions older than the field), so an unknown value survives the trip to
    your code instead of blowing up inside the SDK.
    """

    CARD = "card"
    WALLET = "wallet"
    BANK_TRANSFER = "bank_transfer"
    SEPA = "sepa"


#: Every payment method category the API reports today, in contract order.
PAYMENT_METHOD_CATEGORIES: Tuple[str, ...] = tuple(
    member.value for member in PaymentMethodCategory
)


class WalletType(str, Enum):
    """The wallets the gateway currently names in ``walletType``.

    Pinned against the published contract fixture. The field can carry a lower-cased
    identifier not in this list yet - treat unknown values as a valid wallet, not an
    error. ``get_status()`` reports it as the raw string, null for non-wallet
    payments.
    """

    APPLE_PAY = "apple_pay"
    GOOGLE_PAY = "google_pay"
    SAMSUNG_PAY = "samsung_pay"


#: Every wallet the gateway names today, in contract order.
WALLET_TYPES: Tuple[str, ...] = tuple(member.value for member in WalletType)
