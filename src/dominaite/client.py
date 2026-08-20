"""Server-side client for the Dominaite merchant API."""

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Mapping, Optional

from .exceptions import (
    ApiError,
    AuthenticationError,
    CheckoutRefusedError,
    TransportError,
)

__version__ = "0.1.0"

DEFAULT_BASE_URL = "https://api.dominaite.com/payments"
SESSIONS_PATH = "/merchant-api/checkout/sessions"
DEFAULT_TIMEOUT_SECONDS = 45.0  # serverless cold starts hit 10+s on dev; 15s was a coin flip

_TRANSACTION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    path: str,
    idempotency_key: str,
    body: str,
) -> str:
    """Build the ``X-Signature`` value for one request.

    Lowercase hex HMAC-SHA256 over
    ``"{timestamp}\\n{METHOD}\\n{path}\\n{idempotencyKey}\\n{sha256hex(body)}"``.

    The idempotency key is INSIDE the signature, so a captured request cannot be
    replayed with a different key to mint extra sessions. The server rejects
    timestamps more than 5 minutes off - keep your server clock on NTP.

    Exposed so you can pin it in your own test against the published vector.
    """
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    payload = "\n".join([timestamp, method.upper(), path, idempotency_key, body_hash])
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


class DominaiteClient:
    """Server-side client for the Dominaite merchant API.

    Keep your API secret on the server. Never ship it to a browser, never commit it,
    never log it. Card details never touch your backend or this SDK - the payer enters
    them inside the hosted checkout widget.

    Usage::

        client = DominaiteClient(os.environ["DOMINAITE_KEY_ID"], os.environ["DOMINAITE_SECRET"])
        session = client.create_checkout_session(
            amount=2500,              # minor units: 25.00 EUR
            currency="EUR",
            order_reference="order-1042",
            customer={"firstName": "Ana", "lastName": "K", "email": "ana@example.com"},
        )
        # Hand session["cashierKey"] + session["cashierToken"] to the embed snippet.
    """

    def __init__(
        self,
        key_id: str,
        secret: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        """
        :param key_id: Your API key id (``dmk_...``), from the Dominaite dashboard.
        :param secret: Your API secret (``dms_...``). Server-side only.
        :param base_url: Override for non-production environments.
        :param timeout: Per-request socket timeout in seconds.
        """
        if not key_id.startswith("dmk_"):
            raise ValueError("key_id must start with dmk_")
        if not secret.startswith("dms_"):
            raise ValueError("secret must start with dms_")
        self._key_id = key_id
        self._secret = secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def create_checkout_session(
        self,
        amount: int,
        currency: str,
        order_reference: str,
        customer: Optional[Mapping[str, Any]] = None,
        country: Optional[str] = None,
        language: Optional[str] = None,
        theme: Optional[str] = None,
        description: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a hosted checkout session for one payment.

        :param amount: Integer in MINOR units (2500 = 25.00 EUR). Never a float.
        :param currency: ISO 4217 code.
        :param order_reference: Your own order id, 100 chars or fewer.
        :param customer: ``{"firstName", "lastName", "email", "phone"}``. Pass everything
            you already know - prefilled fields are hidden from the payer, so the
            checkout form stays short.
        :param country: ISO 3166-1 alpha-2.
        :param language: ISO 639-1, the widget UI language.
        :param theme: ``light``, ``dark``, or ``bright``.
        :param description: Free-text description shown on the checkout.
        :param idempotency_key: Auto-generated when omitted. Retrying with the same key
            never creates a second payment.
        :returns: ``{"transactionId", "orderId", "cashierKey", "cashierToken", "amount",
            "currency", "expiresAt"}``.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature
            (fix config; do not retry).
        :raises CheckoutRefusedError: The gateway refused the session (inspect
            ``error_code``).
        :raises ApiError: Unexpected API response.
        :raises TransportError: Network-level failure (safe to retry WITH the same
            ``idempotency_key``).
        """
        # bool is a subclass of int in Python, so True would otherwise sail through
        # and get serialized as `true`.
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise ValueError(
                "amount must be a positive integer in MINOR units (e.g. 2500 for 25.00 EUR)"
            )
        if not currency:
            raise ValueError("currency is required")
        if not order_reference:
            raise ValueError("order_reference is required")

        key = idempotency_key if idempotency_key is not None else secrets.token_hex(16)
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError(
                "idempotency_key must be a non-empty string of at most 100 characters"
            )

        body: Dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "orderReference": order_reference,
        }
        if customer is not None:
            body["customer"] = dict(customer)
        if country is not None:
            body["country"] = country
        if language is not None:
            body["language"] = language
        if theme is not None:
            body["theme"] = theme
        if description is not None:
            body["description"] = description

        response = self._request("POST", SESSIONS_PATH, body, key)

        if response.get("success") is not True or "checkout" not in response:
            raise CheckoutRefusedError(
                str(response.get("errorCode") or "UNKNOWN"),
                str(response.get("errorMessage") or "The checkout session was refused."),
            )

        return response["checkout"]

    def create_checkout_session_with_retry(
        self,
        max_attempts: int = 3,
        backoff_seconds: float = 0.5,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Create a session, retrying transport failures with the SAME idempotency key.

        A ``TransportError`` (network blip, 5xx, ``MERCHANT_API_UNAVAILABLE``) leaves you
        not knowing whether the request landed. Reusing the key is what makes the retry
        safe: the server returns the original session instead of opening a second one.
        Generating a fresh key here would be the double-charge bug this method exists to
        prevent.

        Refusals and authentication failures are raised immediately - retrying them just
        burns time.

        :param max_attempts: Total attempts including the first one.
        :param backoff_seconds: Base delay; doubles after each failed attempt.
        :param kwargs: Passed straight to :meth:`create_checkout_session`.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

        kwargs.setdefault("idempotency_key", secrets.token_hex(16))

        delay = backoff_seconds
        for attempt in range(1, max_attempts + 1):
            try:
                return self.create_checkout_session(**kwargs)
            except TransportError:
                if attempt == max_attempts:
                    raise
                if delay > 0:
                    time.sleep(delay)
                delay *= 2

        raise AssertionError("unreachable")

    def get_status(self, transaction_id: str) -> Dict[str, Any]:
        """Read the payment status of one of your checkout sessions.

        Status values: ``pending``, ``processing``, ``succeeded``, ``failed``,
        ``refunded``, ``partially_refunded``, ``cancelled``, ``disputed``,
        ``abandoned``. While a session is still payable the response carries
        ``expiresAt``; amounts are integers in MINOR units.

        :param transaction_id: The ``transactionId`` from
            :meth:`create_checkout_session`.

        :raises AuthenticationError: Wrong/revoked credentials or bad signature.
        :raises ApiError: Unknown transaction id (HTTP 404) or unexpected response.
        :raises TransportError: Network-level failure (safe to retry).
        """
        normalized = transaction_id.strip().lower()
        if not _TRANSACTION_ID_RE.match(normalized):
            raise ValueError(
                "transaction_id must be the UUID returned by create_checkout_session()"
            )

        return self._request("GET", SESSIONS_PATH + "/" + normalized, None, "")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        """Sign and send one request.

        ``body`` is None for GET: an empty body (and an empty idempotency key) is what
        gets signed.
        """
        if body is None:
            payload = ""
        else:
            # Compact separators and no ASCII escaping: the bytes we hash must be the
            # exact bytes we send, so the JSON is serialized once and reused.
            payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        timestamp = str(int(time.time()))
        signature = sign_request(
            self._secret, timestamp, method, path, idempotency_key, payload
        )

        headers = {
            "Content-Type": "application/json",
            "X-Api-Key-Id": self._key_id,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
            # Some edges block requests without a real User-Agent - always send one.
            "User-Agent": "dominaite-python/" + __version__,
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        data = payload.encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path, data=data, headers=headers, method=method
        )

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            # 4xx/5xx: the body still carries the machine-readable code.
            status = error.code
            raw = error.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as error:
            raise TransportError(
                "Could not reach the Dominaite API: {0}".format(error)
            ) from error

        try:
            decoded = json.loads(raw)
        except ValueError:
            decoded = None
        if not isinstance(decoded, dict):
            raise ApiError(status, "The API returned a non-JSON response")

        # The gateway wraps responses as { success, data, ... }; unwrap when present.
        # Error responses carry the machine-readable code at error.code.
        inner = decoded.get("data")
        result = inner if isinstance(inner, dict) else decoded
        envelope_error = decoded.get("error")
        if not isinstance(envelope_error, dict):
            envelope_error = {}

        if status in (401, 403):
            raise AuthenticationError(
                str(result.get("errorCode") or envelope_error.get("code") or "UNAUTHORIZED"),
                "Authentication failed - check your key id, secret, and server clock.",
            )
        if status >= 500:
            raise TransportError(
                "The Dominaite API is unavailable (HTTP {0}); "
                "retry with the same idempotency key.".format(status)
            )
        if status >= 400:
            raise ApiError(
                status,
                str(
                    result.get("errorMessage")
                    or envelope_error.get("message")
                    or "Request rejected"
                ),
            )

        return result
