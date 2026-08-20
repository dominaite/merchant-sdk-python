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
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    DominaiteError,
    TransportError,
    WebhookVerificationError,
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
    "PING_PATH",
    "SESSIONS_PATH",
    "WEBHOOK_SIGNATURE_HEADER",
    "ApiError",
    "AuthenticationError",
    "CheckoutRefusedError",
    "DominaiteClient",
    "DominaiteError",
    "TransportError",
    "WebhookVerificationError",
    "__version__",
    "sign_request",
    "sign_webhook",
    "verify_webhook",
]
