# Changelog

## 0.3.1 (unreleased)

### Added

- `WebhookEvent`, `AgreementEventData`, `ChargeEventData` and `ChargeEventPaymentMethod`
  `TypedDict`s for webhook payloads. `WebhookEvent` has the optional `apiVersion` (the dated
  payload version, `2026-09-25` today); the agreement and charge data types have the optional
  `sequence` that orders deliveries per object. `verify_webhook` still returns a plain dict,
  and payloads without these fields verify and parse as before.
- `create_refund(transaction_id, amount=None, reason=None, idempotency_key=...)` and
  `get_refund(transaction_id, refund_id)` for `POST` and `GET`
  `/merchant-api/payments/{transactionId}/refunds`. The idempotency key is required and signed,
  like a charge's; leaving `amount` out refunds everything still refundable and sends no
  `amount` key. Both return a `Refund` with every field present (absent reads as None).
  `RefundStatus`, `REFUND_STATUSES`, `REFUND_ERROR_CODES`, `REFUND_FAILURE_CODES` and
  `PAYMENTS_PATH`.
- `RefundError` (a subclass of `ApiError`) for the codes the refund routes answer with, with
  `retryable` and `retry_stop_after_seconds` (`REFUND_NOT_FOUND` 60 seconds,
  `DUPLICATE_REQUEST` 120 seconds, both with the same key; also `REFUND_RETRY_WINDOWS_SECONDS`).
  A 5xx stays a `TransportError`.
- `PaymentEventData` for `payment.*` webhook data, including `storedPaymentMethod`: the card a
  `save_card` payment kept on file, on `payment.succeeded` and `payment.requires_capture`. It
  can be None even when a card was saved; `get_status()` stays the source of truth.
- `StoredPaymentMethod`, the one type for the card on file on `get_status()` and on payment
  webhooks.
- Contract fixture refreshed with the refund endpoints and vocabularies.

## 0.3.0

### Breaking

- `idempotency_key` is required on `create_checkout_session`, `create_checkout_session_with_retry`
  and `charge_payment_method`. The SDK no longer generates a random key; a missing or empty key
  raises `ValueError` before any request is sent.
- An idempotency key must be 1 to 100 visible ASCII characters. Non-ASCII keys used to pass the
  local check and then fail in the HTTP layer or the signature check; they now raise
  `ValueError` up front.
- A create call refused for its storefront raises `StorefrontError` instead of a plain
  `ApiError`. It subclasses `ApiError`, so `except ApiError` still catches it.

### Migration

Pass `idempotency_key=order_idempotency_key("checkout", order_id, amount, currency)` on every
create call, and a key derived from the billing period on every charge. The same order at the
same amount then replays the same session, and a changed amount gets a new one.

### Added

- `order_idempotency_key(scope, order_id, amount_minor, currency)` builds
  `{scope}-{order_id}-{amount_minor}-{CURRENCY}`.
- `to_minor_units(amount, currency)` converts a decimal string or `Decimal` using the gateway's
  decimals per currency (`CURRENCY_EXPONENTS`). HUF is 0 decimals there, unlike ISO 4217.
  ISK, KRW, OMR, JOD and TND raise (`UNSUPPORTED_CURRENCIES`).
- `is_paid`, `is_terminal` and `TERMINAL_PAYMENT_STATUSES`.
- `ErrorCode` enum, `STOREFRONT_ERROR_CODES` and `StorefrontError` for
  `STOREFRONT_NOT_WHITELISTED` (409), `STOREFRONT_INACTIVE` (409) and `STOREFRONT_MISMATCH` (400).
  `STOREFRONT_ERROR_CODES` is now in the contract's order.
- Saved cards can be `retired`: the platform stopped the card on its own and it never becomes
  active again. `StoredPaymentMethodStatus.RETIRED`, plus `StoredPaymentMethodRetiredReason` and
  `STORED_PAYMENT_METHOD_RETIRED_REASONS` (`hard_decline`, `chargeback`, `source_sale_reversed`).
  `get_status()["storedPaymentMethod"]["retiredReason"]` is always present, None unless retired.
- Contract fixtures refreshed from the gateway (contract version 2026-09-16).

### Fixed

- `create_checkout_session_with_retry` retries a `PAYMENT_PROCESSING_UNAVAILABLE` refusal with
  the same key, as the API contract says to.
