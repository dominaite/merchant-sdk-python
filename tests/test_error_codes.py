"""The error-code vocabulary: every code the gateway answers has a name and a home.

The wire fixture pins what the integration contract publishes. The contract has no
charge, plan, agreement or webhook groups yet, so those lists are pinned here to the
codes the gateway answers on each route.
"""

import json
from pathlib import Path

from dominaite import (
    AUTHORIZATION_ERROR_CODES,
    CHARGE_ERROR_CODES,
    FIELD_VALIDATION_CODES,
    RECURRING_ERROR_CODES,
    REFUND_ERROR_CODES,
    REFUND_FAILURE_CODES,
    REVOKE_ERROR_CODES,
    SESSION_REFUSAL_ERROR_CODES,
    STOREFRONT_ERROR_CODES,
    SUBSCRIPTION_REFUSAL_ERROR_CODES,
    VALIDATION_ERROR_CODES,
    WEBHOOK_ERROR_CODES,
    ErrorCode,
)

HERE = Path(__file__).resolve().parent
WIRE = json.loads((HERE / "merchant-api-wire-contract.json").read_text("utf-8"))

#: HTTP 200 ``data.errorCode`` values the checkout create route answers with
#: ``agreement_id``.
SUBSCRIPTION_REFUSALS = [
    "AGREEMENT_NOT_FOUND",
    "AGREEMENT_NOT_PENDING",
    "AGREEMENT_AMOUNT_MISMATCH",
    "AGREEMENT_CONSENT_REQUIRED",
    "AGREEMENT_SESSION_OPEN",
    "AGREEMENT_ALREADY_PAID",
    "AGREEMENT_VARIABLE_AMOUNT_UNSUPPORTED",
    "SUBSCRIPTIONS_NOT_ENABLED",
]

#: Every coded answer of the charge route that is not a result (402) or a rate limit (429).
GATEWAY_CHARGE_CODES = [
    "PAYMENT_METHOD_NOT_FOUND",
    "PAYMENT_METHOD_NOT_ACTIVE",
    "CHARGE_IN_PROGRESS",
    "DUPLICATE_REQUEST",
    "IDEMPOTENCY_KEY_REUSED",
    "CHARGE_CURRENCY_MISMATCH",
    "AGREEMENT_NOT_FOUND",
    "AGREEMENT_METHOD_MISMATCH",
    "AGREEMENT_CONSENT_MISSING",
    "AGREEMENT_CURRENCY_MISMATCH",
    "AGREEMENT_AMOUNT_MISMATCH",
    "CHARGE_AMOUNT_ABOVE_AGREEMENT",
    "AGREEMENT_PERIOD_ALREADY_CHARGED",
    "AGREEMENT_ENDED",
    "AGREEMENT_NOT_STARTED",
    "CHARGE_AMOUNT_ABOVE_CAP",
    "CHARGE_OUTCOME_UNKNOWN",
    "CHARGE_FAILED",
    "PAYMENT_METHOD_CHARGES_DISABLED",
    "PAYMENT_PROCESSING_UNAVAILABLE",
]

GATEWAY_RECURRING_CODES = [
    "PLAN_NOT_FOUND",
    "PLAN_NOT_ACTIVE",
    "AGREEMENT_NOT_FOUND",
    "AGREEMENT_ENDED",
    "PAYMENT_METHOD_NOT_FOUND",
    "PAYMENT_METHOD_NOT_ACTIVE",
    "PAYMENT_METHOD_CURRENCY_MISMATCH",
    "PAYMENT_METHOD_CURRENCY_UNKNOWN",
    "MAX_AMOUNT_BELOW_PLAN_AMOUNT",
    "AGREEMENT_CONSENT_REQUIRED",
    "IDEMPOTENCY_KEY_REUSED",
    "MERCHANT_API_UNAVAILABLE",
]

GATEWAY_WEBHOOK_CODES = [
    "WEBHOOK_NOT_FOUND",
    "WEBHOOK_LIMIT_REACHED",
    "WEBHOOK_ALREADY_REGISTERED",
    "WEBHOOK_URL_REJECTED",
    "IDEMPOTENCY_KEY_REUSED",
    "DUPLICATE_REQUEST",
    "MERCHANT_API_UNAVAILABLE",
]


def _wire_codes():
    return [entry["code"] for group in WIRE["errorCodes"].values() for entry in group]


def test_every_code_in_the_wire_contract_has_a_named_constant():
    names = {member.value for member in ErrorCode}
    missing = [code for code in _wire_codes() if code not in names]
    assert missing == []


def test_field_validation_codes_are_the_wire_contract_validation_group_in_order():
    assert list(FIELD_VALIDATION_CODES) == [
        entry["code"] for entry in WIRE["errorCodes"]["validation"]
    ]


def test_subscription_refusals_are_the_gateway_list():
    assert sorted(SUBSCRIPTION_REFUSAL_ERROR_CODES) == sorted(SUBSCRIPTION_REFUSALS)


def test_subscription_refusals_stay_out_of_the_wire_pinned_session_refusals():
    assert not set(SUBSCRIPTION_REFUSAL_ERROR_CODES) & set(SESSION_REFUSAL_ERROR_CODES)


def test_charge_codes_are_every_coded_charge_failure_of_the_gateway():
    assert sorted(CHARGE_ERROR_CODES) == sorted(GATEWAY_CHARGE_CODES)
    assert "CHARGE_DECLINED" not in CHARGE_ERROR_CODES


def test_recurring_and_webhook_codes_are_the_gateway_lists():
    assert sorted(RECURRING_ERROR_CODES) == sorted(GATEWAY_RECURRING_CODES)
    assert sorted(WEBHOOK_ERROR_CODES) == sorted(GATEWAY_WEBHOOK_CODES)


def test_every_listed_code_has_a_named_constant_equal_to_the_wire_string():
    grouped = (
        AUTHORIZATION_ERROR_CODES
        + SESSION_REFUSAL_ERROR_CODES
        + SUBSCRIPTION_REFUSAL_ERROR_CODES
        + VALIDATION_ERROR_CODES
        + STOREFRONT_ERROR_CODES
        + CHARGE_ERROR_CODES
        + REVOKE_ERROR_CODES
        + REFUND_ERROR_CODES
        + REFUND_FAILURE_CODES
        + RECURRING_ERROR_CODES
        + WEBHOOK_ERROR_CODES
        + FIELD_VALIDATION_CODES
    )
    for code in grouped:
        assert getattr(ErrorCode, code) == code


def test_storefront_codes_stay_out_of_the_session_refusals():
    assert not set(STOREFRONT_ERROR_CODES) & set(SESSION_REFUSAL_ERROR_CODES)


def test_no_list_holds_a_code_twice():
    for codes in (
        SESSION_REFUSAL_ERROR_CODES,
        SUBSCRIPTION_REFUSAL_ERROR_CODES,
        CHARGE_ERROR_CODES,
        RECURRING_ERROR_CODES,
        WEBHOOK_ERROR_CODES,
        FIELD_VALIDATION_CODES,
    ):
        assert len(set(codes)) == len(codes)
