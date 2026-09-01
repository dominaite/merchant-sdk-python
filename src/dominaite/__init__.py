"""Server-side Python client for the Dominaite merchant API."""

from .client import (
    DEFAULT_BASE_URL,
    PING_PATH,
    SESSIONS_PATH,
    DominaiteClient,
    __version__,
    sign_request,
)
from .exceptions import (
    SESSION_REFUSAL_ERROR_CODES,
    VALIDATION_ERROR_CODES,
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    DominaiteError,
    RateLimitError,
    TransportError,
    WebhookVerificationError,
)
from .statuses import (
    PAYMENT_METHOD_CATEGORIES,
    PAYMENT_STATUSES,
    WALLET_TYPES,
    PaymentMethodCategory,
    PaymentStatus,
    WalletType,
)
from .webhooks import (
    DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    WEBHOOK_SIGNATURE_HEADER,
    sign_webhook,
    verify_webhook,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_WEBHOOK_TOLERANCE_SECONDS",
    "PAYMENT_METHOD_CATEGORIES",
    "PAYMENT_STATUSES",
    "PING_PATH",
    "SESSIONS_PATH",
    "SESSION_REFUSAL_ERROR_CODES",
    "VALIDATION_ERROR_CODES",
    "WALLET_TYPES",
    "WEBHOOK_SIGNATURE_HEADER",
    "ApiError",
    "AuthenticationError",
    "CheckoutRefusedError",
    "DominaiteClient",
    "DominaiteError",
    "PaymentMethodCategory",
    "PaymentStatus",
    "RateLimitError",
    "TransportError",
    "WalletType",
    "WebhookVerificationError",
    "__version__",
    "sign_request",
    "sign_webhook",
    "verify_webhook",
]
