"""Pins this SDK's hardcoded enumerations against the gateway's live contract.

``merchant-api-wire-contract.json`` in this directory is the machine-relevant
projection of the gateway's ``GET /merchant-api/integration/contract``, refreshed by
``.github/workflows/contract-drift.yml``. When one of these fails the gateway moved:
fix the SDK and release, never the fixture.
"""

import json
from pathlib import Path

from dominaite import (
    AUTHORIZATION_ERROR_CODES,
    PAYMENT_STATUSES,
    SESSION_REFUSAL_ERROR_CODES,
    STOREFRONT_ERROR_CODES,
    VALIDATION_ERROR_CODES,
    WALLET_TYPES,
)

HERE = Path(__file__).resolve().parent
CONTRACT = json.loads((HERE / "merchant-api-contract.json").read_text("utf-8"))
WIRE = json.loads((HERE / "merchant-api-wire-contract.json").read_text("utf-8"))


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


def test_storefront_codes_match_the_gateway_statuses_and_none_is_retryable():
    storefront = WIRE["errorCodes"]["storefront"]
    assert [entry["code"] for entry in storefront] == list(STOREFRONT_ERROR_CODES)
    assert {entry["code"]: entry["httpStatus"] for entry in storefront} == {
        "STOREFRONT_MISMATCH": 400,
        "STOREFRONT_INACTIVE": 409,
        "STOREFRONT_NOT_WHITELISTED": 409,
    }
    assert all(entry["retry"] is False for entry in storefront)
    assert not set(STOREFRONT_ERROR_CODES) & set(SESSION_REFUSAL_ERROR_CODES)


def test_authorization_codes_are_exactly_the_gateway_403_group_in_order():
    authorization = WIRE["errorCodes"]["authorization"]
    assert [entry["code"] for entry in authorization] == list(AUTHORIZATION_ERROR_CODES)
    assert all(entry["httpStatus"] == 403 and entry["retry"] is False for entry in authorization)


def test_the_contract_still_lists_this_sdk():
    assert "python" in WIRE["sdks"]


def test_wallet_types_are_exactly_the_gateway_contract_in_order():
    assert list(WALLET_TYPES) == WIRE["wallets"]["walletTypes"]


def test_wallet_reporting_fields_are_optional_strings_on_the_status_read():
    fields = WIRE["wallets"]["reportingFields"]
    assert [field["path"] for field in fields] == ["paymentMethod", "walletType"]
    assert all(field["type"] == "string" and field["required"] is False for field in fields)
    # get_status passes the body through, so the status fields it promises are the
    # canonical fixture's: both reporting fields have to be among them.
    assert set(field["path"] for field in fields) <= set(CONTRACT["endpoints"]["getStatus"]["fields"])


def test_recurring_webhook_events_are_the_agreement_and_charge_events():
    recurring = WIRE["recurringWebhookEvents"]
    assert recurring == [
        "agreement.activated",
        "agreement.past_due",
        "agreement.cancelled",
        "charge.succeeded",
        "charge.failed",
        "charge.retrying",
    ]
    # The payment events live in the catalog, the recurring ones here. No event is in both.
    assert not set(recurring) & set(WIRE["webhookEventCatalog"])
