"""Contract tests for is_paid / is_terminal: when to ship the order, when to stop polling."""

import pytest

from dominaite import PAYMENT_STATUSES, TERMINAL_PAYMENT_STATUSES, PaymentStatus, is_paid, is_terminal

TERMINAL = ["succeeded", "failed", "cancelled", "abandoned", "refunded", "partially_refunded"]
OPEN = ["pending", "processing", "requires_capture", "disputed"]


def test_every_contract_status_is_classified_as_terminal_or_open():
    """A status added to the vocabulary must be placed on purpose, not fall through."""
    assert sorted(TERMINAL + OPEN) == sorted(PAYMENT_STATUSES)
    assert sorted(TERMINAL_PAYMENT_STATUSES) == sorted(TERMINAL)


@pytest.mark.parametrize("status", TERMINAL)
def test_terminal_statuses_stop_polling(status):
    assert is_terminal(status)
    assert is_terminal(PaymentStatus(status))


@pytest.mark.parametrize("status", OPEN)
def test_open_statuses_keep_polling(status):
    assert not is_terminal(status)


@pytest.mark.parametrize("status", ["chargeback_won", "", "SUCCEEDED", None])
def test_an_unknown_status_is_never_terminal(status):
    """A value the API adds later must keep you polling, never close a live order."""
    assert not is_terminal(status)
    assert not is_paid(status)


def test_only_succeeded_is_paid():
    assert is_paid("succeeded")
    assert is_paid(PaymentStatus.SUCCEEDED)
    for status in PAYMENT_STATUSES:
        if status != "succeeded":
            assert not is_paid(status), status


def test_requires_capture_is_neither_paid_nor_terminal():
    """An approved hold: the payer has paid, the money is not captured yet."""
    assert not is_paid("requires_capture")
    assert not is_terminal("requires_capture")
