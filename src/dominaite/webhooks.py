"""Verification for the webhooks Dominaite delivers to your endpoint.

Verify BEFORE you parse. The body is attacker-controlled until the signature checks
out, so nothing in it - not the event type, not the amount - means anything until
:func:`verify_webhook` has returned.
"""

import hashlib
import hmac
import json
import re
import time
from typing import Any, Dict, Optional, Tuple, Union

from .exceptions import WebhookVerificationError

#: Header Dominaite signs each delivery with. An endpoint can be configured to send a
#: different header name; the value format is the same either way.
WEBHOOK_SIGNATURE_HEADER = "X-Webhook-Signature"

#: How far the delivery timestamp may sit from your clock before it is rejected.
DEFAULT_WEBHOOK_TOLERANCE_SECONDS = 300

_TIMESTAMP_RE = re.compile(r"^[0-9]{1,19}$")
_V1_RE = re.compile(r"^[0-9a-f]{64}$")


def sign_webhook(secret: str, timestamp: str, payload: str) -> str:
    """Build the ``v1`` value for one delivery.

    Lowercase hex HMAC-SHA256 over ``"{timestamp}.{raw_body}"``, keyed with the UTF-8
    bytes of the endpoint secret. The timestamp is inside the signature, so a captured
    delivery cannot be re-dated to slip past the tolerance window.

    Exposed so you can pin it in your own test against the published vector, and so you
    can forge a delivery in your own test suite without hand-rolling the HMAC.
    """
    signed = timestamp + "." + payload
    return hmac.new(
        secret.encode("utf-8"), signed.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_webhook(
    payload: Union[str, bytes],
    signature_header: str,
    secret: str,
    tolerance_seconds: int = DEFAULT_WEBHOOK_TOLERANCE_SECONDS,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    """Verify one delivery and return its decoded event.

    :param payload: The RAW request body, exactly as received. Bytes are safest; a
        ``str`` is encoded back as UTF-8. Never a re-serialized dict - a framework that
        reorders keys or drops a space changes the bytes and the signature will not
        match.
    :param signature_header: The ``X-Webhook-Signature`` value, ``t=...,v1=...``.
    :param secret: The endpoint secret from the dashboard, shown once at creation.
    :param tolerance_seconds: How far the delivery timestamp may sit from ``now``.
    :param now: Unix seconds to check the timestamp against. Defaults to the system
        clock; pass it to keep your own tests deterministic.
    :returns: The decoded event: ``{"id", "type", "createdAt", "data"}``. Dedupe on
        ``id`` - delivery is at-least-once.

    :raises WebhookVerificationError: The delivery is not authentic, or not fresh.
        Branch on ``error_code``; respond 400 and do no work.
    :raises ValueError: You called this wrong (empty secret, negative tolerance).

    Usage::

        try:
            event = verify_webhook(request.get_data(), header, os.environ["WHSEC"])
        except WebhookVerificationError:
            return "", 400
    """
    if not secret:
        raise ValueError("secret is required")
    # No whsec_ prefix check: an endpoint may be created with a secret you supplied,
    # which does not have to carry the prefix our generated ones do.
    if isinstance(tolerance_seconds, bool) or not isinstance(
        tolerance_seconds, (int, float)
    ):
        raise ValueError("tolerance_seconds must be a number of seconds")
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must not be negative")

    body = payload.decode("utf-8") if isinstance(payload, bytes) else payload
    timestamp, provided = _parse_signature_header(signature_header)

    expected = sign_webhook(secret, timestamp, body)
    if not hmac.compare_digest(expected, provided):
        # Wrong secret and tampered body are the same answer on purpose: telling them
        # apart would tell an attacker which half to keep guessing at.
        raise WebhookVerificationError(
            "INVALID_SIGNATURE",
            "Webhook signature does not match - wrong endpoint secret, or the body "
            "was modified in transit.",
        )

    current = int(time.time()) if now is None else int(now)
    if abs(current - int(timestamp)) > tolerance_seconds:
        # The MAC was good, so this is a replay or your clock. Fix NTP before you widen
        # the tolerance.
        raise WebhookVerificationError(
            "TIMESTAMP_OUT_OF_RANGE",
            "Webhook timestamp is outside the {0}s tolerance - check your server "
            "clock (NTP).".format(tolerance_seconds),
        )

    try:
        event = json.loads(body)
    except ValueError as error:
        raise WebhookVerificationError(
            "INVALID_PAYLOAD", "Webhook body is signed but is not valid JSON."
        ) from error
    if not isinstance(event, dict):
        raise WebhookVerificationError(
            "INVALID_PAYLOAD", "Webhook body is signed but is not a JSON object."
        )

    return event


def _parse_signature_header(signature_header: str) -> Tuple[str, str]:
    """Pull ``t`` and ``v1`` out of the header, or say why it cannot be read.

    Returns them as strings: ``t`` is re-signed as text, so parsing it to an int and
    back could silently change the bytes we hash.
    """
    malformed = WebhookVerificationError(
        "MALFORMED_SIGNATURE",
        "Webhook signature header is not in the expected t=...,v1=... format.",
    )
    if not isinstance(signature_header, str) or not signature_header:
        raise malformed

    timestamp = None
    provided = None
    for part in signature_header.split(","):
        name, separator, value = part.strip().partition("=")
        if not separator:
            raise malformed
        if name == "t":
            # A repeated key is ambiguous, and ambiguity in a security check is a bug
            # waiting to be exploited. Refuse instead of picking one.
            if timestamp is not None:
                raise malformed
            timestamp = value
        elif name == "v1":
            if provided is not None:
                raise malformed
            provided = value
        # Unknown keys are ignored so a future scheme version can be added alongside v1.

    if timestamp is None or provided is None:
        raise malformed
    if not _TIMESTAMP_RE.match(timestamp) or not _V1_RE.match(provided):
        raise malformed

    return timestamp, provided
