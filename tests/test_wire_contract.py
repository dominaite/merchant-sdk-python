"""Pins this SDK's hardcoded enumerations against the gateway's live contract.

``merchant-api-wire-contract.json`` in this directory is the machine-relevant
projection of the gateway's ``GET /merchant-api/integration/contract``, refreshed by
``.github/workflows/contract-drift.yml``. When one of these fails the gateway moved:
fix the SDK and release, never the fixture.
"""

import json
from pathlib import Path

from dominaite import PAYMENT_STATUSES, SESSION_REFUSAL_ERROR_CODES, VALIDATION_ERROR_CODES

WIRE = json.loads(
    (Path(__file__).resolve().parent / "merchant-api-wire-contract.json").read_text("utf-8")
)


def _codes_with_status(groups, http_status):
    return sorted(entry["code"] for group in groups for entry in group if entry["httpStatus"] == http_status)


def test_status_vocabulary_matches_the_gateway_in_order():
    assert list(PAYMENT_STATUSES) == WIRE["statuses"]


def test_refusal_codes_are_exactly_the_http_200_error_codes():
    expected = _codes_with_status(
        [WIRE["errorCodes"]["transient"], WIRE["errorCodes"]["idempotency"]], 200
    )
    assert sorted(SESSION_REFUSAL_ERROR_CODES) == expected


def test_validation_codes_are_exactly_the_http_400_idempotency_codes():
    expected = _codes_with_status([WIRE["errorCodes"]["idempotency"]], 400)
    assert sorted(VALIDATION_ERROR_CODES) == expected
    assert WIRE["validationHttpStatus"] == 400


def test_the_contract_still_lists_this_sdk():
    assert "python" in WIRE["sdks"]
